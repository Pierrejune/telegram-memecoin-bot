import os
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import logging
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
from queue import Queue
import aiohttp
import websockets
from cryptography.fernet import Fernet
import random
from collections import deque

# Configuration logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# File d’attente Telegram
message_queue = Queue()
message_lock = threading.Lock()

# Variables globales
daily_trades = {'buys': [], 'sells': []}
trade_active = False
bot_active = True
portfolio = {}
detected_tokens = {}
last_detection_time = {}
BLACKLISTED_TOKENS = {"So11111111111111111111111111111111111111112"}
dynamic_blacklist = set()
pause_auto_sell = False
chat_id_global = None
stop_event = threading.Event()
token_metrics = {}
solana_keypair = None
price_history = {}
buy_volume = {}
sell_volume = {}
buy_count = {}
sell_count = {}
token_timestamps = {}
accounts_in_tx = {}
sol_price_usd = 0

# Variables d’environnement
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
QUICKNODE_SOL_URL = os.getenv("QUICKNODE_SOL_URL", "https://little-maximum-glade.solana-mainnet.quiknode.pro/5da088be927d31731b0d7284c30a0640d8e4dd50/")
QUICKNODE_WS_URL = os.getenv("QUICKNODE_WS_URL")  # Ajout de l'URL WebSocket
BITQUERY_ACCESS_TOKEN = os.getenv("BITQUERY_ACCESS_TOKEN")
PORT = int(os.getenv("PORT", 8080))

# Chiffrement clé privée
cipher_suite = Fernet(Fernet.generate_key())
encrypted_private_key = cipher_suite.encrypt(SOLANA_PRIVATE_KEY.encode()).decode() if SOLANA_PRIVATE_KEY else "N/A"

# Paramètres trading (critères stricts conservés)
mise_depart_sol = 0.5
stop_loss_threshold = 10
trailing_stop_percentage = 3
take_profit_steps = [1.2, 2, 10, 100, 500]
take_profit_percentages = [10, 15, 20, 25, 50]
max_positions = 5
profit_reinvestment_ratio = 0.9
slippage_max = 0.25
MIN_VOLUME_SOL = 1
MAX_VOLUME_SOL = 2000000
MIN_LIQUIDITY = 100
MIN_MARKET_CAP_USD = 5000
MAX_MARKET_CAP_USD = 10000
MIN_BUY_SELL_RATIO = 5.0
MAX_TOKEN_AGE_SECONDS = 900  # 15 minutes
MIN_SOL_AMOUNT = 0.1
MIN_ACCOUNTS_IN_TX = 1
DETECTION_COOLDOWN = 10
MIN_SOL_BALANCE = 0.05
HOLDER_CONCENTRATION_THRESHOLD = 0.98
DUMP_WARNING_THRESHOLD = 0.05
LIQUIDITY_DUMP_THRESHOLD = 0.20
MAX_SELL_ACTIVITY_WINDOW = 30

# Constantes Solana
PUMP_FUN_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
RAYDIUM_PROGRAM_ID = Pubkey.from_string("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8")
SOL_USDC_POOL = Pubkey.from_string("58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2")

app = Flask(__name__)
bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=False)

def send_message_worker():
    while bot_active:
        item = message_queue.get()
        chat_id, text = item[0], item[1]
        reply_markup = item[2] if len(item) > 2 else None
        message_id = item[3] if len(item) > 3 else None
        try:
            with message_lock:
                if message_id and reply_markup:
                    bot.edit_message_text(text, chat_id, message_id, reply_markup=reply_markup, parse_mode='Markdown')
                elif reply_markup:
                    sent_msg = bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode='Markdown')
                    if "Analyse de" in text:
                        message_queue.put((chat_id, text, reply_markup, sent_msg.message_id))
                else:
                    bot.send_message(chat_id, text, parse_mode='Markdown')
            time.sleep(0.05)
        except Exception as e:
            logger.error(f"Erreur envoi message: {str(e)}")
        message_queue.task_done()

threading.Thread(target=send_message_worker, daemon=True).start()

def queue_message(chat_id, text, reply_markup=None, message_id=None):
    logger.info(f"Queueing message to {chat_id}: {text}")
    message_queue.put((chat_id, text, reply_markup, message_id))

async def initialize_bot(chat_id):
    global solana_keypair
    try:
        solana_keypair = Keypair.from_base58_string(SOLANA_PRIVATE_KEY)
        queue_message(chat_id, "✅ Clé Solana initialisée")
        logger.info(f"Solana initialisé avec clé chiffrée: {encrypted_private_key[:10]}...")
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur initialisation Solana: `{str(e)}`")
        logger.error(f"Erreur initialisation: {str(e)}")
        solana_keypair = None

def set_webhook():
    try:
        bot.remove_webhook()
        time.sleep(0.5)
        success = bot.set_webhook(url=WEBHOOK_URL)
        if success:
            logger.info(f"Webhook configuré sur {WEBHOOK_URL}")
            return True
        else:
            raise Exception("Échec configuration webhook")
    except Exception as e:
        logger.error(f"Erreur configuration webhook: {str(e)}")
        queue_message(chat_id_global, f"⚠️ Erreur webhook: `{str(e)}`")
        return False

async def get_sol_price_fallback(chat_id):
    global sol_price_usd
    try:
        async with aiohttp.ClientSession() as session:
            response = await session.get(
                "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
                timeout=aiohttp.ClientTimeout(total=10)
            )
            data = await response.json()
            sol_price_usd = float(data['solana']['usd'])
            queue_message(chat_id, f"ℹ️ Prix SOL récupéré via HTTP (CoinGecko): ${sol_price_usd:.2f}")
            logger.info(f"Prix SOL récupéré: ${sol_price_usd:.2f}")
    except Exception as e:
        logger.error(f"Erreur récupération prix SOL: {str(e)}")
        sol_price_usd = 140.0
        queue_message(chat_id, f"⚠️ Échec récupération prix SOL, valeur par défaut: ${sol_price_usd:.2f}")

