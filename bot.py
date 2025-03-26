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
from solders.signature import Signature
import threading
import asyncio
from datetime import datetime
from waitress import serve
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from queue import Queue
import aiohttp
import websockets
from cryptography.fernet import Fernet
import random

# Configuration logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Session HTTP avec retries
session = requests.Session()
retry_strategy = Retry(total=5, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("https://", adapter)

# File d’attente Telegram
message_queue = Queue()
message_lock = threading.Lock()

# Variables globales
daily_trades = {'buys': [], 'sells': []}
rejected_tokens = {}
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
last_twitter_check = 0
last_polling_check = 0
token_metrics = {}
solana_keypair = None

# Variables d’environnement
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
QUICKNODE_SOL_URL = os.getenv("QUICKNODE_SOL_URL")
QUICKNODE_WS_URL = os.getenv("QUICKNODE_WS_URL")
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY", "5be903b581bc47d29bbfb5ab859de2eb")
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "0f7563a5bbd10275056bf3c2175823cd")
TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN", "AAAAAAAAAAAAAAAAAAAAAD6%2BzQEAAAAAaDN4Thznh7iGRdfqEhebMgWtohs%3DyuaSpNWBCnPcQv5gjERphqmZTIclzPiVqqnirPmdZt4fpRd96D")
PORT = int(os.getenv("PORT", 8080))

# Chiffrement clé privée
cipher_suite = Fernet(Fernet.generate_key())
encrypted_private_key = cipher_suite.encrypt(SOLANA_PRIVATE_KEY.encode()).decode() if SOLANA_PRIVATE_KEY else "N/A"

# Paramètres trading (modifiables via menu, critères stricts conservés)
mise_depart_sol = 0.5
stop_loss_threshold = 10
trailing_stop_percentage = 3
take_profit_steps = [1.2, 2, 10, 100, 500]
max_positions = 5
profit_reinvestment_ratio = 0.9
slippage_max = 0.25
MIN_VOLUME_SOL = 100
MAX_VOLUME_SOL = 2000000
MIN_LIQUIDITY = 5000
MIN_MARKET_CAP_SOL = 1000
MAX_MARKET_CAP_SOL = 5000000
MIN_BUY_SELL_RATIO = 5
MAX_TOKEN_AGE_SECONDS = 300
MIN_SOCIAL_MENTIONS = 5
MIN_SOL_AMOUNT = 0.1
MIN_ACCOUNTS_IN_TX = 3
DETECTION_COOLDOWN = 60
MIN_SOL_BALANCE = 0.05
TWITTER_CHECK_INTERVAL = 900
POLLING_INTERVAL = 15
HOLDER_CONCENTRATION_THRESHOLD = 0.8

# Constantes Solana
PUMP_FUN_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfH43SboMiMEWCPkDPk")
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")

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
            time.sleep(0.1)
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
        time.sleep(1)
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

def validate_address(token_address):
    try:
        Pubkey.from_string(token_address)
        return len(token_address) >= 32 and len(token_address) <= 44
    except ValueError:
        return False

