import html
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime
from urllib.parse import urljoin
from apscheduler.schedulers.background import BackgroundScheduler
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import Flask
import pytz
import requests
import telebot

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
  raise ValueError("Errore: BOT_TOKEN mancante nel file .env o nell'ambiente.")

# Inserisci il tuo chat_id numerico come fallback di emergenza nelle env o qui
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()

BASE_URL_CONCORSI = "https://www.regione.piemonte.it/governo/bollettino/abbonati/2026/corrente/concorsi/"
URL_INDICE = urljoin(BASE_URL_CONCORSI, "index.htm")
KEYWORD = "chirurgia"
DB_FILE = "iscritti.db"
TIMEZONE = pytz.timezone("Europe/Rome")

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

log_flask = logging.getLogger("werkzeug")
log_flask.setLevel(logging.ERROR)


# --- FLASK HEALTH CHECK ---
@app.route("/")
@app.route("/health")
def health_check():
  return "OK", 200


# --- DATABASE SQLITE ---
def init_db():
  with sqlite3.connect(DB_FILE) as conn:
    cursor = conn.cursor()
    cursor.execute("""
            CREATE TABLE IF NOT EXISTS iscritti (
                chat_id INTEGER PRIMARY KEY,
                username TEXT,
                data_iscrizione TEXT
            )
        """)
    conn.commit()


def aggiungi_utente(chat_id: int, username: str) -> bool:
  with sqlite3.connect(DB_FILE) as conn:
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id FROM iscritti WHERE chat_id = ?", (chat_id,))
    if cursor.fetchone():
      return False

    now = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "INSERT INTO iscritti (chat_id, username, data_iscrizione) VALUES (?,?,"
        " ?)",
        (chat_id, username or "Sconosciuto", now),
    )
    conn.commit()
    logger.info(
        f"Nuovo utente iscritto al database: {chat_id} (@{username})"
    )
    return True


def rimuovi_utente(chat_id: int) -> bool:
  with sqlite3.connect(DB_FILE) as conn:
    cursor = conn.cursor()
    cursor.execute("DELETE FROM iscritti WHERE chat_id = ?", (chat_id,))
    conn.commit()
    logger.info(f"Utente rimosso: {chat_id}")
    return cursor.rowcount > 0


def get_tutti_iscritti() -> list[int]:
  with sqlite3.connect(DB_FILE) as conn:
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id FROM iscritti")
    iscritti = [row[0] for row in cursor.fetchall()]

  # Aggiunge l'ADMIN_CHAT_ID se configurato per evitare di perdere la notifica in caso di wipe del DB
  if ADMIN_CHAT_ID and ADMIN_CHAT_ID.isdigit():
    admin_id = int(ADMIN_CHAT_ID)
    if admin_id not in iscritti:
      iscritti.append(admin_id)

  return iscritti


# --- SCRAPING CON ITERAZIONE SUI SINGOLI DOCUMENTI ---
def ottieni_lista_url_atti(session: requests.Session) -> tuple[str, list[str]]:
  intestazione = "Bollettino Ufficiale - Regione Piemonte"
  links_atti = []

  try:
    resp = session.get(URL_INDICE, timeout=15)
    if resp.status_code == 200:
      resp.encoding = "iso-8859-1"
      soup = BeautifulSoup(resp.text, "html.parser")

      match_data = re.search(
          r"Bollettino\s+Ufficiale[^\n\r]*?n\.?\s*\d+[^\n\r]*?del\s+\d{1,2}\s+[a-zA-ZÀ-ÿ]+\s+\d{4}",
          soup.get_text("\n", strip=True),
          re.IGNORECASE,
      )
      if match_data:
        intestazione = re.sub(r"\s+", " ", match_data.group(0)).strip()

      for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if re.search(r"\d+\.htm[l]?", href, re.IGNORECASE):
          full_url = urljoin(BASE_URL_CONCORSI, href)
          if full_url not in links_atti:
            links_atti.append(full_url)
  except Exception as e:
    logger.warning(f"Errore lettura indice: {e}")

  if not links_atti:
    for i in range(1, 101):
      links_atti.append(urljoin(BASE_URL_CONCORSI, f"{i:08d}.htm"))

  return intestazione, links_atti