async def validate_with_bitquery(token_address):
    try:
        async with aiohttp.ClientSession() as session:
            url = "https://graphql.bitquery.io/"
            headers = {
                "Content-Type": "application/json",
                "X-API-KEY": BITQUERY_ACCESS_TOKEN
            }
            query = """
            query($address: String!) {
              Solana {
                Token(address: $address) {
                  MintAddress
                  Supply
                  Decimals
                  Price(currency: "USD")
                  MarketCap(currency: "USD")
                  CreatedAt
                }
              }
            }
            """
            variables = {"address": token_address}
            logger.info(f"Envoi requête Bitquery pour {token_address}")
            response = await session.post(url, json={"query": query, "variables": variables}, headers=headers)
            data = await response.json()
            logger.info(f"Réponse Bitquery pour {token_address}: {json.dumps(data, indent=2)}")
            if "data" in data and data["data"]["Solana"]["Token"]:
                token_info = data["data"]["Solana"]["Token"]
                market_cap = float(token_info["MarketCap"]) if token_info["MarketCap"] else 0
                created_at = token_info["CreatedAt"]
                age_seconds = (datetime.now() - datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ")).total_seconds()
                return {
                    "market_cap": market_cap,
                    "age_seconds": age_seconds,
                    "supply": float(token_info["Supply"]) / 10**int(token_info["Decimals"]),
                    "price": float(token_info["Price"]) if token_info["Price"] else 0
                }
            logger.warning(f"Aucune donnée Bitquery pour {token_address}")
            return None
    except Exception as e:
        logger.error(f"Erreur Bitquery pour {token_address}: {str(e)}")
        return None

@app.route("/quicknode-webhook", methods=['POST'])
async def quicknode_webhook():
    global sol_price_usd
    if request.method != "POST":
        logger.error("Méthode non autorisée pour /quicknode-webhook")
        return abort(405)

    try:
        headers = dict(request.headers)
        logger.info(f"En-têtes QuickNode: {headers}")
        raw_data = request.get_data(as_text=True)
        logger.info(f"Données brutes QuickNode (longueur: {len(raw_data)}): {raw_data[:1000] or 'VIDE'}...")

        if not raw_data:
            logger.warning("Payload vide reçu de QuickNode")
            return "OK", 200

        content_type = headers.get('Content-Type', 'non spécifié')
        try:
            data = json.loads(raw_data)
            logger.info(f"Données JSON parsées (Content-Type: {content_type}): {json.dumps(data, indent=2)}")
        except json.JSONDecodeError as e:
            logger.error(f"Échec parsing JSON: {str(e)}. Données: {raw_data[:1000]}...")
            return "OK", 200

        # Gestion de différents formats de données
        items_to_process = []
        if isinstance(data, list):
            items_to_process = data
        elif isinstance(data, dict):
            if 'result' in data:
                result = data['result']
                if isinstance(result, list):
                    items_to_process = result
                elif isinstance(result, dict):
                    items_to_process = [result]
            elif 'transactions' in data:  # Format du code test
                for tx in data.get('transactions', []):
                    token_address = tx.get('info', {}).get('tokenAddress')
                    operation = tx.get('operation')
                    if token_address and operation in ['buy', 'mint']:
                        logger.info(f"Token détecté via webhook (format alternatif): {token_address}, operation={operation}")
                        if token_address not in BLACKLISTED_TOKENS and token_address not in detected_tokens and token_address not in dynamic_blacklist:
                            token_data = await validate_with_bitquery(token_address)
                            if token_data:
                                market_cap = token_data["market_cap"]
                                age_seconds = token_data["age_seconds"]
                                if age_seconds <= MAX_TOKEN_AGE_SECONDS and MIN_MARKET_CAP_USD <= market_cap <= MAX_MARKET_CAP_USD:
                                    queue_message(chat_id_global, f"🎯 Token détecté via QuickNode (webhook alternatif) : `{token_address}` (Market Cap: ${market_cap:.2f})")
                                    detected_tokens[token_address] = True
                                    last_detection_time[token_address] = time.time()
                                    await buy_token_solana(chat_id_global, token_address, mise_depart_sol)
            else:
                items_to_process = [data]
        else:
            logger.warning(f"Données invalides (type: {type(data)}), ignorées")
            return "OK", 200

        for item in items_to_process:
            if not isinstance(item, dict):
                logger.warning(f"Élément invalide (type: {type(item)}), ignoré")
                continue

            if 'accounts' in item and 'instructions' in item:
                instructions = item.get('instructions', [])
                logger.info(f"Instructions trouvées: {len(instructions)}")
                for instruction in instructions:
                    program_id = instruction.get('programId')
                    if not program_id:
                        logger.warning("Aucune programId trouvée dans l'instruction")
                        continue

                    if program_id in [str(PUMP_FUN_PROGRAM_ID), str(RAYDIUM_PROGRAM_ID)]:
                        token_address = None
                        accounts = instruction.get('accounts', [])
                        logger.info(f"Accounts dans l'instruction: {accounts}")
                        if accounts:
                            token_address = accounts[0].get('pubkey') if isinstance(accounts[0], dict) else accounts[0]
                            logger.info(f"Token address extrait: {token_address}")

                        if not token_address or token_address in BLACKLISTED_TOKENS:
                            logger.warning("Aucune adresse de token trouvée ou token blacklisté")
                            continue

                        data_bytes = instruction.get('data')
                        if not data_bytes:
                            logger.warning("Aucune donnée dans l'instruction")
                            continue

                        try:
                            data_bytes = base58.b58decode(data_bytes)
                            logger.info(f"Données décodées: {data_bytes}")
                        except Exception as e:
                            logger.error(f"Échec décodage base58: {str(e)}")
                            continue

                        if len(data_bytes) < 9:
                            logger.warning("Données trop courtes pour analyse")
                            continue

                        amount = int.from_bytes(data_bytes[1:9], 'little') / 10**9
                        seller_address = accounts[0] if isinstance(accounts[0], str) else accounts[0].get('pubkey')
                        accounts_in_tx[token_address] = accounts_in_tx.get(token_address, set()).union(
                            {acc if isinstance(acc, str) else acc.get('pubkey') for acc in accounts}
                        )

                        if data_bytes[0] == 2:  # Buy
                            buy_volume[token_address] = buy_volume.get(token_address, 0) + amount
                            buy_count[token_address] = buy_count.get(token_address, 0) + 1
                            if token_address not in token_timestamps:
                                token_timestamps[token_address] = time.time()
                            logger.info(f"Achat détecté pour {token_address}: {amount} SOL")
                        elif data_bytes[0] == 3:  # Sell
                            sell_volume[token_address] = sell_volume.get(token_address, 0) + amount
                            sell_count[token_address] = sell_count.get(token_address, 0) + 1
                            if token_address not in token_timestamps:
                                token_timestamps[token_address] = time.time()
                            logger.info(f"Vente détectée pour {token_address}: {amount} SOL")

                        age_seconds = time.time() - token_timestamps.get(token_address, time.time())
                        if age_seconds <= MAX_TOKEN_AGE_SECONDS:
                            bv = buy_volume.get(token_address, 0)
                            sv = sell_volume.get(token_address, 0)
                            ratio = bv / sv if sv > 0 else (bv > 0 and float('inf') or 1)
                            token_metrics[token_address] = {
                                'buy_sell_ratio': ratio,
                                'buy_count': buy_count.get(token_address, 0),
                                'sell_count': sell_count.get(token_address, 0),
                                'last_update': time.time(),
                                'top_holders': token_metrics.get(token_address, {}).get('top_holders', {}),
                                'sell_activity': token_metrics.get(token_address, {}).get('sell_activity', {})
                            }

                            queue_message(chat_id_global, f"🔍 Analyse {token_address}: Ratio A/V = {ratio:.2f}, BV = {bv:.2f}, SV = {sv:.2f}")
                            logger.info(f"Analyse pour {token_address}: Ratio A/V = {ratio:.2f}, BV = {bv:.2f}, SV = {sv:.2f}")

                            if sv > bv * 0.5:
                                dynamic_blacklist.add(token_address)
                                queue_message(chat_id_global, f"⚠️ `{token_address}` rejeté : Dump artificiel détecté")
                                logger.info(f"Token {token_address} rejeté: Dump artificiel")
                            elif len(accounts_in_tx.get(token_address, set())) < MIN_ACCOUNTS_IN_TX:
                                queue_message(chat_id_global, f"⚠️ `{token_address}` rejeté : Pas assez de comptes dans TX")
                                logger.info(f"Token {token_address} rejeté: Pas assez de comptes")
                            elif ratio >= MIN_BUY_SELL_RATIO:
                                token_data = await validate_with_bitquery(token_address)
                                if token_data:
                                    market_cap = token_data["market_cap"]
                                    age_seconds = token_data["age_seconds"]
                                    logger.info(f"Token {token_address}: Market Cap = ${market_cap:.2f}, Âge = {age_seconds/60:.2f} min")
                                    if age_seconds > MAX_TOKEN_AGE_SECONDS:
                                        queue_message(chat_id_global, f"⚠️ `{token_address}` rejeté : Trop vieux ({age_seconds/60:.2f} min)")
                                        logger.info(f"Token {token_address} rejeté: Trop vieux")
                                        continue
                                    if market_cap < MIN_MARKET_CAP_USD or market_cap > MAX_MARKET_CAP_USD:
                                        queue_message(chat_id_global, f"⚠️ `{token_address}` rejeté : Market Cap ${market_cap:.2f} hors plage [{MIN_MARKET_CAP_USD}, {MAX_MARKET_CAP_USD}]")
                                        logger.info(f"Token {token_address} rejeté: Market Cap hors plage")
                                        continue
                                    queue_message(chat_id_global, f"🎯 Pump détecté : `{token_address}` (Ratio A/V: {ratio:.2f}, Market Cap: ${market_cap:.2f})")
                                    logger.info(f"Pump détecté pour {token_address}: Ratio A/V = {ratio:.2f}, Market Cap = ${market_cap:.2f}")
                                    detected_tokens[token_address] = True
                                    last_detection_time[token_address] = time.time()
                                    await buy_token_solana(chat_id_global, token_address, mise_depart_sol)

                        if time.time() - token_timestamps.get(token_address, 0) > MAX_TOKEN_AGE_SECONDS:
                            buy_volume.pop(token_address, None)
                            sell_volume.pop(token_address, None)
                            buy_count.pop(token_address, None)
                            sell_count.pop(token_address, None)
                            accounts_in_tx.pop(token_address, None)
                            token_timestamps.pop(token_address, None)
                            token_metrics.pop(token_address, None)

            elif 'pubkey' in item and item['pubkey'] == str(SOL_USDC_POOL):
                account_data = item.get('data', [None])[0]
                if account_data:
                    decoded_data = base58.b58decode(account_data)
                    sol_amount = int.from_bytes(decoded_data[64:80], 'little') / 10**9
                    usdc_amount = int.from_bytes(decoded_data[80:96], 'little') / 10**6
                    if sol_amount > 0:
                        sol_price_usd = usdc_amount / sol_amount
                        queue_message(chat_id_global, f"💰 Prix SOL mis à jour: ${sol_price_usd:.2f}")
                        logger.info(f"Prix SOL mis à jour via SOL_USDC_POOL: ${sol_price_usd:.2f}")

        return "OK", 200
    except Exception as e:
        logger.error(f"Erreur critique webhook QuickNode: {str(e)}")
        queue_message(chat_id_global, f"⚠️ Erreur webhook QuickNode: `{str(e)}`")
        return "OK", 200

async def websocket_monitor(chat_id):
    if not QUICKNODE_WS_URL:
        logger.error("QUICKNODE_WS_URL non défini, WebSocket désactivé")
        return

    try:
        async with websockets.connect(QUICKNODE_WS_URL) as ws:
            await ws.send(json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "programSubscribe",
                "params": [str(PUMP_FUN_PROGRAM_ID), {"encoding": "base64", "commitment": "confirmed"}]
            }))
            logger.info("WebSocket connecté à QuickNode pour Pump.fun")

            while not stop_event.is_set() and bot_active:
                try:
                    message = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    data = json.loads(message)
                    logger.info(f"WebSocket message reçu: {json.dumps(data, indent=2)}")
                    if 'result' not in data:
                        tx_data = data.get('params', {}).get('result', {}).get('value', {})
                        if not tx_data or 'account' not in tx_data:
                            logger.info("Aucune donnée de compte dans la transaction")
                            continue

                        token_address = tx_data['account']['pubkey']
                        if token_address in detected_tokens or token_address in dynamic_blacklist:
                            continue

                        instruction = tx_data.get('instruction', {})
                        if instruction and instruction.get('programId') == str(PUMP_FUN_PROGRAM_ID):
                            data_bytes = base58.b58decode(instruction.get('data', ''))
                            if len(data_bytes) < 9:
                                logger.info(f"Données invalides pour {token_address}")
                                continue
                            amount = int.from_bytes(data_bytes[1:9], 'little') / 10**9
                            accounts = {acc['pubkey'] for acc in instruction.get('accounts', [])}
                            accounts_in_tx[token_address] = accounts_in_tx.get(token_address, set()).union(accounts)

                            if data_bytes[0] == 2:  # Buy
                                buy_volume[token_address] = buy_volume.get(token_address, 0) + amount
                                buy_count[token_address] = buy_count.get(token_address, 0) + 1
                                if token_address not in token_timestamps:
                                    token_timestamps[token_address] = time.time()
                                logger.info(f"Achat détecté pour {token_address}: {amount} SOL")
                            elif data_bytes[0] == 3:  # Sell
                                sell_volume[token_address] = sell_volume.get(token_address, 0) + amount
                                sell_count[token_address] = sell_count.get(token_address, 0) + 1
                                if token_address not in token_timestamps:
                                    token_timestamps[token_address] = time.time()
                                logger.info(f"Vente détectée pour {token_address}: {amount} SOL")

                            age_seconds = time.time() - token_timestamps.get(token_address, time.time())
                            if age_seconds <= MAX_TOKEN_AGE_SECONDS:
                                bv = buy_volume.get(token_address, 0)
                                sv = sell_volume.get(token_address, 0)
                                ratio = bv / sv if sv > 0 else (bv > 0 and float('inf') or 1)
                                token_metrics[token_address] = {
                                    'buy_sell_ratio': ratio,
                                    'buy_count': buy_count.get(token_address, 0),
                                    'sell_count': sell_count.get(token_address, 0),
                                    'last_update': time.time(),
                                    'top_holders': token_metrics.get(token_address, {}).get('top_holders', {}),
                                    'sell_activity': token_metrics.get(token_address, {}).get('sell_activity', {})
                                }

                                queue_message(chat_id, f"🔍 Analyse {token_address}: Ratio A/V = {ratio:.2f}, BV = {bv:.2f}, SV = {sv:.2f}")
                                logger.info(f"Analyse pour {token_address}: Ratio A/V = {ratio:.2f}, BV = {bv:.2f}, SV = {sv:.2f}")

                                if sv > bv * 0.5:
                                    dynamic_blacklist.add(token_address)
                                    queue_message(chat_id, f"⚠️ `{token_address}` rejeté : Dump artificiel détecté")
                                    continue

                                if len(accounts_in_tx.get(token_address, set())) < MIN_ACCOUNTS_IN_TX:
                                    logger.info(f"Token {token_address} rejeté : Pas assez de comptes actifs")
                                    continue

                                if ratio >= MIN_BUY_SELL_RATIO:
                                    token_data = await validate_with_bitquery(token_address)
                                    if token_data:
                                        market_cap = token_data["market_cap"]
                                        age_seconds = token_data["age_seconds"]
                                        logger.info(f"Token {token_address}: Market Cap = ${market_cap:.2f}, Âge = {age_seconds/60:.2f} min")
                                        if age_seconds > MAX_TOKEN_AGE_SECONDS:
                                            queue_message(chat_id, f"⚠️ `{token_address}` rejeté : Trop vieux ({age_seconds/60:.2f} min)")
                                            continue
                                        if market_cap < MIN_MARKET_CAP_USD or market_cap > MAX_MARKET_CAP_USD:
                                            queue_message(chat_id, f"⚠️ `{token_address}` rejeté : Market Cap ${market_cap:.2f} hors plage [{MIN_MARKET_CAP_USD}, {MAX_MARKET_CAP_USD}]")
                                            continue
                                        queue_message(chat_id, f"🎯 Pump détecté via WebSocket : `{token_address}` (Ratio A/V: {ratio:.2f}, Market Cap: ${market_cap:.2f})")
                                        detected_tokens[token_address] = True
                                        last_detection_time[token_address] = time.time()
                                        await buy_token_solana(chat_id, token_address, mise_depart_sol)

                            if time.time() - token_timestamps.get(token_address, 0) > MAX_TOKEN_AGE_SECONDS:
                                buy_volume.pop(token_address, None)
                                sell_volume.pop(token_address, None)
                                buy_count.pop(token_address, None)
                                sell_count.pop(token_address, None)
                                accounts_in_tx.pop(token_address, None)
                                token_timestamps.pop(token_address, None)
                                token_metrics.pop(token_address, None)

                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    logger.error(f"Erreur WebSocket: {str(e)}")
                    await asyncio.sleep(1)
    except Exception as e:
        logger.error(f"Erreur connexion WebSocket: {str(e)}")
        queue_message(chat_id, f"⚠️ Erreur WebSocket: `{str(e)}`")

