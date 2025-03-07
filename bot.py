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
import websockets
from datetime import datetime
import re
from waitress import serve
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Configuration logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Session HTTP avec retries
session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
retry_strategy = Retry(total=5, backoff_factor=1, status_forcelist=[500, 502, 503, 504, 429])
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("https://", adapter)

# Variables globales
daily_trades = {'buys': [], 'sells': []}
rejected_tokens = {}
trade_active = False
portfolio = {}
detected_tokens = {}
BLACKLISTED_TOKENS = {"So11111111111111111111111111111111111111112"}

# Variables d’environnement
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "default_token")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "0x0")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "dummy_key")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://default.example.com/webhook")
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY", "dummy_solana_key")
TWITTERAPI_ID = "288621526915493900"
TWITTERAPI_KEY = "16826fa2edc64438a510a261337e6645"
BSCSCAN_API_KEY = os.getenv("BSCSCAN_API_KEY", "3F24ENAM4DHUTN3PFCRDWAIY53T1XACS22")
QUICKNODE_BSC_URL = "https://smart-necessary-ensemble.bsc.quiknode.pro/aeb370bcf4299bc365bbbd3d14d19a31f6e46f06/"
QUICKNODE_ETH_URL = "https://side-cold-diamond.quiknode.pro/698f06abfe4282fc22edbab42297cf468d78070f/"
QUICKNODE_SOL_URL = "https://little-maximum-glade.solana-mainnet.quiknode.pro/5da088be927d31731b0d7284c30a0640d8e4dd50/"
QUICKNODE_BSC_WS_URL = "wss://smart-necessary-ensemble.bsc.quiknode.pro/aeb370bcf4299bc365bbbd3d14d19a31f6e46f06/"
QUICKNODE_ETH_WS_URL = "wss://side-cold-diamond.quiknode.pro/698f06abfe4282fc22edbab42297cf468d78070f/"
QUICKNODE_SOL_WS_URL = "wss://little-maximum-glade.solana-mainnet.quiknode.pro/5da088be927d31731b0d7284c30a0640d8e4dd50/"
PORT = int(os.getenv("PORT", 8080))

app = Flask(__name__)
bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=False)

w3_bsc = None
w3_eth = None
solana_keypair = None

# Paramètres de trading
mise_depart_bsc = 0.02
mise_depart_sol = 0.37
mise_depart_eth = 0.05
gas_fee = 10
stop_loss_threshold = 15
trailing_stop_percentage = 5
take_profit_steps = [1.5, 2, 5]
max_positions = 5
profit_reinvestment_ratio = 0.7
slippage_max = 0.05  # 5%

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
MIN_X_MENTIONS = 10

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

def initialize_bot(chat_id):
    global w3_bsc, w3_eth, solana_keypair
    try:
        w3_bsc = Web3(Web3.WebsocketProvider(QUICKNODE_BSC_WS_URL))
        w3_bsc.middleware_onion.inject(geth_poa_middleware, layer=0)
        if not w3_bsc.is_connected():
            w3_bsc = Web3(Web3.HTTPProvider(QUICKNODE_BSC_URL))
        if w3_bsc.is_connected():
            logger.info("Connexion BSC réussie")
            bot.send_message(chat_id, "✅ Connexion BSC établie : QuickNode")
        else:
            raise Exception("Échec connexion BSC")
    except Exception as e:
        logger.error(f"Erreur connexion BSC: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur connexion BSC: {str(e)}")
        w3_bsc = None

    try:
        w3_eth = Web3(Web3.WebsocketProvider(QUICKNODE_ETH_WS_URL))
        if not w3_eth.is_connected():
            w3_eth = Web3(Web3.HTTPProvider(QUICKNODE_ETH_URL))
        if w3_eth.is_connected():
            logger.info("Connexion Ethereum réussie")
            bot.send_message(chat_id, "✅ Connexion Ethereum établie : QuickNode")
        else:
            raise Exception("Échec connexion Ethereum")
    except Exception as e:
        logger.error(f"Erreur connexion Ethereum: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur connexion Ethereum: {str(e)}")
        w3_eth = None

    try:
        solana_keypair = Keypair.from_base58_string(SOLANA_PRIVATE_KEY)
        logger.info("Clé Solana initialisée")
        bot.send_message(chat_id, "✅ Clé Solana initialisée")
    except Exception as e:
        logger.error(f"Erreur initialisation Solana: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur initialisation Solana: {str(e)}")
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

def get_token_data(token_address, chain):
    try:
        response = session.get(f"https://api.dexscreener.com/latest/dex/tokens/{token_address}", timeout=2)
        response.raise_for_status()
        pairs = response.json().get('pairs', [])
        if not pairs or pairs[0].get('chainId') != chain:
            return None
        data = pairs[0]
        return {
            'volume_24h': float(data.get('volume', {}).get('h24', 0)),
            'liquidity': float(data.get('liquidity', {}).get('usd', 0)),
            'market_cap': float(data.get('marketCap', 0)),
            'price': float(data.get('priceUsd', 0)),
            'buy_sell_ratio': float(data.get('txns', {}).get('m5', {}).get('buys', 0)) / max(float(data.get('txns', {}).get('m5', {}).get('sells', 0)), 1),
            'pair_created_at': data.get('pairCreatedAt', 0) / 1000 if data.get('pairCreatedAt') else time.time() - 3600
        }
    except Exception as e:
        logger.error(f"Erreur DexScreener {token_address}: {str(e)}")
        return None

