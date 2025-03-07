import os
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import logging
import requests
import time
import json
import base58
from flask import Flask, request, abort
from web3 import Web3
from web3.middleware import geth_poa_middleware
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
from telethon import TelegramClient, events
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
pause_auto_sell = False
portfolio = {}
detected_tokens = {}
executor_futures = []  # Pour gérer les threads
BLACKLISTED_TOKENS = {"So11111111111111111111111111111111111111112"}

# Variables d’environnement
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "default_token")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "0x0")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "dummy_key")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://default.example.com/webhook")
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY", "dummy_solana_key")
BSCSCAN_API_KEY = os.getenv("BSCSCAN_API_KEY", "3F24ENAM4DHUTN3PFCRDWAIY53T1XACS22")
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY", "5be903b581bc47d29bbfb5ab859de2eb")
TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID", "your_telegram_api_id")
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "your_telegram_api_hash")
TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN", "AAAAAAAAAAAAAAAAAAAAAD6%2BzQEAAAAAaDN4Thznh7iGRdfqEhebMgWtohs%3DyuaSpNWBCnPcQv5gjERphqmZTIclzPiVqqnirPmdZt4fpRd96D")
QUICKNODE_BSC_URL = "https://smart-necessary-ensemble.bsc.quiknode.pro/aeb370bcf4299bc365bbbd3d14d19a31f6e46f06/"
QUICKNODE_ETH_URL = "https://side-cold-diamond.quiknode.pro/698f06abfe4282fc22edbab42297cf468d78070f/"
QUICKNODE_SOL_URL = "https://little-maximum-glade.solana-mainnet.quiknode.pro/5da088be927d31731b0d7284c30a0640d8e4dd50/"
QUICKNODE_BSC_WS_URL = "wss://smart-necessary-ensemble.bsc.quiknode.pro/aeb370bcf4299bc365bbbd3d14d19a31f6e46f06/"
QUICKNODE_ETH_WS_URL = "wss://side-cold-diamond.quiknode.pro/698f06abfe4282fc22edbab42297cf468d78070f/"
QUICKNODE_SOL_WS_URL = "wss://little-maximum-glade.solana-mainnet.quiknode.pro/5da088be927d31731b0d7284c30a0640d8e4dd50/"
PORT = int(os.getenv("PORT", 8080))

app = Flask(__name__)
bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=False)
telegram_client = TelegramClient('session_name', TELEGRAM_API_ID, TELEGRAM_API_HASH) if TELEGRAM_API_ID and TELEGRAM_API_HASH else None

w3_bsc = None
w3_eth = None
solana_keypair = None

# Paramètres de trading
mise_depart_bsc = 0.02
mise_depart_sol = 0.37
mise_depart_eth = 0.05
gas_fee = 15
stop_loss_threshold = 15
trailing_stop_percentage = 5
take_profit_steps = [1.2, 2, 10, 100, 500]
max_positions = 5
profit_reinvestment_ratio = 0.9
slippage_max = 0.05

MIN_VOLUME_SOL = 100
MAX_VOLUME_SOL = 2000000
MIN_VOLUME_BSC = 100
MAX_VOLUME_BSC = 2000000
MIN_VOLUME_ETH = 100
MAX_VOLUME_ETH = 2000000
MIN_LIQUIDITY = 5000
MIN_MARKET_CAP_SOL = 1000
MAX_MARKET_CAP_SOL = 5000000
MIN_MARKET_CAP_BSC = 1000
MAX_MARKET_CAP_BSC = 5000000
MIN_MARKET_CAP_ETH = 1000
MAX_MARKET_CAP_ETH = 5000000
MIN_BUY_SELL_RATIO = 1.5
MAX_TOKEN_AGE_HOURS = 6
MIN_SOCIAL_MENTIONS = 5

# Constantes des exchanges
ERC20_ABI = json.loads('[{"constant": true, "inputs": [], "name": "totalSupply", "outputs": [{"name": "", "type": "uint256"}], "payable": false, "stateMutability": "view", "type": "function"}]')
PANCAKE_ROUTER_ADDRESS = "0x10ED43C718714eb63d5aA57B78B54704E256024E"
PANCAKE_FACTORY_ADDRESS = "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"
UNISWAP_ROUTER_ADDRESS = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"
UNISWAP_FACTORY_ADDRESS = "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f"
PANCAKE_ROUTER_ABI = json.loads('[{"inputs": [{"internalType": "uint256", "name": "amountIn", "type": "uint256"}, {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"}, {"internalType": "address[]", "name": "path", "type": "address[]"}, {"internalType": "address", "name": "to", "type": "address"}, {"internalType": "uint256", "name": "deadline", "type": "uint256"}], "name": "swapExactETHForTokens", "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}], "stateMutability": "payable", "type": "function"}, {"inputs": [{"internalType": "uint256", "name": "amountOut", "type": "uint256"}, {"internalType": "uint256", "name": "amountInMax", "type": "uint256"}, {"internalType": "address[]", "name": "path", "type": "address[]"}, {"internalType": "address", "name": "to", "type": "address"}, {"internalType": "uint256", "name": "deadline", "type": "uint256"}], "name": "swapExactTokensForETH", "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}], "stateMutability": "nonpayable", "type": "function"}]')
PANCAKE_FACTORY_ABI = json.loads('[{"anonymous":false,"inputs":[{"indexed":true,"internalType":"address","name":"token0","type":"address"},{"indexed":true,"internalType":"address","name":"token1","type":"address"},{"indexed":false,"internalType":"address","name":"pair","type":"address"},{"indexed":false,"internalType":"uint256","name":"","type":"uint256"}],"name":"PairCreated","type":"event"}]')
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
    message_queue.put((chat_id, text, reply_markup) if reply_markup else (chat_id, text))

def initialize_bot(chat_id):
    global w3_bsc, w3_eth, solana_keypair
    try:
        w3_bsc = Web3(Web3.HTTPProvider(QUICKNODE_BSC_URL))
        w3_bsc.middleware_onion.inject(geth_poa_middleware, layer=0)
        if w3_bsc.is_connected():
            queue_message(chat_id, "✅ Connexion BSC établie : QuickNode")
            logger.info("BSC connecté")
        else:
            raise Exception("Échec connexion BSC")
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur connexion BSC: {str(e)}")
        logger.error(f"Erreur BSC: {str(e)}")
        w3_bsc = None

    try:
        w3_eth = Web3(Web3.HTTPProvider(QUICKNODE_ETH_URL))
        if w3_eth.is_connected():
            queue_message(chat_id, "✅ Connexion Ethereum établie : QuickNode")
            logger.info("Ethereum connecté")
        else:
            raise Exception("Échec connexion Ethereum")
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur connexion Ethereum: {str(e)}")
        logger.error(f"Erreur Ethereum: {str(e)}")
        w3_eth = None

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

def validate_address(token_address, chain):
    if chain in ['bsc', 'eth']:
        return bool(re.match(r'^0x[a-fA-F0-9]{40}$', token_address))
    elif chain == 'solana':
        try:
            Pubkey.from_string(token_address)
            return len(token_address) >= 32 and len(token_address) <= 44
        except ValueError:
            return False
    return False

def get_dynamic_gas_price(chain):
    w3 = w3_bsc if chain == 'bsc' else w3_eth if chain == 'eth' else None
    if w3 and w3.is_connected():
        return w3.eth.gas_price * 1.2
    return w3.to_wei(15, 'gwei')

def check_mev_risk(chain):
    w3 = w3_bsc if chain == 'bsc' else w3_eth if chain == 'eth' else None
    if w3 and w3.is_connected():
        latest_block = w3.eth.get_block('latest', full_transactions=True)
        for tx in latest_block['transactions']:
            if 'to' in tx and tx['to'] in [PANCAKE_ROUTER_ADDRESS, UNISWAP_ROUTER_ADDRESS] and tx['gasPrice'] > w3.eth.gas_price * 2:
                return False
    return True