async def validate_address(token_address):
    try:
        Pubkey.from_string(token_address)
        return len(token_address) >= 32 and len(token_address) <= 44
    except ValueError:
        return False

async def custom_rug_detector(chat_id, token_address):
    try:
        async with aiohttp.ClientSession() as session:
            holders_response = await session.post(
                QUICKNODE_SOL_URL,
                json={"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts", "params": [token_address]},
                timeout=0.5
            )
            holders_data = await holders_response.json()
            top_holders = holders_data.get('result', {}).get('value', [])
            token_data = await validate_with_bitquery(token_address)
            if not token_data:
                queue_message(chat_id, f"⚠️ `{token_address}`: Données insuffisantes pour analyse rug")
                return False

            supply = token_data['supply']
            liquidity = token_data['market_cap'] * 0.1  # Approximation
            age_seconds = token_data['age_seconds']
            buy_count = token_metrics.get(token_address, {}).get('buy_count', 0)
            sell_count = token_metrics.get(token_address, {}).get('sell_count', 0)
            sell_buy_ratio = sell_count / buy_count if buy_count > 0 else 0

            top_5_holdings = sum(float(h.get('amount', 0)) / 10**6 for h in top_holders[:5])
            holder_concentration = top_5_holdings / supply if supply > 0 else 0

            risks = []
            if holder_concentration > HOLDER_CONCENTRATION_THRESHOLD:
                risks.append(f"Concentration holders élevée: {holder_concentration:.2%}")
            if liquidity < MIN_LIQUIDITY:
                risks.append(f"Liquidité faible: ${liquidity:.2f}")
            if sell_buy_ratio > 2 and age_seconds < 300:
                risks.append(f"Activité suspecte: Ratio V/A = {sell_buy_ratio:.2f}")
            if age_seconds < 300 and (buy_count + sell_count) < 5:
                risks.append(f"Token trop jeune: {age_seconds/60:.2f} min, {buy_count + sell_count} TX")
            if supply > 10**12:
                risks.append(f"Supply excessif: {supply:.2e} tokens")

            if risks:
                queue_message(chat_id, f"🚨 `{token_address}` détecté comme risqué: {', '.join(risks)}")
                return False
            queue_message(chat_id, f"✅ `{token_address}` semble sûr - Liquidité: ${liquidity:.2f}")
            return True
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur détection rug `{token_address}`: `{str(e)}`")
        return False