def check_token_security(token_address, chain):
    try:
        api_url = f"https://api.gopluslabs.io/api/v1/token_security/{chain}?contract_addresses={token_address}"
        response = session.get(api_url, timeout=2).json()
        data = response.get('result', {}).get(token_address.lower(), {})
        taxes = float(data.get('buy_tax', 0)) + float(data.get('sell_tax', 0))
        top_holder_pct = float(data.get('holder_percent_top_1', 0))
        is_locked = data.get('is_liquidity_locked', '0') == '1'
        return taxes < 0.05 and top_holder_pct < 0.20 and is_locked
    except Exception:
        return False

async def get_x_mentions(token_address):
    try:
        response = session.get(
            f"https://api.twitterapi.io/v1/tweets/search?query={token_address}&id={TWITTERAPI_ID}&key={TWITTERAPI_KEY}",
            timeout=2
        )
        response.raise_for_status()
        tweets = response.json().get('data', [])
        return sum(1 for t in tweets if t.get('user', {}).get('followers_count', 0) > 500)
    except Exception as e:
        logger.error(f"Erreur Twitter API pour {token_address}: {str(e)}")
        return 0

async def snipe_new_pairs_bsc(chat_id):
    while trade_active:
        if not w3_bsc or not w3_bsc.is_connected():
            initialize_bot(chat_id)
            if not w3_bsc or not w3_bsc.is_connected():
                bot.send_message(chat_id, "⚠️ BSC non connecté, sniping désactivé")
                await asyncio.sleep(60)
                continue
        try:
            async with websockets.connect(QUICKNODE_BSC_WS_URL, ping_interval=20) as ws:
                await ws.send(json.dumps({"method": "eth_subscribe", "params": ["logs", {"address": PANCAKE_FACTORY_ADDRESS}], "id": 1}))
                while trade_active:
                    log = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                    if 'params' not in log or log['params']['subscription'] != "logs":
                        continue
                    data = log['params']['result']
                    if data['topics'][0] != Web3.keccak(text="PairCreated(address,address,address,uint256)").hex():
                        continue
                    token0 = w3_bsc.to_checksum_address('0x' + data['data'][26:66])
                    token1 = w3_bsc.to_checksum_address('0x' + data['data'][90:130])
                    token_address = token0 if token0 != "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c" else token1
                    if token_address in BLACKLISTED_TOKENS or token_address in portfolio or (token_address in rejected_tokens and (time.time() - rejected_tokens[token_address]) / 3600 <= 6):
                        continue
                    bot.send_message(chat_id, f'🎯 Snipe détecté : {token_address} (BSC - PancakeSwap)')
                    await validate_and_trade(chat_id, token_address, 'bsc')
                    await asyncio.sleep(0.005)
        except Exception as e:
            bot.send_message(chat_id, f"⚠️ Erreur sniping BSC: {str(e)}")
            await asyncio.sleep(5)

async def snipe_new_pairs_eth(chat_id):
    while trade_active:
        if not w3_eth or not w3_eth.is_connected():
            initialize_bot(chat_id)
            if not w3_eth or not w3_eth.is_connected():
                bot.send_message(chat_id, "⚠️ Ethereum non connecté, sniping désactivé")
                await asyncio.sleep(60)
                continue
        try:
            async with websockets.connect(QUICKNODE_ETH_WS_URL, ping_interval=20) as ws:
                await ws.send(json.dumps({"method": "eth_subscribe", "params": ["logs", {"address": UNISWAP_FACTORY_ADDRESS}], "id": 1}))
                while trade_active:
                    log = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                    if 'params' not in log or log['params']['subscription'] != "logs":
                        continue
                    data = log['params']['result']
                    if data['topics'][0] != Web3.keccak(text="PairCreated(address,address,address,uint256)").hex():
                        continue
                    token0 = w3_eth.to_checksum_address('0x' + data['data'][26:66])
                    token1 = w3_eth.to_checksum_address('0x' + data['data'][90:130])
                    token_address = token0 if token0 != "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2" else token1
                    if token_address in BLACKLISTED_TOKENS or token_address in portfolio or (token_address in rejected_tokens and (time.time() - rejected_tokens[token_address]) / 3600 <= 6):
                        continue
                    bot.send_message(chat_id, f'🎯 Snipe détecté : {token_address} (Ethereum - Uniswap)')
                    await validate_and_trade(chat_id, token_address, 'eth')
                    await asyncio.sleep(0.005)
        except Exception as e:
            bot.send_message(chat_id, f"⚠️ Erreur sniping Ethereum: {str(e)}")
            await asyncio.sleep(5)