async def analyze_token(chat_id, token_address, message_id=None):
    try:
        if not validate_address(token_address):
            queue_message(chat_id, f"⚠️ `{token_address}` n’est pas une adresse Solana valide.", message_id=message_id)
            return

        if not solana_keypair:
            await initialize_bot(chat_id)
            if not solana_keypair:
                queue_message(chat_id, "⚠️ Wallet Solana non initialisé.", message_id=message_id)
                return

        async with aiohttp.ClientSession() as session:
            gmgn_url = f"https://api.gmgn.ai/v1/tokens/{token_address}/security"
            gmgn_response = await session.get(gmgn_url)
            gmgn_data = await gmgn_response.json() if gmgn_response.status == 200 else {}
            rug_status = gmgn_data.get("risk_level", "N/A")
            rug_details = gmgn_data.get("details", "Analyse indisponible")

            dex_url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
            dex_response = await session.get(dex_url)
            dex_data = await dex_response.json()
            pair = dex_data.get('pairs', [{}])[0]
            market_cap = float(pair.get('marketCap', 0))
            liquidity = float(pair.get('liquidity', {}).get('usd', 0))

            holders_response = await session.post(
                QUICKNODE_SOL_URL,
                json={"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts", "params": [token_address]},
                timeout=5
            )
            holders_data = await holders_response.json()
            num_holders = len(holders_data.get('result', {}).get('value', []))

            metrics = token_metrics.get(token_address, {})
            buy_sell_ratio = metrics.get('buy_sell_ratio', 1.0)
            buy_count = metrics.get('buy_count', 0)
            sell_count = metrics.get('sell_count', 0)

            msg = (
                f"📊 *Analyse de `{token_address}`*\n\n"
                f"**Sécurité (GMGN)** : {rug_status}\n"
                f"Détails : {rug_details}\n\n"
                f"**Market Cap** : ${market_cap:.2f}\n"
                f"**Liquidité** : ${liquidity:.2f}\n"
                f"**Holders** : {num_holders}\n"
                f"**Ratio Achat/Vente** : {buy_sell_ratio:.2f}\n"
                f"**Achats (5min)** : {buy_count}\n"
                f"**Ventes (5min)** : {sell_count}"
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
        logger.error(f"Erreur analyse token: {str(e)}")

@app.route("/quicknode-webhook", methods=['POST'])
def quicknode_webhook():
    global chat_id_global
    logger.info("Webhook QuickNode appelé")
    if request.method == "POST":
        try:
            data = request.get_json()
            logger.info(f"Webhook QuickNode reçu: {json.dumps(data, indent=2)}")
        except Exception as e:
            logger.error(f"Erreur parsing webhook QuickNode: {str(e)}")
            return "Invalid JSON", 400

        try:
            if not chat_id_global:
                logger.warning("chat_id_global non défini")
                return "No chat ID", 400
            for block in data:
                for tx in block.get('transactions', []):
                    token_address = tx.get('info', {}).get('tokenAddress')
                    operation = tx.get('operation')
                    if not token_address or token_address in BLACKLISTED_TOKENS or token_address in detected_tokens:
                        continue
                    if not validate_address(token_address):
                        continue
                    if operation not in ['buy', 'mint']:
                        continue

                    token_data = asyncio.run(get_token_data(token_address))
                    if token_data and asyncio.run(validate_token(chat_id_global, token_address, token_data)):
                        queue_message(chat_id_global, f"🎯 Token détecté via QuickNode Webhook : `{token_address}`")
                        detected_tokens[token_address] = True
                        last_detection_time[token_address] = time.time()
                        asyncio.run(buy_token_solana(chat_id_global, token_address, mise_depart_sol))
            return 'OK', 200
        except Exception as e:
            logger.error(f"Erreur traitement webhook QuickNode: {str(e)}")
            queue_message(chat_id_global, f"⚠️ Erreur webhook QuickNode: `{str(e)}`")
            return f"Erreur: {str(e)}", 500
    return abort(403)

async def websocket_monitor(chat_id):
    while not stop_event.is_set() and bot_active:
        try:
            async with websockets.connect(QUICKNODE_WS_URL) as ws:
                await ws.send(json.dumps({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "logsSubscribe",
                    "params": [{"mentions": [str(PUMP_FUN_PROGRAM_ID)]}, {"commitment": "confirmed"}]
                }))
                logger.info("WebSocket connecté à QuickNode")
                queue_message(chat_id, "🔗 Connexion WebSocket établie avec QuickNode")

                buy_volume = {}
                sell_volume = {}
                buy_count = {}
                sell_count = {}
                token_timestamps = {}
                accounts_in_tx = {}

                while not stop_event.is_set() and bot_active:
                    try:
                        message = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        data = json.loads(message)
                        if 'result' in data:  # Confirmation d'abonnement
                            logger.info(f"Abonnement WebSocket confirmé: {data['result']}")
                            queue_message(chat_id, f"✅ Abonnement WebSocket confirmé: {data['result']}")
                            continue

                        log_data = data.get('params', {}).get('result', {}).get('value', {})
                        if not log_data or 'logs' not in log_data:
                            continue

                        signature = log_data.get('signature')
                        tx_response = await session.post(
                            QUICKNODE_SOL_URL,
                            json={"jsonrpc": "2.0", "id": 1, "method": "getTransaction", "params": [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]}
                        )
                        tx_data = (await tx_response.json()).get('result', {})
                        if not tx_data:
                            continue

                        token_address = None
                        for instr in tx_data.get('transaction', {}).get('message', {}).get('instructions', []):
                            if instr.get('programId') == str(PUMP_FUN_PROGRAM_ID):
                                accounts = instr.get('accounts', [])
                                if len(accounts) > 1:
                                    token_address = accounts[1]  # Deuxième compte souvent le token
                                    break

                        if not token_address or token_address in detected_tokens or token_address in dynamic_blacklist:
                            continue

                        data_bytes = base58.b58decode(instr.get('data', ''))
                        if len(data_bytes) < 9:
                            continue
                        amount = int.from_bytes(data_bytes[1:9], 'little') / 10**9
                        accounts = set(accounts)

                        if data_bytes[0] == 2:  # Buy
                            buy_volume[token_address] = buy_volume.get(token_address, 0) + amount
                            buy_count[token_address] = buy_count.get(token_address, 0) + 1
                            token_timestamps[token_address] = token_timestamps.get(token_address, time.time())
                        elif data_bytes[0] == 3:  # Sell
                            sell_volume[token_address] = sell_volume.get(token_address, 0) + amount
                            sell_count[token_address] = sell_count.get(token_address, 0) + 1

                        accounts_in_tx[token_address] = accounts_in_tx.get(token_address, set()).union(accounts)

                        age_seconds = time.time() - token_timestamps.get(token_address, time.time())
                        if age_seconds <= MAX_TOKEN_AGE_SECONDS:
                            bv = buy_volume.get(token_address, 0)
                            sv = sell_volume.get(token_address, 0)
                            ratio = bv / sv if sv > 0 else (bv > 0 and 10 or 1)
                            token_metrics[token_address] = {
                                'buy_sell_ratio': ratio,
                                'buy_count': buy_count.get(token_address, 0),
                                'sell_count': sell_count.get(token_address, 0),
                                'last_update': time.time()
                            }

                            logger.info(f"Token {token_address}: Ratio A/V = {ratio}, BV = {bv}, SV = {sv}")
                            queue_message(chat_id, f"🔍 Analyse {token_address}: Ratio A/V = {ratio:.2f}, BV = {bv:.2f}, SV = {sv:.2f}")

                            if sv > bv * 0.5:
                                dynamic_blacklist.add(token_address)
                                queue_message(chat_id, f"⚠️ `{token_address}` rejeté : Dump artificiel détecté")
                                continue

                            if len(accounts_in_tx.get(token_address, set())) < MIN_ACCOUNTS_IN_TX:
                                queue_message(chat_id, f"⚠️ `{token_address}` rejeté : Pas assez de comptes dans TX")
                                continue

                            if ratio >= MIN_BUY_SELL_RATIO:
                                token_data = await validate_token_full(chat_id, token_address)
                                if token_data:
                                    queue_message(chat_id, f"🎯 Pump détecté : `{token_address}` (Ratio A/V: {ratio:.2f})")
                                    detected_tokens[token_address] = True
                                    last_detection_time[token_address] = time.time()
                                    await buy_token_solana(chat_id, token_address, mise_depart_sol)

                        if time.time() - token_timestamps.get(token_address, 0) > 300:
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
                        logger.error(f"Erreur WebSocket interne: {str(e)}")
                        queue_message(chat_id, f"⚠️ Erreur WebSocket interne: `{str(e)}`")

        except Exception as e:
            logger.error(f"Erreur connexion WebSocket: {str(e)}")
            queue_message(chat_id, f"⚠️ Erreur connexion WebSocket: `{str(e)}`")
            await asyncio.sleep(5)  # Reconnexion après 5 secondes

async def get_token_data(token_address):
    try:
        async with aiohttp.ClientSession() as session:
            dex_url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
            dex_response = await session.get(dex_url)
            dex_data = await dex_response.json()
            pair = dex_data.get('pairs', [{}])[0]
            liquidity = float(pair.get('liquidity', {}).get('usd', 0))
            price = float(pair.get('priceUsd', 0)) or 0
            volume_24h = float(pair.get('volume', {}).get('h24', 0)) / 1000 or 0

            if not liquidity or not price:
                birdeye_url = f"https://public-api.birdeye.so/public/token_overview?address={token_address}"
                birdeye_response = await session.get(birdeye_url, headers={"X-API-KEY": BIRDEYE_API_KEY})
                birdeye_data = await birdeye_response.json()
                birdeye_info = birdeye_data.get('data', {})
                liquidity = birdeye_info.get('liquidity', 0)
                price = birdeye_info.get('price', 0)
                volume_24h = birdeye_info.get('volume', 0) / 1000
                pair_created_at = birdeye_info.get('created_at', time.time())
                if isinstance(pair_created_at, str):
                    pair_created_at = datetime.strptime(pair_created_at, "%Y-%m-%dT%H:%M:%S.%fZ").timestamp()
            else:
                pair_created_at = time.time() - 3600

            supply_response = await session.post(
                QUICKNODE_SOL_URL,
                json={"jsonrpc": "2.0", "id": 1, "method": "getTokenSupply", "params": [token_address]},
                timeout=5
            )
            supply_data = await supply_response.json()
            supply = float(supply_data.get('result', {}).get('value', {}).get('amount', '0')) / 10**6

            holders_response = await session.post(
                QUICKNODE_SOL_URL,
                json={"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts", "params": [token_address]},
                timeout=5
            )
            holders_data = await holders_response.json()
            top_holders = holders_data.get('result', {}).get('value', [])
            top_holder_ratio = sum(float(h.get('amount', 0)) / 10**6 for h in top_holders[:5]) / supply if supply > 0 else 0
            top_holder_ratio = min(top_holder_ratio, 1.0)

            if not liquidity and volume_24h > 0:
                liquidity = volume_24h * 0.1

        market_cap = supply * price if price > 0 else 0
        return {
            'volume_24h': volume_24h,
            'liquidity': liquidity,
            'market_cap': market_cap,
            'price': price,
            'buy_sell_ratio': token_metrics.get(token_address, {}).get('buy_sell_ratio', 1.0),
            'pair_created_at': pair_created_at,
            'supply': supply,
            'has_liquidity': liquidity > MIN_LIQUIDITY,
            'top_holder_ratio': top_holder_ratio
        }
    except Exception as e:
        logger.error(f"Erreur récupération données pour {token_address}: {str(e)}")
        return None

async def validate_token(chat_id, token_address, data):
    try:
        if not data:
            queue_message(chat_id, f"⚠️ `{token_address}` rejeté : Données indisponibles")
            return False

        age_seconds = time.time() - data.get('pair_created_at', time.time())
        volume_24h = data.get('volume_24h', 0)
        liquidity = data.get('liquidity', 0)
        market_cap = data.get('market_cap', 0)
        buy_sell_ratio = data.get('buy_sell_ratio', 1)
        top_holder_ratio = data.get('top_holder_ratio', 0)

        if age_seconds > MAX_TOKEN_AGE_SECONDS:
            queue_message(chat_id, f"⚠️ `{token_address}` rejeté : Trop vieux ({age_seconds/60:.2f} min)")
            return False
        if top_holder_ratio > HOLDER_CONCENTRATION_THRESHOLD:
            queue_message(chat_id, f"⚠️ `{token_address}` rejeté : Concentration holders trop élevée ({top_holder_ratio:.2%})")
            dynamic_blacklist.add(token_address)
            return False
        if len(portfolio) >= max_positions:
            queue_message(chat_id, f"⚠️ `{token_address}` rejeté : Limite de {max_positions} positions atteinte")
            return False
        if liquidity <= MIN_LIQUIDITY:
            queue_message(chat_id, f"⚠️ `{token_address}` rejeté : Liquidité ${liquidity:.2f} <= ${MIN_LIQUIDITY}")
            return False
        if volume_24h < MIN_VOLUME_SOL or volume_24h > MAX_VOLUME_SOL:
            queue_message(chat_id, f"⚠️ `{token_address}` rejeté : Volume ${volume_24h:.2f} hors plage [{MIN_VOLUME_SOL}, {MAX_VOLUME_SOL}]")
            return False
        if market_cap < MIN_MARKET_CAP_SOL or market_cap > MAX_MARKET_CAP_SOL:
            queue_message(chat_id, f"⚠️ `{token_address}` rejeté : Market Cap ${market_cap:.2f} hors plage [{MIN_MARKET_CAP_SOL}, {MAX_MARKET_CAP_SOL}]")
            return False
        if buy_sell_ratio < MIN_BUY_SELL_RATIO:
            queue_message(chat_id, f"⚠️ `{token_address}` rejeté : Ratio A/V {buy_sell_ratio:.2f} < {MIN_BUY_SELL_RATIO}")
            return False
        return True
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur validation `{token_address}`: `{str(e)}`")
        return False

async def validate_token_full(chat_id, token_address):
    async with aiohttp.ClientSession() as session:
        birdeye_url = f"https://public-api.birdeye.so/public/token_overview?address={token_address}"
        birdeye_response = await session.get(birdeye_url, headers={"X-API-KEY": BIRDEYE_API_KEY})
        birdeye_data = await birdeye_response.json()
        birdeye_info = birdeye_data.get('data', {})
        liquidity = birdeye_info.get('liquidity', 0)
        volume_24h = birdeye_info.get('volume', 0) / 1000
        price = birdeye_info.get('price', 0)
        pair_created_at = birdeye_info.get('created_at', time.time())
        if isinstance(pair_created_at, str):
            pair_created_at = datetime.strptime(pair_created_at, "%Y-%m-%dT%H:%M:%S.%fZ").timestamp()

        age_seconds = time.time() - pair_created_at
        if age_seconds > MAX_TOKEN_AGE_SECONDS:
            queue_message(chat_id, f"⚠️ `{token_address}` rejeté : Trop vieux ({age_seconds/60:.2f} min)")
            return None
        if liquidity <= MIN_LIQUIDITY:
            queue_message(chat_id, f"⚠️ `{token_address}` rejeté : Liquidité ${liquidity:.2f} <= ${MIN_LIQUIDITY}")
            return None
        if volume_24h < MIN_VOLUME_SOL or volume_24h > MAX_VOLUME_SOL:
            queue_message(chat_id, f"⚠️ `{token_address}` rejeté : Volume ${volume_24h:.2f} hors plage [{MIN_VOLUME_SOL}, {MAX_VOLUME_SOL}]")
            return None

        supply_response = await session.post(
            QUICKNODE_SOL_URL,
            json={"jsonrpc": "2.0", "id": 1, "method": "getTokenSupply", "params": [token_address]},
        )
        supply_data = await supply_response.json()
        supply = float(supply_data.get('result', {}).get('value', {}).get('amount', '0')) / 10**6

        holders_response = await session.post(
            QUICKNODE_SOL_URL,
            json={"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts", "params": [token_address]},
        )
        holders_data = await holders_response.json()
        top_holders = holders_data.get('result', {}).get('value', [])
        top_holder_ratio = sum(float(h.get('amount', 0)) / 10**6 for h in top_holders[:5]) / supply if supply > 0 else 0
        if top_holder_ratio > HOLDER_CONCENTRATION_THRESHOLD:
            queue_message(chat_id, f"⚠️ `{token_address}` rejeté : Concentration holders trop élevée ({top_holder_ratio:.2%})")
            return None

        market_cap = supply * price
        if market_cap < MIN_MARKET_CAP_SOL or market_cap > MAX_MARKET_CAP_SOL:
            queue_message(chat_id, f"⚠️ `{token_address}` rejeté : Market Cap ${market_cap:.2f} hors plage [{MIN_MARKET_CAP_SOL}, {MAX_MARKET_CAP_SOL}]")
            return None

        return {
            'liquidity': liquidity,
            'volume_24h': volume_24h,
            'market_cap': market_cap,
            'price': price,
            'pair_created_at': pair_created_at,
            'top_holder_ratio': top_holder_ratio
        }

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
            jupiter_url = f"https://quote-api.jupiter.ag/v6/quote?inputMint=So11111111111111111111111111111111111111112&outputMint={contract_address}&amount={int(amount * 10**9)}&slippageBps={int(slippage_max * 10000)}"
            jupiter_response = await session.get(jupiter_url)
            quote = await jupiter_response.json()
            swap_tx = await session.post("https://quote-api.jupiter.ag/v6/swap", json={
                "quoteResponse": quote,
                "userPublicKey": str(solana_keypair.pubkey())
            })
            tx_data = await swap_tx.json()
            tx_hash = tx_data.get("swapTransaction")

        token_data = await get_token_data(contract_address)
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
        response = session.post(QUICKNODE_SOL_URL, json={
            "jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash",
            "params": [{"commitment": "finalized"}]
        }, timeout=5)
        blockhash = response.json()['result']['value']['blockhash']
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
        tx_hash = session.post(QUICKNODE_SOL_URL, json={
            "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
            "params": [base58.b58encode(tx.serialize()).decode('utf-8')]
        }, timeout=5).json()['result']

        profit = (current_price - portfolio[contract_address]['entry_price']) * amount
        portfolio[contract_address]['profit'] += profit
        portfolio[contract_address]['amount'] -= amount
        reinvest_amount = profit * profit_reinvestment_ratio if profit > 0 else 0
        mise_depart_sol += reinvest_amount
        queue_message(chat_id, f"✅ Vente réussie : {amount:.4f} SOL, Profit: {profit:.4f} SOL (TX: `{tx_hash}`)")
        daily_trades['sells'].append({'token': contract_address, 'amount': amount, 'pnl': profit, 'timestamp': datetime.now().strftime('%H:%M:%S')})
        if portfolio[contract_address]['amount'] <= 0:
            del portfolio[contract_address]
    except Exception as e:
        queue_message(chat_id, f"⚠️ Échec vente Solana `{contract_address}`: `{str(e)}`")
        logger.error(f"Échec vente Solana: {str(e)}")

async def monitor_and_sell(chat_id):
    while not stop_event.is_set() and bot_active:
        try:
            if not portfolio:
                await asyncio.sleep(2)
                continue
            for contract_address, data in list(portfolio.items()):
                amount = data['amount']
                current_data = await get_token_data(contract_address)
                if not current_data:
                    continue
                current_price = current_data.get('price', 0)
                data['price_history'].append(current_price)
                if len(data['price_history']) > 10:
                    data['price_history'].pop(0)
                profit_pct = (current_price - data['entry_price']) / data['entry_price'] * 100 if data['entry_price'] > 0 else 0
                loss_pct = -profit_pct if profit_pct < 0 else 0
                data['highest_price'] = max(data['highest_price'], current_price)
                trailing_stop_price = data['highest_price'] * (1 - trailing_stop_percentage / 100)

                if len(data['price_history']) >= 3 and current_price < data['price_history'][-2] * 0.7:
                    dynamic_blacklist.add(contract_address)
                    await sell_token(chat_id, contract_address, amount, current_price)
                    queue_message(chat_id, f"⚠️ Dump détecté sur `{contract_address}`, vente totale !")

                if not pause_auto_sell:
                    if profit_pct >= 200:
                        await sell_token(chat_id, contract_address, amount, current_price)
                        queue_message(chat_id, f"💰 Vente à ×2 sur `{contract_address}` !")
                    elif profit_pct >= take_profit_steps[4] * 100:
                        await sell_token(chat_id, contract_address, amount * 0.5, current_price)
                    elif profit_pct >= take_profit_steps[3] * 100:
                        await sell_token(chat_id, contract_address, amount * 0.25, current_price)
                    elif profit_pct >= take_profit_steps[2] * 100:
                        await sell_token(chat_id, contract_address, amount * 0.2, current_price)
                    elif profit_pct >= take_profit_steps[1] * 100:
                        await sell_token(chat_id, contract_address, amount * 0.15, current_price)
                    elif profit_pct >= take_profit_steps[0] * 100:
                        await sell_token(chat_id, contract_address, amount * 0.1, current_price)
                    elif current_price <= trailing_stop_price or loss_pct >= stop_loss_threshold:
                        await sell_token(chat_id, contract_address, amount, current_price)
            await asyncio.sleep(0.5)
        except Exception as e:
            queue_message(chat_id, f"⚠️ Erreur surveillance: `{str(e)}`")
            logger.error(f"Erreur surveillance: {str(e)}")
            await asyncio.sleep(5)

async def check_twitter_mentions(chat_id):
    global last_twitter_check
    try:
        if time.time() - last_twitter_check < TWITTER_CHECK_INTERVAL:
            return
        async with aiohttp.ClientSession() as async_session:
            headers = {"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"}
            url = "https://api.twitter.com/2/tweets/search/recent?query=memecoin solana -is:retweet&tweet.fields=public_metrics&max_results=100"
            response = await async_session.get(url, headers=headers)
            data = await response.json()
            tweets = data.get('data', [])
            token_mentions = {}
            for tweet in tweets:
                text = tweet['text'].lower()
                for token in detected_tokens.keys():
                    if token[:6] in text or token[-6:] in text:
                        token_mentions[token] = token_mentions.get(token, 0) + tweet['public_metrics']['like_count'] + tweet['public_metrics']['retweet_count']

            for token, score in token_mentions.items():
                if score >= MIN_SOCIAL_MENTIONS and token not in portfolio and token not in dynamic_blacklist:
                    token_data = await get_token_data(token)
                    if token_data and await validate_token(chat_id, token, token_data):
                        queue_message(chat_id, f"📣 Token détecté via Twitter : `{token}` (score: {score})")
                        await buy_token_solana(chat_id, token, mise_depart_sol)
            last_twitter_check = time.time()
    except Exception as e:
        logger.error(f"Erreur vérification Twitter: {str(e)}")
        queue_message(chat_id, f"⚠️ Erreur Twitter: `{str(e)}`")

async def poll_new_tokens(chat_id):
    global last_polling_check
    try:
        if time.time() - last_polling_check < POLLING_INTERVAL:
            return
        async with aiohttp.ClientSession() as async_session:
            birdeye_url = "https://public-api.birdeye.so/public/tokenlist?sort_by=volume&sort_type=desc&offset=0&limit=50"
            birdeye_response = await async_session.get(birdeye_url, headers={"X-API-KEY": BIRDEYE_API_KEY})
            birdeye_data = await birdeye_response.json()
            tokens = birdeye_data.get('data', {}).get('tokens', [])

            for token in tokens:
                token_address = token.get('address')
                if not token_address or token_address in BLACKLISTED_TOKENS or token_address in detected_tokens or token_address in dynamic_blacklist:
                    continue
                if not validate_address(token_address):
                    continue

                token_data = await get_token_data(token_address)
                if token_data and await validate_token(chat_id, token_address, token_data):
                    queue_message(chat_id, f"🎯 Token détecté via polling Birdeye : `{token_address}`")
                    detected_tokens[token_address] = True
                    last_detection_time[token_address] = time.time()
                    await buy_token_solana(chat_id, token_address, mise_depart_sol)

        last_polling_check = time.time()
    except Exception as e:
        logger.error(f"Erreur polling nouveaux tokens: {str(e)}")
        queue_message(chat_id, f"⚠️ Erreur polling: `{str(e)}`")

async def show_portfolio(chat_id):
    try:
        sol_balance = await get_solana_balance(chat_id)
        msg = f"💰 *Portefeuille:*\nSOL : {sol_balance:.4f}\n\n"
        markup = InlineKeyboardMarkup()
        for ca, data in portfolio.items():
            current_price = (await get_token_data(ca)).get('price', 0)
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
        response = session.post(QUICKNODE_SOL_URL, json={
            "jsonrpc": "2.0", "id": 1, "method": "getBalance",
            "params": [str(solana_keypair.pubkey())]
        }, timeout=5)
        return response.json().get('result', {}).get('value', 0) / 10**9
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur solde Solana: `{str(e)}`")
        return 0

async def show_daily_summary(chat_id):
    try:
        msg = f"📅 *Récapitulatif du jour ({datetime.now().strftime('%Y-%m-%d')})*:\n\n"
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

async def adjust_mise_sol(message):
    global mise_depart_sol
    chat_id = message.chat.id
    try:
        new_mise = float(message.text)
        if new_mise > 0:
            mise_depart_sol = new_mise
            queue_message(chat_id, f"✅ Mise Solana mise à jour à {mise_depart_sol} SOL")
        else:
            queue_message(chat_id, "⚠️ La mise doit être positive!")
    except ValueError:
        queue_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 0.5)")