async def monitor_top_holders(chat_id, token_address):
    try:
        async with aiohttp.ClientSession() as session:
            holders_response = await session.post(
                QUICKNODE_SOL_URL,
                json={"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts", "params": [token_address]},
                timeout=0.5
            )
            holders_data = await holders_response.json()
            top_holders = holders_data.get('result', {}).get('value', [])
            if not top_holders:
                return None

            token_data = await validate_with_bitquery(token_address)
            if not token_data:
                return None

            supply = token_data['supply']
            top_5_holdings = sum(float(h.get('amount', 0)) / 10**6 for h in top_holders[:5])
            top_5_concentration = top_5_holdings / supply if supply > 0 else 0

            if token_address not in token_metrics:
                token_metrics[token_address] = {}
            token_metrics[token_address]['top_holders'] = {
                h['address']: float(h['amount']) / 10**6 for h in top_holders[:5]
            }
            token_metrics[token_address]['top_5_concentration'] = top_5_concentration
            token_metrics[token_address]['sell_activity'] = token_metrics.get(token_address, {}).get('sell_activity', {})

            if top_5_concentration > HOLDER_CONCENTRATION_THRESHOLD:
                queue_message(chat_id, f"⚠️ `{token_address}`: Concentration top 5 = {top_5_concentration:.2%}, risque de dump !")

            return token_metrics[token_address]['top_holders']
    except Exception as e:
        logger.error(f"Erreur surveillance holders {token_address}: {str(e)}")
        return None