async def snipe_4meme_bsc_with_bscscan(chat_id):
    last_timestamp = int(time.time()) - 600
    while trade_active:
        if not w3_bsc or not w3_bsc.is_connected():
            initialize_bot(chat_id)
            if not w3_bsc or not w3_bsc.is_connected():
                bot.send_message(chat_id, "⚠️ BSC non connecté, sniping 4Meme désactivé")
                await asyncio.sleep(60)
                continue
        try:
            response = session.get(
                f"https://api.bscscan.com/api?module=account&action=tokentx&sort=desc&apikey={BSCSCAN_API_KEY}&startblock=0&endblock=99999999",
                timeout=2
            )
            response.raise_for_status()
            txs = response.json().get('result', [])
            if not isinstance(txs, list):
                await asyncio.sleep(5)
                continue
            for tx in txs:
                if not isinstance(tx, dict) or 'timeStamp' not in tx or 'contractAddress' not in tx:
                    continue
                timestamp = int(tx['timeStamp'])
                if timestamp <= last_timestamp:
                    continue
                token_address = tx['contractAddress']
                if token_address in BLACKLISTED_TOKENS or token_address in portfolio or (token_address in rejected_tokens and (time.time() - rejected_tokens[token_address]) / 3600 <= 6):
                    continue
                if validate_address(token_address, 'bsc'):
                    data = get_token_data(token_address, 'bsc')
                    if data and data['volume_24h'] < 1000 and data['liquidity'] >= MIN_LIQUIDITY and data['market_cap'] < 50000:
                        bot.send_message(chat_id, f'🎯 Snipe détecté via BscScan : {token_address} (BSC - 4Meme probable)')
                        await validate_and_trade(chat_id, token_address, 'bsc')
            last_timestamp = int(time.time())
            await asyncio.sleep(5)
        except Exception as e:
            bot.send_message(chat_id, f"⚠️ Erreur sniping 4Meme via BscScan: {str(e)}")
            await asyncio.sleep(10)

async def snipe_solana_pools(chat_id):
    while trade_active:
        if not solana_keypair:
            initialize_bot(chat_id)
            if not solana_keypair:
                bot.send_message(chat_id, "⚠️ Solana non initialisé, sniping désactivé")
                await asyncio.sleep(60)
                continue
        try:
            async with websockets.connect(QUICKNODE_SOL_WS_URL, ping_interval=20) as ws:
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "programSubscribe", "params": [str(RAYDIUM_PROGRAM_ID), {"encoding": "base64"}]}))
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "programSubscribe", "params": [str(PUMP_FUN_PROGRAM_ID), {"encoding": "base64"}]}))
                while trade_active:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                    if 'result' not in msg or 'params' not in msg:
                        continue
                    accounts = msg['params']['result']['value']['account']['data'][0]
                    token_address = None
                    for acc in accounts.split():
                        if validate_address(acc, 'solana') and acc not in BLACKLISTED_TOKENS and acc not in [str(RAYDIUM_PROGRAM_ID), str(PUMP_FUN_PROGRAM_ID), str(TOKEN_PROGRAM_ID)] and acc not in portfolio:
                            token_address = acc
                            break
                    if token_address:
                        exchange = 'Raydium' if str(RAYDIUM_PROGRAM_ID) in msg['params']['result']['pubkey'] else 'Pump.fun'
                        bot.send_message(chat_id, f'🎯 Snipe détecté : {token_address} (Solana - {exchange})')
                        await validate_and_trade(chat_id, token_address, 'solana')
                    await asyncio.sleep(0.05)
        except Exception as e:
            bot.send_message(chat_id, f"⚠️ Erreur sniping Solana: {str(e)}")
            await asyncio.sleep(5)

async def monitor_x(chat_id):
    while trade_active:
        try:
            response = session.get(
                f"https://api.twitterapi.io/v1/tweets/search?query=\"contract address\" OR CA OR launch OR pump&id={TWITTERAPI_ID}&key={TWITTERAPI_KEY}",
                timeout=2
            )
            response.raise_for_status()
            tweets = response.json().get('data', [])
            for tweet in tweets:
                followers = tweet.get('user', {}).get('followers_count', 0)
                if followers >= 500:
                    text = tweet['text'].lower()
                    words = text.split()
                    for word in words:
                        chain = 'bsc' if word.startswith("0x") and len(word) == 42 else 'solana' if len(word) in [32, 44] else 'eth'
                        if word in BLACKLISTED_TOKENS or word in portfolio or (word in rejected_tokens and (time.time() - rejected_tokens[word]) / 3600 <= 6):
                            continue
                        if validate_address(word, chain):
                            bot.send_message(chat_id, f'🔍 Token détecté via X (@{tweet["user"]["username"]}, {followers} abonnés): {word} ({chain})')
                            await validate_and_trade(chat_id, word, chain)
            await asyncio.sleep(10)  # Réduit la fréquence pour éviter les limites
        except Exception as e:
            bot.send_message(chat_id, f"⚠️ Erreur X: {str(e)}")
            await asyncio.sleep(30)

