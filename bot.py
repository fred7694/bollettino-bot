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
    return True


def rimuovi_utente(chat_id: int) -> bool:
  with sqlite3.connect(DB_FILE) as conn:
    cursor = conn.cursor()
    cursor.execute("DELETE FROM iscritti WHERE chat_id = ?", (chat_id,))
    conn.commit()
    return cursor.rowcount > 0


def get_tutti_iscritti():
  with sqlite3.connect(DB_FILE) as conn:
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id FROM iscritti")
    return [row[0] for row in cursor.fetchall()]


# --- SCRAPING CON ITERAZIONE SUI SINGOLI DOCUMENTI ---
def ottieni_lista_url_atti(session: requests.Session) -> tuple[str, list[str]]:
  """Legge l'indice generale, estrae la data del bollettino e genera la lista dei link agli atti."""
  intestazione = "Bollettino Ufficiale - Regione Piemonte"
  links_atti = []

  try:
    resp = session.get(URL_INDICE, timeout=15)
    if resp.status_code == 200:
      resp.encoding = "iso-8859-1"
      soup = BeautifulSoup(resp.text, "html.parser")

      # Estrazione data/numero bollettino
      match_data = re.search(
          r"Bollettino\s+Ufficiale[^\n\r]*?n\.?\s*\d+[^\n\r]*?del\s+\d{1,2}\s+[a-zA-ZÀ-ÿ]+\s+\d{4}",
          soup.get_text("\n", strip=True),
          re.IGNORECASE,
      )
      if match_data:
        intestazione = re.sub(r"\s+", " ", match_data.group(0)).strip()

      # Raccoglie i link agli atti (es. 00000001.htm, 00000002.htm...)
      for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if re.search(r"\d+\.htm[l]?", href, re.IGNORECASE):
          full_url = urljoin(BASE_URL_CONCORSI, href)
          if full_url not in links_atti:
            links_atti.append(full_url)
  except Exception as e:
    logger.warning(f"Errore lettura indice, fallback su iterazione numerica: {e}")

  # Se l'indice non conteneva i link, genera i primi 100 in sequenza progressiva
  if not links_atti:
    for i in range(1, 101):
      links_atti.append(urljoin(BASE_URL_CONCORSI, f"{i:08d}.htm"))

  return intestazione, links_atti


def cerca_nel_bollettino() -> str:
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

      # Se la pagina non esiste (404), interrompe la sequenza numerica
      if r.status_code == 404:
        break
      if r.status_code != 200:
        continue

      r.encoding = "iso-8859-1"
      testo_pagina = r.text

      # Verifica se la parola chiave è presente nel testo integrale dell'atto
      if KEYWORD.lower() in testo_pagina.lower():
        soup_atto = BeautifulSoup(testo_pagina, "html.parser")

        # Rimuove script e stili
        for s in soup_atto(["script", "style"]):
          s.decompose()

        # Estrae l'oggetto o titolo del bando dalla pagina
        titolo_atto = ""
        # Cerca tag di intestazione o primo paragrafo significativo
        tag_titolo = soup_atto.find(["h1", "h2", "h3", "title"])
        if tag_titolo:
          titolo_atto = tag_titolo.get_text(" ", strip=True)

        if not titolo_atto or len(titolo_atto) < 20:
          # Prende il primo testo visibile significativo
          testo_pulito = re.sub(
              r"\s+", " ", soup_atto.get_text(" ", strip=True)
          ).strip()
          titolo_atto = testo_pulito[:250] + (
              "..." if len(testo_pulito) > 250 else ""
          )

        titolo_pulito = re.sub(r"\s+", " ", titolo_atto).strip()
        trovati.append(
            f"• <b>Atto trovato:</b>\n{html.escape(titolo_pulito)}\n  👉 <a"
            f" href='{url_atto}'>Apri documento completo</a>"
        )

    except requests.RequestException as e:
      logger.error(f"Errore controllo URL {url_atto}: {e}")
      continue

  data_controllo = datetime.now(TIMEZONE).strftime("%d/%m/%Y %H:%M")

  if trovati:
    risultati = "\n\n".join(trovati)
    messaggio = (
        f"📋 <b>{html.escape(intestazione)}</b>\n"
        f"🕒 <i>Controllo del: {data_controllo}</i>\n\n"
        f"🔍 <b>Trovati {len(trovati)} atti per '{KEYWORD}':</b>\n\n"
        f"{risultati}"
    )
    if len(messaggio) > 4000:
      messaggio = (
          messaggio[:3900]
          + f"\n\n... <i>(ulteriori risultati sul <a href='{URL_INDICE}'>sito</a>)</i>"
      )
    return messaggio
  else:
    return (
        f"📋 <b>{html.escape(intestazione)}</b>\n"
        f"🕒 <i>Controllo del: {data_controllo}</i>\n\n"
        f"ℹ️ Nessun concorso o atto contenente la parola <b>'{KEYWORD}'</b>"
        " trovato nell'edizione corrente."
    )


def invia_notifica_programmata():
  logger.info("Esecuzione notifica programmata...")
  messaggio = cerca_nel_bollettino()
  iscritti = get_tutti_iscritti()

  for chat_id in iscritti:
    try:
      bot.send_message(
          chat_id, messaggio, parse_mode="HTML", disable_web_page_preview=True
      )
    except telebot.apihelper.ApiTelegramException as e:
      if e.error_code in [403, 400]:
        rimuovi_utente(chat_id)


# --- COMANDI TELEGRAM ---
@bot.message_handler(commands=["start"])
def comando_start(message):
  chat_id = message.chat.id
  username = message.from_user.username
  is_nuovo = aggiungi_utente(chat_id, username)

  if is_nuovo:
    testo = (
        "👋 <b>Benvenuto!</b>\n\n"
        f"Sei iscritto agli aggiornamenti per la parola <i>'{KEYWORD}'</i> nella"
        " sezione <b>Concorsi</b>.\n"
        "Riceverai una notifica automatica <b>ogni giovedì alle 10:00</b>.\n\n"
        "👉 Usa /cerca per controllare subito\n"
        "👉 Usa /stop per cancellarti"
    )
  else:
    testo = (
        "Sei già iscritto!\n\n"
        "👉 Usa /cerca per verificare subito\n"
        "👉 Usa /stop per cancellarti"
    )
  bot.send_message(chat_id, testo, parse_mode="HTML")


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
  esito = cerca_nel_bollettino()
  bot.send_message(
      message.chat.id, esito, parse_mode="HTML", disable_web_page_preview=True
  )


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

  scheduler = BackgroundScheduler(timezone=TIMEZONE)
  scheduler.add_job(
      invia_notifica_programmata, "cron", day_of_week="thu", hour=10, minute=0
  )
  scheduler.start()

  threading.Thread(target=avvia_polling_sicuro, daemon=True).start()

  porta = int(os.getenv("PORT", 10000))
  logger.info(f"Avvio Flask su porta {porta}")
  app.run(host="0.0.0.0", port=porta)