def cerca_nel_bollettino() -> tuple[str, list[dict]]:
  session = requests.Session()
  session.headers.update({
      "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
          " like Gecko) Chrome/120.0.0.0 Safari/537.36"
      )
  })

  intestazione, lista_urls = ottieni_lista_url_atti(session)
  trovati = []

  for url_atto in lista_urls:
    try:
      r = session.get(url_atto, timeout=10)
      if r.status_code == 404:
        break
      if r.status_code != 200:
        continue

      r.encoding = "iso-8859-1"
      testo_pagina = r.text

      if KEYWORD.lower() in testo_pagina.lower():
        soup_atto = BeautifulSoup(testo_pagina, "html.parser")
        for tag in soup_atto(["script", "style", "meta", "link", "noscript"]):
          tag.decompose()

        testo_pulito = re.sub(
            r"\n\s*\n+", "\n\n", soup_atto.get_text("\n", strip=True)
        ).strip()
        if len(testo_pulito) > 3700:
          testo_pulito = (
              testo_pulito[:3650]
              + "\n\n... [Testo lungo: visualizza il documento completo al link]"
          )

        trovati.append({"testo": testo_pulito, "url": url_atto})
    except requests.RequestException as e:
      logger.error(f"Errore controllo URL {url_atto}: {e}")
      continue

  return intestazione, trovati