async def validate_and_trade(chat_id, token_address, chain):
    try:
        if token_address in BLACKLISTED_TOKENS:
            return

        data = get_token_data(token_address, chain)
        if not data:
            rejected_tokens[token_address] = time.time()
            bot.send_message(chat_id, f'⚠️ {token_address} rejeté : Pas de données DexScreener')
            return

        volume_24h = data['volume_24h']
        liquidity = data['liquidity']
        market_cap = data['market_cap']
        buy_sell_ratio = data['buy_sell_ratio']
        price = data['price']
        age_hours = (time.time() - data['pair_created_at']) / 3600

        min_volume = MIN_VOLUME_BSC if chain == 'bsc' else MIN_VOLUME_ETH if chain == 'eth' else MIN_VOLUME_SOL
        max_volume = MAX_VOLUME_BSC if chain == 'bsc' else MAX_VOLUME_ETH if chain == 'eth' else MAX_VOLUME_SOL
        min_market_cap = MIN_MARKET_CAP_BSC if chain == 'bsc' else MIN_MARKET_CAP_ETH if chain == 'eth' else MIN_MARKET_CAP_SOL
        max_market_cap = MAX_MARKET_CAP_BSC if chain == 'bsc' else MAX_MARKET_CAP_ETH if chain == 'eth' else MAX_MARKET_CAP_SOL
        min_buy_sell = 1.0 if age_hours < 1 else MIN_BUY_SELL_RATIO

        if len(portfolio) >= max_positions:
            bot.send_message(chat_id, f'⚠️ {token_address} rejeté : Limite de {max_positions} positions atteinte')
            return
        if age_hours > MAX_TOKEN_AGE_HOURS:
            rejected_tokens[token_address] = time.time()
            bot.send_message(chat_id, f'⚠️ {token_address} rejeté : Âge {age_hours:.2f}h > {MAX_TOKEN_AGE_HOURS}h')
            return
        if liquidity < MIN_LIQUIDITY:
            rejected_tokens[token_address] = time.time()
            bot.send_message(chat_id, f'⚠️ {token_address} rejeté : Liquidité ${liquidity:.2f} < ${MIN_LIQUIDITY}')
            return
        if volume_24h < min_volume or volume_24h > max_volume:
            rejected_tokens[token_address] = time.time()
            bot.send_message(chat_id, f'⚠️ {token_address} rejeté : Volume ${volume_24h:.2f} hors plage [{min_volume}, {max_volume}]')
            return
        if market_cap < min_market_cap or market_cap > max_market_cap:
            rejected_tokens[token_address] = time.time()
            bot.send_message(chat_id, f'⚠️ {token_address} rejeté : Market Cap ${market_cap:.2f} hors plage [{min_market_cap}, {max_market_cap}]')
            return
        if buy_sell_ratio < min_buy_sell:
            rejected_tokens[token_address] = time.time()
            bot.send_message(chat_id, f'⚠️ {token_address} rejeté : Ratio A/V {buy_sell_ratio:.2f} < {min_buy_sell}')
            return
        if not check_token_security(token_address, chain):
            rejected_tokens[token_address] = time.time()
            bot.send_message(chat_id, f'⚠️ {token_address} rejeté : Sécurité insuffisante')
            return
        x_mentions = await get_x_mentions(token_address)
        if x_mentions < MIN_X_MENTIONS:
            rejected_tokens[token_address] = time.time()
            bot.send_message(chat_id, f'⚠️ {token_address} rejeté : Hype insuffisant ({x_mentions} mentions)')
            return

        exchange = ('PancakeSwap' if chain == 'bsc' else 'Uniswap' if chain == 'eth' else 'Raydium' if 'Raydium' in token_address else 'Pump.fun')
        bot.send_message(chat_id, f'✅ Token validé : {token_address} ({chain} - {exchange})')
        detected_tokens[token_address] = {
            'address': token_address, 'volume': volume_24h, 'liquidity': liquidity,
            'market_cap': market_cap, 'supply': market_cap / price if price > 0 else 0, 'price': price,
            'buy_sell_ratio': buy_sell_ratio
        }
        if chain == 'bsc':
            await buy_token_bsc(chat_id, token_address, mise_depart_bsc)
        elif chain == 'eth':
            await buy_token_eth(chat_id, token_address, mise_depart_eth)
        else:
            await buy_token_solana(chat_id, token_address, mise_depart_sol)
    except Exception as e:
        bot.send_message(chat_id, f"⚠️ Erreur validation {token_address}: {str(e)}")

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
            int(time.time()) + 60
        ).build_transaction({
            'from': WALLET_ADDRESS, 'value': amount_in, 'gas': 250000,
            'gasPrice': w3_bsc.to_wei(gas_fee * 1.2, 'gwei'), 'nonce': w3_bsc.eth.get_transaction_count(WALLET_ADDRESS)
        })
        signed_tx = w3_bsc.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash = w3_bsc.eth.send_raw_transaction(signed_tx.rawTransaction)
        bot.send_message(chat_id, f'⏳ Achat BSC {amount} BNB de {contract_address}, TX: {tx_hash.hex()}')
        receipt = w3_bsc.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        if receipt.status == 1:
            entry_price = detected_tokens[contract_address]['price']
            portfolio[contract_address] = {
                'amount': amount, 'chain': 'bsc', 'entry_price': entry_price,
                'market_cap_at_buy': detected_tokens[contract_address]['market_cap'],
                'current_market_cap': detected_tokens[contract_address]['market_cap'],
                'price_history': [entry_price], 'highest_price': entry_price, 'profit': 0.0,
                'buy_time': time.time()
            }
            bot.send_message(chat_id, f'✅ Achat réussi : {amount} BNB de {contract_address} (PancakeSwap)')
            daily_trades['buys'].append({'token': contract_address, 'chain': 'bsc', 'amount': amount, 'timestamp': datetime.now().strftime('%H:%M:%S')})
    except Exception as e:
        bot.send_message(chat_id, f"⚠️ Échec achat BSC {contract_address}: {str(e)}")