def calculate_buy_amount(chain, liquidity):
    base_amount = mise_depart_bsc if chain == 'bsc' else mise_depart_eth if chain == 'eth' else mise_depart_sol
    if liquidity < 10000:
        return base_amount * 0.5
    elif liquidity > 50000:
        return base_amount * 1.5
    return base_amount

@backoff.on_exception(backoff.expo, Exception, max_tries=3)
def get_token_data(token_address, chain):
    for _ in range(3):
        try:
            response = session.get(f"https://api.dexscreener.com/latest/dex/tokens/{token_address}", timeout=15)
            response.raise_for_status()
            pairs = response.json().get('pairs', [])
            if not pairs or pairs[0].get('chainId') != chain:
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
                'pair_created_at': data.get('pairCreatedAt', 0) / 1000 if data.get('pairCreatedAt') else time.time()
            }
        except Exception as e:
            logger.error(f"Erreur DexScreener pour {token_address}: {str(e)}")
            time.sleep(1)
    w3 = w3_bsc if chain == 'bsc' else w3_eth if chain == 'eth' else None
    if w3 and w3.is_connected():
        try:
            contract = w3.eth.contract(address=w3.to_checksum_address(token_address), abi=ERC20_ABI)
            supply = contract.functions.totalSupply().call() / 10**18
            return {'volume_24h': 0, 'liquidity': MIN_LIQUIDITY, 'market_cap': 0, 'price': 0, 'buy_sell_ratio': 1, 'pair_created_at': time.time()}
        except:
            pass
    return None

@backoff.on_exception(backoff.expo, Exception, max_tries=5)
def check_token_security(token_address, chain):
    api_url = f"https://api.gopluslabs.io/api/v1/token_security/{chain}?contract_addresses={token_address}"
    response = session.get(api_url, timeout=5).json()
    data = response.get('result', {}).get(token_address.lower(), {})
    taxes = float(data.get('buy_tax', 0)) + float(data.get('sell_tax', 0))
    top_holder_pct = float(data.get('holder_percent_top_1', 0))
    is_locked = data.get('is_liquidity_locked', '0') == '1'
    is_honeypot = data.get('is_honeypot', '0') == '1'
    creator = data.get('creator_address', '')
    if creator and (creator.startswith('0x') and len(creator) == 42):
        w3 = w3_bsc if chain == 'bsc' else w3_eth if chain == 'eth' else None
        if w3 and w3.eth.get_code(w3.to_checksum_address(creator)) != '0x':
            return False
    return taxes < 0.03 and top_holder_pct < 0.15 and is_locked and not is_honeypot

@backoff.on_exception(backoff.expo, Exception, max_tries=3)
def check_dump_risk(token_address, chain):
    try:
        url = QUICKNODE_BSC_URL if chain == 'bsc' else QUICKNODE_ETH_URL if chain == 'eth' else QUICKNODE_SOL_URL
        response = session.post(url, json={
            "jsonrpc": "2.0", "id": 1, "method": "getTransactionHistory",
            "params": [token_address, {"limit": 50, "timeframe": "1h"}]
        }, timeout=5)
        txs = response.json().get('result', [])
        total_sold = 0
        for tx in txs:
            if tx.get('type') == 'sell':
                total_sold += tx.get('amount', 0) / 10**18
        supply = get_token_data(token_address, chain).get('supply', 1)
        if total_sold / supply > 0.05:
            return False
        return True
    except Exception as e:
        logger.error(f"Erreur check_dump_risk pour {token_address}: {str(e)}")
        return True

@backoff.on_exception(backoff.expo, Exception, max_tries=5)
async def get_twitter_mentions(token_address, chat_id):
    global last_twitter_request_time
    try:
        current_time = time.time()
        if 'last_twitter_request_time' not in globals() or (current_time - last_twitter_request_time) >= 1800:  # 30min
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
    except requests.exceptions.HTTPError as e:
        queue_message(chat_id, f"⚠️ Erreur Twitter API: {e.response.status_code} - {e.response.text}")
        logger.error(f"Twitter API: {e.response.status_code} - {e.response.text}")
        return MIN_SOCIAL_MENTIONS
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur Twitter API: {str(e)}")
        logger.error(f"Erreur Twitter: {str(e)}")
        return MIN_SOCIAL_MENTIONS

@backoff.on_exception(backoff.expo, Exception, max_tries=5)
async def snipe_new_pairs_bsc(chat_id):
    while trade_active:
        if not w3_bsc or not w3_bsc.is_connected():
            initialize_bot(chat_id)
            if not w3_bsc:
                await asyncio.sleep(30)
                continue
        try:
            factory = w3_bsc.eth.contract(address=PANCAKE_FACTORY_ADDRESS, abi=PANCAKE_FACTORY_ABI)
            latest_block = w3_bsc.eth.block_number
            from_block = max(latest_block - 200, 0)
            events = factory.events.PairCreated.get_logs(fromBlock=from_block, toBlock=latest_block)
            queue_message(chat_id, f"🔄 Sniping BSC actif (PancakeSwap) - {len(events)} paires détectées")
            for event in events:
                block_time = w3_bsc.eth.get_block(event['blockNumber'])['timestamp']
                if (time.time() - block_time) / 3600 > MAX_TOKEN_AGE_HOURS:
                    continue
                token0 = w3_bsc.to_checksum_address(event['args']['token0'])
                token1 = w3_bsc.to_checksum_address(event['args']['token1'])
                token_address = token0 if token0 != "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c" else token1
                if token_address in BLACKLISTED_TOKENS or token_address in portfolio or (token_address in rejected_tokens and (time.time() - rejected_tokens[token_address]) / 3600 <= 6):
                    continue
                queue_message(chat_id, f'🎯 Snipe détecté : {token_address} (BSC - PancakeSwap)')
                logger.info(f"Snipe BSC: {token_address}")
                await validate_and_trade(chat_id, token_address, 'bsc')
            await asyncio.sleep(1)
        except Exception as e:
            queue_message(chat_id, f"⚠️ Erreur sniping BSC: {str(e)}")
            logger.error(f"Erreur sniping BSC: {str(e)}")
            await asyncio.sleep(10)

@backoff.on_exception(backoff.expo, Exception, max_tries=5)
async def snipe_new_pairs_eth(chat_id):
    while trade_active:
        if not w3_eth or not w3_eth.is_connected():
            initialize_bot(chat_id)
            if not w3_eth:
                await asyncio.sleep(30)
                continue
        try:
            factory = w3_eth.eth.contract(address=UNISWAP_FACTORY_ADDRESS, abi=PANCAKE_FACTORY_ABI)
            latest_block = w3_eth.eth.block_number
            from_block = max(latest_block - 200, 0)
            events = factory.events.PairCreated.get_logs(fromBlock=from_block, toBlock=latest_block)
            queue_message(chat_id, f"🔄 Sniping Ethereum actif (Uniswap) - {len(events)} paires détectées")
            for event in events:
                block_time = w3_eth.eth.get_block(event['blockNumber'])['timestamp']
                if (time.time() - block_time) / 3600 > MAX_TOKEN_AGE_HOURS:
                    continue
                token0 = w3_eth.to_checksum_address(event['args']['token0'])
                token1 = w3_eth.to_checksum_address(event['args']['token1'])
                token_address = token0 if token0 != "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2" else token1
                if token_address in BLACKLISTED_TOKENS or token_address in portfolio or (token_address in rejected_tokens and (time.time() - rejected_tokens[token_address]) / 3600 <= 6):
                    continue
                queue_message(chat_id, f'🎯 Snipe détecté : {token_address} (Ethereum - Uniswap)')
                logger.info(f"Snipe Ethereum: {token_address}")
                await validate_and_trade(chat_id, token_address, 'eth')
            await asyncio.sleep(1)
        except Exception as e:
            queue_message(chat_id, f"⚠️ Erreur sniping Ethereum: {str(e)}")
            logger.error(f"Erreur sniping Ethereum: {str(e)}")
            await asyncio.sleep(10)

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
                        logger.info(f"Données Solana WebSocket: {msg}")
                        if 'result' not in msg or 'params' not in msg:
                            continue
                        data = msg['params']['result']['value']['account']['data'][0]
                        for acc in data.split():
                            if validate_address(acc, 'solana') and acc not in BLACKLISTED_TOKENS and acc not in portfolio:
                                token_address = acc
                                response = session.post(QUICKNODE_SOL_URL, json={
                                    "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo", "params": [token_address]
                                }, timeout=5)
                                account_info = response.json().get('result', {}).get('value', {})
                                if account_info and (time.time() - account_info.get('lamports', 0)) / 3600 <= MAX_TOKEN_AGE_HOURS:
                                    exchange = 'Raydium' if str(RAYDIUM_PROGRAM_ID) in msg['params']['result']['pubkey'] else 'Pump.fun'
                                    queue_message(chat_id, f'🎯 Snipe détecté : {token_address} (Solana - {exchange})')
                                    logger.info(f"Snipe Solana: {token_address}")
                                    await validate_and_trade(chat_id, token_address, 'solana')
                    except Exception as e:
                        logger.error(f"Erreur sniping Solana WebSocket: {str(e)}")
                    await asyncio.sleep(0.01)
        except Exception as e:
            queue_message(chat_id, f"⚠️ Erreur sniping Solana: {str(e)}")
            logger.error(f"Erreur sniping Solana connexion: {str(e)}")
            await asyncio.sleep(5)

