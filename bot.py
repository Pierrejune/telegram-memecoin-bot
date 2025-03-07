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
from concurrent.futures import ThreadPoolExecutor
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
active_threads = []  # Liste pour suivre les threads actifs

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

# Paramètres de trading
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
    message_queue.put((chat_id, text, reply_markup) if reply_markup else (chat_id, text))

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
def get_token_data(token_address):
    for _ in range(5):
        try:
            response = session.get(f"https://api.dexscreener.com/latest/dex/tokens/{token_address}", timeout=10)
            response.raise_for_status()
            pairs = response.json().get('pairs', [])
            if not pairs or pairs[0].get('chainId') != 'solana':
                time.sleep(1)
                continue
            data = pairs[0]
            age_hours = (time.time() - (data.get('pairCreatedAt', 0) / 1000)) / 3600 if data.get('pairCreatedAt') else 0
            if age_hours > MAX_TOKEN_AGE_HOURS:
                return None
            return {
                'volume_24h': float(data.get('volume', {}).get('h24', 0) or 0),
                'liquidity': float(data.get('liquidity', {}).get('usd', 0) or 0),
                'market_cap': float(data.get('marketCap', 0) or 0),
                'price': float(data.get('priceUsd', 0) or 0),
                'buy_sell_ratio': float(data.get('txns', {}).get('m5', {}).get('buys', 0) or 0) / max(float(data.get('txns', {}).get('m5', {}).get('sells', 0) or 1), 1),
                'pair_created_at': data.get('pairCreatedAt', 0) / 1000 if data.get('pairCreatedAt') else time.time(),
                'supply': float(data.get('marketCap', 0) or 0) / float(data.get('priceUsd', 0) or 1)
            }
        except Exception as e:
            logger.error(f"Erreur DexScreener pour {token_address}: {str(e)}")
            time.sleep(1)
    try:
        response = session.post(QUICKNODE_SOL_URL, json={
            "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
            "params": [token_address, {"encoding": "jsonParsed"}]
        }, timeout=5)
        account_info = response.json().get('result', {}).get('value', {})
        if not account_info:
            return None
        return {
            'volume_24h': MIN_VOLUME_SOL,
            'liquidity': MIN_LIQUIDITY,
            'market_cap': MIN_MARKET_CAP_SOL,
            'price': 0.001,
            'buy_sell_ratio': 1,
            'pair_created_at': time.time() - (account_info.get('lamports', 0) % 3600),
            'supply': MIN_MARKET_CAP_SOL / 0.001
        }
    except Exception as e:
        logger.error(f"Erreur RPC pour {token_address}: {str(e)}")
        return None

@backoff.on_exception(backoff.expo, Exception, max_tries=5)
def check_token_security(token_address):
    api_url = f"https://api.gopluslabs.io/api/v1/token_security/solana?contract_addresses={token_address}"
    try:
        response = session.get(api_url, timeout=5)
        response.raise_for_status()
        data = response.json().get('result', {}).get(token_address.lower(), {})
        taxes = float(data.get('buy_tax', 0)) + float(data.get('sell_tax', 0))
        top_holder_pct = float(data.get('holder_percent_top_1', 0))
        is_locked = data.get('is_liquidity_locked', '0') == '1'
        is_honeypot = data.get('is_honeypot', '0') == '1'
        return taxes < 0.03 and top_holder_pct < 0.15 and is_locked and not is_honeypot
    except Exception as e:
        logger.error(f"Erreur GoPlus pour {token_address}: {str(e)}")
        return False