async def buy_token_eth(chat_id, contract_address, amount):
    try:
        if not w3_eth or not w3_eth.is_connected():
            initialize_bot(chat_id)
            if not w3_eth.is_connected():
                raise Exception("Ethereum non connecté")
        router = w3_eth.eth.contract(address=UNISWAP_ROUTER_ADDRESS, abi=PANCAKE_ROUTER_ABI)
        amount_in = w3_eth.to_wei(amount, 'ether')
        amount_out_min = int(amount_in * (1 - slippage_max))
        tx = router.functions.swapExactETHForTokens(
            amount_out_min,
            [w3_eth.to_checksum_address('0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2'), w3_eth.to_checksum_address(contract_address)],
            w3_eth.to_checksum_address(WALLET_ADDRESS),
            int(time.time()) + 60
        ).build_transaction({
            'from': WALLET_ADDRESS, 'value': amount_in, 'gas': 250000,
            'gasPrice': w3_eth.to_wei(gas_fee * 1.2, 'gwei'), 'nonce': w3_eth.eth.get_transaction_count(WALLET_ADDRESS)
        })
        signed_tx = w3_eth.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash = w3_eth.eth.send_raw_transaction(signed_tx.rawTransaction)
        bot.send_message(chat_id, f'⏳ Achat Ethereum {amount} ETH de {contract_address}, TX: {tx_hash.hex()}')
        receipt = w3_eth.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        if receipt.status == 1:
            entry_price = detected_tokens[contract_address]['price']
            portfolio[contract_address] = {
                'amount': amount, 'chain': 'eth', 'entry_price': entry_price,
                'market_cap_at_buy': detected_tokens[contract_address]['market_cap'],
                'current_market_cap': detected_tokens[contract_address]['market_cap'],
                'price_history': [entry_price], 'highest_price': entry_price, 'profit': 0.0,
                'buy_time': time.time()
            }
            bot.send_message(chat_id, f'✅ Achat réussi : {amount} ETH de {contract_address} (Uniswap)')
            daily_trades['buys'].append({'token': contract_address, 'chain': 'eth', 'amount': amount, 'timestamp': datetime.now().strftime('%H:%M:%S')})
    except Exception as e:
        bot.send_message(chat_id, f"⚠️ Échec achat Ethereum {contract_address}: {str(e)}")

async def buy_token_solana(chat_id, contract_address, amount):
    try:
        if not solana_keypair:
            initialize_bot(chat_id)
            if not solana_keypair:
                raise Exception("Solana non initialisé")
        amount_in = int(amount * 10**9)
        response = session.post(QUICKNODE_SOL_URL, json={
            "jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash", "params": [{"commitment": "finalized"}]
        }, timeout=2)
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
        }, timeout=2).json()['result']
        bot.send_message(chat_id, f'⏳ Achat Solana {amount} SOL de {contract_address}, TX: {tx_hash}')
        entry_price = detected_tokens[contract_address]['price']
        portfolio[contract_address] = {
            'amount': amount, 'chain': 'solana', 'entry_price': entry_price,
            'market_cap_at_buy': detected_tokens[contract_address]['market_cap'],
            'current_market_cap': detected_tokens[contract_address]['market_cap'],
            'price_history': [entry_price], 'highest_price': entry_price, 'profit': 0.0,
            'buy_time': time.time()
        }
        exchange = 'Raydium' if 'Raydium' in contract_address else 'Pump.fun'
        bot.send_message(chat_id, f'✅ Achat réussi : {amount} SOL de {contract_address} ({exchange})')
        daily_trades['buys'].append({'token': contract_address, 'chain': 'solana', 'amount': amount, 'timestamp': datetime.now().strftime('%H:%M:%S')})
    except Exception as e:
        bot.send_message(chat_id, f"⚠️ Échec achat Solana {contract_address}: {str(e)}")

