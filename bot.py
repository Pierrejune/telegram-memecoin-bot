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
from waitress import serve
import backoff
import websockets
from queue import Queue

# Configuration logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Session HTTP avec retries
session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})

# File d’attente pour Telegram
message_queue = Queue()
message_lock = threading.Lock()

# Variables globales
trade_active = False
portfolio = {}
detected_tokens = {}
BLACKLISTED_TOKENS = {"So11111111111111111111111111111111111111112"}
chat_id_global = None

# Variables d’environnement
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY", "5be903b581bc47d29bbfb5ab859de2eb")
QUICKNODE_SOL_URL = "https://little-maximum-glade.solana-mainnet.quiknode.pro/5da088be927d31731b0d7284c30a0640d8e4dd50/"
QUICKNODE_SOL_WS_URL = "wss://little-maximum-glade.solana-mainnet.quiknode.pro/5da088be927d31731b0d7284c30a0640d8e4dd50/"
PORT = int(os.getenv("PORT", 8080))

app = Flask(__name__)
bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=False)

solana_keypair = None

# Paramètres de trading
mise_depart_sol = 0.37
MAX_TOKEN_AGE_HOURS = 6

# Constantes Solana
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
                    bot.send_message(chat_id, text, reply_markup=reply_markup)
                else:
                    bot.send_message(chat_id, text)
            time.sleep(0.1)
        except Exception as e:
            logger.error(f"Erreur envoi message Telegram: {str(e)}")
        message_queue.task_done()

threading.Thread(target=send_message_worker, daemon=True).start()

def queue_message(chat_id, text, reply_markup=None):
    logger.info(f"Queueing message to {chat_id}: {text}")
    message_queue.put((chat_id, text, reply_markup) if reply_markup else (chat_id, text))

def initialize_bot(chat_id):
    global solana_keypair
    try:
        solana_keypair = Keypair.from_base58_string(SOLANA_PRIVATE_KEY)
        queue_message(chat_id, "✅ Clé Solana initialisée")
        logger.info("Solana initialisé")
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur initialisation Solana: {str(e)}")
        logger.error(f"Erreur Solana: {str(e)}")
        solana_keypair = None

def set_webhook():
    try:
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(url=WEBHOOK_URL)
        logger.info(f"Webhook configuré sur {WEBHOOK_URL}")
        return True
    except Exception as e:
        logger.error(f"Erreur configuration webhook: {str(e)}")
        queue_message(chat_id_global, f"⚠️ Erreur webhook: {str(e)}")
        return False

def validate_address(token_address):
    try:
        Pubkey.from_string(token_address)
        return len(token_address) >= 32 and len(token_address) <= 44
    except ValueError:
        return False

@backoff.on_exception(backoff.expo, Exception, max_tries=5)
def get_token_data(token_address):
    try:
        response = session.get(f"https://public-api.birdeye.so/v1/token/overview?address={token_address}", headers={"X-API-KEY": BIRDEYE_API_KEY}, timeout=10)
        response.raise_for_status()
        data = response.json()
        if not data or 'data' not in data:
            logger.error(f"Réponse Birdeye vide pour {token_address}: {response.text}")
            return None
        token_data = data['data']
        age_hours = (time.time() - token_data.get('created_at', time.time()) / 1000) / 3600
        if age_hours > MAX_TOKEN_AGE_HOURS:
            return None
        return {
            'volume_24h': token_data.get('volume', {}).get('h24', 0),
            'liquidity': token_data.get('liquidity', 0),
            'market_cap': token_data.get('mc', 0),
            'price': token_data.get('price', 0),
            'buy_sell_ratio': 1,
            'pair_created_at': token_data.get('created_at', time.time()) / 1000
        }
    except Exception as e:
        logger.error(f"Erreur Birdeye pour {token_address}: {str(e)}")
        return None