async def analyze_token(chat_id, token_address, message_id=None):
    try:
        if not await validate_address(token_address):
            queue_message(chat_id, f"⚠️ `{token_address}` n’est pas une adresse Solana valide.", message_id=message_id)
            return

        if not solana_keypair:
            await initialize_bot(chat_id)
            if not solana_keypair:
                queue_message(chat_id, "⚠️ Wallet Solana non initialisé.", message_id=message_id)
                return

        if not await custom_rug_detector(chat_id, token_address):
            return

        token_data = await validate_with_bitquery(token_address)
        if not token_data:
            queue_message(chat_id, f"⚠️ `{token_address}`: Données indisponibles.", message_id=message_id)
            return

        market_cap = token_data['market_cap']
        liquidity = market_cap * 0.1  # Approximation
        async with aiohttp.ClientSession() as session:
            holders_response = await session.post(
                QUICKNODE_SOL_URL,
                json={"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts", "params": [token_address]},
                timeout=0.5
            )
            holders_data = await holders_response.json()
            num_holders = len(holders_data.get('result', {}).get('value', []))
        buy_sell_ratio = token_metrics.get(token_address, {}).get('buy_sell_ratio', 1.0)
        buy_count = token_metrics.get(token_address, {}).get('buy_count', 0)
        sell_count = token_metrics.get(token_address, {}).get('sell_count', 0)

        msg = (
            f"📊 *Analyse de `{token_address}`*\n\n"
            f"**Market Cap** : ${market_cap:.2f}\n"
            f"**Liquidité** : ${liquidity:.2f}\n"
            f"**Holders** : {num_holders}\n"
            f"**Ratio Achat/Vente** : {buy_sell_ratio:.2f}\n"
            f"**Achats (5min)** : {buy_count}\n"
            f"**Ventes (5min)** : {sell_count}\n"
            f"**Prix SOL** : ${sol_price_usd:.2f}"
        )

        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton(f"💰 Acheter {mise_depart_sol} SOL", callback_data=f"buy_manual_{token_address}"),
            InlineKeyboardButton("💸 Vendre 100%", callback_data=f"sell_manual_{token_address}")
        )
        markup.add(
            InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh_{token_address}")
        )

        queue_message(chat_id, msg, reply_markup=markup, message_id=message_id)
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur analyse `{token_address}` : `{str(e)}`", message_id=message_id)

async def buy_token_solana(chat_id, contract_address, amount):
    try:
        if not solana_keypair:
            await initialize_bot(chat_id)
            if not solana_keypair:
                raise Exception("Solana non initialisé")
        sol_balance = await get_solana_balance(chat_id)
        if sol_balance < MIN_SOL_AMOUNT + MIN_SOL_BALANCE:
            raise Exception(f"Solde SOL insuffisant: {sol_balance:.4f} < {MIN_SOL_AMOUNT + MIN_SOL_BALANCE:.4f}")

        amount = amount * random.uniform(0.95, 1.05)

        async with aiohttp.ClientSession() as session:
            jupiter_url = f"https://quote-api.jup.ag/v6/quote?inputMint=So11111111111111111111111111111111111111112&outputMint={contract_address}&amount={int(amount * 10**9)}&slippageBps={int(slippage_max * 10000)}"
            jupiter_response = await session.get(jupiter_url, timeout=aiohttp.ClientTimeout(total=1))
            quote = await jupiter_response.json()
            swap_tx = await session.post("https://quote-api.jup.ag/v6/swap", json={
                "quoteResponse": quote,
                "userPublicKey": str(solana_keypair.pubkey())
            }, timeout=aiohttp.ClientTimeout(total=1))
            tx_data = await swap_tx.json()
            tx_hash = tx_data.get("swapTransaction")

        token_data = await validate_with_bitquery(contract_address)
        current_price = token_data['price']
        queue_message(chat_id, f"✅ Achat réussi : {amount:.4f} SOL de `{contract_address}` (TX: `{tx_hash}`)")
        entry_price = current_price
        portfolio[contract_address] = {
            'amount': amount, 'chain': 'solana', 'entry_price': entry_price,
            'price_history': [entry_price], 'highest_price': entry_price, 'profit': 0.0,
            'buy_time': time.time()
        }
        daily_trades['buys'].append({'token': contract_address, 'amount': amount, 'timestamp': datetime.now().strftime('%H:%M:%S')})
    except Exception as e:
        queue_message(chat_id, f"⚠️ Échec achat Solana `{contract_address}`: `{str(e)}`")
        logger.error(f"Échec achat Solana: {str(e)}")

async def sell_token(chat_id, contract_address, amount, current_price):
    global mise_depart_sol
    try:
        if not solana_keypair:
            await initialize_bot(chat_id)
            if not solana_keypair:
                raise Exception("Solana non initialisé")

        amount_out = int(amount * 10**9)
        async with aiohttp.ClientSession() as session:
            response = await session.post(QUICKNODE_SOL_URL, json={
                "jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash",
                "params": [{"commitment": "finalized"}]
            }, timeout=1)
            blockhash = (await response.json())['result']['value']['blockhash']
        tx = Transaction()
        tx.recent_blockhash = Pubkey.from_string(blockhash)
        instruction = Instruction(
            program_id=PUMP_FUN_PROGRAM_ID,
            accounts=[
                {"pubkey": solana_keypair.pubkey(), "is_signer": True, "is_writable": True},
                {"pubkey": Pubkey.from_string(contract_address), "is_signer": False, "is_writable": True},
                {"pubkey": TOKEN_PROGRAM_ID, "is_signer": False, "is_writable": False}
            ],
            data=bytes([3]) + amount_out.to_bytes(8, 'little')
        )
        tx.add(instruction)
        tx.sign([solana_keypair])
        async with aiohttp.ClientSession() as session:
            tx_hash = (await session.post(QUICKNODE_SOL_URL, json={
                "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                "params": [base58.b58encode(tx.serialize()).decode('utf-8')]
            }, timeout=1)).json()['result']

        profit = (current_price - portfolio[contract_address]['entry_price']) * amount
        portfolio[contract_address]['profit'] += profit
        portfolio[contract_address]['amount'] -= amount
        reinvest_amount = profit * profit_reinvestment_ratio if profit > 0 else 0
        mise_depart_sol += reinvest_amount
        queue_message(chat_id, f"✅ Vente réussie : {amount:.4f} SOL, Profit: {profit:.4f} SOL (TX: `{tx_hash}`)")
        daily_trades['sells'].append({'token': contract_address, 'amount': amount, 'pnl': profit, 'timestamp': datetime.now().strftime('%H:%M:%S')})
        if portfolio[contract_address]['amount'] <= 0:
            del portfolio[contract_address]
            price_history.pop(contract_address, None)
    except Exception as e:
        queue_message(chat_id, f"⚠️ Échec vente Solana `{contract_address}`: `{str(e)}`")
        logger.error(f"Échec vente Solana: {str(e)}")

