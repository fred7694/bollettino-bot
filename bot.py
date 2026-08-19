import os
import sqlite3
import logging
import threading
from urllib.parse import urljoin
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
import pytz
import requests
from bs4 import BeautifulSoup
import telebot
from apscheduler.schedulers.background import BackgroundScheduler

# --- CONFIGURAZIONE ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "8769617935:AAErJVJn_FVNOQCWlL3EcaqPZ_0MFXqA20A")
URL_BOLLETTINO = "https://www.regione.piemonte.it/governo/bollettino/abbonati/2026/corrente/"
KEYWORD = "chirurgia"
DB_FILE = "iscritti.db"
TIMEZONE = pytz.timezone("Europe/Rome")

# Setup Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

bot = telebot.TeleBot(BOT_TOKEN)


# --- 1. SERVER WEB PER RENDER (PORT BINDING) ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Risponde 204 No Content: zero dati inviati, nessun errore di output
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):
        # Supporta le richieste HEAD di cron-job.org / UptimeRobot
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format, *args):
        # Disabilita i log delle richieste HTTP per non intasare la console
        return

def avvia_server_web():
    porta = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", porta), HealthCheckHandler)
    logger.info(f"Server HTTP avviato sulla porta {porta}")
    server.serve_forever()


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
            "INSERT INTO iscritti (chat_id, username, data_iscrizione) VALUES (?, ?, ?)",
            (chat_id, username or "Sconosciuto", now)
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
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    try:
        response = requests.get(URL_BOLLETTINO, headers=headers, timeout=20)
        response.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"Errore connessione bollettino: {e}")
        return f"⚠️ <b>Errore:</b> Impossibile raggiungere la pagina del bollettino.\n<code>{e}</code>"

    soup = BeautifulSoup(response.text, "html.parser")
    trovati = []

    for link in soup.find_all("a"):
        testo = link.get_text(strip=True)
        href = link.get("href", "")
        
        if KEYWORD.lower() in testo.lower():
            link_completo = urljoin(URL_BOLLETTINO, href)
            trovati.append(f"• <a href='{link_completo}'>{testo}</a>")

    data_oggi = datetime.now(TIMEZONE).strftime("%d/%m/%Y")
    
    if trovati:
        risultati = "\n\n".join(trovati)
        return (
            f"📋 <b>Bollettino Ufficiale Regione Piemonte</b>\n"
            f"📅 Data: <b>{data_oggi}</b>\n"
            f"🔍 Risultati per <i>'{KEYWORD}'</i>:\n\n"
            f"{risultati}"
        )
    else:
        return (
            f"📋 <b>Bollettino Ufficiale Regione Piemonte</b>\n"
            f"📅 Data: <b>{data_oggi}</b>\n\n"
            f"ℹ️ Nessun atto contenente la parola <b>'{KEYWORD}'</b> trovato nel bollettino corrente."
        )

def invia_notifica_programmata():
    logger.info("Esecuzione invio programmato...")
    messaggio = cerca_nel_bollettino()
    iscritti = get_tutti_iscritti()
    
    for chat_id in iscritti:
        try:
            bot.send_message(chat_id, messaggio, parse_mode="HTML", disable_web_page_preview=True)
        except telebot.apihelper.ApiTelegramException as e:
            if e.error_code in [403, 400]:
                rimuovi_utente(chat_id)


# --- 4. COMANDI TELEGRAM ---
@bot.message_handler(commands=['start'])
def comando_start(message):
    chat_id = message.chat.id
    username = message.from_user.username
    is_nuovo = aggiungi_utente(chat_id, username)
    
    if is_nuovo:
        testo = (
            "👋 <b>Benvenuto!</b>\n\n"
            f"Sei iscritto agli aggiornamenti per la parola <i>'{KEYWORD}'</i>.\n"
            "Riceverai una notifica automatica <b>ogni giovedì alle 10:30</b>.\n\n"
            "<b>Comandi:</b>\n"
            "👉 /cerca - Controlla subito il bollettino corrente\n"
            "👉 /stop - Cancella la tua iscrizione"
        )
    else:
        testo = (
            "Sei già iscritto!\n"
            "Riceverai gli avvisi ogni giovedì alle 10:30.\n\n"
            "Usa /cerca per verificare ora o /stop per cancellarti."
        )
    bot.send_message(chat_id, testo, parse_mode="HTML")

@bot.message_handler(commands=['stop'])
def comando_stop(message):
    if rimuovi_utente(message.chat.id):
        bot.send_message(message.chat.id, "❌ Ti sei disiscritto dal servizio. Non riceverai più notifiche.")
    else:
        bot.send_message(message.chat.id, "Non risultavi nella lista degli iscritti.")

@bot.message_handler(commands=['cerca'])
def comando_cerca(message):
    bot.send_message(message.chat.id, "🔍 Controllo in corso sul Bollettino...")
    esito = cerca_nel_bollettino()
    bot.send_message(message.chat.id, esito, parse_mode="HTML", disable_web_page_preview=True)


# --- 5. AVVIO APPLICAZIONE ---
if __name__ == "__main__":
    init_db()
    
    # 1. Avvio server Web in background per Render
    threading.Thread(target=avvia_server_web, daemon=True).start()
    
    # 2. Configurazione controllo programmato (ogni giovedì alle 09:00 italiane)
    scheduler = BackgroundScheduler(timezone=TIMEZONE)
    scheduler.add_job(invia_notifica_programmata, 'cron', day_of_week='thu', hour=10, minute=30)
    scheduler.start()
    
    # 3. Avvio ascolto messaggi Telegram
    logger.info("Bot avviato e pronto all'uso.")
    try:
        bot.infinity_polling(timeout=20, long_polling_timeout=10)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