async def sell_token(chat_id, contract_address, amount, chain, current_price):
    global mise_depart_bsc, mise_depart_sol, mise_depart_eth
    if chain == "solana":
        try:
            if not solana_keypair:
                initialize_bot(chat_id)
                if not solana_keypair:
                    raise Exception("Solana non initialisé")
            amount_out = int(amount * 10**9)
            response = session.post(QUICKNODE_SOL_URL, json={
                "jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash", "params": [{"commitment": "finalized"}]
            }, timeout=2)
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
            }, timeout=2).json()['result']
            bot.send_message(chat_id, f'⏳ Vente Solana {amount} SOL de {contract_address}, TX: {tx_hash}')
            profit = (current_price - portfolio[contract_address]['entry_price']) * amount
            portfolio[contract_address]['profit'] += profit
            portfolio[contract_address]['amount'] -= amount
            if portfolio[contract_address]['amount'] <= 0:
                del portfolio[contract_address]
            reinvest_amount = profit * profit_reinvestment_ratio
            mise_depart_sol += reinvest_amount
            exchange = 'Raydium' if 'Raydium' in contract_address else 'Pump.fun'
            bot.send_message(chat_id, f'✅ Vente Solana réussie : {amount} SOL, Profit: {profit:.4f} SOL, Réinvesti: {reinvest_amount:.4f} SOL ({exchange})')
            daily_trades['sells'].append({'token': contract_address, 'chain': 'solana', 'amount': amount, 'pnl': profit, 'timestamp': datetime.now().strftime('%H:%M:%S')})
        except Exception as e:
            bot.send_message(chat_id, f"⚠️ Échec vente Solana {contract_address}: {str(e)}")
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
                int(time.time()) + 60
            ).build_transaction({
                'from': WALLET_ADDRESS, 'gas': 250000,
                'gasPrice': w3_eth.to_wei(gas_fee * 1.2, 'gwei'), 'nonce': w3_eth.eth.get_transaction_count(WALLET_ADDRESS)
            })
            signed_tx = w3_eth.eth.account.sign_transaction(tx, PRIVATE_KEY)
            tx_hash = w3_eth.eth.send_raw_transaction(signed_tx.rawTransaction)
            bot.send_message(chat_id, f'⏳ Vente Ethereum {amount} ETH de {contract_address}, TX: {tx_hash.hex()}')
            receipt = w3_eth.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            if receipt.status == 1:
                profit = (current_price - portfolio[contract_address]['entry_price']) * amount
                portfolio[contract_address]['profit'] += profit
                portfolio[contract_address]['amount'] -= amount
                if portfolio[contract_address]['amount'] <= 0:
                    del portfolio[contract_address]
                reinvest_amount = profit * profit_reinvestment_ratio
                mise_depart_eth += reinvest_amount
                bot.send_message(chat_id, f'✅ Vente Ethereum réussie : {amount} ETH, Profit: {profit:.4f} ETH, Réinvesti: {reinvest_amount:.4f} ETH (Uniswap)')
                daily_trades['sells'].append({'token': contract_address, 'chain': 'eth', 'amount': amount, 'pnl': profit, 'timestamp': datetime.now().strftime('%H:%M:%S')})
        except Exception as e:
            bot.send_message(chat_id, f"⚠️ Échec vente Ethereum {contract_address}: {str(e)}")
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
                int(time.time()) + 60
            ).build_transaction({
                'from': WALLET_ADDRESS, 'gas': 250000,
                'gasPrice': w3_bsc.to_wei(gas_fee * 1.2, 'gwei'), 'nonce': w3_bsc.eth.get_transaction_count(WALLET_ADDRESS)
            })
            signed_tx = w3_bsc.eth.account.sign_transaction(tx, PRIVATE_KEY)
            tx_hash = w3_bsc.eth.send_raw_transaction(signed_tx.rawTransaction)
            bot.send_message(chat_id, f'⏳ Vente BSC {amount} BNB de {contract_address}, TX: {tx_hash.hex()}')
            receipt = w3_bsc.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            if receipt.status == 1:
                profit = (current_price - portfolio[contract_address]['entry_price']) * amount
                portfolio[contract_address]['profit'] += profit
                portfolio[contract_address]['amount'] -= amount
                if portfolio[contract_address]['amount'] <= 0:
                    del portfolio[contract_address]
                reinvest_amount = profit * profit_reinvestment_ratio
                mise_depart_bsc += reinvest_amount
                bot.send_message(chat_id, f'✅ Vente BSC réussie : {amount} BNB, Profit: {profit:.4f} BNB, Réinvesti: {reinvest_amount:.4f} BNB (PancakeSwap)')
                daily_trades['sells'].append({'token': contract_address, 'chain': 'bsc', 'amount': amount, 'pnl': profit, 'timestamp': datetime.now().strftime('%H:%M:%S')})
        except Exception as e:
            bot.send_message(chat_id, f"⚠️ Échec vente BSC {contract_address}: {str(e)}")

async def sell_token_percentage(chat_id, token, percentage):
    try:
        if token not in portfolio:
            bot.send_message(chat_id, f'⚠️ Vente impossible : {token} n\'est pas dans le portefeuille')
            return
        total_amount = portfolio[token]['amount']
        amount_to_sell = total_amount * (percentage / 100)
        chain = portfolio[token]['chain']
        market_cap = get_token_data(token, chain)['market_cap']
        supply = detected_tokens.get(token, {}).get('supply', 0)
        current_price = market_cap / supply if supply > 0 else 0
        await sell_token(chat_id, token, amount_to_sell, chain, current_price)
    except Exception as e:
        bot.send_message(chat_id, f"⚠️ Erreur vente partielle {token}: {str(e)}")