async def monitor_and_sell(chat_id):
    while not stop_event.is_set() and bot_active:
        try:
            if not portfolio:
                await asyncio.sleep(0.1)
                continue
            for contract_address, data in list(portfolio.items()):
                amount = data['amount']
                token_data = await validate_with_bitquery(contract_address)
                if not token_data:
                    continue
                current_price = token_data['price']
                data['price_history'].append(current_price)
                if len(data['price_history']) > 10:
                    data['price_history'].pop(0)
                profit_pct = (current_price - data['entry_price']) / data['entry_price'] * 100 if data['entry_price'] > 0 else 0
                loss_pct = -profit_pct if profit_pct < 0 else 0
                data['highest_price'] = max(data['highest_price'], current_price)
                trailing_stop_price = data['highest_price'] * (1 - trailing_stop_percentage / 100)

                await monitor_top_holders(chat_id, contract_address)

                if contract_address in price_history and len(price_history[contract_address]) >= 5:
                    recent_prices = [p[1] for p in price_history[contract_address]]
                    max_recent_price = max(recent_prices)
                    if current_price < max_recent_price * 0.9:
                        queue_message(chat_id, f"⚠️ `{contract_address}`: Chute rapide (-{100 - (current_price / max_recent_price * 100):.2f}%), dump imminent !")
                        if contract_address in portfolio:
                            await sell_token(chat_id, contract_address, amount, current_price)

                if not pause_auto_sell:
                    if profit_pct >= 200:
                        await sell_token(chat_id, contract_address, amount, current_price)
                        queue_message(chat_id, f"💰 Vente à ×2 sur `{contract_address}` !")
                    elif profit_pct >= take_profit_steps[4] * 100:
                        await sell_token(chat_id, contract_address, amount * (take_profit_percentages[4] / 100), current_price)
                    elif profit_pct >= take_profit_steps[3] * 100:
                        await sell_token(chat_id, contract_address, amount * (take_profit_percentages[3] / 100), current_price)
                    elif profit_pct >= take_profit_steps[2] * 100:
                        await sell_token(chat_id, contract_address, amount * (take_profit_percentages[2] / 100), current_price)
                    elif profit_pct >= take_profit_steps[1] * 100:
                        await sell_token(chat_id, contract_address, amount * (take_profit_percentages[1] / 100), current_price)
                    elif profit_pct >= take_profit_steps[0] * 100:
                        await sell_token(chat_id, contract_address, amount * (take_profit_percentages[0] / 100), current_price)
                    elif current_price <= trailing_stop_price or loss_pct >= stop_loss_threshold:
                        await sell_token(chat_id, contract_address, amount, current_price)
            await asyncio.sleep(0.1)
        except Exception as e:
            queue_message(chat_id, f"⚠️ Erreur surveillance: `{str(e)}`")
            logger.error(f"Erreur surveillance: {str(e)}")
            await asyncio.sleep(1)

async def show_portfolio(chat_id):
    try:
        sol_balance = await get_solana_balance(chat_id)
        msg = f"💰 *Portefeuille:*\nSOL : {sol_balance:.4f} (${sol_balance * sol_price_usd:.2f})\nPrix SOL: ${sol_price_usd:.2f}\n\n"
        markup = InlineKeyboardMarkup()
        for ca, data in portfolio.items():
            token_data = await validate_with_bitquery(ca)
            current_price = token_data['price'] if token_data else 0
            profit = (current_price - data['entry_price']) * data['amount']
            markup.add(
                InlineKeyboardButton(f"💸 Sell 25% {ca[:6]}", callback_data=f"sell_pct_{ca}_25"),
                InlineKeyboardButton(f"💸 Sell 50% {ca[:6]}", callback_data=f"sell_pct_{ca}_50"),
                InlineKeyboardButton(f"💸 Sell 100% {ca[:6]}", callback_data=f"sell_{ca}")
            )
            msg += (
                f"*Token:* `{ca}` (Solana)\n"
                f"Montant: {data['amount']:.4f} SOL\n"
                f"Prix d’entrée: ${data['entry_price']:.6f}\n"
                f"Prix actuel: ${current_price:.6f}\n"
                f"Profit: {profit:.4f} SOL\n\n"
            )
        queue_message(chat_id, msg, reply_markup=markup if portfolio else None)
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur portefeuille: `{str(e)}`")
        logger.error(f"Erreur portefeuille: {str(e)}")

async def get_solana_balance(chat_id):
    try:
        if not solana_keypair:
            await initialize_bot(chat_id)
        if not solana_keypair:
            return 0
        async with aiohttp.ClientSession() as session:
            response = await session.post(QUICKNODE_SOL_URL, json={
                "jsonrpc": "2.0", "id": 1, "method": "getBalance",
                "params": [str(solana_keypair.pubkey())]
            }, timeout=1)
            return (await response.json()).get('result', {}).get('value', 0) / 10**9
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur solde Solana: `{str(e)}`")
        return 0

async def show_daily_summary(chat_id):
    try:
        msg = f"📅 *Récapitulatif du jour ({datetime.now().strftime('%Y-%m-%d')})*:\nPrix SOL: ${sol_price_usd:.2f}\n\n"
        msg += "📈 *Achats* :\n"
        total_buys = 0
        for trade in daily_trades['buys']:
            total_buys += trade['amount']
            msg += f"- `{trade['token']}` : {trade['amount']:.4f} SOL à {trade['timestamp']}\n"
        msg += f"Total investi : {total_buys:.4f} SOL\n\n"
        msg += "📉 *Ventes* :\n"
        total_profit = 0
        for trade in daily_trades['sells']:
            total_profit += trade['pnl']
            msg += f"- `{trade['token']}` : {trade['amount']:.4f} SOL à {trade['timestamp']}, PNL: {trade['pnl']:.4f} SOL\n"
        msg += f"Profit net : {total_profit:.4f} SOL\n"
        queue_message(chat_id, msg)
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur récapitulatif: `{str(e)}`")
        logger.error(f"Erreur récapitulatif: {str(e)}")

def adjust_mise_sol(message):
    global mise_depart_sol
    chat_id = message.chat.id
    try:
        new_mise = float(message.text.strip())
        if new_mise > 0:
            mise_depart_sol = new_mise
            queue_message(chat_id, f"✅ Mise Solana mise à jour à {mise_depart_sol} SOL")
        else:
            queue_message(chat_id, "⚠️ La mise doit être positive!")
    except ValueError:
        queue_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 0.5)")

def adjust_stop_loss(message):
    global stop_loss_threshold
    chat_id = message.chat.id
    try:
        new_sl = float(message.text.strip())
        if new_sl > 0:
            stop_loss_threshold = new_sl
            queue_message(chat_id, f"✅ Stop-Loss mis à jour à {stop_loss_threshold} %")
        else:
            queue_message(chat_id, "⚠️ Le Stop-Loss doit être positif!")
    except ValueError:
        queue_message(chat_id, "⚠️ Erreur : Entrez un pourcentage valide (ex. : 10)")