@backoff.on_exception(backoff.expo, Exception, max_tries=5)
async def snipe_solana_pools(chat_id):
    while trade_active:
        if not solana_keypair:
            initialize_bot(chat_id)
            if not solana_keypair:
                await asyncio.sleep(30)
                continue
        try:
            async with websockets.connect(QUICKNODE_SOL_WS_URL, ping_interval=10, ping_timeout=20) as ws:
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "programSubscribe", "params": [str(RAYDIUM_PROGRAM_ID), {"encoding": "base64"}]}))
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "programSubscribe", "params": [str(PUMP_FUN_PROGRAM_ID), {"encoding": "base64"}]}))
                queue_message(chat_id, "🔄 Sniping Solana actif (Raydium/Pump.fun)")
                logger.info("Sniping Solana démarré")
                while trade_active:
                    try:
                        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                        if 'result' not in msg or 'params' not in msg:
                            continue
                        data = msg['params']['result']['value']['account']['data'][0]
                        logger.info(f"Données Solana reçues: {data}")
                        token_addresses = [acc for acc in data.split() if validate_address(acc) and acc not in BLACKLISTED_TOKENS and acc not in portfolio]
                        for token_address in token_addresses:
                            response = session.post(QUICKNODE_SOL_URL, json={
                                "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo", "params": [token_address]
                            }, timeout=5)
                            account_info = response.json().get('result', {}).get('value', {})
                            if account_info and (time.time() - account_info.get('lamports', 0)) / 3600 <= MAX_TOKEN_AGE_HOURS:
                                exchange = 'Raydium' if str(RAYDIUM_PROGRAM_ID) in msg['params']['result']['pubkey'] else 'Pump.fun'
                                queue_message(chat_id, f'🎯 Snipe détecté : {token_address} (Solana - {exchange})')
                                logger.info(f"Snipe Solana: {token_address}")
                                await validate_and_trade(chat_id, token_address)
                    except Exception as e:
                        logger.error(f"Erreur sniping Solana WebSocket: {str(e)}")
                    await asyncio.sleep(0.1)  # Réduit la charge
        except Exception as e:
            queue_message(chat_id, f"⚠️ Erreur sniping Solana: {str(e)}")
            logger.error(f"Erreur sniping Solana connexion: {str(e)}")
            await asyncio.sleep(5)