async def monitor_and_sell(chat_id):
    while trade_active:
        try:
            if not portfolio:
                await asyncio.sleep(5)
                continue
            for contract_address, data in list(portfolio.items()):
                chain = data['chain']
                amount = data['amount']
                current_data = get_token_data(contract_address, chain)
                if not current_data:
                    continue
                current_mc = current_data['market_cap']
                current_price = current_mc / data['supply'] if 'supply' in data and data['supply'] > 0 else 0
                data['price_history'].append(current_price)
                if len(data['price_history']) > 10:
                    data['price_history'].pop(0)
                portfolio[contract_address]['current_market_cap'] = current_mc
                profit_pct = (current_price - data['entry_price']) / data['entry_price'] * 100 if data['entry_price'] > 0 else 0
                loss_pct = -profit_pct if profit_pct < 0 else 0
                data['highest_price'] = max(data['highest_price'], current_price)
                trailing_stop_price = data['highest_price'] * (1 - trailing_stop_percentage / 100)

                if profit_pct >= take_profit_steps[2] * 100:
                    await sell_token(chat_id, contract_address, amount, chain, current_price)
                elif profit_pct >= take_profit_steps[1] * 100:
                    await sell_token(chat_id, contract_address, amount / 2, chain, current_price)
                elif profit_pct >= take_profit_steps[0] * 100:
                    await sell_token(chat_id, contract_address, amount / 3, chain, current_price)
                elif current_price <= trailing_stop_price or loss_pct >= stop_loss_threshold or current_data['buy_sell_ratio'] < 0.5:
                    await sell_token(chat_id, contract_address, amount, chain, current_price)
            await asyncio.sleep(5)
        except Exception as e:
            bot.send_message(chat_id, f"⚠️ Erreur surveillance: {str(e)}")
            await asyncio.sleep(10)

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
            current_mc = get_token_data(ca, chain)['market_cap']
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
        bot.send_message(chat_id, msg, reply_markup=markup if portfolio else None)
    except Exception as e:
        bot.send_message(chat_id, f'⚠️ Erreur portefeuille: {str(e)}')

async def get_solana_balance(chat_id):
    try:
        if not solana_keypair:
            initialize_bot(chat_id)
        if not solana_keypair:
            return 0
        response = session.post(QUICKNODE_SOL_URL, json={
            "jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [str(solana_keypair.pubkey())]
        }, timeout=2)
        return response.json().get('result', {}).get('value', 0) / 10**9
    except Exception as e:
        bot.send_message(chat_id, f"⚠️ Erreur solde Solana: {str(e)}")
        return 0

async def sell_token_immediate(chat_id, token):
    try:
        if token not in portfolio:
            bot.send_message(chat_id, f'⚠️ Vente impossible : {token} n\'est pas dans le portefeuille')
            return
        amount = portfolio[token]["amount"]
        chain = portfolio[token]['chain']
        current_price = get_token_data(token, chain)['market_cap'] / detected_tokens.get(token, {}).get('supply', 1)
        await sell_token(chat_id, token, amount, chain, current_price)
    except Exception as e:
        bot.send_message(chat_id, f'⚠️ Erreur vente immédiate {token}: {str(e)}')

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
            msg += f"- {trade['token']} ({trade['chain']}) : {trade['amount']} {trade['chain'].upper()} à {trade['timestamp']}, PNL: {trade['pnl']:.4f}\n"
        msg += f"Profit net : {total_profit['bsc']:.4f} BNB / {total_profit['eth']:.4f} ETH / {total_profit['solana']:.4f} SOL\n"
        bot.send_message(chat_id, msg)
    except Exception as e:
        bot.send_message(chat_id, f'⚠️ Erreur récapitulatif: {str(e)}')

async def adjust_mise_bsc(message):
    global mise_depart_bsc
    chat_id = message.chat.id
    try:
        new_mise = float(message.text)
        if new_mise > 0:
            mise_depart_bsc = new_mise
            bot.send_message(chat_id, f'✅ Mise BSC mise à jour à {mise_depart_bsc} BNB')
        else:
            bot.send_message(chat_id, "⚠️ La mise doit être positive!")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 0.02)")
    await show_main_menu(chat_id)

async def adjust_mise_eth(message):
    global mise_depart_eth
    chat_id = message.chat.id
    try:
        new_mise = float(message.text)
        if new_mise > 0:
            mise_depart_eth = new_mise
            bot.send_message(chat_id, f'✅ Mise Ethereum mise à jour à {mise_depart_eth} ETH')
        else:
            bot.send_message(chat_id, "⚠️ La mise doit être positive!")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 0.05)")
    await show_main_menu(chat_id)

async def adjust_mise_sol(message):
    global mise_depart_sol
    chat_id = message.chat.id
    try:
        new_mise = float(message.text)
        if new_mise > 0:
            mise_depart_sol = new_mise
            bot.send_message(chat_id, f'✅ Mise Solana mise à jour à {mise_depart_sol} SOL')
        else:
            bot.send_message(chat_id, "⚠️ La mise doit être positive!")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 0.37)")
    await show_main_menu(chat_id)