@backoff.on_exception(backoff.expo, Exception, max_tries=5)
async def get_twitter_mentions(token_address, chat_id):
    global last_twitter_request_time
    try:
        current_time = time.time()
        if 'last_twitter_request_time' not in globals() or (current_time - last_twitter_request_time) >= 900:
            headers = {"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"}
            response = session.get(
                "https://api.twitter.com/2/tweets/search/recent",
                headers=headers,
                params={
                    "query": f"{token_address} -is:retweet",
                    "max_results": 100,
                    "tweet.fields": "created_at,author_id",
                    "user.fields": "public_metrics",
                    "expansions": "author_id"
                },
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            tweets = data.get('data', [])
            users = {u['id']: u for u in data.get('includes', {}).get('users', [])}
            mentions = 0
            for tweet in tweets:
                author_id = tweet['author_id']
                user = users.get(author_id, {})
                followers = user.get('public_metrics', {}).get('followers_count', 0)
                created_at = datetime.strptime(tweet.get('created_at', ''), '%Y-%m-%dT%H:%M:%SZ').timestamp()
                if followers > 500 and (time.time() - created_at) / 3600 <= MAX_TOKEN_AGE_HOURS:
                    mentions += 1
            logger.info(f"Twitter mentions pour {token_address}: {mentions}")
            globals()['last_twitter_request_time'] = current_time
            return mentions
        else:
            logger.info(f"Twitter API en attente pour {token_address}")
            return MIN_SOCIAL_MENTIONS
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur Twitter API: `{str(e)}`")
        logger.error(f"Erreur Twitter: {str(e)}")
        return MIN_SOCIAL_MENTIONS

@backoff.on_exception(backoff.expo, Exception, max_tries=5)
def check_dump_risk(token_address):
    try:
        response = session.post(QUICKNODE_SOL_URL, json={
            "jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress",
            "params": [token_address, {"limit": 50}]
        }, timeout=5)
        txs = response.json().get('result', [])
        total_sold = 0
        for tx in txs:
            tx_details = session.post(QUICKNODE_SOL_URL, json={
                "jsonrpc": "2.0", "id": 1, "method": "getTransaction",
                "params": [tx['signature'], {"encoding": "jsonParsed"}]
            }).json()
            if 'sell' in str(tx_details).lower():
                total_sold += 1
        if total_sold > 5:
            return False
        return True
    except Exception as e:
        logger.error(f"Erreur check_dump_risk pour {token_address}: {str(e)}")
        return True

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
                while trade_active:
                    try:
                        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                        if 'params' not in msg or 'result' not in msg:
                            continue
                        data = msg['params']['result']['value']['account']['data'][0]
                        pubkey = msg['params']['result']['pubkey']
                        logger.info(f"Données WebSocket reçues: {data[:100]}... (pubkey: {pubkey})")
                        potential_addresses = [addr for addr in data.split() if validate_address(addr) and addr not in BLACKLISTED_TOKENS and addr not in portfolio]
                        for token_address in potential_addresses:
                            response = session.post(QUICKNODE_SOL_URL, json={
                                "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                                "params": [token_address, {"encoding": "jsonParsed"}]
                            }, timeout=5)
                            account_info = response.json().get('result', {}).get('value', {})
                            if account_info and 'lamports' in account_info:
                                age_hours = (time.time() - account_info.get('lamports', 0)) / 3600
                                if age_hours <= MAX_TOKEN_AGE_HOURS:
                                    exchange = 'Raydium' if str(RAYDIUM_PROGRAM_ID) in pubkey else 'Pump.fun'
                                    queue_message(chat_id, f"🎯 Snipe détecté : `{token_address}` (Solana - {exchange})")
                                    logger.info(f"Snipe Solana: {token_address}")
                                    await validate_and_trade(chat_id, token_address)
                    except Exception as e:
                        logger.error(f"Erreur sniping Solana WebSocket: {str(e)}")
                    await asyncio.sleep(0.1)
        except Exception as e:
            queue_message(chat_id, f"⚠️ Erreur sniping Solana: `{str(e)}`")
            logger.error(f"Erreur sniping Solana connexion: {str(e)}")
            await asyncio.sleep(5)

@backoff.on_exception(backoff.expo, Exception, max_tries=5)
async def detect_dexscreener(chat_id):
    while trade_active:
        try:
            response = session.get("https://api.dexscreener.com/latest/dex/search?q=Solana", timeout=10)
            response.raise_for_status()
            data = response.json()
            if not data or 'pairs' not in data:
                logger.error(f"Réponse DexScreener vide: {data}")
                await asyncio.sleep(10)
                continue
            pairs = data['pairs']
            for pair in pairs:
                if pair.get('chainId') != 'solana':
                    continue
                token_address = pair.get('baseToken', {}).get('address')
                age_hours = (time.time() - (pair.get('pairCreatedAt', 0) / 1000)) / 3600 if pair.get('pairCreatedAt') else 0
                if not token_address or age_hours > MAX_TOKEN_AGE_HOURS or token_address in BLACKLISTED_TOKENS or token_address in portfolio:
                    continue
                queue_message(chat_id, f"🔍 Détection DexScreener : `{token_address}` (Solana)")
                logger.info(f"Détection DexScreener: {token_address}")
                await validate_and_trade(chat_id, token_address)
            await asyncio.sleep(5)
        except Exception as e:
            queue_message(chat_id, f"⚠️ Erreur DexScreener: `{str(e)}`")
            logger.error(f"Erreur DexScreener: {str(e)}")
            await asyncio.sleep(10)

@backoff.on_exception(backoff.expo, Exception, max_tries=5)
async def detect_birdeye(chat_id):
    while trade_active:
        try:
            response = session.get(
                f"https://public-api.birdeye.so/public/tokenlist?sort_by=v24hUSD&sort_type=desc",
                headers={"X-API-KEY": BIRDEYE_API_KEY},
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            if not data or 'data' not in data or 'tokens' not in data['data']:
                logger.error(f"Réponse Birdeye invalide: {data}")
                await asyncio.sleep(60)
                continue
            tokens = data['data']['tokens']
            for token in tokens:
                token_address = token.get('address')
                age_hours = (time.time() - token.get('updateTime', time.time()) / 1000) / 3600
                if not token_address or age_hours > MAX_TOKEN_AGE_HOURS or token_address in BLACKLISTED_TOKENS or token_address in portfolio:
                    continue
                queue_message(chat_id, f"🔍 Détection Birdeye : `{token_address}` (Solana)")
                logger.info(f"Détection Birdeye: {token_address}")
                await validate_and_trade(chat_id, token_address)
            await asyncio.sleep(10)
        except Exception as e:
            queue_message(chat_id, f"⚠️ Erreur Birdeye: `{str(e)}`")
            logger.error(f"Erreur Birdeye: {str(e)}")
            await asyncio.sleep(60)

def calculate_buy_amount(liquidity):
    if liquidity < 10000:
        return mise_depart_sol * 0.5
    elif liquidity > 50000:
        return mise_depart_sol * 1.5
    return mise_depart_sol

async def validate_and_trade(chat_id, token_address):
    try:
        if token_address in BLACKLISTED_TOKENS:
            return
        data = get_token_data(token_address)
        if data is None:
            rejected_tokens[token_address] = time.time()
            queue_message(chat_id, f"⚠️ `{token_address}` rejeté : Pas de données ou trop vieux")
            return
        volume_24h = data.get('volume_24h', 0)
        liquidity = data.get('liquidity', 0)
        market_cap = data.get('market_cap', 0)
        buy_sell_ratio = data.get('buy_sell_ratio', 1)
        price = data.get('price', 0)
        age_hours = (time.time() - data.get('pair_created_at', time.time())) / 3600

        if len(portfolio) >= max_positions:
            queue_message(chat_id, f"⚠️ `{token_address}` rejeté : Limite de {max_positions} positions atteinte")
            return
        if age_hours > MAX_TOKEN_AGE_HOURS:
            rejected_tokens[token_address] = time.time()
            queue_message(chat_id, f"⚠️ `{token_address}` rejeté : Âge {age_hours:.2f}h > {MAX_TOKEN_AGE_HOURS}h")
            return
        if liquidity < MIN_LIQUIDITY:
            rejected_tokens[token_address] = time.time()
            queue_message(chat_id, f"⚠️ `{token_address}` rejeté : Liquidité ${liquidity:.2f} < ${MIN_LIQUIDITY}")
            return
        if volume_24h < MIN_VOLUME_SOL or volume_24h > MAX_VOLUME_SOL:
            rejected_tokens[token_address] = time.time()
            queue_message(chat_id, f"⚠️ `{token_address}` rejeté : Volume ${volume_24h:.2f} hors plage [{MIN_VOLUME_SOL}, {MAX_VOLUME_SOL}]")
            return
        if market_cap < MIN_MARKET_CAP_SOL or market_cap > MAX_MARKET_CAP_SOL:
            rejected_tokens[token_address] = time.time()
            queue_message(chat_id, f"⚠️ `{token_address}` rejeté : Market Cap ${market_cap:.2f} hors plage [{MIN_MARKET_CAP_SOL}, {MAX_MARKET_CAP_SOL}]")
            return
        if buy_sell_ratio < MIN_BUY_SELL_RATIO:
            rejected_tokens[token_address] = time.time()
            queue_message(chat_id, f"⚠️ `{token_address}` rejeté : Ratio A/V {buy_sell_ratio:.2f} < {MIN_BUY_SELL_RATIO}")
            return
        if not check_token_security(token_address):
            rejected_tokens[token_address] = time.time()
            queue_message(chat_id, f"⚠️ `{token_address}` rejeté : Sécurité insuffisante")
            return
        if not check_dump_risk(token_address):
            rejected_tokens[token_address] = time.time()
            queue_message(chat_id, f"⚠️ `{token_address}` rejeté : Risque de dump détecté")
            return
        twitter_mentions = await get_twitter_mentions(token_address, chat_id)
        if twitter_mentions < MIN_SOCIAL_MENTIONS:
            rejected_tokens[token_address] = time.time()
            queue_message(chat_id, f"⚠️ `{token_address}` rejeté : Hype insuffisant ({twitter_mentions} mentions)")
            return

        queue_message(chat_id, f"✅ Token validé : `{token_address}` (Solana)")
        logger.info(f"Token validé: {token_address}")
        detected_tokens[token_address] = {
            'address': token_address, 'volume': volume_24h, 'liquidity': liquidity,
            'market_cap': market_cap, 'supply': data['supply'], 'price': price,
            'buy_sell_ratio': buy_sell_ratio
        }
        amount = calculate_buy_amount(liquidity)
        await buy_token_solana(chat_id, token_address, amount)
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur validation `{token_address}`: `{str(e)}`")
        logger.error(f"Erreur validation: {str(e)}")

async def buy_token_solana(chat_id, contract_address, amount):
    try:
        if not solana_keypair:
            initialize_bot(chat_id)
            if not solana_keypair:
                raise Exception("Solana non initialisé")
        amount_in = int(amount * 10**9)
        response = session.post(QUICKNODE_SOL_URL, json={
            "jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash",
            "params": [{"commitment": "finalized"}]
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
        queue_message(chat_id, f"⏳ Achat Solana {amount} SOL : `{contract_address}`, TX: `{tx_hash}`")
        entry_price = detected_tokens[contract_address]['price']
        portfolio[contract_address] = {
            'amount': amount, 'chain': 'solana', 'entry_price': entry_price,
            'market_cap_at_buy': detected_tokens[contract_address]['market_cap'],
            'current_market_cap': detected_tokens[contract_address]['market_cap'],
            'price_history': [entry_price], 'highest_price': entry_price, 'profit': 0.0,
            'buy_time': time.time(), 'alerted_x100': False
        }
        exchange = 'Raydium' if 'Raydium' in contract_address else 'Pump.fun'
        queue_message(chat_id, f"✅ Achat réussi : {amount} SOL de `{contract_address}` ({exchange})")
        daily_trades['buys'].append({'token': contract_address, 'amount': amount, 'timestamp': datetime.now().strftime('%H:%M:%S')})
    except Exception as e:
        queue_message(chat_id, f"⚠️ Échec achat Solana `{contract_address}`: `{str(e)}`")
        logger.error(f"Échec achat Solana: {str(e)}")

def get_dynamic_trailing_stop(profit_pct):
    if profit_pct < 1000:
        return 5
    elif profit_pct < 10000:
        return 10
    else:
        return 20

async def sell_token(chat_id, contract_address, amount, current_price):
    global mise_depart_sol
    try:
        if not solana_keypair:
            initialize_bot(chat_id)
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
            program_id=RAYDIUM_PROGRAM_ID if 'Raydium' in contract_address else PUMP_FUN_PROGRAM_ID,
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
        queue_message(chat_id, f"⏳ Vente Solana {amount} SOL : `{contract_address}`, TX: `{tx_hash}`")
        profit = (current_price - portfolio[contract_address]['entry_price']) * amount
        portfolio[contract_address]['profit'] += profit
        portfolio[contract_address]['amount'] -= amount
        profit_pct = (current_price - portfolio[contract_address]['entry_price']) / portfolio[contract_address]['entry_price'] * 100
        reinvest_amount = profit * (0.95 if profit_pct > 10000 else profit_reinvestment_ratio)
        mise_depart_sol += reinvest_amount
        exchange = 'Raydium' if 'Raydium' in contract_address else 'Pump.fun'
        queue_message(chat_id, f"✅ Vente réussie : {amount} SOL, Profit: {profit:.4f} SOL, Réinvesti: {reinvest_amount:.4f} SOL ({exchange})")
        daily_trades['sells'].append({'token': contract_address, 'amount': amount, 'pnl': profit, 'timestamp': datetime.now().strftime('%H:%M:%S')})
        if portfolio[contract_address]['amount'] <= 0:
            del portfolio[contract_address]
    except Exception as e:
        queue_message(chat_id, f"⚠️ Échec vente Solana `{contract_address}`: `{str(e)}`")
        logger.error(f"Échec vente Solana: {str(e)}")

async def sell_token_percentage(chat_id, token, percentage):
    try:
        if token not in portfolio:
            queue_message(chat_id, f"⚠️ Vente impossible : `{token}` n'est pas dans le portefeuille")
            return
        total_amount = portfolio[token]['amount']
        amount_to_sell = total_amount * (percentage / 100)
        current_price = get_token_data(token).get('market_cap', 0) / detected_tokens.get(token, {}).get('supply', 1)
        await sell_token(chat_id, token, amount_to_sell, current_price)
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur vente partielle `{token}`: `{str(e)}`")
        logger.error(f"Erreur vente partielle: {str(e)}")

async def monitor_and_sell(chat_id):
    while trade_active:
        try:
            if not portfolio:
                await asyncio.sleep(2)
                continue
            for contract_address, data in list(portfolio.items()):
                amount = data['amount']
                current_data = get_token_data(contract_address)
                if not current_data:
                    continue
                current_mc = current_data.get('market_cap', 0)
                current_price = current_mc / data['supply'] if 'supply' in data and data['supply'] > 0 else 0
                data['price_history'].append(current_price)
                if len(data['price_history']) > 10:
                    data['price_history'].pop(0)
                portfolio[contract_address]['current_market_cap'] = current_mc
                profit_pct = (current_price - data['entry_price']) / data['entry_price'] * 100 if data['entry_price'] > 0 else 0
                loss_pct = -profit_pct if profit_pct < 0 else 0
                data['highest_price'] = max(data['highest_price'], current_price)
                trailing_stop_percentage_temp = get_dynamic_trailing_stop(profit_pct)
                trailing_stop_price = data['highest_price'] * (1 - trailing_stop_percentage_temp / 100)

                if profit_pct >= 10000 and not data.get('alerted_x100', False):
                    queue_message(chat_id, f"🚀 Pump exceptionnel sur `{contract_address}`: +{profit_pct:.2f}% (x{profit_pct/100:.1f}) - Désactiver ventes auto ? (/pause)")
                    data['alerted_x100'] = True

                if not pause_auto_sell:
                    if profit_pct >= take_profit_steps[4] * 100:
                        await sell_token(chat_id, contract_address, amount * 0.5, current_price)
                        queue_message(chat_id, f"💰 Vente 50% à x500 : `{contract_address}` (+{profit_pct:.2f}%)")
                    elif profit_pct >= take_profit_steps[3] * 100:
                        await sell_token(chat_id, contract_address, amount * 0.25, current_price)
                    elif profit_pct >= take_profit_steps[2] * 100:
                        await sell_token(chat_id, contract_address, amount * 0.2, current_price)
                    elif profit_pct >= take_profit_steps[1] * 100:
                        await sell_token(chat_id, contract_address, amount * 0.15, current_price)
                    elif profit_pct >= take_profit_steps[0] * 100:
                        await sell_token(chat_id, contract_address, amount * 0.1, current_price)
                    elif current_price <= trailing_stop_price or loss_pct >= stop_loss_threshold or current_data.get('buy_sell_ratio', 1) < 0.5:
                        await sell_token(chat_id, contract_address, amount, current_price)
            await asyncio.sleep(0.5)
        except Exception as e:
            queue_message(chat_id, f"⚠️ Erreur surveillance: `{str(e)}`")
            logger.error(f"Erreur surveillance: {str(e)}")
            await asyncio.sleep(5)

async def show_portfolio(chat_id):
    try:
        sol_balance = await get_solana_balance(chat_id)
        msg = f"💰 *Portefeuille:*\nSOL : {sol_balance:.4f}\n\n"
        markup = InlineKeyboardMarkup()
        for ca, data in portfolio.items():
            current_mc = get_token_data(ca).get('market_cap', 0)
            profit = (current_mc - data['market_cap_at_buy']) / data['market_cap_at_buy'] * 100 if data['market_cap_at_buy'] > 0 else 0
            markup.add(
                InlineKeyboardButton(f"💸 Sell 25% {ca[:6]}", callback_data=f"sell_pct_{ca}_25"),
                InlineKeyboardButton(f"💸 Sell 50% {ca[:6]}", callback_data=f"sell_pct_{ca}_50"),
                InlineKeyboardButton(f"💸 Sell 100% {ca[:6]}", callback_data=f"sell_{ca}")
            )
            msg += (
                f"*Token:* `{ca}` (Solana)\n"
                f"MC Achat: ${data['market_cap_at_buy']:.2f}\n"
                f"MC Actuel: ${current_mc:.2f}\n"
                f"Profit: {profit:.2f}%\n"
                f"Profit cumulé: {data['profit']:.4f} SOL\n\n"
            )
        queue_message(chat_id, msg, reply_markup=markup if portfolio else None)
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur portefeuille: `{str(e)}`")
        logger.error(f"Erreur portefeuille: {str(e)}")

async def get_solana_balance(chat_id):
    try:
        if not solana_keypair:
            initialize_bot(chat_id)
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

async def sell_token_immediate(chat_id, token):
    try:
        if token not in portfolio:
            queue_message(chat_id, f"⚠️ Vente impossible : `{token}` n'est pas dans le portefeuille")
            return
        amount = portfolio[token]['amount']
        current_price = get_token_data(token).get('market_cap', 0) / detected_tokens.get(token, {}).get('supply', 1)
        await sell_token(chat_id, token, amount, current_price)
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur vente immédiate `{token}`: `{str(e)}`")
        logger.error(f"Erreur vente immédiate: {str(e)}")

async def show_daily_summary(chat_id):
    try:
        msg = f"📅 *Récapitulatif du jour ({datetime.now().strftime('%Y-%m-%d')})*:\n\n"
        msg += "📈 *Achats* :\n"
        total_buys = 0
        for trade in daily_trades['buys']:
            total_buys += trade['amount']
            msg += f"- `{trade['token']}` : {trade['amount']} SOL à {trade['timestamp']}\n"
        msg += f"Total investi : {total_buys:.4f} SOL\n\n"
        msg += "📉 *Ventes* :\n"
        total_profit = 0
        for trade in daily_trades['sells']:
            total_profit += trade['pnl']
            msg += f"- `{trade['token']}` : {trade['amount']} SOL à {trade['timestamp']}, PNL: {trade['pnl']:.4f} SOL\n"
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
        queue_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 0.37)")
    await show_main_menu(chat_id)

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
        queue_message(chat_id, "⚠️ Erreur : Entrez un pourcentage valide (ex. : 15)")
    await show_main_menu(chat_id)

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
    await show_main_menu(chat_id)

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
    await show_main_menu(chat_id)

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
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur initialisation: `{str(e)}`")
        logger.error(f"Erreur initialisation: {str(e)}")
        trade_active = False

@app.route("/webhook", methods=['POST'])
def webhook():
    global trade_active
    if request.method == "POST" and request.headers.get("content-type") == "application/json":
        update_json = request.get_json()
        logger.info(f"Webhook reçu: {json.dumps(update_json)}")
        try:
            update = telebot.types.Update.de_json(update_json)
            bot.process_new_updates([update])
            if update.message and update.message.text == "/start" and not trade_active:
                initialize_and_run_threads(update.message.chat.id)
            return 'OK', 200
        except Exception as e:
            logger.error(f"Erreur traitement webhook: {str(e)}")
            return f"Erreur: {str(e)}", 500
    logger.error("Requête webhook invalide")
    return abort(403)

@bot.message_handler(commands=['start'])
def start_message(message):
    global last_start_time
    chat_id = message.chat.id
    current_time = time.time()
    with start_lock:
        if current_time - last_start_time < 1:
            return
        last_start_time = current_time
        logger.info(f"Commande /start reçue de {chat_id}")
        queue_message(chat_id, "✅ Bot démarré!")
        asyncio.run_coroutine_threadsafe(show_main_menu(chat_id), asyncio.get_event_loop())

@bot.message_handler(commands=['menu'])
def menu_message(message):
    chat_id = message.chat.id
    logger.info(f"Commande /menu reçue de {chat_id}")
    asyncio.run_coroutine_threadsafe(show_main_menu(chat_id), asyncio.get_event_loop())

@bot.message_handler(commands=['stop'])
def stop_message(message):
    global trade_active, active_threads
    chat_id = message.chat.id
    logger.info(f"Commande /stop reçue de {chat_id}")
    trade_active = False
    for thread in active_threads:
        if thread.is_alive():
            logger.info(f"Arrêt du thread {thread.name}")
            # Les threads daemon s'arrêtent automatiquement à la fin du programme
    active_threads = []
    while not message_queue.empty():  # Vider la file d'attente
        message_queue.get()
    queue_message(chat_id, "⏹️ Trading arrêté.")
    logger.info("Trading arrêté, threads réinitialisés")

@bot.message_handler(commands=['pause'])
def pause_auto_sell_handler(message):
    global pause_auto_sell
    chat_id = message.chat.id
    logger.info(f"Commande /pause reçue de {chat_id}")
    pause_auto_sell = True
    queue_message(chat_id, "⏸️ Ventes automatiques désactivées. Utilisez /resume pour réactiver.")

@bot.message_handler(commands=['resume'])
def resume_auto_sell_handler(message):
    global pause_auto_sell
    chat_id = message.chat.id
    logger.info(f"Commande /resume reçue de {chat_id}")
    pause_auto_sell = False
    queue_message(chat_id, "▶️ Ventes automatiques réactivées.")

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

@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    global trade_active
    chat_id = call.message.chat.id
    logger.info(f"Callback reçu: {call.data} de {chat_id}")
    try:
        if call.data == "status":
            queue_message(chat_id, (
                f"ℹ️ *Statut actuel* :\n"
                f"Trading actif: {'Oui' if trade_active else 'Non'}\n"
                f"Mise Solana: {mise_depart_sol} SOL\n"
                f"Positions: {len(portfolio)}/{max_positions}\n"
                f"Stop-Loss: {stop_loss_threshold}%\n"
                f"Take-Profit: x{take_profit_steps[0]}, x{take_profit_steps[1]}, x{take_profit_steps[2]}, x{take_profit_steps[3]}, x{take_profit_steps[4]}"
            ))
        elif call.data == "launch":
            if not trade_active:
                initialize_and_run_threads(chat_id)
            else:
                queue_message(chat_id, "⚠️ Trading déjà en cours.")
        elif call.data == "stop":
            global active_threads
            trade_active = False
            for thread in active_threads:
                if thread.is_alive():
                    logger.info(f"Arrêt du thread {thread.name}")
            active_threads = []
            while not message_queue.empty():
                message_queue.get()
            queue_message(chat_id, "⏹️ Trading arrêté.")
        elif call.data == "portfolio":
            asyncio.run_coroutine_threadsafe(show_portfolio(chat_id), asyncio.get_event_loop())
        elif call.data == "daily_summary":
            asyncio.run_coroutine_threadsafe(show_daily_summary(chat_id), asyncio.get_event_loop())
        elif call.data == "adjust_mise_sol":
            queue_message(chat_id, "Entrez la nouvelle mise Solana (en SOL, ex. : 0.37) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_mise_sol)
        elif call.data == "adjust_stop_loss":
            queue_message(chat_id, "Entrez le nouveau seuil de Stop-Loss (en %, ex. : 15) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_stop_loss)
        elif call.data == "adjust_take_profit":
            queue_message(chat_id, "Entrez les nouveaux seuils de Take-Profit (5 valeurs, ex. : 1.2,2,10,100,500) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_take_profit)
        elif call.data == "adjust_reinvestment":
            queue_message(chat_id, "Entrez le nouveau ratio de réinvestissement (0-1, ex. : 0.9) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_reinvestment_ratio)
        elif call.data.startswith("sell_"):
            token = call.data.split("_")[1]
            asyncio.run_coroutine_threadsafe(sell_token_immediate(chat_id, token), asyncio.get_event_loop())
        elif call.data.startswith("sell_pct_"):
            _, token, pct = call.data.split("_")
            asyncio.run_coroutine_threadsafe(sell_token_percentage(chat_id, token, float(pct)), asyncio.get_event_loop())
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur générale: `{str(e)}`")
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