@backoff.on_exception(backoff.expo, Exception, max_tries=5)
async def detect_pump_fun(chat_id):
    queue_message(chat_id, "⚠️ Pump.fun API désactivée : endpoint 404")
    logger.info("Pump.fun désactivé en attendant un endpoint valide")
    return

@backoff.on_exception(backoff.expo, Exception, max_tries=5)
async def detect_birdeye(chat_id):
    try:
        response = session.get(
            f"https://public-api.birdeye.so/v1/token/list?sort_by=mc&sort_type=desc&limit=50",
            headers={"X-API-KEY": BIRDEYE_API_KEY},
            timeout=15
        )
        response.raise_for_status()
        data = response.json()
        logger.info(f"Birdeye réponse: {data}")
        if not data or 'data' not in data or 'tokens' not in data['data']:
            logger.error(f"Réponse Birdeye invalide: {data}")
            await asyncio.sleep(10)
            return
        tokens = data['data']['tokens']
        for token in tokens:
            token_address = token.get('address')
            age_hours = (time.time() - token.get('created_at', time.time()) / 1000) / 3600
            if not token_address or age_hours > MAX_TOKEN_AGE_HOURS or token_address in BLACKLISTED_TOKENS or token_address in portfolio:
                continue
            queue_message(chat_id, f'🔍 Détection Birdeye : {token_address} (Solana)')
            logger.info(f"Détection Birdeye: {token_address}")
            await validate_and_trade(chat_id, token_address, 'solana')
        await asyncio.sleep(5)
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur Birdeye: {str(e)}")
        logger.error(f"Erreur Birdeye: {str(e)}")
        await asyncio.sleep(10)

async def detect_bsc_blocks(chat_id):
    last_block = 0
    while trade_active:
        try:
            if not w3_bsc or not w3_bsc.is_connected():
                initialize_bot(chat_id)
                if not w3_bsc:
                    await asyncio.sleep(30)
                    continue
            latest_block = w3_bsc.eth.block_number
            if last_block == 0:
                last_block = max(latest_block - 200, 0)
            for block_num in range(last_block + 1, latest_block + 1):
                block = w3_bsc.eth.get_block(block_num, full_transactions=True)
                block_time = block['timestamp']
                if (time.time() - block_time) / 3600 > MAX_TOKEN_AGE_HOURS:
                    continue
                for tx in block['transactions']:
                    if 'to' in tx and tx['to'] and validate_address(tx['to'], 'bsc'):
                        token_address = tx['to']
                        if token_address in BLACKLISTED_TOKENS or token_address in portfolio or (token_address in rejected_tokens and (time.time() - rejected_tokens[token_address]) / 3600 <= 6):
                            continue
                        queue_message(chat_id, f'🔍 Détection bloc : {token_address} (BSC)')
                        logger.info(f"Détection bloc BSC: {token_address}")
                        await validate_and_trade(chat_id, token_address, 'bsc')
            last_block = latest_block
            await asyncio.sleep(1)
        except Exception as e:
            queue_message(chat_id, f"⚠️ Erreur détection blocs BSC: {str(e)}")
            logger.error(f"Erreur blocs BSC: {str(e)}")
            await asyncio.sleep(5)

async def detect_eth_blocks(chat_id):
    last_block = 0
    while trade_active:
        try:
            if not w3_eth or not w3_eth.is_connected():
                initialize_bot(chat_id)
                if not w3_eth:
                    await asyncio.sleep(30)
                    continue
            latest_block = w3_eth.eth.block_number
            if last_block == 0:
                last_block = max(latest_block - 200, 0)
            for block_num in range(last_block + 1, latest_block + 1):
                block = w3_eth.eth.get_block(block_num, full_transactions=True)
                block_time = block['timestamp']
                if (time.time() - block_time) / 3600 > MAX_TOKEN_AGE_HOURS:
                    continue
                for tx in block['transactions']:
                    if 'to' in tx and tx['to'] and validate_address(tx['to'], 'eth'):
                        token_address = tx['to']
                        if token_address in BLACKLISTED_TOKENS or token_address in portfolio or (token_address in rejected_tokens and (time.time() - rejected_tokens[token_address]) / 3600 <= 6):
                            continue
                        queue_message(chat_id, f'🔍 Détection bloc : {token_address} (Ethereum)')
                        logger.info(f"Détection bloc Ethereum: {token_address}")
                        await validate_and_trade(chat_id, token_address, 'eth')
            last_block = latest_block
            await asyncio.sleep(1)
        except Exception as e:
            queue_message(chat_id, f"⚠️ Erreur détection blocs Ethereum: {str(e)}")
            logger.error(f"Erreur blocs Ethereum: {str(e)}")
            await asyncio.sleep(5)

@backoff.on_exception(backoff.expo, Exception, max_tries=5)
async def detect_bscscan(chat_id):
    last_timestamp = int(time.time()) - 600
    while trade_active:
        try:
            response = session.get(
                f"https://api.bscscan.com/api?module=account&action=tokentx&sort=desc&apikey={BSCSCAN_API_KEY}&startblock=0&endblock=99999999",
                timeout=5
            )
            response.raise_for_status()
            txs = response.json().get('result', [])
            for tx in txs:
                if 'timeStamp' not in tx or 'contractAddress' not in tx:
                    continue
                timestamp = int(tx['timeStamp'])
                if timestamp <= last_timestamp or (time.time() - timestamp) / 3600 > MAX_TOKEN_AGE_HOURS:
                    continue
                token_address = tx['contractAddress']
                if token_address in BLACKLISTED_TOKENS or token_address in portfolio or (token_address in rejected_tokens and (time.time() - rejected_tokens[token_address]) / 3600 <= 6):
                    continue
                queue_message(chat_id, f'🔍 Détection BscScan : {token_address} (BSC)')
                logger.info(f"Détection BscScan: {token_address}")
                await validate_and_trade(chat_id, token_address, 'bsc')
            last_timestamp = int(time.time())
            await asyncio.sleep(5)
        except Exception as e:
            queue_message(chat_id, f"⚠️ Erreur BscScan: {str(e)}")
            logger.error(f"Erreur BscScan: {str(e)}")
            await asyncio.sleep(10)