async def adjust_stop_loss(message):
    global stop_loss_threshold
    chat_id = message.chat.id
    try:
        new_sl = float(message.text)
        if new_sl > 0:
            stop_loss_threshold = new_sl
            queue_message(chat_id, f"✅ Stop-Loss mis à jour à {stop_loss_threshold} %")
        else:
            queue_message(chat_id, "⚠️ Le Stop-Loss doit être positif!")
    except ValueError:
        queue_message(chat_id, "⚠️ Erreur : Entrez un pourcentage valide (ex. : 10)")

async def adjust_take_profit(message):
    global take_profit_steps
    chat_id = message.chat.id
    try:
        new_tp = [float(x) for x in message.text.split(",")]
        if len(new_tp) == 5 and all(x > 0 for x in new_tp):
            take_profit_steps = new_tp
            queue_message(chat_id, f"✅ Take-Profit mis à jour à x{take_profit_steps[0]}, x{take_profit_steps[1]}, x{take_profit_steps[2]}, x{take_profit_steps[3]}, x{take_profit_steps[4]}")
        else:
            queue_message(chat_id, "⚠️ Entrez 5 valeurs positives séparées par des virgules (ex. : 1.2,2,10,100,500)")
    except ValueError:
        queue_message(chat_id, "⚠️ Erreur : Entrez des nombres valides (ex. : 1.2,2,10,100,500)")