def adjust_take_profit(message):
    global take_profit_steps, take_profit_percentages
    chat_id = message.chat.id
    try:
        values = [float(x.strip()) for x in message.text.split(",")]
        if len(values) != 10:
            queue_message(chat_id, "⚠️ Entrez 10 valeurs (seuil1,pct1,seuil2,pct2,...,seuil5,pct5) ex. : 1.2,10,2,15,10,20,100,25,500,50")
            return
        new_steps = values[0::2]
        new_percentages = values[1::2]
        if all(x > 0 for x in new_steps) and all(0 < x <= 100 for x in new_percentages):
            take_profit_steps = new_steps
            take_profit_percentages = new_percentages
            queue_message(chat_id, f"✅ Take-Profit mis à jour à x{take_profit_steps[0]} ({take_profit_percentages[0]}%), x{take_profit_steps[1]} ({take_profit_percentages[1]}%), x{take_profit_steps[2]} ({take_profit_percentages[2]}%), x{take_profit_steps[3]} ({take_profit_percentages[3]}%), x{take_profit_steps[4]} ({take_profit_percentages[4]}%)")
        else:
            queue_message(chat_id, "⚠️ Les seuils doivent être positifs et les pourcentages entre 0 et 100 !")
    except ValueError:
        queue_message(chat_id, "⚠️ Erreur : Entrez des nombres valides (ex. : 1.2,10,2,15,10,20,100,25,500,50)")

def adjust_reinvestment_ratio(message):
    global profit_reinvestment_ratio
    chat_id = message.chat.id
    try:
        new_ratio = float(message.text.strip())
        if 0 <= new_ratio <= 1:
            profit_reinvestment_ratio = new_ratio
            queue_message(chat_id, f"✅ Ratio de réinvestissement mis à jour à {profit_reinvestment_ratio * 100}%")
        else:
            queue_message(chat_id, "⚠️ Le ratio doit être entre 0 et 1 (ex. : 0.9)")
    except ValueError:
        queue_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 0.9)")

def adjust_detection_criteria(message):
    global MIN_VOLUME_SOL, MAX_VOLUME_SOL, MIN_LIQUIDITY, MIN_MARKET_CAP_USD, MAX_MARKET_CAP_USD, MIN_BUY_SELL_RATIO, MAX_TOKEN_AGE_SECONDS
    chat_id = message.chat.id
    try:
        criteria = [float(x.strip()) for x in message.text.split(",")]
        if len(criteria) != 7:
            queue_message(chat_id, "⚠️ Entrez 7 valeurs séparées par des virgules (ex. : 1,2000000,100,5000,10000,5.0,900)")
            return
        min_vol, max_vol, min_liq, min_mc, max_mc, min_ratio, max_age = criteria
        if min_vol < 0 or max_vol < min_vol or min_liq < 0 or min_mc < 0 or max_mc < min_mc or min_ratio < 0 or max_age < 0:
            queue_message(chat_id, "⚠️ Valeurs invalides ! Assurez-vous que toutes sont positives et cohérentes.")
            return
        MIN_VOLUME_SOL = min_vol
        MAX_VOLUME_SOL = max_vol
        MIN_LIQUIDITY = min_liq
        MIN_MARKET_CAP_USD = min_mc
        MAX_MARKET_CAP_USD = max_mc
        MIN_BUY_SELL_RATIO = min_ratio
        MAX_TOKEN_AGE_SECONDS = max_age
        queue_message(chat_id, (
            f"✅ Critères de détection mis à jour :\n"
            f"Volume : [{MIN_VOLUME_SOL}, {MAX_VOLUME_SOL}] SOL\n"
            f"Liquidité min : {MIN_LIQUIDITY} $\n"
            f"Market Cap : [{MIN_MARKET_CAP_USD}, {MAX_MARKET_CAP_USD}] $\n"
            f"Ratio A/V min : {MIN_BUY_SELL_RATIO}\n"
            f"Âge max : {MAX_TOKEN_AGE_SECONDS/60:.2f} min"
        ))
    except ValueError:
        queue_message(chat_id, "⚠️ Erreur : Entrez des nombres valides (ex. : 1,2000000,100,5000,10000,5.0,900)")

async def run_tasks(chat_id):
    await asyncio.gather(
        monitor_and_sell(chat_id),
        websocket_monitor(chat_id)
    )

def initialize_and_run_tasks(chat_id):
    global trade_active, chat_id_global, bot_active
    chat_id_global = chat_id
    try:
        asyncio.run(initialize_bot(chat_id))
        if solana_keypair:
            trade_active = True
            bot_active = True
            stop_event.clear()
            queue_message(chat_id, "▶️ Trading Solana lancé avec succès! En attente des données QuickNode...")
            threading.Thread(target=lambda: asyncio.run(run_tasks(chat_id)), daemon=True).start()
            threading.Thread(target=heartbeat, args=(chat_id,), daemon=True).start()
            asyncio.run(get_sol_price_fallback(chat_id))
        else:
            queue_message(chat_id, "⚠️ Échec initialisation : Solana non connecté")
            trade_active = False
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur initialisation: `{str(e)}`")
        logger.error(f"Erreur initialisation: {str(e)}")
        trade_active = False

def heartbeat(chat_id):
    while trade_active and bot_active:
        queue_message(chat_id, f"💓 Bot actif - Prix SOL: ${sol_price_usd:.2f}")
        time.sleep(300)

@app.route("/webhook", methods=['POST'])
def telegram_webhook():
    if request.method == "POST" and request.headers.get("content-type") == "application/json":
        update_json = request.get_json()
        logger.info(f"Webhook Telegram reçu: {json.dumps(update_json)}")
        try:
            update = telebot.types.Update.de_json(update_json)
            bot.process_new_updates([update])
            return 'OK', 200
        except Exception as e:
            logger.error(f"Erreur traitement webhook Telegram: {str(e)}")
            if chat_id_global:
                queue_message(chat_id_global, f"⚠️ Erreur webhook Telegram: `{str(e)}`")
            return f"Erreur: {str(e)}", 500
    logger.error("Requête webhook Telegram invalide")
    return abort(403)

@bot.message_handler(commands=['start', 'Start', 'START'])
def start_message(message):
    global trade_active, bot_active, chat_id_global
    chat_id = message.chat.id
    chat_id_global = chat_id
    bot_active = True
    if not trade_active:
        queue_message(chat_id, "✅ Bot démarré!")
        initialize_and_run_tasks(chat_id)
    else:
        queue_message(chat_id, "ℹ️ Trading déjà actif!")

@bot.message_handler(commands=['menu', 'Menu', 'MENU'])
def menu_message(message):
    chat_id = message.chat.id
    queue_message(chat_id, "ℹ️ Affichage du menu...")
    show_main_menu(chat_id)

@bot.message_handler(commands=['stop', 'Stop', 'STOP'])
def stop_message(message):
    global trade_active, bot_active
    chat_id = message.chat.id
    trade_active = False
    bot_active = False
    stop_event.set()
    while not message_queue.empty():
        message_queue.get()
    queue_message(chat_id, "⏹️ Trading et bot arrêtés.")
    logger.info("Trading et bot arrêtés")

@bot.message_handler(commands=['pause', 'Pause', 'PAUSE'])
def pause_auto_sell_handler(message):
    global pause_auto_sell
    chat_id = message.chat.id
    pause_auto_sell = True
    queue_message(chat_id, "⏸️ Ventes automatiques désactivées.")