@backoff.on_exception(backoff.expo, Exception, max_tries=5)
async def monitor_twitter(chat_id):
    headers = {"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"}
    while trade_active:
        try:
            response = session.get(
                "https://api.twitter.com/2/tweets/search/recent",
                headers=headers,
                params={"query": '"contract address" OR CA OR launch OR pump', "max_results": 100},
                timeout=5
            )
            response.raise_for_status()
            tweets = response.json().get('data', [])
            for tweet in tweets:
                try:
                    created_at = datetime.strptime(tweet.get('created_at', ''), '%Y-%m-%dT%H:%M:%SZ')
                    age_hours = (datetime.now() - created_at).total_seconds() / 3600
                    if age_hours > MAX_TOKEN_AGE_HOURS:
                        continue
                except ValueError:
                    continue
                followers = tweet.get('user', {}).get('followers_count', 0)
                if followers >= 500:
                    text = tweet['text'].lower()
                    words = text.split()
                    for word in words:
                        chain = 'bsc' if word.startswith("0x") and len(word) == 42 else 'solana' if len(word) in [32, 44] else 'eth'
                        if word in BLACKLISTED_TOKENS or word in portfolio or (word in rejected_tokens and (time.time() - rejected_tokens[word]) / 3600 <= 6):
                            continue
                        if validate_address(word, chain):
                            queue_message(chat_id, f'🔍 Détection Twitter (@{tweet["user"]["username"]}, {followers} abonnés): {word} ({chain})')
                            logger.info(f"Détection Twitter: {word}")
                            await validate_and_trade(chat_id, word, chain)
            await asyncio.sleep(3)
        except requests.exceptions.HTTPError as e:
            queue_message(chat_id, f"⚠️ Erreur Twitter API: {e.response.status_code} - {e.response.text}")
            logger.error(f"Erreur Twitter HTTP: {str(e)}")
            await asyncio.sleep(60)
        except Exception as e:
            queue_message(chat_id, f"⚠️ Erreur Twitter API: {str(e)}")
            logger.error(f"Erreur Twitter: {str(e)}")
            await asyncio.sleep(10)

async def monitor_telegram(chat_id):
    if not telegram_client:
        queue_message(chat_id, "⚠️ Telegram API non configurée")
        logger.error("Telegram API non configurée")
        return
    try:
        await telegram_client.start()
        queue_message(chat_id, "🔄 Monitoring Telegram actif")
        logger.info("Monitoring Telegram démarré")

        @telegram_client.on(events.NewMessage)
        async def handler(event):
            try:
                text = event.message.text.lower()
                message_time = event.message.date.timestamp()
                if (time.time() - message_time) / 3600 > MAX_TOKEN_AGE_HOURS:
                    return
                words = text.split()
                for word in words:
                    chain = 'bsc' if word.startswith("0x") and len(word) == 42 else 'solana' if len(word) in [32, 44] else 'eth'
                    if word in BLACKLISTED_TOKENS or word in portfolio or (word in rejected_tokens and (time.time() - rejected_tokens[word]) / 3600 <= 6):
                        continue
                    if validate_address(word, chain):
                        sender = await event.get_sender()
                        if sender and hasattr(sender, 'participants_count') and sender.participants_count > 1000:
                            queue_message(chat_id, f'🔍 Détection Telegram (@{sender.username}): {word} ({chain})')
                            logger.info(f"Détection Telegram: {word}")
                            await validate_and_trade(chat_id, word, chain)
            except Exception as e:
                queue_message(chat_id, f"⚠️ Erreur Telegram: {str(e)}")
                logger.error(f"Erreur Telegram: {str(e)}")

        await telegram_client.run_until_disconnected()
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur démarrage Telegram: {str(e)}")
        logger.error(f"Erreur démarrage Telegram: {str(e)}")

async def validate_and_trade(chat_id, token_address, chain):
    try:
        if token_address in BLACKLISTED_TOKENS:
            return
        data = get_token_data(token_address, chain)
        if data is None:
            rejected_tokens[token_address] = time.time()
            queue_message(chat_id, f'⚠️ {token_address} rejeté : Pas de données ou trop vieux')
            return
        volume_24h = data.get('volume_24h', 0)
        liquidity = data.get('liquidity', 0)
        market_cap = data.get('market_cap', 0)
        buy_sell_ratio = data.get('buy_sell_ratio', 1)
        price = data.get('price', 0)
        age_hours = (time.time() - data.get('pair_created_at', time.time())) / 3600

        min_volume = MIN_VOLUME_BSC if chain == 'bsc' else MIN_VOLUME_ETH if chain == 'eth' else MIN_VOLUME_SOL
        max_volume = MAX_VOLUME_BSC if chain == 'bsc' else MAX_VOLUME_ETH if chain == 'eth' else MAX_VOLUME_SOL
        min_market_cap = MIN_MARKET_CAP_BSC if chain == 'bsc' else MIN_MARKET_CAP_ETH if chain == 'eth' else MIN_MARKET_CAP_SOL
        max_market_cap = MAX_MARKET_CAP_BSC if chain == 'bsc' else MAX_MARKET_CAP_ETH if chain == 'eth' else MAX_MARKET_CAP_SOL
        min_buy_sell = 1.0 if age_hours < 1 else MIN_BUY_SELL_RATIO

        if len(portfolio) >= max_positions:
            queue_message(chat_id, f'⚠️ {token_address} rejeté : Limite de {max_positions} positions atteinte')
            return
        if age_hours > MAX_TOKEN_AGE_HOURS:
            rejected_tokens[token_address] = time.time()
            queue_message(chat_id, f'⚠️ {token_address} rejeté : Âge {age_hours:.2f}h > {MAX_TOKEN_AGE_HOURS}h')
            return
        if liquidity < MIN_LIQUIDITY:
            rejected_tokens[token_address] = time.time()
            queue_message(chat_id, f'⚠️ {token_address} rejeté : Liquidité ${liquidity:.2f} < ${MIN_LIQUIDITY}')
            return
        if volume_24h < min_volume or volume_24h > max_volume:
            rejected_tokens[token_address] = time.time()
            queue_message(chat_id, f'⚠️ {token_address} rejeté : Volume ${volume_24h:.2f} hors plage [{min_volume}, {max_volume}]')
            return
        if market_cap < min_market_cap or market_cap > max_market_cap:
            rejected_tokens[token_address] = time.time()
            queue_message(chat_id, f'⚠️ {token_address} rejeté : Market Cap ${market_cap:.2f} hors plage [{min_market_cap}, {max_market_cap}]')
            return
        if buy_sell_ratio < min_buy_sell:
            rejected_tokens[token_address] = time.time()
            queue_message(chat_id, f'⚠️ {token_address} rejeté : Ratio A/V {buy_sell_ratio:.2f} < {min_buy_sell}')
            return
        if not check_token_security(token_address, chain):
            rejected_tokens[token_address] = time.time()
            queue_message(chat_id, f'⚠️ {token_address} rejeté : Sécurité insuffisante (rug/honeypot)')
            return
        if not check_dump_risk(token_address, chain):
            rejected_tokens[token_address] = time.time()
            queue_message(chat_id, f'⚠️ {token_address} rejeté : Risque de dump détecté')
            return
        if not check_mev_risk(chain):
            rejected_tokens[token_address] = time.time()
            queue_message(chat_id, f'⚠️ {token_address} rejeté : Risque MEV détecté')
            return
        twitter_mentions = await get_twitter_mentions(token_address, chat_id)
        if twitter_mentions < MIN_SOCIAL_MENTIONS:
            rejected_tokens[token_address] = time.time()
            queue_message(chat_id, f'⚠️ {token_address} rejeté : Hype insuffisant ({twitter_mentions} mentions Twitter)')
            return

        if buy_sell_ratio > 2 and age_hours < 0.083:
            queue_message(chat_id, f'🚀 Pump potentiel détecté : {token_address} (Ratio A/V: {buy_sell_ratio})')

        exchange = 'PancakeSwap' if chain == 'bsc' else 'Uniswap' if chain == 'eth' else 'Raydium' if 'Raydium' in token_address else 'Pump.fun'
        queue_message(chat_id, f'✅ Token validé : {token_address} ({chain} - {exchange})')
        logger.info(f"Token validé: {token_address}")
        detected_tokens[token_address] = {
            'address': token_address, 'volume': volume_24h, 'liquidity': liquidity,
            'market_cap': market_cap, 'supply': market_cap / price if price > 0 else 0, 'price': price,
            'buy_sell_ratio': buy_sell_ratio
        }
        amount = calculate_buy_amount(chain, liquidity)
        if chain == 'bsc':
            await buy_token_bsc(chat_id, token_address, amount)
        elif chain == 'eth':
            await buy_token_eth(chat_id, token_address, amount)
        else:
            await buy_token_solana(chat_id, token_address, amount)
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur validation {token_address}: {str(e)}")
        logger.error(f"Erreur validation: {str(e)}")

