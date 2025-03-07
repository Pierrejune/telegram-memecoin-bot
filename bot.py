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
import threading
import asyncio
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

# Variables globales
trade_active = False
chat_id_global = None
stop_event = threading.Event()  # Signal d’arrêt pour les threads
active_threads = []

# Variables d’environnement
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
QUICKNODE_SOL_WS_URL = "wss://little-maximum-glade.solana-mainnet.quiknode.pro/5da088be927d31731b0d7284c30a0640d8e4dd50/"
PORT = int(os.getenv("PORT", 8080))

app = Flask(__name__)
bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=False)

solana_keypair = None

# Constantes Solana
RAYDIUM_PROGRAM_ID = Pubkey.from_string("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8")
PUMP_FUN_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfH43SboMiMEWCPkDPk")

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

def validate_address(token_address):
    try:
        Pubkey.from_string(token_address)
        return len(token_address) >= 32 and len(token_address) <= 44
    except ValueError:
        return False

@backoff.on_exception(backoff.expo, Exception, max_tries=5)
async def snipe_solana_pools(chat_id):
    while not stop_event.is_set():
        if not solana_keypair:
            initialize_bot(chat_id)
            if not solana_keypair:
                await asyncio.sleep(30)
                continue
        try:
            async with websockets.connect(QUICKNODE_SOL_WS_URL, ping_interval=10, ping_timeout=20) as ws:
                await ws.send(json.dumps({
                    "jsonrpc": "2.0", "id": 1, "method": "programSubscribe",
                    "params": [str(RAYDIUM_PROGRAM_ID), {"encoding": "base64"}]
                }))
                await ws.send(json.dumps({
                    "jsonrpc": "2.0", "id": 2, "method": "programSubscribe",
                    "params": [str(PUMP_FUN_PROGRAM_ID), {"encoding": "base64"}]
                }))
                queue_message(chat_id, "🔄 Sniping Solana actif (Raydium/Pump.fun)")
                logger.info("Sniping Solana démarré")
                while not stop_event.is_set():
                    try:
                        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                        if 'params' not in msg or 'result' not in msg:
                            continue
                        data = msg['params']['result']['value']['account']['data'][0]
                        pubkey = msg['params']['result']['pubkey']
                        logger.info(f"Données WebSocket reçues: {data[:100]}... (pubkey: {pubkey})")
                        potential_addresses = [addr for addr in data.split() if validate_address(addr) and addr not in BLACKLISTED_TOKENS]
                        for token_address in potential_addresses:
                            queue_message(chat_id, f"🎯 Token détecté : `{token_address}` (Solana)")
                            logger.info(f"Token détecté: {token_address}")
                    except Exception as e:
                        logger.error(f"Erreur sniping Solana WebSocket: {str(e)}")
                    await asyncio.sleep(0.1)
        except Exception as e:
            queue_message(chat_id, f"⚠️ Erreur sniping Solana: `{str(e)}`")
            logger.error(f"Erreur sniping Solana connexion: {str(e)}")
            await asyncio.sleep(5)

def run_task_in_thread(task, *args):
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(task(*args))
        loop.close()
    except Exception as e:
        logger.error(f"Erreur dans thread {task.__name__}: {str(e)}")
        queue_message(args[0], f"⚠️ Erreur thread `{task.__name__}`: `{str(e)}`")

def initialize_and_run_threads(chat_id):
    global trade_active, chat_id_global, active_threads
    chat_id_global = chat_id
    try:
        initialize_bot(chat_id)
        if solana_keypair:
            trade_active = True
            stop_event.clear()  # Réinitialiser l’événement d’arrêt
            queue_message(chat_id, "▶️ Trading Solana lancé avec succès!")
            logger.info("Trading démarré")
            tasks = [snipe_solana_pools]  # Temporairement une seule tâche pour tester
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
    global trade_active
    chat_id = message.chat.id
    logger.info(f"Commande /start reçue de {chat_id}")
    queue_message(chat_id, "✅ Bot démarré!")
    if not trade_active:
        initialize_and_run_threads(chat_id)
    else:
        queue_message(chat_id, "ℹ️ Trading déjà actif!")
    show_main_menu(chat_id)  # Appel synchrone

@bot.message_handler(commands=['menu', 'Menu', 'MENU'])
def menu_message(message):
    chat_id = message.chat.id
    logger.info(f"Commande /menu reçue de {chat_id}")
    queue_message(chat_id, "ℹ️ Affichage du menu...")
    show_main_menu(chat_id)  # Appel synchrone

@bot.message_handler(commands=['stop', 'Stop', 'STOP'])
def stop_message(message):
    global trade_active, active_threads
    chat_id = message.chat.id
    logger.info(f"Commande /stop reçue de {chat_id}")
    if trade_active:
        trade_active = False
        stop_event.set()  # Signal d’arrêt pour tous les threads
        for thread in active_threads:
            if thread.is_alive():
                logger.info(f"Attente arrêt du thread {thread.name}")
                thread.join(timeout=2)  # Attendre max 2s
        active_threads = []
        while not message_queue.empty():
            message_queue.get()
        queue_message(chat_id, "⏹️ Trading arrêté.")
        logger.info("Trading arrêté, threads réinitialisés")
    else:
        queue_message(chat_id, "ℹ️ Trading déjà arrêté!")
    show_main_menu(chat_id)  # Appel synchrone

def show_main_menu(chat_id):
    try:
        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton("ℹ️ Statut", callback_data="status"),
            InlineKeyboardButton("▶️ Lancer", callback_data="launch"),
            InlineKeyboardButton("⏹️ Arrêter", callback_data="stop")
        )
        queue_message(chat_id, "*Menu principal:*", reply_markup=markup)
        logger.info(f"Menu affiché pour {chat_id}")
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur affichage menu: `{str(e)}`")
        logger.error(f"Erreur affichage menu: {str(e)}")

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