async def adjust_reinvestment_ratio(message):
    global profit_reinvestment_ratio
    chat_id = message.chat.id
    try:
        new_ratio = float(message.text)
        if 0 <= new_ratio <= 1:
            profit_reinvestment_ratio = new_ratio
            queue_message(chat_id, f"✅ Ratio de réinvestissement mis à jour à {profit_reinvestment_ratio * 100}%")
        else:
            queue_message(chat_id, "⚠️ Le ratio doit être entre 0 et 1 (ex. : 0.9)")
    except ValueError:
        queue_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 0.9)")

async def adjust_detection_criteria(message):
    global MIN_VOLUME_SOL, MAX_VOLUME_SOL, MIN_LIQUIDITY, MIN_MARKET_CAP_SOL, MAX_MARKET_CAP_SOL, MIN_BUY_SELL_RATIO, MAX_TOKEN_AGE_SECONDS
    chat_id = message.chat.id
    try:
        criteria = message.text.split(",")
        if len(criteria) != 7:
            queue_message(chat_id, "⚠️ Entrez 7 valeurs séparées par des virgules (ex. : 100,2000000,5000,1000,5000000,5,300)")
            return
        min_vol, max_vol, min_liq, min_mc, max_mc, min_ratio, max_age = map(float, criteria)
        if min_vol < 0 or max_vol < min_vol or min_liq < 0 or min_mc < 0 or max_mc < min_mc or min_ratio < 0 or max_age < 0:
            queue_message(chat_id, "⚠️ Valeurs invalides ! Assurez-vous que toutes sont positives et cohérentes.")
            return
        MIN_VOLUME_SOL = min_vol
        MAX_VOLUME_SOL = max_vol
        MIN_LIQUIDITY = min_liq
        MIN_MARKET_CAP_SOL = min_mc
        MAX_MARKET_CAP_SOL = max_mc
        MIN_BUY_SELL_RATIO = min_ratio
        MAX_TOKEN_AGE_SECONDS = max_age
        queue_message(chat_id, (
            f"✅ Critères de détection mis à jour :\n"
            f"Volume : [{MIN_VOLUME_SOL}, {MAX_VOLUME_SOL}] SOL\n"
            f"Liquidité min : {MIN_LIQUIDITY} $\n"
            f"Market Cap : [{MIN_MARKET_CAP_SOL}, {MAX_MARKET_CAP_SOL}] $\n"
            f"Ratio A/V min : {MIN_BUY_SELL_RATIO}\n"
            f"Âge max : {MAX_TOKEN_AGE_SECONDS/60:.2f} min"
        ))
    except ValueError:
        queue_message(chat_id, "⚠️ Erreur : Entrez des nombres valides (ex. : 100,2000000,5000,1000,5000000,5,300)")