@bot.message_handler(commands=['resume', 'Resume', 'RESUME'])
def resume_auto_sell_handler(message):
    global pause_auto_sell
    chat_id = message.chat.id
    pause_auto_sell = False
    queue_message(chat_id, "▶️ Ventes automatiques réactivées.")

@bot.message_handler(func=lambda message: validate_address(message.text.strip()))
def handle_token_address(message):
    chat_id = message.chat.id
    token_address = message.text.strip()
    asyncio.run(analyze_token(chat_id, token_address))

@bot.message_handler(commands=['check'])
def check_token_command(message):
    chat_id = message.chat.id
    try:
        token_address = message.text.split()[1]
        asyncio.run(analyze_token(chat_id, token_address))
    except IndexError:
        queue_message(chat_id, "⚠️ Veuillez fournir une adresse de token (ex. `/check H5jmVE...pump`).")

def show_main_menu(chat_id):
    try:
        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton("ℹ️ Statut", callback_data="status"),
            InlineKeyboardButton("▶️ Lancer", callback_data="launch"),
            InlineKeyboardButton("⏹️ Arrêter", callback_data="stop")
        )
        markup.add(
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
        markup.add(
            InlineKeyboardButton("🔍 Ajuster Critères Détection", callback_data="adjust_detection_criteria")
        )
        queue_message(chat_id, "*Menu principal:*", reply_markup=markup)
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur affichage menu: `{str(e)}`")
        logger.error(f"Erreur affichage menu: {str(e)}")

@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    global trade_active, bot_active
    chat_id = call.message.chat.id
    message_id = call.message.message_id
    try:
        if call.data == "status":
            sol_balance = asyncio.run(get_solana_balance(chat_id))
            take_profit_display = ", ".join(
                f"x{take_profit_steps[i]} ({take_profit_percentages[i]}%)" for i in range(len(take_profit_steps))
            )
            queue_message(chat_id, (
                f"ℹ️ *Statut actuel* :\n"
                f"Trading actif: {'Oui' if trade_active else 'Non'}\n"
                f"SOL disponible: {sol_balance:.4f} (${sol_balance * sol_price_usd:.2f})\n"
                f"Prix SOL: ${sol_price_usd:.2f}\n"
                f"Positions: {len(portfolio)}/{max_positions}\n"
                f"Mise Solana: {mise_depart_sol} SOL\n"
                f"Stop-Loss: {stop_loss_threshold}%\n"
                f"Take-Profit: {take_profit_display}\n"
                f"Critères détection: Volume [{MIN_VOLUME_SOL}, {MAX_VOLUME_SOL}] SOL, Liquidité min {MIN_LIQUIDITY} $, Market Cap [{MIN_MARKET_CAP_USD}, {MAX_MARKET_CAP_USD}] $, Ratio A/V min {MIN_BUY_SELL_RATIO}, Âge max {MAX_TOKEN_AGE_SECONDS/60:.2f} min"
            ))
        elif call.data == "launch":
            if not trade_active:
                initialize_and_run_tasks(chat_id)
            else:
                queue_message(chat_id, "ℹ️ Trading déjà actif!")
        elif call.data == "stop":
            trade_active = False
            bot_active = False
            stop_event.set()
            while not message_queue.empty():
                message_queue.get()
            queue_message(chat_id, "⏹️ Trading et bot arrêtés.")
        elif call.data == "portfolio":
            asyncio.run(show_portfolio(chat_id))
        elif call.data == "daily_summary":
            asyncio.run(show_daily_summary(chat_id))
        elif call.data == "adjust_mise_sol":
            queue_message(chat_id, "Entrez la nouvelle mise Solana (ex. : 0.5) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_mise_sol)
        elif call.data == "adjust_stop_loss":
            queue_message(chat_id, "Entrez le nouveau Stop-Loss (ex. : 10) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_stop_loss)
        elif call.data == "adjust_take_profit":
            queue_message(chat_id, "Entrez les nouveaux Take-Profit (seuil1,pct1,seuil2,pct2,...,seuil5,pct5) ex. : 1.2,10,2,15,10,20,100,25,500,50 :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_take_profit)
        elif call.data == "adjust_reinvestment":
            queue_message(chat_id, "Entrez le nouveau ratio de réinvestissement (ex. : 0.9) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_reinvestment_ratio)
        elif call.data == "adjust_detection_criteria":
            queue_message(chat_id, "Entrez les nouveaux critères (min_vol, max_vol, min_liq, min_mc, max_mc, min_ratio, max_age_seconds) ex. : 1,2000000,100,5000,10000,5.0,900 :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_detection_criteria)
        elif call.data.startswith("sell_pct_"):
            parts = call.data.split("_")
            contract_address, pct = parts[2], int(parts[3]) / 100
            if contract_address in portfolio:
                token_data = asyncio.run(validate_with_bitquery(contract_address))
                if token_data:
                    amount = portfolio[contract_address]['amount'] * pct
                    asyncio.run(sell_token(chat_id, contract_address, amount, token_data['price']))
        elif call.data.startswith("sell_"):
            contract_address = call.data.split("_")[1]
            if contract_address in portfolio:
                token_data = asyncio.run(validate_with_bitquery(contract_address))
                if token_data:
                    amount = portfolio[contract_address]['amount']
                    asyncio.run(sell_token(chat_id, contract_address, amount, token_data['price']))
        elif call.data.startswith("buy_manual_"):
            token_address = call.data.split("_")[2]
            asyncio.run(buy_token_solana(chat_id, token_address, mise_depart_sol))
        elif call.data.startswith("sell_manual_"):
            token_address = call.data.split("_")[2]
            if token_address in portfolio:
                token_data = asyncio.run(validate_with_bitquery(token_address))
                if token_data:
                    amount = portfolio[token_address]['amount']
                    asyncio.run(sell_token(chat_id, token_address, amount, token_data['price']))
            else:
                queue_message(chat_id, f"⚠️ `{token_address}` n’est pas dans le portefeuille.")
        elif call.data.startswith("refresh_"):
            token_address = call.data.split("_")[1]
            asyncio.run(analyze_token(chat_id, token_address, message_id))
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur callback: `{str(e)}`")
        logger.error(f"Erreur callback: {str(e)}")

if __name__ == "__main__":
    if not all([TELEGRAM_TOKEN, WALLET_ADDRESS, SOLANA_PRIVATE_KEY, WEBHOOK_URL, QUICKNODE_SOL_URL, BITQUERY_ACCESS_TOKEN]):
        logger.error("Variables d’environnement manquantes")
        exit(1)
    if set_webhook():
        logger.info("Webhook Telegram configuré, démarrage du serveur...")
        serve(app, host="0.0.0.0", port=PORT, threads=10)
    else:
        logger.error("Échec du webhook Telegram, passage en mode polling")
        bot.polling(none_stop=True)
