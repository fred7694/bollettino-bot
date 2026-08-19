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

# Carica le variabili d'ambiente dal file .env (se presente)
load_dotenv()

# --- CONFIGURAZIONE ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
  raise ValueError(
      "BOT_TOKEN mancante! Impostalo nelle variabili d'ambiente o nel file"
      " .env"
  )

URL_BOLLETTINO = "https://www.regione.piemonte.it/governo/bollettino/abbonati/2026/corrente/concorsi/index.htm"
KEYWORD = "chirurgia"
DB_FILE = "iscritti.db"
TIMEZONE = pytz.timezone("Europe/Rome")

# Setup Logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

log_flask = logging.getLogger("werkzeug")
log_flask.setLevel(logging.ERROR)


# --- 1. SERVER FLASK (Anti-Bad Gateway) ---
@app.route("/")
@app.route("/health")
def health_check():
  return "OK", 200


# --- 2. GESTIONE DATABASE SQLITE ---
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


# --- 3. SCRAPING BOLLETTINO ---
def cerca_nel_bollettino() -> str:
  headers = {
      "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
          " like Gecko) Chrome/120.0.0.0 Safari/537.36"
      )
  }

  try:
    response = requests.get(URL_BOLLETTINO, headers=headers, timeout=20)
    response.raise_for_status()
    response.encoding = response.apparent_encoding
  except requests.RequestException as e:
    logger.error(f"Errore connessione bollettino: {e}")
    return (
        f"⚠️ <b>Errore:</b> Impossibile raggiungere la pagina del"
        f" bollettino.\n<code>{e}</code>"
    )

  soup = BeautifulSoup(response.text, "html.parser")

  # Estrazione dell'intestazione/data del Bollettino dalla pagina HTML
  intestazione_bollettino = ""
  match_data = re.search(
      r"Bollettino\s+Ufficiale\s+n\.\s*\d+\s+del\s+[0-9a-zA-Z\s]+",
      soup.get_text(),
      re.IGNORECASE,
  )
  if match_data:
    intestazione_bollettino = (
        match_data.group(0).replace("\n", " ").strip()
    )  # Pulisce eventuali a capo
  else:
    # Fallback se non trovato nell'HTML
    intestazione_bollettino = (
        "Bollettino Ufficiale - Sezione Concorsi (Data non rilevata)"
    )

  data_controllo = datetime.now(TIMEZONE).strftime("%d/%m/%Y %H:%M")
  trovati = []

  # Ricerca di link contenenti la parola chiave
  for link in soup.find_all("a"):
    testo = link.get_text(" ", strip=True)
    href = link.get("href", "")

    if not href or not testo:
      continue

    if KEYWORD.lower() in testo.lower():
      link_completo = urljoin(URL_BOLLETTINO, href)
      testo_pulito = html.escape(testo)
      trovati.append(f"• <a href='{link_completo}'>{testo_pulito}</a>")

  if trovati:
    risultati = "\n\n".join(trovati)
    messaggio = (
        f"📋 <b>{html.escape(intestazione_bollettino)}</b>\n"
        f"🕒 Controllo effettuato il: <b>{data_controllo}</b>\n\n"
        f"🔍 Risultati per <i>'{KEYWORD}'</i> ({len(trovati)} trovati):\n\n"
        f"{risultati}"
    )
    if len(messaggio) > 4000:
      messaggio = (
          messaggio[:3950] + "\n\n... <i>(ulteriori risultati sul sito web)</i>"
      )
    return messaggio
  else:
    return (
        f"📋 <b>{html.escape(intestazione_bollettino)}</b>\n"
        f"🕒 Controllo effettuato il: <b>{data_controllo}</b>\n\n"
        f"ℹ️ Nessun concorso contenente la parola <b>'{KEYWORD}'</b> trovato"
        " nell'edizione corrente."
    )


def invia_notifica_programmata():
  logger.info("Esecuzione invio programmato del giovedì...")
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


# --- 4. COMANDI TELEGRAM ---
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
        "<b>Comandi:</b>\n"
        "👉 /cerca - Controlla subito i concorsi correnti\n"
        "👉 /stop - Cancella la tua iscrizione"
    )
  else:
    testo = (
        "Sei già iscritto!\n"
        "Riceverai gli avvisi ogni giovedì alle 10:00.\n\n"
        "Usa /cerca per verificare ora o /stop per cancellarti."
    )
  bot.send_message(chat_id, testo, parse_mode="HTML")


@bot.message_handler(commands=["stop"])
def comando_stop(message):
  if rimuovi_utente(message.chat.id):
    bot.send_message(
        message.chat.id,
        "❌ Ti sei disiscritto dal servizio. Non riceverai più notifiche.",
    )
  else:
    bot.send_message(
        message.chat.id, "Non risultavi nella lista degli iscritti."
    )


@bot.message_handler(commands=["cerca"])
def comando_cerca(message):
  bot.send_message(
      message.chat.id, "🔍 Controllo in corso sulla sezione Concorsi..."
  )
  esito = cerca_nel_bollettino()
  bot.send_message(
      message.chat.id, esito, parse_mode="HTML", disable_web_page_preview=True
  )


# --- 5. LOOP RESILIENTE TELEGRAM ---
def avvia_polling_sicuro():
  while True:
    try:
      logger.info("Avvio connessione con i server di Telegram...")
      bot.infinity_polling(
          timeout=20, long_polling_timeout=10, skip_pending=True
      )
    except Exception as err:
      logger.error(
          f"Errore nel polling: {err}. Riconnessione automatica tra 5"
          " secondi..."
      )
      time.sleep(5)


# --- 6. AVVIO APPLICAZIONE ---
if __name__ == "__main__":
  init_db()

  # 1. Avvia Scheduler
  scheduler = BackgroundScheduler(timezone=TIMEZONE)
  scheduler.add_job(
      invia_notifica_programmata, "cron", day_of_week="thu", hour=10, minute=0
  )
  scheduler.start()

  # 2. Avvia Telegram in un thread protetto
  threading.Thread(target=avvia_polling_sicuro, daemon=True).start()

  # 3. Avvia Flask sul thread principale per Render
  porta = int(os.getenv("PORT", 10000))
  logger.info(f"Avvio server Web Flask sulla porta {porta}")
  app.run(host="0.0.0.0", port=porta)