async def buy_token_bsc(chat_id, contract_address, amount):
    try:
        if not w3_bsc or not w3_bsc.is_connected():
            initialize_bot(chat_id)
            if not w3_bsc.is_connected():
                raise Exception("BSC non connecté")
        router = w3_bsc.eth.contract(address=PANCAKE_ROUTER_ADDRESS, abi=PANCAKE_ROUTER_ABI)
        amount_in = w3_bsc.to_wei(amount, 'ether')
        amount_out_min = int(amount_in * (1 - slippage_max))
        tx = router.functions.swapExactETHForTokens(
            amount_out_min,
            [w3_bsc.to_checksum_address('0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c'), w3_bsc.to_checksum_address(contract_address)],
            w3_bsc.to_checksum_address(WALLET_ADDRESS),
            int(time.time()) + 30
        ).build_transaction({
            'from': WALLET_ADDRESS, 'value': amount_in, 'gas': 250000,
            'gasPrice': get_dynamic_gas_price('bsc'), 'nonce': w3_bsc.eth.get_transaction_count(WALLET_ADDRESS)
        })
        signed_tx = w3_bsc.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash = w3_bsc.eth.send_raw_transaction(signed_tx.rawTransaction)
        queue_message(chat_id, f'⏳ Achat BSC {amount} BNB : {contract_address}, TX: {tx_hash.hex()}')
        receipt = w3_bsc.eth.wait_for_transaction_receipt(tx_hash, timeout=20)
        if receipt.status == 1:
            entry_price = detected_tokens[contract_address]['price']
            portfolio[contract_address] = {
                'amount': amount, 'chain': 'bsc', 'entry_price': entry_price,
                'market_cap_at_buy': detected_tokens[contract_address]['market_cap'],
                'current_market_cap': detected_tokens[contract_address]['market_cap'],
                'price_history': [entry_price], 'highest_price': entry_price, 'profit': 0.0,
                'buy_time': time.time(), 'alerted_x100': False
            }
            queue_message(chat_id, f'✅ Achat réussi : {amount} BNB de {contract_address} (PancakeSwap)')
            daily_trades['buys'].append({'token': contract_address, 'chain': 'bsc', 'amount': amount, 'timestamp': datetime.now().strftime('%H:%M:%S')})
    except Exception as e:
        queue_message(chat_id, f"⚠️ Échec achat BSC {contract_address}: {str(e)}")
        logger.error(f"Échec achat BSC: {str(e)}")

async def buy_token_eth(chat_id, contract_address, amount):
    try:
        if not w3_eth or not w3_eth.is_connected():
            initialize_bot(chat_id)
            if not w3_eth.is_connected():
                raise Exception("Ethereum non connecté")
        if not isinstance(PRIVATE_KEY, str) or not PRIVATE_KEY.startswith('0x') or len(PRIVATE_KEY) != 66:
            raise ValueError("Clé privée Ethereum invalide")
        router = w3_eth.eth.contract(address=UNISWAP_ROUTER_ADDRESS, abi=PANCAKE_ROUTER_ABI)
        amount_in = w3_eth.to_wei(amount, 'ether')
        amount_out_min = int(amount_in * (1 - slippage_max))
        tx = router.functions.swapExactETHForTokens(
            amount_out_min,
            [w3_eth.to_checksum_address('0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2'), w3_eth.to_checksum_address(contract_address)],
            w3_eth.to_checksum_address(WALLET_ADDRESS),
            int(time.time()) + 30
        ).build_transaction({
            'from': WALLET_ADDRESS, 'value': amount_in, 'gas': 250000,
            'gasPrice': get_dynamic_gas_price('eth'), 'nonce': w3_eth.eth.get_transaction_count(WALLET_ADDRESS)
        })
        signed_tx = w3_eth.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash = w3_eth.eth.send_raw_transaction(signed_tx.rawTransaction)
        queue_message(chat_id, f'⏳ Achat Ethereum {amount} ETH : {contract_address}, TX: {tx_hash.hex()}')
        receipt = w3_eth.eth.wait_for_transaction_receipt(tx_hash, timeout=20)
        if receipt.status == 1:
            entry_price = detected_tokens[contract_address]['price']
            portfolio[contract_address] = {
                'amount': amount, 'chain': 'eth', 'entry_price': entry_price,
                'market_cap_at_buy': detected_tokens[contract_address]['market_cap'],
                'current_market_cap': detected_tokens[contract_address]['market_cap'],
                'price_history': [entry_price], 'highest_price': entry_price, 'profit': 0.0,
                'buy_time': time.time(), 'alerted_x100': False
            }
            queue_message(chat_id, f'✅ Achat réussi : {amount} ETH de {contract_address} (Uniswap)')
            daily_trades['buys'].append({'token': contract_address, 'chain': 'eth', 'amount': amount, 'timestamp': datetime.now().strftime('%H:%M:%S')})
    except Exception as e:
        queue_message(chat_id, f"⚠️ Échec achat Ethereum {contract_address}: {str(e)}")
        logger.error(f"Échec achat Ethereum: {str(e)}")

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
        entry_price = detected_tokens[contract_address]['price']
        portfolio[contract_address] = {
            'amount': amount, 'chain': 'solana', 'entry_price': entry_price,
            'market_cap_at_buy': detected_tokens[contract_address]['market_cap'],
            'current_market_cap': detected_tokens[contract_address]['market_cap'],
            'price_history': [entry_price], 'highest_price': entry_price, 'profit': 0.0,
            'buy_time': time.time(), 'alerted_x100': False
        }
        exchange = 'Raydium' if 'Raydium' in contract_address else 'Pump.fun'
        queue_message(chat_id, f'✅ Achat réussi : {amount} SOL de {contract_address} ({exchange})')
        daily_trades['buys'].append({'token': contract_address, 'chain': 'solana', 'amount': amount, 'timestamp': datetime.now().strftime('%H:%M:%S')})
    except Exception as e:
        queue_message(chat_id, f"⚠️ Échec achat Solana {contract_address}: {str(e)}")
        logger.error(f"Échec achat Solana: {str(e)}")

def get_dynamic_trailing_stop(profit_pct):
    if profit_pct < 1000:
        return 5
    elif profit_pct < 10000:
        return 10
    else:
        return 20