@backoff.on_exception(backoff.expo, Exception, max_tries=5)
async def detect_birdeye(chat_id):
    while trade_active:
        try:
            response = session.get(
                f"https://public-api.birdeye.so/v1/token/list?sort_by=mc&sort_type=desc&limit=50",
                headers={"X-API-KEY": BIRDEYE_API_KEY},
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            if not data or 'data' not in data or 'tokens' not in data['data']:
                logger.error(f"Réponse Birdeye invalide: {data}")
                await asyncio.sleep(10)
                continue
            tokens = data['data']['tokens']
            for token in tokens:
                token_address = token.get('address')
                age_hours = (time.time() - token.get('created_at', time.time()) / 1000) / 3600
                if not token_address or age_hours > MAX_TOKEN_AGE_HOURS or token_address in BLACKLISTED_TOKENS or token_address in portfolio:
                    continue
                queue_message(chat_id, f'🔍 Détection Birdeye : {token_address} (Solana)')
                logger.info(f"Détection Birdeye: {token_address}")
                await validate_and_trade(chat_id, token_address)
            await asyncio.sleep(10)  # Réduit la fréquence pour éviter rate limit
        except Exception as e:
            queue_message(chat_id, f"⚠️ Erreur Birdeye: {str(e)}")
            logger.error(f"Erreur Birdeye: {str(e)}")
            await asyncio.sleep(10)

async def validate_and_trade(chat_id, token_address):
    try:
        if token_address in BLACKLISTED_TOKENS:
            return
        data = get_token_data(token_address)
        if data is None:
            queue_message(chat_id, f'⚠️ {token_address} rejeté : Pas de données ou trop vieux')
            return
        amount = mise_depart_sol
        await buy_token_solana(chat_id, token_address, amount)
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur validation {token_address}: {str(e)}")
        logger.error(f"Erreur validation: {str(e)}")

async def buy_token_solana(chat_id, contract_address, amount):
    try:
        if not solana_keypair:
            initialize_bot(chat_id)
            if not solana_keypair:
                raise Exception("Solana non initialisé")
        amount_in = int(amount * 10**9)
        response = session.post(QUICKNODE_SOL_URL, json={
            "jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash", "params": [{"commitment": "finalized"}]
        }, timeout=5)
        blockhash = response.json()['result']['value']['blockhash']
        tx = Transaction()
        tx.recent_blockhash = Pubkey.from_string(blockhash)
        instruction = Instruction(
            program_id=RAYDIUM_PROGRAM_ID if 'Raydium' in contract_address else PUMP_FUN_PROGRAM_ID,
            accounts=[
                {"pubkey": solana_keypair.pubkey(), "is_signer": True, "is_writable": True},
                {"pubkey": Pubkey.from_string(contract_address), "is_signer": False, "is_writable": True},
                {"pubkey": TOKEN_PROGRAM_ID, "is_signer": False, "is_writable": False}
            ],
            data=bytes([2]) + amount_in.to_bytes(8, 'little')
        )
        tx.add(instruction)
        tx.sign([solana_keypair])
        tx_hash = session.post(QUICKNODE_SOL_URL, json={
            "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
            "params": [base58.b58encode(tx.serialize()).decode('utf-8')]
        }, timeout=5).json()['result']
        queue_message(chat_id, f'⏳ Achat Solana {amount} SOL : {contract_address}, TX: {tx_hash}')
        entry_price = get_token_data(contract_address).get('price', 0)
        portfolio[contract_address] = {
            'amount': amount, 'chain': 'solana', 'entry_price': entry_price,
            'price_history': [entry_price], 'highest_price': entry_price, 'profit': 0.0,
            'buy_time': time.time()
        }
        exchange = 'Raydium' if 'Raydium' in contract_address else 'Pump.fun'
        queue_message(chat_id, f'✅ Achat réussi : {amount} SOL de {contract_address} ({exchange})')
    except Exception as e:
        queue_message(chat_id, f"⚠️ Échec achat Solana {contract_address}: {str(e)}")
        logger.error(f"Échec achat Solana: {str(e)}")

def run_task_in_thread(task, *args):
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(task(*args))
    except Exception as e:
        logger.error(f"Erreur dans thread {task.__name__}: {str(e)}")
        queue_message(args[0], f"⚠️ Erreur thread {task.__name__}: {str(e)}")

def initialize_and_run_threads(chat_id):
    global trade_active, chat_id_global
    chat_id_global = chat_id
    try:
        initialize_bot(chat_id)
        if solana_keypair:
            trade_active = True
            queue_message(chat_id, "▶️ Trading Solana lancé avec succès!")
            logger.info("Trading démarré")
            tasks = [snipe_solana_pools, detect_birdeye]
            for task in tasks:
                threading.Thread(target=run_task_in_thread, args=(task, chat_id), daemon=True).start()
                logger.info(f"Tâche {task.__name__} lancée")
        else:
            queue_message(chat_id, "⚠️ Échec initialisation : Solana non connecté")
            logger.error("Échec initialisation: Solana manquant")
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur initialisation: {str(e)}")
        logger.error(f"Erreur initialisation: {str(e)}")
        trade_active = False

@app.route("/webhook", methods=['POST'])
def webhook():
    global trade_active
    if request.method == "POST" and request.headers.get("content-type") == "application/json":
        update = telebot.types.Update.de_json(request.get_json())
        logger.info(f"Webhook reçu: {request.get_json()}")
        try:
            bot.process_new_updates([update])
            if not trade_active and update.message and update.message.text == "/start":
                initialize_and_run_threads(update.message.chat.id)
            return 'OK', 200
        except Exception as e:
            logger.error(f"Erreur traitement webhook: {str(e)}")
            return 'ERROR', 500
    logger.error("Requête webhook invalide")
    return abort(403)

@bot.message_handler(commands=['start'])
def start_message(message):
    chat_id = message.chat.id
    logger.info(f"Commande /start reçue de {chat_id}")
    queue_message(chat_id, "✅ Bot démarré!")
    threading.Thread(target=run_task_in_thread, args=(show_main_menu, chat_id), daemon=True).start()

@bot.message_handler(commands=['menu'])
def menu_message(message):
    chat_id = message.chat.id
    logger.info(f"Commande /menu reçue de {chat_id}")
    threading.Thread(target=run_task_in_thread, args=(show_main_menu, chat_id), daemon=True).start()

@bot.message_handler(commands=['stop'])
def stop_message(message):
    global trade_active
    chat_id = message.chat.id
    logger.info(f"Commande /stop reçue de {chat_id}")
    trade_active = False
    queue_message(chat_id, "⏹️ Trading arrêté.")

async def show_main_menu(chat_id):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("ℹ️ Statut", callback_data="status"),
        InlineKeyboardButton("▶️ Lancer", callback_data="launch"),
        InlineKeyboardButton("⏹️ Arrêter", callback_data="stop")
    )
    queue_message(chat_id, "Menu principal:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    global trade_active
    chat_id = call.message.chat.id
    logger.info(f"Callback reçu: {call.data} de {chat_id}")
    try:
        if call.data == "status":
            queue_message(chat_id, f"ℹ️ Statut actuel :\nTrading actif: {'Oui' if trade_active else 'Non'}\nMise Solana: {mise_depart_sol} SOL")
        elif call.data == "launch":
            if not trade_active:
                initialize_and_run_threads(chat_id)
            else:
                queue_message(chat_id, "⚠️ Trading déjà en cours.")
        elif call.data == "stop":
            trade_active = False
            queue_message(chat_id, "⏹️ Trading arrêté.")
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur générale: {str(e)}")
        logger.error(f"Erreur callback: {str(e)}")

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