def invia_esito_a_chat(chat_id: int, intestazione: str, trovati: list[dict]):
  data_controllo = datetime.now(TIMEZONE).strftime("%d/%m/%Y %H:%M")

  if not trovati:
    messaggio = (
        f"📋 <b>{html.escape(intestazione)}</b>\n"
        f"🕒 <i>Controllo del: {data_controllo}</i>\n\n"
        f"ℹ️ Nessun concorso o atto contenente la parola <b>'{KEYWORD}'</b>"
        " trovato nell'edizione corrente."
    )
    bot.send_message(
        chat_id, messaggio, parse_mode="HTML", disable_web_page_preview=True
    )
    return

  messaggio_intro = (
      f"📋 <b>{html.escape(intestazione)}</b>\n"
      f"🕒 <i>Controllo del: {data_controllo}</i>\n\n"
      f"🔍 <b>Trovati {len(trovati)} atti per '{KEYWORD}':</b>"
  )
  bot.send_message(
      chat_id, messaggio_intro, parse_mode="HTML", disable_web_page_preview=True
  )

  for idx, bando in enumerate(trovati, 1):
    testo_formattato = (
        f"📄 <b>Bando {idx} di {len(trovati)}:</b>\n\n"
        f"{html.escape(bando['testo'])}\n\n"
        f"👉 <a href='{bando['url']}'>Apri documento originale</a>"
    )
    bot.send_message(
        chat_id,
        testo_formattato,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    time.sleep(0.3)


def invia_notifica_programmata():
  logger.info("AVVIO NOTIFICA PROGRAMMATA DEL GIOVEDÌ...")
  try:
    iscritti = get_tutti_iscritti()
    logger.info(
        f"Numero iscritti destinatari: {len(iscritti)} -> {iscritti}"
    )

    if not iscritti:
      logger.warning(
          "Nessun utente iscritto nel database. Notifica annullata."
      )
      return

    intestazione, trovati = cerca_nel_bollettino()

    for chat_id in iscritti:
      try:
        invia_esito_a_chat(chat_id, intestazione, trovati)
        logger.info(f"Notifica inviata con successo a chat_id: {chat_id}")
      except telebot.apihelper.ApiTelegramException as e:
        logger.error(
            f"Errore invio Telegram a {chat_id}: {e.description} (Code:"
            f" {e.error_code})"
        )
        if e.error_code in [403, 400]:
          rimuovi_utente(chat_id)
      except Exception as ex:
        logger.error(f"Errore generico invio a {chat_id}: {ex}")
  except Exception as e:
    logger.error(f"Errore critico durante l'esecuzione programmata: {e}")


# --- COMANDI TELEGRAM ---
@bot.message_handler(commands=["start"])
def comando_start(message):
  chat_id = message.chat.id
  username = message.from_user.username
  is_nuovo = aggiungi_utente(chat_id, username)

  if is_nuovo:
    testo = (
        "👋 <b>Benvenuto!</b>\n\n"
        f"Sei stato iscritto con successo agli aggiornamenti per"
        f" <i>'{KEYWORD}'</i>.\n"
        f"Il tuo ID Telegram: <code>{chat_id}</code>\n"
        "Riceverai una notifica automatica <b>ogni giovedì alle 10:00</b>.\n\n"
        "👉 /cerca - Controlla subito\n"
        "👉 /stop - Cancellati"
    )
  else:
    testo = (
        f"Sei già iscritto! (ID: <code>{chat_id}</code>)\n\n"
        "👉 Usa /cerca per controllare subito\n"
        "👉 Usa /stop per cancellarti"
    )
  bot.send_message(chat_id, testo, parse_mode="HTML")


@bot.message_handler(commands=["iscritti"])
def comando_iscritti(message):
  """Permette di visualizzare gli ID iscritti per debug."""
  iscritti = get_tutti_iscritti()
  bot.send_message(
      message.chat.id,
      f"📊 <b>Iscritti nel DB:</b> {len(iscritti)}\n<code>{iscritti}</code>",
      parse_mode="HTML",
  )


@bot.message_handler(commands=["test_notifica"])
def comando_test_notifica(message):
  """Forza l'esecuzione della funzione del giovedì per testare l'invio a tutti gli iscritti."""
  bot.send_message(
      message.chat.id,
      "⚡ Esecuzione manuale del ciclo di notifica del giovedì in corso...",
  )
  invia_notifica_programmata()


@bot.message_handler(commands=["stop"])
def comando_stop(message):
  if rimuovi_utente(message.chat.id):
    bot.send_message(
        message.chat.id, "❌ Ti sei disiscritto. Non riceverai più notifiche."
    )
  else:
    bot.send_message(
        message.chat.id, "Non risultavi nella lista degli iscritti."
    )


@bot.message_handler(commands=["cerca"])
def comando_cerca(message):
  bot.send_message(
      message.chat.id,
      "🔍 Controllo in corso su tutti i singoli atti del Bollettino...",
  )
  intestazione, trovati = cerca_nel_bollettino()
  invia_esito_a_chat(message.chat.id, intestazione, trovati)


def avvia_polling_sicuro():
  while True:
    try:
      logger.info("Avvio bot polling Telegram...")
      bot.infinity_polling(
          timeout=20, long_polling_timeout=10, skip_pending=True
      )
    except Exception as err:
      logger.error(f"Errore polling: {err}. Riconnessione tra 5 secondi...")
      time.sleep(5)


if __name__ == "__main__":
  init_db()

  # Pianificazione scheduler giovedì ore 10:00 (Europe/Rome)
  scheduler = BackgroundScheduler(timezone=TIMEZONE)
  scheduler.add_job(
      invia_notifica_programmata,
      "cron",
      day_of_week="thu",
      hour=10,
      minute=0,
      misfire_grace_time=3600,  # Se il server era in sleep/riavvio, esegue il job se entro 1 ora
  )
  scheduler.start()
  logger.info("Scheduler avviato con successo.")

  threading.Thread(target=avvia_polling_sicuro, daemon=True).start()

  porta = int(os.getenv("PORT", 10000))
  logger.info(f"Avvio Flask su porta {porta}")
  app.run(host="0.0.0.0", port=porta)