async def sell_token(chat_id, contract_address, amount, chain, current_price):
    global mise_depart_bsc, mise_depart_sol, mise_depart_eth
    profit_pct = (current_price - portfolio[contract_address]['entry_price']) / portfolio[contract_address]['entry_price'] * 100 if portfolio[contract_address]['entry_price'] > 0 else 0
    if chain == "solana":
        try:
            if not solana_keypair:
                initialize_bot(chat_id)
                if not solana_keypair:
                    raise Exception("Solana non initialisé")
            amount_out = int(amount * 10**9)
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
                data=bytes([3]) + amount_out.to_bytes(8, 'little')
            )
            tx.add(instruction)
            tx.sign([solana_keypair])
            tx_hash = session.post(QUICKNODE_SOL_URL, json={
                "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                "params": [base58.b58encode(tx.serialize()).decode('utf-8')]
            }, timeout=5).json()['result']
            queue_message(chat_id, f'⏳ Vente Solana {amount} SOL : {contract_address}, TX: {tx_hash}')
            profit = (current_price - portfolio[contract_address]['entry_price']) * amount
            portfolio[contract_address]['profit'] += profit
            portfolio[contract_address]['amount'] -= amount
            reinvest_amount = profit * (0.95 if profit_pct > 10000 else profit_reinvestment_ratio)
            mise_depart_sol += reinvest_amount
            if portfolio[contract_address]['amount'] <= 0:
                del portfolio[contract_address]
            exchange = 'Raydium' if 'Raydium' in contract_address else 'Pump.fun'
            queue_message(chat_id, f'✅ Vente réussie : {amount} SOL, Profit: {profit:.4f} SOL, Réinvesti: {reinvest_amount:.4f} SOL ({exchange})')
            daily_trades['sells'].append({'token': contract_address, 'chain': 'solana', 'amount': amount, 'pnl': profit, 'timestamp': datetime.now().strftime('%H:%M:%S')})
        except Exception as e:
            queue_message(chat_id, f"⚠️ Échec vente Solana {contract_address}: {str(e)}")
            logger.error(f"Échec vente Solana: {str(e)}")
    elif chain == "eth":
        try:
            if not w3_eth or not w3_eth.is_connected():
                initialize_bot(chat_id)
                if not w3_eth.is_connected():
                    raise Exception("Ethereum non connecté")
            token_amount = w3_eth.to_wei(amount, 'ether')
            amount_in_max = int(token_amount * (1 + slippage_max))
            router = w3_eth.eth.contract(address=UNISWAP_ROUTER_ADDRESS, abi=PANCAKE_ROUTER_ABI)
            tx = router.functions.swapExactTokensForETH(
                token_amount,
                amount_in_max,
                [w3_eth.to_checksum_address(contract_address), w3_eth.to_checksum_address('0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2')],
                w3_eth.to_checksum_address(WALLET_ADDRESS),
                int(time.time()) + 30
            ).build_transaction({
                'from': WALLET_ADDRESS, 'gas': 250000,
                'gasPrice': get_dynamic_gas_price('eth'), 'nonce': w3_eth.eth.get_transaction_count(WALLET_ADDRESS)
            })
            signed_tx = w3_eth.eth.account.sign_transaction(tx, PRIVATE_KEY)
            tx_hash = w3_eth.eth.send_raw_transaction(signed_tx.rawTransaction)
            queue_message(chat_id, f'⏳ Vente Ethereum {amount} ETH : {contract_address}, TX: {tx_hash.hex()}')
            receipt = w3_eth.eth.wait_for_transaction_receipt(tx_hash, timeout=20)
            if receipt.status == 1:
                profit = (current_price - portfolio[contract_address]['entry_price']) * amount
                portfolio[contract_address]['profit'] += profit
                portfolio[contract_address]['amount'] -= amount
                reinvest_amount = profit * (0.95 if profit_pct > 10000 else profit_reinvestment_ratio)
                mise_depart_eth += reinvest_amount
                if portfolio[contract_address]['amount'] <= 0:
                    del portfolio[contract_address]
                queue_message(chat_id, f'✅ Vente réussie : {amount} ETH, Profit: {profit:.4f} ETH, Réinvesti: {reinvest_amount:.4f} ETH (Uniswap)')
                daily_trades['sells'].append({'token': contract_address, 'chain': 'eth', 'amount': amount, 'pnl': profit, 'timestamp': datetime.now().strftime('%H:%M:%S')})
        except Exception as e:
            queue_message(chat_id, f"⚠️ Échec vente Ethereum {contract_address}: {str(e)}")
            logger.error(f"Échec vente Ethereum: {str(e)}")
    else:
        try:
            if not w3_bsc or not w3_bsc.is_connected():
                initialize_bot(chat_id)
                if not w3_bsc.is_connected():
                    raise Exception("BSC non connecté")
            token_amount = w3_bsc.to_wei(amount, 'ether')
            amount_in_max = int(token_amount * (1 + slippage_max))
            router = w3_bsc.eth.contract(address=PANCAKE_ROUTER_ADDRESS, abi=PANCAKE_ROUTER_ABI)
            tx = router.functions.swapExactTokensForETH(
                token_amount,
                amount_in_max,
                [w3_bsc.to_checksum_address(contract_address), w3_bsc.to_checksum_address('0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c')],
                w3_bsc.to_checksum_address(WALLET_ADDRESS),
                int(time.time()) + 30
            ).build_transaction({
                'from': WALLET_ADDRESS, 'gas': 250000,
                'gasPrice': get_dynamic_gas_price('bsc'), 'nonce': w3_bsc.eth.get_transaction_count(WALLET_ADDRESS)
            })
            signed_tx = w3_bsc.eth.account.sign_transaction(tx, PRIVATE_KEY)
            tx_hash = w3_bsc.eth.send_raw_transaction(signed_tx.rawTransaction)
            queue_message(chat_id, f'⏳ Vente BSC {amount} BNB : {contract_address}, TX: {tx_hash.hex()}')
            receipt = w3_bsc.eth.wait_for_transaction_receipt(tx_hash, timeout=20)
            if receipt.status == 1:
                profit = (current_price - portfolio[contract_address]['entry_price']) * amount
                portfolio[contract_address]['profit'] += profit
                portfolio[contract_address]['amount'] -= amount
                reinvest_amount = profit * (0.95 if profit_pct > 10000 else profit_reinvestment_ratio)
                mise_depart_bsc += reinvest_amount
                if portfolio[contract_address]['amount'] <= 0:
                    del portfolio[contract_address]
                queue_message(chat_id, f'✅ Vente réussie : {amount} BNB, Profit: {profit:.4f} BNB, Réinvesti: {reinvest_amount:.4f} BNB (PancakeSwap)')
                daily_trades['sells'].append({'token': contract_address, 'chain': 'bsc', 'amount': amount, 'pnl': profit, 'timestamp': datetime.now().strftime('%H:%M:%S')})
        except Exception as e:
            queue_message(chat_id, f"⚠️ Échec vente BSC {contract_address}: {str(e)}")
            logger.error(f"Échec vente BSC: {str(e)}")

async def sell_token_percentage(chat_id, token, percentage):
    try:
        if token not in portfolio:
            queue_message(chat_id, f'⚠️ Vente impossible : {token} n\'est pas dans le portefeuille')
            return
        total_amount = portfolio[token]['amount']
        amount_to_sell = total_amount * (percentage / 100)
        chain = portfolio[token]['chain']
        market_cap = get_token_data(token, chain).get('market_cap', 0)
        supply = detected_tokens.get(token, {}).get('supply', 0)
        current_price = market_cap / supply if supply > 0 else 0
        await sell_token(chat_id, token, amount_to_sell, chain, current_price)
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur vente partielle {token}: {str(e)}")
        logger.error(f"Erreur vente partielle: {str(e)}")