async def adjust_stop_loss(message):
    global stop_loss_threshold
    chat_id = message.chat.id
    try:
        new_sl = float(message.text)
        if new_sl > 0:
            stop_loss_threshold = new_sl
            bot.send_message(chat_id, f'✅ Stop-Loss mis à jour à {stop_loss_threshold} %')
        else:
            bot.send_message(chat_id, "⚠️ Le Stop-Loss doit être positif!")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un pourcentage valide (ex. : 15)")
    await show_main_menu(chat_id)

async def adjust_take_profit(message):
    global take_profit_steps
    chat_id = message.chat.id
    try:
        new_tp = [float(x) for x in message.text.split(",")]
        if len(new_tp) == 3 and all(x > 0 for x in new_tp):
            take_profit_steps = new_tp
            bot.send_message(chat_id, f'✅ Take-Profit mis à jour à x{take_profit_steps[0]}, x{take_profit_steps[1]}, x{take_profit_steps[2]}')
        else:
            bot.send_message(chat_id, "⚠️ Entrez 3 valeurs positives séparées par des virgules (ex. : 1.5,2,5)")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Erreur : Entrez des nombres valides (ex. : 1.5,2,5)")
    await show_main_menu(chat_id)

async def run_trading_tasks(chat_id):
    tasks = [
        snipe_new_pairs_bsc(chat_id),
        snipe_new_pairs_eth(chat_id),
        snipe_4meme_bsc_with_bscscan(chat_id),
        snipe_solana_pools(chat_id),
        monitor_x(chat_id),
        monitor_and_sell(chat_id)
    ]
    await asyncio.gather(*tasks)

def start_trading_thread(chat_id):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run_trading_tasks(chat_id))

def initialize_and_run_threads(chat_id):
    global trade_active
    try:
        initialize_bot(chat_id)
        if w3_bsc and w3_eth and solana_keypair:
            trade_active = True
            bot.send_message(chat_id, "▶️ Trading lancé avec succès!")
            threading.Thread(target=start_trading_thread, args=(chat_id,), daemon=True).start()
        else:
            bot.send_message(chat_id, "⚠️ Échec initialisation : Connexion(s) manquante(s)")
    except Exception as e:
        bot.send_message(chat_id, f"⚠️ Erreur initialisation: {str(e)}")
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
    bot.send_message(message.chat.id, "✅ Bot démarré!")
    asyncio.run(show_main_menu(message.chat.id))

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
    bot.send_message(chat_id, "Menu principal:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    global trade_active
    chat_id = call.message.chat.id
    try:
        if call.data == "status":
            bot.send_message(chat_id, (
                f"ℹ️ Statut actuel :\nTrading actif: {'Oui' if trade_active else 'Non'}\n"
                f"Mise BSC: {mise_depart_bsc} BNB\nMise Ethereum: {mise_depart_eth} ETH\nMise Solana: {mise_depart_sol} SOL\n"
                f"Positions: {len(portfolio)}/{max_positions}\n"
                f"Stop-Loss: {stop_loss_threshold}%\nTake-Profit: x{take_profit_steps[0]}, x{take_profit_steps[1]}, x{take_profit_steps[2]}"
            ))
        elif call.data == "launch":
            if not trade_active:
                initialize_and_run_threads(chat_id)
            else:
                bot.send_message(chat_id, "⚠️ Trading déjà en cours.")
        elif call.data == "stop":
            trade_active = False
            bot.send_message(chat_id, "⏹️ Trading arrêté.")
        elif call.data == "portfolio":
            asyncio.run(show_portfolio(chat_id))
        elif call.data == "daily_summary":
            asyncio.run(show_daily_summary(chat_id))
        elif call.data == "adjust_mise_bsc":
            bot.send_message(chat_id, "Entrez la nouvelle mise BSC (en BNB, ex. : 0.02) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_mise_bsc)
        elif call.data == "adjust_mise_eth":
            bot.send_message(chat_id, "Entrez la nouvelle mise Ethereum (en ETH, ex. : 0.05) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_mise_eth)
        elif call.data == "adjust_mise_sol":
            bot.send_message(chat_id, "Entrez la nouvelle mise Solana (en SOL, ex. : 0.37) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_mise_sol)
        elif call.data == "adjust_stop_loss":
            bot.send_message(chat_id, "Entrez le nouveau seuil de Stop-Loss (en %, ex. : 15) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_stop_loss)
        elif call.data == "adjust_take_profit":
            bot.send_message(chat_id, "Entrez les nouveaux seuils de Take-Profit (3 valeurs séparées par des virgules, ex. : 1.5,2,5) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_take_profit)
        elif call.data.startswith("sell_"):
            token = call.data.split("_")[1]
            asyncio.run(sell_token_immediate(chat_id, token))
        elif call.data.startswith("sell_pct_"):
            _, token, pct = call.data.split("_")
            asyncio.run(sell_token_percentage(chat_id, token, float(pct)))
    except Exception as e:
        bot.send_message(chat_id, f"⚠️ Erreur générale: {str(e)}")

if __name__ == "__main__":
    if set_webhook():
        logger.info("Webhook configuré, démarrage du serveur...")
        serve(app, host="0.0.0.0", port=PORT, threads=8)
    else:
        logger.error("Échec du webhook, passage en mode polling")
        bot.polling(none_stop=True)