async def run_tasks(chat_id):
    await asyncio.gather(
        monitor_and_sell(chat_id),
        check_twitter_mentions(chat_id),
        poll_new_tokens(chat_id),
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
            queue_message(chat_id, "▶️ Trading Solana lancé avec succès!")
            threading.Thread(target=lambda: asyncio.run(run_tasks(chat_id)), daemon=True).start()
            threading.Thread(target=heartbeat, args=(chat_id,), daemon=True).start()
        else:
            queue_message(chat_id, "⚠️ Échec initialisation : Solana non connecté")
            trade_active = False
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur initialisation: `{str(e)}`")
        logger.error(f"Erreur initialisation: {str(e)}")
        trade_active = False

def heartbeat(chat_id):
    while trade_active and bot_active:
        queue_message(chat_id, "💓 Bot actif - Surveillance en cours")
        time.sleep(300)

@app.route("/webhook", methods=['POST'])
def webhook():
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
            queue_message(chat_id, (
                f"ℹ️ *Statut actuel* :\n"
                f"Trading actif: {'Oui' if trade_active else 'Non'}\n"
                f"SOL disponible: {sol_balance:.4f}\n"
                f"Positions: {len(portfolio)}/{max_positions}\n"
                f"Mise Solana: {mise_depart_sol} SOL\n"
                f"Stop-Loss: {stop_loss_threshold}%\n"
                f"Take-Profit: x{take_profit_steps[0]}, x{take_profit_steps[1]}, x{take_profit_steps[2]}, x{take_profit_steps[3]}, x{take_profit_steps[4]}\n"
                f"Critères détection: Volume [{MIN_VOLUME_SOL}, {MAX_VOLUME_SOL}] SOL, Liquidité min {MIN_LIQUIDITY} $, Market Cap [{MIN_MARKET_CAP_SOL}, {MAX_MARKET_CAP_SOL}] $, Ratio A/V min {MIN_BUY_SELL_RATIO}, Âge max {MAX_TOKEN_AGE_SECONDS/60:.2f} min"
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
            bot.register_next_step_handler(call.message, adjust_mise_sol)
        elif call.data == "adjust_stop_loss":
            queue_message(chat_id, "Entrez le nouveau Stop-Loss (ex. : 10) :")
            bot.register_next_step_handler(call.message, adjust_stop_loss)
        elif call.data == "adjust_take_profit":
            queue_message(chat_id, "Entrez les nouveaux Take-Profit (ex. : 1.2,2,10,100,500) :")
            bot.register_next_step_handler(call.message, adjust_take_profit)
        elif call.data == "adjust_reinvestment":
            queue_message(chat_id, "Entrez le nouveau ratio de réinvestissement (ex. : 0.9) :")
            bot.register_next_step_handler(call.message, adjust_reinvestment_ratio)
        elif call.data == "adjust_detection_criteria":
            queue_message(chat_id, "Entrez les nouveaux critères (min_vol, max_vol, min_liq, min_mc, max_mc, min_ratio, max_age_seconds) ex. : 100,2000000,5000,1000,5000000,5,300 :")
            bot.register_next_step_handler(call.message, adjust_detection_criteria)
        elif call.data.startswith("sell_pct_"):
            parts = call.data.split("_")
            contract_address, pct = parts[2], int(parts[3]) / 100
            if contract_address in portfolio:
                current_data = asyncio.run(get_token_data(contract_address))
                if current_data:
                    amount = portfolio[contract_address]['amount'] * pct
                    asyncio.run(sell_token(chat_id, contract_address, amount, current_data['price']))
        elif call.data.startswith("sell_"):
            contract_address = call.data.split("_")[1]
            if contract_address in portfolio:
                current_data = asyncio.run(get_token_data(contract_address))
                if current_data:
                    amount = portfolio[contract_address]['amount']
                    asyncio.run(sell_token(chat_id, contract_address, amount, current_data['price']))
        elif call.data.startswith("buy_manual_"):
            token_address = call.data.split("_")[2]
            asyncio.run(buy_token_solana(chat_id, token_address, mise_depart_sol))
        elif call.data.startswith("sell_manual_"):
            token_address = call.data.split("_")[2]
            if token_address in portfolio:
                current_data = asyncio.run(get_token_data(token_address))
                if current_data:
                    amount = portfolio[token_address]['amount']
                    asyncio.run(sell_token(chat_id, token_address, amount, current_data['price']))
            else:
                queue_message(chat_id, f"⚠️ `{token_address}` n’est pas dans le portefeuille.")
        elif call.data.startswith("refresh_"):
            token_address = call.data.split("_")[1]
            asyncio.run(analyze_token(chat_id, token_address, message_id))
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur callback: `{str(e)}`")
        logger.error(f"Erreur callback: {str(e)}")

if __name__ == "__main__":
    if not all([TELEGRAM_TOKEN, WALLET_ADDRESS, SOLANA_PRIVATE_KEY, WEBHOOK_URL, QUICKNODE_SOL_URL, QUICKNODE_WS_URL, BIRDEYE_API_KEY, TWITTER_BEARER_TOKEN]):
        logger.error("Variables d’environnement manquantes")
        exit(1)
    if set_webhook():
        logger.info("Webhook Telegram configuré, démarrage du serveur...")
        serve(app, host="0.0.0.0", port=PORT, threads=10)
    else:
        logger.error("Échec du webhook Telegram, passage en mode polling")
        bot.polling(none_stop=True)
