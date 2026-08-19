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

# Carica il file .env per non esporre il token
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
  raise ValueError(
      "Errore: BOT_TOKEN mancante. Impostalo nel file .env o nelle variabili"
      " d'ambiente."
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


# --- 1. SERVER FLASK (Anti-Bad Gateway per hosting come Render) ---
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
def estrai_data_bollettino(soup: BeautifulSoup) -> str:
  """Estrae con precisione la testata con numero e data senza catturare il corpo della pagina."""
  testo = soup.get_text("\n", strip=True)

  # Cerca la dicitura precisa "Bollettino Ufficiale n. X del GG Mese AAAA"
  match = re.search(
      r"Bollettino\s+Ufficiale[^\n\r]*?n\.?\s*\d+[^\n\r]*?del\s+\d{1,2}\s+[a-zA-ZÀ-ÿ]+\s+\d{4}",
      testo,
      re.IGNORECASE,
  )
  if match:
    return re.sub(r"\s+", " ", match.group(0)).strip()

  # Fallback sul primo elemento di intestazione valido
  for h in soup.find_all(["h1", "h2", "caption", "p"]):
    t = h.get_text(" ", strip=True)
    if "bollettino" in t.lower():
      return t[:100]

  return "Bollettino Ufficiale - Regione Piemonte"

def cerca_nel_bollettino() -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    try:
        response = requests.get(URL_BOLLETTINO, headers=headers, timeout=20)
        response.raise_for_status()
        # Il Bollettino Piemonte usa la codifica ISO-8859-1 / Latin-1 per le lettere accentate e virgolette
        response.encoding = 'iso-8859-1'
    except requests.RequestException as e:
        logger.error(f"Errore connessione bollettino: {e}")
        return f"⚠️ <b>Errore:</b> Impossibile raggiungere il sito del Bollettino.\n<code>{e}</code>"

    soup = BeautifulSoup(response.text, "html.parser")
    intestazione_bollettino = estrai_data_bollettino(soup)

    trovati = []
    link_univoci = set()

    # Cerca tutti i link che portano a un file/dettaglio atto (.htm, .html, .pdf, .rtf)
    for a_tag in soup.find_all("a", href=True):
        href = a_tag.get("href", "").strip()
        
        # Ignora ancore interne, javascript e pagine indice generali
        if not href or href.startswith("#") or "javascript" in href.lower() or "index" in href.lower():
            continue

        link_completo = urljoin(URL_BOLLETTINO, href)
        if link_completo in link_univoci:
            continue

        # RECUPERO DEL TESTO DEL SINGOLO BANDO:
        # 1. Se il testo è dentro al tag <a> stesso
        testo_singolo = a_tag.get_text(" ", strip=True)
        
        # 2. Se <a> è solo un pulsante/codice (es. "Scarica", o codice atto),
        # raccogliamo il testo immediatamente precedente risalendo fino al bando precedente
        if len(testo_singolo) < 15:
            parti_testo = []
            for prev in a_tag.previous_siblings:
                # Se incontra un altro link, un'interruzione di sezione <hr> o intestazioni di tabella, si ferma
                if getattr(prev, 'name', None) in ['a', 'hr', 'h1', 'h2', 'h3', 'table']:
                    break
                t = prev.get_text(" ", strip=True) if hasattr(prev, 'get_text') else str(prev).strip()
                if t:
                    parti_testo.insert(0, t)
            
            if parti_testo:
                testo_singolo = " ".join(parti_testo)

        # Pulizia caratteri di spaziatura
        testo_singolo = re.sub(r"\s+", " ", testo_singolo).strip()

        # Verifica se la parola chiave è presente nel singolo bando
        if KEYWORD.lower() in testo_singolo.lower():
            link_univoci.add(link_completo)
            
            if len(testo_singolo) > 350:
                testo_singolo = testo_singolo[:347] + "..."

            trovati.append(
                f"• <b>Atto:</b>\n{html.escape(testo_singolo)}\n"
                f"👉 <a href='{link_completo}'>Apri documento</a>"
            )

    data_controllo = datetime.now(TIMEZONE).strftime("%d/%m/%Y %H:%M")
    
    if trovati:
        risultati = "\n\n".join(trovati)
        messaggio = (
            f"📋 <b>{html.escape(intestazione_bollettino)}</b>\n"
            f"🕒 <i>Aggiornato al: {data_controllo}</i>\n\n"
            f"🔍 <b>Trovati {len(trovati)} atti per '{KEYWORD}':</b>\n\n"
            f"{risultati}"
        )
        if len(messaggio) > 4000:
            messaggio = messaggio[:3900] + f"\n\n... <i>(ulteriori risultati sul <a href='{URL_BOLLETTINO}'>sito del Bollettino</a>)</i>"
        return messaggio
    else:
        return (
            f"📋 <b>{html.escape(intestazione_bollettino)}</b>\n"
            f"🕒 <i>Aggiornato al: {data_controllo}</i>\n\n"
            f"ℹ️ Nessun concorso o atto contenente la parola <b>'{KEYWORD}'</b> trovato nell'edizione corrente."
        )


def invia_notifica_programmata():
  logger.info("Esecuzione notifica automatica...")
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
        "<b>Comandi disponibili:</b>\n"
        "👉 /cerca - Controlla subito i concorsi correnti\n"
        "👉 /stop - Cancella la tua iscrizione"
    )
  else:
    testo = (
        "Sei già iscritto al servizio!\n\n"
        "👉 Usa /cerca per controllare subito il bollettino\n"
        "👉 Usa /stop per annullare l'iscrizione"
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
      message.chat.id, "🔍 Controllo in corso sul Bollettino Ufficiale..."
  )
  esito = cerca_nel_bollettino()
  bot.send_message(
      message.chat.id, esito, parse_mode="HTML", disable_web_page_preview=True
  )


# --- 5. LOOP TELEGRAM RESILIENTE ---
def avvia_polling_sicuro():
  while True:
    try:
      logger.info("Connessione con Telegram avviata...")
      bot.infinity_polling(
          timeout=20, long_polling_timeout=10, skip_pending=True
      )
    except Exception as err:
      logger.error(f"Errore polling: {err}. Riconnessione tra 5 secondi...")
      time.sleep(5)


# --- 6. AVVIO APPLICAZIONE ---
if __name__ == "__main__":
  init_db()

  # Scheduler per il giovedì alle 10:00
  scheduler = BackgroundScheduler(timezone=TIMEZONE)
  scheduler.add_job(
      invia_notifica_programmata, "cron", day_of_week="thu", hour=10, minute=0
  )
  scheduler.start()

  # Thread Telegram
  threading.Thread(target=avvia_polling_sicuro, daemon=True).start()

  # Server Flask
  porta = int(os.getenv("PORT", 10000))
  logger.info(f"Avvio Flask sulla porta {porta}")
  app.run(host="0.0.0.0", port=porta)
