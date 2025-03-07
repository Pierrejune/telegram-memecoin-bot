import os
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import logging
import requests
import time
import json
import base58
from flask import Flask, request, abort
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from solders.instruction import Instruction
import threading
import asyncio
from datetime import datetime
import re
from waitress import serve
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import backoff
import websockets
from queue import Queue

# Configuration logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Session HTTP avec retries
session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
retry_strategy = Retry(total=5, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET", "POST"])
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("https://", adapter)

# File d’attente pour Telegram
message_queue = Queue()
message_lock = threading.Lock()
start_lock = threading.Lock()
last_start_time = 0
last_twitter_request_time = 0

# Variables globales
daily_trades = {'buys': [], 'sells': []}
rejected_tokens = {}
trade_active = False
portfolio = {}
detected_tokens = {}
BLACKLISTED_TOKENS = {"So11111111111111111111111111111111111111112"}
pause_auto_sell = False
chat_id_global = None
active_threads = []

# Variables d’environnement
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY", "5be903b581bc47d29bbfb5ab859de2eb")
TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN", "AAAAAAAAAAAAAAAAAAAAAD6%2BzQEAAAAAaDN4Thznh7iGRdfqEhebMgWtohs%3DyuaSpNWBCnPcQv5gjERphqmZTIclzPiVqqnirPmdZt4fpRd96D")
QUICKNODE_SOL_URL = "https://little-maximum-glade.solana-mainnet.quiknode.pro/5da088be927d31731b0d7284c30a0640d8e4dd50/"
QUICKNODE_SOL_WS_URL = "wss://little-maximum-glade.solana-mainnet.quiknode.pro/5da088be927d31731b0d7284c30a0640d8e4dd50/"
PORT = int(os.getenv("PORT", 8080))

app = Flask(__name__)
bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=False)

solana_keypair = None

# Paramètres de trading (inchangés)
mise_depart_sol = 0.37
stop_loss_threshold = 15
trailing_stop_percentage = 5
take_profit_steps = [1.2, 2, 10, 100, 500]
max_positions = 5
profit_reinvestment_ratio = 0.9
slippage_max = 0.05
MIN_VOLUME_SOL = 100
MAX_VOLUME_SOL = 2000000
MIN_LIQUIDITY = 5000
MIN_MARKET_CAP_SOL = 1000
MAX_MARKET_CAP_SOL = 5000000
MIN_BUY_SELL_RATIO = 1.5
MAX_TOKEN_AGE_HOURS = 6
MIN_SOCIAL_MENTIONS = 5

# Constantes Solana (inchangées)
RAYDIUM_PROGRAM_ID = Pubkey.from_string("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8")
PUMP_FUN_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfH43SboMiMEWCPkDPk")
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")

def send_message_worker():
    while True:
        item = message_queue.get()
        chat_id, text = item[0], item[1]
        reply_markup = item[2] if len(item) > 2 else None
        try:
            with message_lock:
                if reply_markup:
                    bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode='Markdown')
                else:
                    bot.send_message(chat_id, text, parse_mode='Markdown')
            time.sleep(0.1)
        except Exception as e:
            logger.error(f"Erreur envoi message Telegram: {str(e)}")
        message_queue.task_done()

threading.Thread(target=send_message_worker, daemon=True).start()

def queue_message(chat_id, text, reply_markup=None):
    logger.info(f"Queueing message to {chat_id}: {text}")
    message_queue.put((chat_id, text, reply_markup))

def initialize_bot(chat_id):
    global solana_keypair
    try:
        solana_keypair = Keypair.from_base58_string(SOLANA_PRIVATE_KEY)
        queue_message(chat_id, "✅ Clé Solana initialisée")
        logger.info("Solana initialisé")
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur initialisation Solana: `{str(e)}`")
        logger.error(f"Erreur Solana: {str(e)}")
        solana_keypair = None

def set_webhook():
    try:
        bot.remove_webhook()
        time.sleep(1)
        success = bot.set_webhook(url=WEBHOOK_URL)
        if success:
            logger.info(f"Webhook configuré sur {WEBHOOK_URL}")
            queue_message(chat_id_global, f"✅ Webhook configuré sur {WEBHOOK_URL}")
            return True
        else:
            raise Exception("Échec de la configuration du webhook")
    except Exception as e:
        logger.error(f"Erreur configuration webhook: {str(e)}")
        queue_message(chat_id_global, f"⚠️ Erreur webhook: `{str(e)}`")
        return False