async def monitor_and_sell(chat_id):
    while trade_active:
        try:
            if not portfolio:
                await asyncio.sleep(2)
                continue
            for contract_address, data in list(portfolio.items()):
                chain = data['chain']
                amount = data['amount']
                current_data = get_token_data(contract_address, chain)
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
                price_change = (current_price - data['price_history'][-2]) / data['price_history'][-2] * 100 if len(data['price_history']) > 1 else 0
                trailing_stop_percentage_temp = get_dynamic_trailing_stop(profit_pct)
                trailing_stop_price = data['highest_price'] * (1 - trailing_stop_percentage_temp / 100)

                if price_change > 50:
                    queue_message(chat_id, f'🚀 Pump détecté sur {contract_address}: +{price_change:.2f}%')
                    trailing_stop_percentage_temp = min(trailing_stop_percentage_temp, 2)

                if profit_pct >= 10000 and not data.get('alerted_x100', False):
                    queue_message(chat_id, f'🚀 Pump exceptionnel sur {contract_address}: +{profit_pct:.2f}% (x{profit_pct/100:.1f}) - Désactiver ventes auto ? (/pause)')
                    data['alerted_x100'] = True

                if not pause_auto_sell:
                    if profit_pct >= take_profit_steps[4] * 100:
                        await sell_token(chat_id, contract_address, amount * 0.5, chain, current_price)
                        queue_message(chat_id, f'💰 Vente 50% à x500 : {contract_address} (+{profit_pct:.2f}%)')
                    elif profit_pct >= take_profit_steps[3] * 100:
                        await sell_token(chat_id, contract_address, amount * 0.25, chain, current_price)
                    elif profit_pct >= take_profit_steps[2] * 100:
                        await sell_token(chat_id, contract_address, amount * 0.2, chain, current_price)
                    elif profit_pct >= take_profit_steps[1] * 100:
                        await sell_token(chat_id, contract_address, amount * 0.15, chain, current_price)
                    elif profit_pct >= take_profit_steps[0] * 100:
                        await sell_token(chat_id, contract_address, amount * 0.1, chain, current_price)
                    elif current_price <= trailing_stop_price or loss_pct >= stop_loss_threshold or current_data.get('buy_sell_ratio', 1) < 0.5:
                        await sell_token(chat_id, contract_address, amount, chain, current_price)
            await asyncio.sleep(0.5 if profit_pct > 50 else 1)
        except Exception as e:
            queue_message(chat_id, f"⚠️ Erreur surveillance: {str(e)}")
            logger.error(f"Erreur surveillance: {str(e)}")
            await asyncio.sleep(5)

async def show_portfolio(chat_id):
    try:
        if w3_bsc is None or w3_eth is None or solana_keypair is None:
            initialize_bot(chat_id)
        bnb_balance = w3_bsc.eth.get_balance(WALLET_ADDRESS) / 10**18 if w3_bsc and w3_bsc.is_connected() else 0
        eth_balance = w3_eth.eth.get_balance(WALLET_ADDRESS) / 10**18 if w3_eth and w3_eth.is_connected() else 0
        sol_balance = await get_solana_balance(chat_id)
        msg = f'💰 Portefeuille:\nBNB : {bnb_balance:.4f}\nETH : {eth_balance:.4f}\nSOL : {sol_balance:.4f}\n\n'
        markup = InlineKeyboardMarkup()
        for ca, data in portfolio.items():
            chain = data['chain']
            current_mc = get_token_data(ca, chain).get('market_cap', 0)
            profit = (current_mc - data['market_cap_at_buy']) / data['market_cap_at_buy'] * 100 if data['market_cap_at_buy'] > 0 else 0
            markup.add(
                InlineKeyboardButton(f"💸 Sell 25% {ca[:6]}", callback_data=f"sell_pct_{ca}_25"),
                InlineKeyboardButton(f"💸 Sell 50% {ca[:6]}", callback_data=f"sell_pct_{ca}_50"),
                InlineKeyboardButton(f"💸 Sell 100% {ca[:6]}", callback_data=f"sell_{ca}")
            )
            msg += (
                f"Token: {ca} ({chain})\nMC Achat: ${data['market_cap_at_buy']:.2f}\n"
                f"MC Actuel: ${current_mc:.2f}\nProfit: {profit:.2f}%\nProfit cumulé: {data['profit']:.4f} {chain.upper()}\n\n"
            )
        queue_message(chat_id, msg, reply_markup=markup if portfolio else None)
    except Exception as e:
        queue_message(chat_id, f'⚠️ Erreur portefeuille: {str(e)}')
        logger.error(f"Erreur portefeuille: {str(e)}")

async def get_solana_balance(chat_id):
    try:
        if not solana_keypair:
            initialize_bot(chat_id)
        if not solana_keypair:
            return 0
        response = session.post(QUICKNODE_SOL_URL, json={
            "jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [str(solana_keypair.pubkey())]
        }, timeout=5)
        return response.json().get('result', {}).get('value', 0) / 10**9
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur solde Solana: {str(e)}")
        logger.error(f"Erreur solde Solana: {str(e)}")
        return 0

async def sell_token_immediate(chat_id, token):
    try:
        if token not in portfolio:
            queue_message(chat_id, f'⚠️ Vente impossible : {token} n\'est pas dans le portefeuille')
            return
        amount = portfolio[token]['amount']
        chain = portfolio[token]['chain']
        current_price = get_token_data(token, chain).get('market_cap', 0) / detected_tokens.get(token, {}).get('supply', 1)
        await sell_token(chat_id, token, amount, chain, current_price)
    except Exception as e:
        queue_message(chat_id, f'⚠️ Erreur vente immédiate {token}: {str(e)}')
        logger.error(f"Erreur vente immédiate: {str(e)}")

async def show_daily_summary(chat_id):
    try:
        msg = f"📅 Récapitulatif du jour ({datetime.now().strftime('%Y-%m-%d')}):\n\n"
        msg += "📈 Achats :\n"
        total_buys = {'bsc': 0, 'eth': 0, 'solana': 0}
        for trade in daily_trades['buys']:
            total_buys[trade['chain']] += trade['amount']
            msg += f"- {trade['token']} ({trade['chain']}) : {trade['amount']} {trade['chain'].upper()} à {trade['timestamp']}\n"
        msg += f"Total investi : {total_buys['bsc']:.4f} BNB / {total_buys['eth']:.4f} ETH / {total_buys['solana']:.4f} SOL\n\n"

        msg += "📉 Ventes :\n"
        total_profit = {'bsc': 0, 'eth': 0, 'solana': 0}
        for trade in daily_trades['sells']:
            total_profit[trade['chain']] += trade['pnl']
            msg += f"- {trade['token']} ({trade['chain']}) : {trade['amount']} {trade['chain'].upper()} à {trade['timestamp']}, PNL: {trade['pnl']:.4f} {trade['chain'].upper()}\n"
        msg += f"Profit net : {total_profit['bsc']:.4f} BNB / {total_profit['eth']:.4f} ETH / {total_profit['solana']:.4f} SOL\n"
        queue_message(chat_id, msg)
    except Exception as e:
        queue_message(chat_id, f'⚠️ Erreur récapitulatif: {str(e)}')
        logger.error(f"Erreur récapitulatif: {str(e)}")

async def adjust_mise_bsc(message):
    global mise_depart_bsc
    chat_id = message.chat.id
    try:
        new_mise = float(message.text)
        if new_mise > 0:
            mise_depart_bsc = new_mise
            queue_message(chat_id, f'✅ Mise BSC mise à jour à {mise_depart_bsc} BNB')
        else:
            queue_message(chat_id, "⚠️ La mise doit être positive!")
    except ValueError:
        queue_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 0.02)")
    await show_main_menu(chat_id)

async def adjust_mise_eth(message):
    global mise_depart_eth
    chat_id = message.chat.id
    try:
        new_mise = float(message.text)
        if new_mise > 0:
            mise_depart_eth = new_mise
            queue_message(chat_id, f'✅ Mise Ethereum mise à jour à {mise_depart_eth} ETH')
        else:
            queue_message(chat_id, "⚠️ La mise doit être positive!")
    except ValueError:
        queue_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 0.05)")
    await show_main_menu(chat_id)

async def adjust_mise_sol(message):
    global mise_depart_sol
    chat_id = message.chat.id
    try:
        new_mise = float(message.text)
        if new_mise > 0:
            mise_depart_sol = new_mise
            queue_message(chat_id, f'✅ Mise Solana mise à jour à {mise_depart_sol} SOL')
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
            queue_message(chat_id, f'✅ Stop-Loss mis à jour à {stop_loss_threshold} %')
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
            queue_message(chat_id, f'✅ Take-Profit mis à jour à x{take_profit_steps[0]}, x{take_profit_steps[1]}, x{take_profit_steps[2]}, x{take_profit_steps[3]}, x{take_profit_steps[4]}')
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
            queue_message(chat_id, f'✅ Ratio de réinvestissement mis à jour à {profit_reinvestment_ratio * 100}%')
        else:
            queue_message(chat_id, "⚠️ Le ratio doit être entre 0 et 1 (ex. : 0.9 pour 90%)")
    except ValueError:
        queue_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 0.9)")
    await show_main_menu(chat_id)

chat_id_global = None

def run_task_in_thread(task, *args):
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(task(*args))
    except Exception as e:
        logger.error(f"Erreur dans thread {task.__name__}: {str(e)}")
        queue_message(args[0], f"⚠️ Erreur thread {task.__name__}: {str(e)}")

def initialize_and_run_threads(chat_id):
    global trade_active, chat_id_global, executor_futures
    chat_id_global = chat_id
    try:
        initialize_bot(chat_id)
        if w3_bsc and w3_eth and solana_keypair:
            trade_active = True
            queue_message(chat_id, "▶️ Trading lancé avec succès!")
            logger.info("Trading démarré")
            tasks = [
                snipe_new_pairs_bsc, snipe_new_pairs_eth, snipe_solana_pools,
                detect_bsc_blocks, detect_eth_blocks, detect_bscscan,
                detect_birdeye, detect_pump_fun, monitor_twitter, monitor_telegram, monitor_and_sell
            ]
            with ThreadPoolExecutor(max_workers=11) as executor:
                executor_futures = [executor.submit(run_task_in_thread, task, chat_id) for task in tasks]
                for task in tasks:
                    logger.info(f"Tâche {task.__name__} lancée")
        else:
            queue_message(chat_id, "⚠️ Échec initialisation : Connexion(s) manquante(s)")
            logger.error("Échec initialisation: connexions manquantes")
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur initialisation: {str(e)}")
        logger.error(f"Erreur initialisation: {str(e)}")
        trade_active = False

@app.route("/webhook", methods=['POST'])
def webhook():
    global trade_active
    if request.method == "POST" and request.headers.get("content-type") == "application/json":
        update = telebot.types.Update.de_json(request.get_json())
        bot.process_new_updates([update])
        if not trade_active and update.message:
            initialize_and_run_threads(update.message.chat.id)
        return 'OK', 200
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
        queue_message(chat_id, "✅ Bot démarré!")
        threading.Thread(target=run_task_in_thread, args=(show_main_menu, chat_id), daemon=True).start()

@bot.message_handler(commands=['menu'])
def menu_message(message):
    chat_id = message.chat.id
    threading.Thread(target=run_task_in_thread, args=(show_main_menu, chat_id), daemon=True).start()

@bot.message_handler(commands=['pause'])
def pause_auto_sell_handler(message):
    global pause_auto_sell
    chat_id = message.chat.id
    pause_auto_sell = True
    queue_message(chat_id, "⏸️ Ventes automatiques désactivées. Utilisez /resume pour réactiver.")

@bot.message_handler(commands=['resume'])
def resume_auto_sell_handler(message):
    global pause_auto_sell
    chat_id = message.chat.id
    pause_auto_sell = False
    queue_message(chat_id, "▶️ Ventes automatiques réactivées.")

@bot.message_handler(commands=['stop'])
def stop_trading_handler(message):
    global trade_active, executor_futures
    chat_id = message.chat.id
    trade_active = False
    logger.info("Commande /stop reçue, trade_active mis à False")
    queue_message(chat_id, "⏹️ Trading arrêté complètement.")
    for future in executor_futures:
        if not future.done():
            future.cancel()
    executor_futures.clear()
    time.sleep(2)

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
        InlineKeyboardButton("🔧 Ajuster Mise BSC", callback_data="adjust_mise_bsc"),
        InlineKeyboardButton("🔧 Ajuster Mise ETH", callback_data="adjust_mise_eth"),
        InlineKeyboardButton("🔧 Ajuster Mise SOL", callback_data="adjust_mise_sol")
    )
    markup.add(
        InlineKeyboardButton("📉 Ajuster Stop-Loss", callback_data="adjust_stop_loss"),
        InlineKeyboardButton("📈 Ajuster Take-Profit", callback_data="adjust_take_profit")
    )
    markup.add(
        InlineKeyboardButton("🔄 Ajuster Réinvestissement", callback_data="adjust_reinvestment")
    )
    queue_message(chat_id, "Menu principal:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    global trade_active
    chat_id = call.message.chat.id
    try:
        if call.data == "status":
            queue_message(chat_id, (
                f"ℹ️ Statut actuel :\nTrading actif: {'Oui' if trade_active else 'Non'}\n"
                f"Mise BSC: {mise_depart_bsc} BNB\nMise Ethereum: {mise_depart_eth} ETH\nMise Solana: {mise_depart_sol} SOL\n"
                f"Positions: {len(portfolio)}/{max_positions}\n"
                f"Stop-Loss: {stop_loss_threshold}%\nTake-Profit: x{take_profit_steps[0]}, x{take_profit_steps[1]}, x{take_profit_steps[2]}, x{take_profit_steps[3]}, x{take_profit_steps[4]}"
            ))
        elif call.data == "launch":
            if not trade_active:
                initialize_and_run_threads(chat_id)
            else:
                queue_message(chat_id, "⚠️ Trading déjà en cours.")
        elif call.data == "stop":
            trade_active = False
            queue_message(chat_id, "⏹️ Trading arrêté.")
        elif call.data == "portfolio":
            threading.Thread(target=run_task_in_thread, args=(show_portfolio, chat_id), daemon=True).start()
        elif call.data == "daily_summary":
            threading.Thread(target=run_task_in_thread, args=(show_daily_summary, chat_id), daemon=True).start()
        elif call.data == "adjust_mise_bsc":
            queue_message(chat_id, "Entrez la nouvelle mise BSC (en BNB, ex. : 0.02) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_mise_bsc)
        elif call.data == "adjust_mise_eth":
            queue_message(chat_id, "Entrez la nouvelle mise Ethereum (en ETH, ex. : 0.05) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_mise_eth)
        elif call.data == "adjust_mise_sol":
            queue_message(chat_id, "Entrez la nouvelle mise Solana (en SOL, ex. : 0.37) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_mise_sol)
        elif call.data == "adjust_stop_loss":
            queue_message(chat_id, "Entrez le nouveau seuil de Stop-Loss (en %, ex. : 15) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_stop_loss)
        elif call.data == "adjust_take_profit":
            queue_message(chat_id, "Entrez les nouveaux seuils de Take-Profit (5 valeurs séparées par des virgules, ex. : 1.2,2,10,100,500) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_take_profit)
        elif call.data == "adjust_reinvestment":
            queue_message(chat_id, "Entrez le nouveau ratio de réinvestissement (0-1, ex. : 0.9 pour 90%) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_reinvestment)
        elif call.data.startswith("sell_"):
            token = call.data.split("_")[1]
            threading.Thread(target=run_task_in_thread, args=(sell_token_immediate, chat_id, token), daemon=True).start()
        elif call.data.startswith("sell_pct_"):
            _, token, pct = call.data.split("_")
            threading.Thread(target=run_task_in_thread, args=(sell_token_percentage, chat_id, token, float(pct)), daemon=True).start()
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur générale: {str(e)}")
        logger.error(f"Erreur callback: {str(e)}")

if __name__ == "__main__":
    if set_webhook():
        logger.info("Webhook configuré, démarrage du serveur...")
        serve(app, host="0.0.0.0", port=PORT, threads=10)
    else:
        logger.error("Échec du webhook, passage en mode polling")
        bot.polling(none_stop=True)