@app.route("/webhook", methods=['POST'])
def webhook():
    if request.method == "POST" and request.headers.get("content-type") == "application/json":
        update_json = request.get_json()
        logger.info(f"Webhook reçu: {json.dumps(update_json)}")
        try:
            update = telebot.types.Update.de_json(update_json)
            bot.process_new_updates([update])
            return 'OK', 200
        except Exception as e:
            logger.error(f"Erreur traitement webhook: {str(e)}")
            queue_message(chat_id_global, f"⚠️ Erreur webhook: `{str(e)}`")
            return f"Erreur: {str(e)}", 500
    logger.error("Requête webhook invalide")
    return abort(403)

@bot.message_handler(commands=['start', 'Start', 'START'])
def start_message(message):
    global trade_active, last_start_time
    chat_id = message.chat.id
    current_time = time.time()
    with start_lock:
        if current_time - last_start_time < 1:
            queue_message(chat_id, "⚠️ Attendez 1 seconde avant de redémarrer!")
            return
        last_start_time = current_time
        logger.info(f"Commande /start reçue de {chat_id}")
        queue_message(chat_id, "✅ Bot démarré!")
        if not trade_active:
            initialize_and_run_threads(chat_id)
        else:
            queue_message(chat_id, "ℹ️ Trading déjà actif!")
        asyncio.run_coroutine_threadsafe(show_main_menu(chat_id), asyncio.get_event_loop())

@bot.message_handler(commands=['menu', 'Menu', 'MENU'])
def menu_message(message):
    chat_id = message.chat.id
    logger.info(f"Commande /menu reçue de {chat_id}")
    queue_message(chat_id, "ℹ️ Affichage du menu...")
    asyncio.run_coroutine_threadsafe(show_main_menu(chat_id), asyncio.get_event_loop())

@bot.message_handler(commands=['stop', 'Stop', 'STOP'])
def stop_message(message):
    global trade_active, active_threads
    chat_id = message.chat.id
    logger.info(f"Commande /stop reçue de {chat_id}")
    if trade_active:
        trade_active = False
        for thread in active_threads:
            if thread.is_alive():
                logger.info(f"Arrêt du thread {thread.name}")
        active_threads = []
        while not message_queue.empty():
            message_queue.get()
        queue_message(chat_id, "⏹️ Trading arrêté.")
        logger.info("Trading arrêté, threads réinitialisés")
    else:
        queue_message(chat_id, "ℹ️ Trading déjà arrêté!")
    asyncio.run_coroutine_threadsafe(show_main_menu(chat_id), asyncio.get_event_loop())

async def show_main_menu(chat_id):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("ℹ️ Statut", callback_data="status"),
        InlineKeyboardButton("▶️ Lancer", callback_data="launch"),
        InlineKeyboardButton("⏹️ Arrêter", callback_data="stop"),
        InlineKeyboardButton("💰 Portefeuille", callback_data="portfolio"),
        InlineKeyboardButton("📅 Récapitulatif", callback_data="daily_summary")
    )
    markup.add(
        InlineKeyboardButton("🔧 Ajuster Mise SOL", callback_data="adjust_mise_sol"),
        InlineKeyboardButton("📉 Ajuster Stop-Loss", callback_data="adjust_stop_loss")
    )
    markup.add(
        InlineKeyboardButton("📈 Ajuster Take-Profit", callback_data="adjust_take_profit"),
        InlineKeyboardButton("🔄 Ajuster Réinvestissement", callback_data="adjust_reinvestment")
    )
    queue_message(chat_id, "*Menu principal:*", reply_markup=markup)

def initialize_and_run_threads(chat_id):
    global trade_active, chat_id_global, active_threads
    chat_id_global = chat_id
    try:
        initialize_bot(chat_id)
        if solana_keypair:
            trade_active = True
            queue_message(chat_id, "▶️ Trading Solana lancé avec succès!")
            logger.info("Trading démarré")
            tasks = [snipe_solana_pools, detect_dexscreener, detect_birdeye, monitor_and_sell]
            active_threads = []
            for task in tasks:
                thread = threading.Thread(target=run_task_in_thread, args=(task, chat_id), daemon=True)
                thread.start()
                active_threads.append(thread)
                logger.info(f"Tâche {task.__name__} lancée")
        else:
            queue_message(chat_id, "⚠️ Échec initialisation : Solana non connecté")
            logger.error("Échec initialisation: Solana manquant")
            trade_active = False
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur initialisation: `{str(e)}`")
        logger.error(f"Erreur initialisation: {str(e)}")
        trade_active = False

# ... (autres fonctions inchangées pour l'instant)

if __name__ == "__main__":
    if not all([TELEGRAM_TOKEN, WALLET_ADDRESS, SOLANA_PRIVATE_KEY, WEBHOOK_URL]):
        logger.error("Variables d’environnement manquantes")
        exit(1)
    if set_webhook():
        logger.info("Webhook configuré, démarrage du serveur...")
        serve(app, host="0.0.0.0", port=PORT, threads=10)
    else:
        logger.error("Échec du webhook, passage en mode polling")
        bot.polling(none_stop=True)
