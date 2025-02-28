import os
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import logging
import requests
import time
import json
import base58
from flask import Flask, request, abort
from web3 import Web3, HTTPProvider
from web3.middleware import geth_poa_middleware
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from solders.instruction import Instruction
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import asyncio
from aiohttp import ClientSession
from statistics import mean
from typing import Dict, List, Optional
from datetime import datetime
import re
from solana.rpc.websocket_api import connect

# Configuration des logs
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Variables globales
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/91.0.4472.124", "Accept": "application/json"}
session = requests.Session()
session.headers.update(HEADERS)
retry_strategy = Retry(total=5, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("https://", adapter)

last_twitter_call = 0
last_valid_token_time = time.time()
twitter_last_reset = time.time()
twitter_requests_remaining = 450
daily_trades = {'buys': [], 'sells': []}
rejected_tokens = {}  # {token_address: timestamp_rejet}

logger.info("Chargement des variables d’environnement...")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY")
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")
TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN")
PORT = int(os.getenv("PORT", 8080))
BSC_RPC = os.getenv("BSC_RPC", "https://bsc-dataseed.binance.org/")
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")

BIRDEYE_HEADERS = {"X-API-KEY": BIRDEYE_API_KEY}
TWITTER_HEADERS = {"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"}

missing_vars = [var for var, val in {
    "TELEGRAM_TOKEN": TELEGRAM_TOKEN, "WALLET_ADDRESS": WALLET_ADDRESS, "PRIVATE_KEY": PRIVATE_KEY,
    "SOLANA_PRIVATE_KEY": SOLANA_PRIVATE_KEY, "WEBHOOK_URL": WEBHOOK_URL, "BIRDEYE_API_KEY": BIRDEYE_API_KEY,
    "TWITTER_BEARER_TOKEN": TWITTER_BEARER_TOKEN
}.items() if not val]
if missing_vars:
    logger.critical(f"Variables manquantes: {missing_vars}")
    raise ValueError(f"Variables manquantes: {missing_vars}")
logger.info("Variables principales chargées.")

# Initialisation globale
app = Flask(__name__)
logger.info("Flask initialisé.")
bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=False)
logger.info("Bot Telegram initialisé.")
w3 = None
solana_keypair = None

# Variables globales pour le trading
mise_depart_bsc = 0.02
mise_depart_sol = 0.37
gas_fee = 5
stop_loss_threshold = 15
trailing_stop_percentage = 5
take_profit_steps = [1.5, 2, 5]
detected_tokens = {}
trade_active = False
portfolio: Dict[str, dict] = {}
twitter_tokens = []
max_positions = 3
profit_reinvestment_ratio = 0.5

MIN_VOLUME_SOL = 5000
MAX_VOLUME_SOL = 500000
MIN_VOLUME_BSC = 5000
MAX_VOLUME_BSC = 500000
MIN_LIQUIDITY = 20000
MIN_MARKET_CAP_SOL = 50000
MAX_MARKET_CAP_SOL = 1000000
MIN_MARKET_CAP_BSC = 50000
MAX_MARKET_CAP_BSC = 1000000
MIN_BUY_SELL_RATIO_BSC = 1.5
MIN_BUY_SELL_RATIO_SOL = 1.5
MAX_HOLDER_PERCENTAGE = 20.0
MIN_RECENT_TXNS = 3
MAX_TOKEN_AGE_HOURS = 72
REJECT_EXPIRATION_HOURS = 24
RETRY_DELAY_MINUTES = 5

ERC20_ABI = json.loads('[{"constant": true, "inputs": [], "name": "totalSupply", "outputs": [{"name": "", "type": "uint256"}], "payable": false, "stateMutability": "view", "type": "function"}]')
PANCAKE_ROUTER_ADDRESS = "0x10ED43C718714eb63d5aA57B78B54704E256024E"
PANCAKE_FACTORY_ADDRESS = "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"
PANCAKE_FACTORY_ABI = [json.loads('{"anonymous": false, "inputs": [{"indexed": true, "internalType": "address", "name": "token0", "type": "address"}, {"indexed": true, "internalType": "address", "name": "token1", "type": "address"}, {"indexed": false, "internalType": "address", "name": "pair", "type": "address"}, {"indexed": false, "internalType": "uint256", "name": "type", "type": "uint256"}], "name": "PairCreated", "type": "event"}')]
PANCAKE_ROUTER_ABI = json.loads('[{"inputs": [{"internalType": "uint256", "name": "amountIn", "type": "uint256"}, {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"}, {"internalType": "address[]", "name": "path", "type": "address[]"}, {"internalType": "address", "name": "to", "type": "address"}, {"internalType": "uint256", "name": "deadline", "type": "uint256"}], "name": "swapExactETHForTokens", "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}], "stateMutability": "payable", "type": "function"}, {"inputs": [{"internalType": "uint256", "name": "amountOut", "type": "uint256"}, {"internalType": "uint256", "name": "amountInMax", "type": "uint256"}, {"internalType": "address[]", "name": "path", "type": "address[]"}, {"internalType": "address", "name": "to", "type": "address"}, {"internalType": "uint256", "name": "deadline", "type": "uint256"}], "name": "swapExactTokensForETH", "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}], "stateMutability": "nonpayable", "type": "function"}]')
RAYDIUM_PROGRAM_ID = Pubkey.from_string("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8")
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")

def initialize_bot():
    global w3, solana_keypair
    logger.info("Initialisation différée du bot commencée...")
    try:
        w3 = Web3(HTTPProvider(BSC_RPC))
        w3.middleware_onion.inject(geth_poa_middleware, layer=0)
        if not w3.is_connected():
            raise ConnectionError("Connexion BSC échouée")
        logger.info(f"Connexion BSC réussie. Bloc actuel : {w3.eth.block_number}")
        solana_keypair = Keypair.from_bytes(base58.b58decode(SOLANA_PRIVATE_KEY))
        logger.info("Clé Solana initialisée.")
    except Exception as e:
        logger.error(f"Erreur dans l'initialisation: {str(e)}")
        raise

def is_safe_token_bsc(token_address: str) -> bool:
    try:
        response = session.get(f"https://api.honeypot.is/v2/IsHoneypot?address={token_address}", timeout=10)
        data = response.json()
        return not data.get("isHoneypot", True) and data.get("buyTax", 0) <= 10 and data.get("sellTax", 0) <= 10
    except Exception as e:
        logger.error(f"Erreur vérification Honeypot BSC: {str(e)}")
        return False

def is_safe_token_solana(token_address: str) -> bool:
    try:
        response = session.get(f"https://public-api.birdeye.so/v1/token/token_security?address={token_address}", headers=BIRDEYE_HEADERS, timeout=10)
        data = response.json()['data']
        return data.get('is_open_source', False) and not data.get('is_honeypot', True)
    except Exception as e:
        logger.error(f"Erreur vérification sécurité Solana: {str(e)}")
        return False

def validate_address(token_address: str, chain: str) -> bool:
    if chain == 'bsc':
        return bool(re.match(r'^0x[a-fA-F0-9]{40}$', token_address))
    elif chain == 'solana':
        try:
            Pubkey.from_string(token_address)
            return len(token_address) >= 32 and len(token_address) <= 44
        except ValueError:
            return False
    return False

async def get_token_data(token_address: str, chain: str, session: ClientSession) -> Dict[str, float]:
    try:
        async with session.get(f"https://api.dexscreener.com/latest/dex/tokens/{token_address}", timeout=10) as resp:
            data = (await resp.json())['pairs'][0] if (await resp.json())['pairs'] else {}
            if not data or data.get('chainId') != chain:
                return {'volume_24h': 0, 'liquidity': 0, 'market_cap': 0, 'price': 0, 'buy_sell_ratio': 0, 'recent_buy_count': 0, 'pair_created_at': time.time()}
            
            volume_24h = float(data.get('volume', {}).get('h24', 0))
            liquidity = float(data.get('liquidity', {}).get('usd', 0))
            market_cap = float(data.get('marketCap', 0))
            price = float(data.get('priceUsd', 0))
            buy_count_5m = float(data.get('txns', {}).get('m5', {}).get('buys', 0))
            sell_count_5m = float(data.get('txns', {}).get('m5', {}).get('sells', 0))
            buy_sell_ratio_5m = buy_count_5m / max(sell_count_5m, 1)
            recent_buy_count = buy_count_5m
            pair_created_at = data.get('pairCreatedAt', 0) / 1000 if data.get('pairCreatedAt') else time.time() - 3600
            
            if chain == 'solana':
                top_holder_pct = await get_holder_distribution(token_address, chain, session)
            else:
                top_holder_pct = 0
            
            return {
                'volume_24h': volume_24h, 'liquidity': liquidity, 'market_cap': market_cap,
                'price': price, 'buy_sell_ratio': buy_sell_ratio_5m, 'recent_buy_count': recent_buy_count,
                'pair_created_at': pair_created_at, 'top_holder_pct': top_holder_pct
            }
    except Exception as e:
        logger.error(f"Erreur récupération données DexScreener {token_address} ({chain}): {str(e)}")
        return {'volume_24h': 0, 'liquidity': 0, 'market_cap': 0, 'price': 0, 'buy_sell_ratio': 0, 'recent_buy_count': 0, 'pair_created_at': time.time()}

async def get_holder_distribution(token_address: str, chain: str, session: ClientSession) -> float:
    if chain == 'solana':
        try:
            async with session.get(f"https://public-api.birdeye.so/v1/token/token_security?address={token_address}", headers=BIRDEYE_HEADERS, timeout=10) as resp:
                data = (await resp.json())['data']
                top_holder = max(data.get('top_10_holder_percent', 0), data.get('top_20_holder_percent', 0))
                return top_holder
        except Exception as e:
            logger.error(f"Erreur récupération distribution holders {token_address}: {str(e)}")
            return 100
    return 0

async def monitor_twitter(chat_id: int, session: ClientSession):
    global twitter_requests_remaining, twitter_last_reset, last_twitter_call
    bot.send_message(chat_id, "📡 Surveillance Twitter activée...")
    while trade_active:
        try:
            current_time = time.time()
            if current_time - twitter_last_reset >= 900:
                twitter_requests_remaining = 450
                twitter_last_reset = current_time
                logger.info("Quota Twitter réinitialisé : 450 requêtes.")

            if twitter_requests_remaining <= 1:
                wait_time = 900 - (current_time - twitter_last_reset) + 10
                bot.send_message(chat_id, f"⚠️ Quota Twitter épuisé, pause de {wait_time:.1f}s...")
                await asyncio.sleep(wait_time)
                continue

            url = "https://api.twitter.com/2/tweets/search/recent"
            params = {
                "query": '"contract address" OR CA OR launch OR pump -is:retweet',
                "max_results": 100,
                "expansions": "author_id",
                "user.fields": "public_metrics"
            }
            async with session.get(url, headers=TWITTER_HEADERS, params=params) as resp:
                resp.raise_for_status()
                twitter_requests_remaining -= 1
                last_twitter_call = current_time
                data = await resp.json()
                tweets = data.get('data', [])
                users = {u['id']: u for u in data.get('includes', {}).get('users', [])}

                for tweet in tweets:
                    user = users.get(tweet['author_id'])
                    followers = user.get('public_metrics', {}).get('followers_count', 0)
                    if followers >= 2000:
                        text = tweet['text'].lower()
                        for word in text.split():
                            chain = 'bsc' if word.startswith("0x") else 'solana'
                            if validate_address(word, chain) and word not in portfolio:
                                if word in rejected_tokens and (time.time() - rejected_tokens[word]) / 3600 < REJECT_EXPIRATION_HOURS:
                                    continue
                                bot.send_message(chat_id, f'🔍 Token Twitter (@{user["username"]}): {word}')
                                await pre_validate_token(chat_id, word, chain, session)
            await asyncio.sleep(10)
        except Exception as e:
            logger.error(f"Erreur Twitter: {str(e)}")
            bot.send_message(chat_id, f'⚠️ Erreur Twitter: {str(e)}. Reprise dans 60s...')
            await asyncio.sleep(60)

async def snipe_new_pairs_bsc(chat_id: int, session: ClientSession):
    bot.send_message(chat_id, "🔫 Sniping BSC activé...")
    factory = w3.eth.contract(address=PANCAKE_FACTORY_ADDRESS, abi=PANCAKE_FACTORY_ABI)
    while trade_active:
        try:
            latest_block = w3.eth.block_number
            events = factory.events.PairCreated.get_logs(fromBlock=latest_block - 10, toBlock=latest_block)
            for event in events:
                token_address = event['args']['token0'] if event['args']['token0'] != "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c" else event['args']['token1']
                if token_address not in portfolio and (token_address not in rejected_tokens or (time.time() - rejected_tokens[token_address]) / 3600 > REJECT_EXPIRATION_HOURS):
                    bot.send_message(chat_id, f'🎯 Snipe détecté : {token_address} (BSC)')
                    await pre_validate_token(chat_id, token_address, 'bsc', session)
            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Erreur sniping BSC: {str(e)}")
            bot.send_message(chat_id, f'⚠️ Erreur sniping BSC: {str(e)}. Reprise dans 5s...')
            await asyncio.sleep(5)

async def snipe_solana_pools(chat_id: int, session: ClientSession):
    async with connect(SOLANA_RPC.replace("https", "wss")) as ws:
        await ws.program_subscribe(RAYDIUM_PROGRAM_ID, commitment="confirmed")
        async for msg in ws:
            try:
                token_address = extract_token_address(msg)  # À implémenter selon logs Raydium
                if token_address and validate_address(token_address, 'solana'):
                    bot.send_message(chat_id, f"🎯 Nouveau pool Solana: {token_address}")
                    await pre_validate_token(chat_id, token_address, 'solana', session)
            except Exception as e:
                logger.error(f"Erreur sniping Solana: {str(e)}")
                await asyncio.sleep(5)

def extract_token_address(msg) -> str:
    # Placeholder : À adapter selon les logs WebSocket Raydium
    try:
        data = str(msg)
        match = re.search(r'[A-Za-z0-9]{32,44}', data)
        return match.group(0) if match else None
    except Exception:
        return None

async def pre_validate_token(chat_id: int, token_address: str, chain: str, session: ClientSession):
    for _ in range(6):  # Vérifie toutes les 10s pendant 1 min
        data = await get_token_data(token_address, chain, session)
        if data['volume_24h'] >= (MIN_VOLUME_BSC if chain == 'bsc' else MIN_VOLUME_SOL) and \
           data['buy_sell_ratio'] >= (MIN_BUY_SELL_RATIO_BSC if chain == 'bsc' else MIN_BUY_SELL_RATIO_SOL):
            bot.send_message(chat_id, f'✅ Critères atteints pour {token_address}')
            return await check_token(chat_id, token_address, chain, session)
        await asyncio.sleep(10)
    bot.send_message(chat_id, f'⚠️ {token_address} n’a pas atteint les critères en 1 min')
    rejected_tokens[token_address] = time.time()
    return False

async def check_token(chat_id: int, token_address: str, chain: str, session: ClientSession) -> bool:
    try:
        if len(portfolio) >= max_positions:
            bot.send_message(chat_id, f'⚠️ Limite de {max_positions} positions atteinte.')
            return False

        data = await get_token_data(token_address, chain, session)
        volume_24h = data['volume_24h']
        liquidity = data['liquidity']
        market_cap = data['market_cap']
        buy_sell_ratio = data['buy_sell_ratio']
        recent_buy_count = data['recent_buy_count']
        price = data['price']
        pair_created_at = data['pair_created_at']
        top_holder_pct = data.get('top_holder_pct', 0)

        age_hours = (time.time() - pair_created_at) / 3600
        if chain == 'bsc':
            min_volume, max_volume = MIN_VOLUME_BSC, MAX_VOLUME_BSC
            min_market_cap, max_market_cap = MIN_MARKET_CAP_BSC, MAX_MARKET_CAP_BSC
        else:
            min_volume, max_volume = MIN_VOLUME_SOL, MAX_VOLUME_SOL
            min_market_cap, max_market_cap = MIN_MARKET_CAP_SOL, MAX_MARKET_CAP_SOL

        if age_hours > MAX_TOKEN_AGE_HOURS or age_hours < 0:
            bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : âge {age_hours:.2f}h > {MAX_TOKEN_AGE_HOURS}h')
            rejected_tokens[token_address] = time.time()
            return False
        if buy_sell_ratio < (MIN_BUY_SELL_RATIO_BSC if chain == 'bsc' else MIN_BUY_SELL_RATIO_SOL):
            bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : ratio {buy_sell_ratio:.2f} < {(MIN_BUY_SELL_RATIO_BSC if chain == "bsc" else MIN_BUY_SELL_RATIO_SOL)}')
            rejected_tokens[token_address] = time.time()
            return False
        if volume_24h < min_volume or volume_24h > max_volume:
            bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : volume ${volume_24h} hors plage [{min_volume}, {max_volume}]')
            rejected_tokens[token_address] = time.time()
            return False
        if liquidity < MIN_LIQUIDITY:
            bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : liquidité ${liquidity} < ${MIN_LIQUIDITY}')
            rejected_tokens[token_address] = time.time()
            return False
        if market_cap < min_market_cap or market_cap > max_market_cap:
            bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : market cap ${market_cap} hors plage [{min_market_cap}, {max_market_cap}]')
            rejected_tokens[token_address] = time.time()
            return False
        if recent_buy_count < MIN_RECENT_TXNS:
            bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : {recent_buy_count} achats récents < {MIN_RECENT_TXNS}')
            rejected_tokens[token_address] = time.time()
            return False
        if chain == 'solana' and top_holder_pct > MAX_HOLDER_PERCENTAGE:
            bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : top holder {top_holder_pct}% > {MAX_HOLDER_PERCENTAGE}%')
            rejected_tokens[token_address] = time.time()
            return False
        if chain == 'bsc' and not is_safe_token_bsc(token_address):
            bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : possible rug ou taxes élevées')
            rejected_tokens[token_address] = time.time()
            return False
        if chain == 'solana' and not is_safe_token_solana(token_address):
            bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : possible rug ou non open-source')
            rejected_tokens[token_address] = time.time()
            return False

        bot.send_message(chat_id, f'🔍 Token détecté : {token_address} ({chain}) - Ratio A/V: {buy_sell_ratio:.2f}, Vol: ${volume_24h:.2f}, Liq: ${liquidity:.2f}, MC: ${market_cap:.2f}')
        detected_tokens[token_address] = {
            'address': token_address, 'volume': volume_24h, 'liquidity': liquidity,
            'market_cap': market_cap, 'supply': market_cap / price, 'price': price,
            'buy_sell_ratio': buy_sell_ratio, 'recent_buy_count': recent_buy_count
        }
        if chain == 'bsc':
            await buy_token_bsc(chat_id, token_address, mise_depart_bsc, session)
        else:
            await buy_token_solana(chat_id, token_address, mise_depart_sol, session)
        daily_trades['buys'].append({'token': token_address, 'chain': chain, 'amount': mise_depart_bsc if chain == 'bsc' else mise_depart_sol, 'timestamp': datetime.now().strftime('%H:%M:%S')})
        return True
    except Exception as e:
        logger.error(f"Erreur vérification token {token_address}: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Erreur vérification token {token_address}: {str(e)}')
        rejected_tokens[token_address] = time.time()
        return False

async def detect_new_tokens_bsc(chat_id: int, session: ClientSession):
    bot.send_message(chat_id, "🔍 Début détection BSC...")
    try:
        if not w3.is_connected():
            raise ConnectionError("Connexion au nœud BSC perdue")
        factory = w3.eth.contract(address=PANCAKE_FACTORY_ADDRESS, abi=PANCAKE_FACTORY_ABI)
        latest_block = w3.eth.block_number
        events = factory.events.PairCreated.get_logs(fromBlock=latest_block - 200, toBlock=latest_block)
        bot.send_message(chat_id, f"⬇️ {len(events)} nouvelles paires détectées sur BSC")
        if not events:
            bot.send_message(chat_id, "ℹ️ Aucun événement PairCreated détecté.")
            return

        rejected_count = 0
        for event in events:
            token_address = event['args']['token0'] if event['args']['token0'] != "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c" else event['args']['token1']
            if token_address not in portfolio and (token_address not in rejected_tokens or (time.time() - rejected_tokens[token_address]) / 3600 > REJECT_EXPIRATION_HOURS):
                result = await check_token(chat_id, token_address, 'bsc', session)
                if not result:
                    rejected_count += 1
        if rejected_count > 0:
            bot.send_message(chat_id, f'⚠️ Aucun token BSC ne correspond aux critères ({rejected_count} rejetés).')
        bot.send_message(chat_id, "✅ Détection BSC terminée.")
    except Exception as e:
        logger.error(f"Erreur détection BSC: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Erreur détection BSC: {str(e)}')

async def detect_new_tokens_solana(chat_id: int, session: ClientSession):
    bot.send_message(chat_id, "🔍 Début détection Solana...")
    try:
        url = "https://public-api.birdeye.so/v1/pairs/list?sort_by=time&sort_type=desc&limit=20"
        async with session.get(url, headers=BIRDEYE_HEADERS) as resp:
            data = await resp.json()
            tokens = data.get('data', {}).get('pairs', [])
            bot.send_message(chat_id, f"⬇️ {len(tokens)} paires détectées sur Solana")
            for token in tokens[:10]:
                token_address = token['address']
                if token_address not in portfolio and (token_address not in rejected_tokens or (time.time() - rejected_tokens[token_address]) / 3600 > REJECT_EXPIRATION_HOURS):
                    volume_24h = float(token.get('volume_24h', 0))
                    age_hours = (time.time() - token.get('created_at', 0) / 1000) / 3600
                    if age_hours <= MAX_TOKEN_AGE_HOURS and volume_24h >= MIN_VOLUME_SOL:
                        bot.send_message(chat_id, f'🆕 Token Solana détecté : {token_address} (Vol: ${volume_24h:.2f})')
                        await check_token(chat_id, token_address, 'solana', session)
    except Exception as e:
        logger.error(f"Erreur détection Solana: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Erreur détection Solana: {str(e)}. Reprise dans 5s...')
        await asyncio.sleep(5)

@app.route("/webhook", methods=['POST'])
def webhook():
    logger.info("Webhook reçu")
    try:
        if request.method == "POST" and request.headers.get("content-type") == "application/json":
            update = telebot.types.Update.de_json(request.get_json())
            bot.process_new_updates([update])
            return 'OK', 200
        return abort(403)
    except Exception as e:
        logger.error(f"Erreur dans webhook: {str(e)}")
        return abort(500)

@bot.message_handler(commands=['start'])
def start_message(message):
    logger.info("Commande /start reçue")
    try:
        bot.send_message(message.chat.id, "✅ Bot démarré!")
        show_main_menu(message.chat.id)
    except Exception as e:
        logger.error(f"Erreur dans start_message: {str(e)}")
        bot.send_message(message.chat.id, f'⚠️ Erreur au démarrage: {str(e)}')

@bot.message_handler(commands=['menu'])
def menu_message(message):
    logger.info("Commande /menu reçue")
    try:
        bot.send_message(message.chat.id, "📋 Menu affiché!")
        show_main_menu(message.chat.id)
    except Exception as e:
        logger.error(f"Erreur dans menu_message: {str(e)}")
        bot.send_message(message.chat.id, f'⚠️ Erreur affichage menu: {str(e)}')

def show_main_menu(chat_id: int) -> None:
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("ℹ️ Statut", callback_data="status"),
        InlineKeyboardButton("⚙️ Configure", callback_data="config"),
        InlineKeyboardButton("▶️ Lancer", callback_data="launch"),
        InlineKeyboardButton("⏹️ Arrêter", callback_data="stop"),
        InlineKeyboardButton("💰 Portefeuille", callback_data="portfolio"),
        InlineKeyboardButton("📅 Récapitulatif", callback_data="daily_summary"),
        InlineKeyboardButton("📋 Gestion Tokens", callback_data="manage_tokens"),
        InlineKeyboardButton("🔧 Réglages", callback_data="settings"),
        InlineKeyboardButton("📈 TP/SL", callback_data="tp_sl_settings"),
        InlineKeyboardButton("📊 Seuils", callback_data="threshold_settings")
    )
    bot.send_message(chat_id, "Voici le menu principal:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    global mise_depart_bsc, mise_depart_sol, trade_active, gas_fee, stop_loss_threshold, take_profit_steps
    global MIN_VOLUME_BSC, MAX_VOLUME_BSC, MIN_LIQUIDITY, MIN_MARKET_CAP_BSC, MAX_MARKET_CAP_BSC
    global MIN_VOLUME_SOL, MAX_VOLUME_SOL, MIN_MARKET_CAP_SOL, MAX_MARKET_CAP_SOL, MIN_BUY_SELL_RATIO_BSC, MIN_BUY_SELL_RATIO_SOL
    chat_id = call.message.chat.id
    logger.info(f"Callback reçu: {call.data}")
    try:
        if call.data == "status":
            bot.send_message(chat_id, (
                f"ℹ️ Statut actuel :\nTrading actif: {'Oui' if trade_active else 'Non'}\n"
                f"Mise BSC: {mise_depart_bsc} BNB\nMise Solana: {mise_depart_sol} SOL\n"
                f"Gas Fee: {gas_fee} Gwei\nPositions: {len(portfolio)}/{max_positions}"
            ))
        elif call.data == "config":
            show_config_menu(chat_id)
        elif call.data == "launch":
            if not trade_active:
                trade_active = True
                bot.send_message(chat_id, "▶️ Trading lancé avec succès!")
                asyncio.run_coroutine_threadsafe(trading_cycle(chat_id, ClientSession()), asyncio.get_event_loop())
                asyncio.run_coroutine_threadsafe(snipe_new_pairs_bsc(chat_id, ClientSession()), asyncio.get_event_loop())
                asyncio.run_coroutine_threadsafe(monitor_twitter(chat_id, ClientSession()), asyncio.get_event_loop())
                asyncio.run_coroutine_threadsafe(snipe_solana_pools(chat_id, ClientSession()), asyncio.get_event_loop())
                asyncio.run_coroutine_threadsafe(watchdog(chat_id), asyncio.get_event_loop())
            else:
                bot.send_message(chat_id, "⚠️ Trading déjà en cours.")
        elif call.data == "stop":
            trade_active = False
            bot.send_message(chat_id, "⏹️ Trading arrêté.")
        elif call.data == "portfolio":
            show_portfolio(chat_id)
        elif call.data == "daily_summary":
            show_daily_summary(chat_id)
        elif call.data == "manage_tokens":
            show_token_management(chat_id)
        elif call.data == "settings":
            show_settings_menu(chat_id)
        elif call.data == "tp_sl_settings":
            show_tp_sl_menu(chat_id)
        elif call.data == "threshold_settings":
            show_threshold_menu(chat_id)
        elif call.data == "increase_mise_bsc":
            mise_depart_bsc += 0.01
            bot.send_message(chat_id, f'🔍 Mise BSC augmentée à {mise_depart_bsc} BNB')
        elif call.data == "increase_mise_sol":
            mise_depart_sol += 0.01
            bot.send_message(chat_id, f'🔍 Mise Solana augmentée à {mise_depart_sol} SOL')
        elif call.data == "adjust_mise_bsc":
            bot.send_message(chat_id, "Entrez la nouvelle mise BSC (en BNB, ex. : 0.02) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_mise_bsc)
        elif call.data == "adjust_mise_sol":
            bot.send_message(chat_id, "Entrez la nouvelle mise Solana (en SOL, ex. : 0.37) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_mise_sol)
        elif call.data == "adjust_gas":
            bot.send_message(chat_id, "Entrez les nouveaux frais de gas (en Gwei, ex. : 5) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_gas_fee)
        elif call.data == "adjust_stop_loss":
            bot.send_message(chat_id, "Entrez le nouveau seuil de Stop-Loss (en %, ex. : 15) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_stop_loss)
        elif call.data == "adjust_take_profit":
            bot.send_message(chat_id, "Entrez les nouveaux seuils de Take-Profit (3 valeurs séparées par des virgules, ex. : 1.5,2,5) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_take_profit)
        elif call.data == "adjust_min_volume_bsc":
            bot.send_message(chat_id, "Entrez le nouveau seuil min de volume BSC (en $, ex. : 5000) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_min_volume_bsc)
        elif call.data == "adjust_max_volume_bsc":
            bot.send_message(chat_id, "Entrez le nouveau seuil max de volume BSC (en $, ex. : 500000) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_max_volume_bsc)
        elif call.data == "adjust_min_liquidity":
            bot.send_message(chat_id, "Entrez le nouveau seuil min de liquidité (en $, ex. : 20000) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_min_liquidity)
        elif call.data == "adjust_min_market_cap_bsc":
            bot.send_message(chat_id, "Entrez le nouveau seuil min de market cap BSC (en $, ex. : 50000) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_min_market_cap_bsc)
        elif call.data == "adjust_max_market_cap_bsc":
            bot.send_message(chat_id, "Entrez le nouveau seuil max de market cap BSC (en $, ex. : 1000000) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_max_market_cap_bsc)
        elif call.data == "adjust_min_volume_sol":
            bot.send_message(chat_id, "Entrez le nouveau seuil min de volume Solana (en $, ex. : 5000) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_min_volume_sol)
        elif call.data == "adjust_max_volume_sol":
            bot.send_message(chat_id, "Entrez le nouveau seuil max de volume Solana (en $, ex. : 500000) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_max_volume_sol)
        elif call.data == "adjust_min_market_cap_sol":
            bot.send_message(chat_id, "Entrez le nouveau seuil min de market cap Solana (en $, ex. : 50000) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_min_market_cap_sol)
        elif call.data == "adjust_max_market_cap_sol":
            bot.send_message(chat_id, "Entrez le nouveau seuil max de market cap Solana (en $, ex. : 1000000) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_max_market_cap_sol)
        elif call.data == "adjust_buy_sell_ratio_bsc":
            bot.send_message(chat_id, "Entrez le nouveau ratio achat/vente min pour BSC (ex. : 1.5) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_buy_sell_ratio_bsc)
        elif call.data == "adjust_buy_sell_ratio_sol":
            bot.send_message(chat_id, "Entrez le nouveau ratio achat/vente min pour Solana (ex. : 1.5) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_buy_sell_ratio_sol)
        elif call.data.startswith("refresh_"):
            token = call.data.split("_")[1]
            refresh_token(chat_id, token)
        elif call.data.startswith("sell_"):
            token = call.data.split("_")[1]
            sell_token_immediate(chat_id, token)
        elif call.data.startswith("sell_pct_"):
            _, token, pct = call.data.split("_")
            sell_token_percentage(chat_id, token, float(pct))
    except Exception as e:
        logger.error(f"Erreur dans callback_query: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Erreur générale: {str(e)}')

async def trading_cycle(chat_id: int, session: ClientSession):
    cycle_count = 0
    asyncio.create_task(monitor_and_sell(chat_id, session))
    while trade_active:
        try:
            cycle_count += 1
            bot.send_message(chat_id, f'🔍 Début du cycle de détection #{cycle_count}...')
            await detect_new_tokens_bsc(chat_id, session)
            await detect_new_tokens_solana(chat_id, session)
            await asyncio.sleep(10)  # Réduit pour plus de réactivité
        except Exception as e:
            logger.error(f"Erreur dans trading_cycle: {str(e)}")
            bot.send_message(chat_id, f'⚠️ Erreur dans le cycle: {str(e)}. Reprise dans 10s...')
            await asyncio.sleep(10)

async def watchdog(chat_id: int):
    logger.info("Watchdog démarré...")
    while trade_active:
        await asyncio.sleep(60)  # Pas besoin de vérifier les threads avec asyncio

def show_config_menu(chat_id: int) -> None:
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("➕ Augmenter mise BSC (+0.01 BNB)", callback_data="increase_mise_bsc"),
        InlineKeyboardButton("➕ Augmenter mise SOL (+0.01 SOL)", callback_data="increase_mise_sol")
    )
    bot.send_message(chat_id, "⚙️ Configuration:", reply_markup=markup)

def show_settings_menu(chat_id: int) -> None:
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("🔧 Ajuster Mise BSC", callback_data="adjust_mise_bsc"),
        InlineKeyboardButton("🔧 Ajuster Mise Solana", callback_data="adjust_mise_sol"),
        InlineKeyboardButton("🔧 Ajuster Gas Fee (BSC)", callback_data="adjust_gas")
    )
    bot.send_message(chat_id, "🔧 Réglages:", reply_markup=markup)

def show_tp_sl_menu(chat_id: int) -> None:
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("📉 Ajuster Stop-Loss", callback_data="adjust_stop_loss"),
        InlineKeyboardButton("📈 Ajuster Take-Profit", callback_data="adjust_take_profit")
    )
    bot.send_message(chat_id, "📈 Configuration TP/SL", reply_markup=markup)

def show_threshold_menu(chat_id: int) -> None:
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("📊 Min Volume BSC", callback_data="adjust_min_volume_bsc"),
        InlineKeyboardButton("📊 Max Volume BSC", callback_data="adjust_max_volume_bsc"),
        InlineKeyboardButton("📊 Min Liquidité", callback_data="adjust_min_liquidity"),
        InlineKeyboardButton("📊 Min Market Cap BSC", callback_data="adjust_min_market_cap_bsc"),
        InlineKeyboardButton("📊 Max Market Cap BSC", callback_data="adjust_max_market_cap_bsc"),
        InlineKeyboardButton("📊 Min Volume Solana", callback_data="adjust_min_volume_sol"),
        InlineKeyboardButton("📊 Max Volume Solana", callback_data="adjust_max_volume_sol"),
        InlineKeyboardButton("📊 Min Market Cap Solana", callback_data="adjust_min_market_cap_sol"),
        InlineKeyboardButton("📊 Max Market Cap Solana", callback_data="adjust_max_market_cap_sol"),
        InlineKeyboardButton("📊 Ratio A/V BSC", callback_data="adjust_buy_sell_ratio_bsc"),
        InlineKeyboardButton("📊 Ratio A/V Solana", callback_data="adjust_buy_sell_ratio_sol")
    )
    bot.send_message(chat_id, (
        f'📊 Seuils de détection :\n- BSC Volume: {MIN_VOLUME_BSC} $ - {MAX_VOLUME_BSC} $\n'
        f'- BSC Ratio A/V: {MIN_BUY_SELL_RATIO_BSC}\n'
        f'- BSC Market Cap : {MIN_MARKET_CAP_BSC} $ - {MAX_MARKET_CAP_BSC} $\n'
        f'- Min Liquidité : {MIN_LIQUIDITY} $\n- Solana Volume: {MIN_VOLUME_SOL} $ - {MAX_VOLUME_SOL} $\n'
        f'- Solana Ratio A/V: {MIN_BUY_SELL_RATIO_SOL}\n'
        f'- Solana Market Cap : {MIN_MARKET_CAP_SOL} $ - {MAX_MARKET_CAP_SOL} $\n'
        f'- Âge max : {MAX_TOKEN_AGE_HOURS}h'
    ), reply_markup=markup)

def adjust_mise_bsc(message):
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

def adjust_mise_sol(message):
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

def adjust_gas_fee(message):
    global gas_fee
    chat_id = message.chat.id
    try:
        new_gas_fee = float(message.text)
        if new_gas_fee > 0:
            gas_fee = new_gas_fee
            bot.send_message(chat_id, f'✅ Frais de gas mis à jour à {gas_fee} Gwei')
        else:
            bot.send_message(chat_id, "⚠️ Les frais de gas doivent être positifs!")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 5)")

def adjust_stop_loss(message):
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

def adjust_take_profit(message):
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

def adjust_min_volume_bsc(message):
    global MIN_VOLUME_BSC
    chat_id = message.chat.id
    try:
        new_value = float(message.text)
        if new_value >= 0:
            MIN_VOLUME_BSC = new_value
            bot.send_message(chat_id, f'✅ Min Volume BSC mis à jour à ${MIN_VOLUME_BSC}')
        else:
            bot.send_message(chat_id, "⚠️ La valeur doit être positive!")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 5000)")

def adjust_max_volume_bsc(message):
    global MAX_VOLUME_BSC
    chat_id = message.chat.id
    try:
        new_value = float(message.text)
        if new_value >= MIN_VOLUME_BSC:
            MAX_VOLUME_BSC = new_value
            bot.send_message(chat_id, f'✅ Max Volume BSC mis à jour à ${MAX_VOLUME_BSC}')
        else:
            bot.send_message(chat_id, "⚠️ La valeur doit être supérieure au minimum!")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 500000)")

def adjust_min_liquidity(message):
    global MIN_LIQUIDITY
    chat_id = message.chat.id
    try:
        new_value = float(message.text)
        if new_value >= 0:
            MIN_LIQUIDITY = new_value
            bot.send_message(chat_id, f'✅ Min Liquidité mis à jour à ${MIN_LIQUIDITY}')
        else:
            bot.send_message(chat_id, "⚠️ La valeur doit être positive!")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 20000)")

def adjust_min_market_cap_bsc(message):
    global MIN_MARKET_CAP_BSC
    chat_id = message.chat.id
    try:
        new_value = float(message.text)
        if new_value >= 0:
            MIN_MARKET_CAP_BSC = new_value
            bot.send_message(chat_id, f'✅ Min Market Cap BSC mis à jour à ${MIN_MARKET_CAP_BSC}')
        else:
            bot.send_message(chat_id, "⚠️ La valeur doit être positive!")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 50000)")

def adjust_max_market_cap_bsc(message):
    global MAX_MARKET_CAP_BSC
    chat_id = message.chat.id
    try:
        new_value = float(message.text)
        if new_value >= MIN_MARKET_CAP_BSC:
            MAX_MARKET_CAP_BSC = new_value
            bot.send_message(chat_id, f'✅ Max Market Cap BSC mis à jour à ${MAX_MARKET_CAP_BSC}')
        else:
            bot.send_message(chat_id, "⚠️ La valeur doit être supérieure au minimum!")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 1000000)")

def adjust_min_volume_sol(message):
    global MIN_VOLUME_SOL
    chat_id = message.chat.id
    try:
        new_value = float(message.text)
        if new_value >= 0:
            MIN_VOLUME_SOL = new_value
            bot.send_message(chat_id, f'✅ Min Volume Solana mis à jour à ${MIN_VOLUME_SOL}')
        else:
            bot.send_message(chat_id, "⚠️ La valeur doit être positive!")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 5000)")

def adjust_max_volume_sol(message):
    global MAX_VOLUME_SOL
    chat_id = message.chat.id
    try:
        new_value = float(message.text)
        if new_value >= MIN_VOLUME_SOL:
            MAX_VOLUME_SOL = new_value
            bot.send_message(chat_id, f'✅ Max Volume Solana mis à jour à ${MAX_VOLUME_SOL}')
        else:
            bot.send_message(chat_id, "⚠️ La valeur doit être supérieure au minimum!")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 500000)")

def adjust_min_market_cap_sol(message):
    global MIN_MARKET_CAP_SOL
    chat_id = message.chat.id
    try:
        new_value = float(message.text)
        if new_value >= 0:
            MIN_MARKET_CAP_SOL = new_value
            bot.send_message(chat_id, f'✅ Min Market Cap Solana mis à jour à ${MIN_MARKET_CAP_SOL}')
        else:
            bot.send_message(chat_id, "⚠️ La valeur doit être positive!")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 50000)")

def adjust_max_market_cap_sol(message):
    global MAX_MARKET_CAP_SOL
    chat_id = message.chat.id
    try:
        new_value = float(message.text)
        if new_value >= MIN_MARKET_CAP_SOL:
            MAX_MARKET_CAP_SOL = new_value
            bot.send_message(chat_id, f'✅ Max Market Cap Solana mis à jour à ${MAX_MARKET_CAP_SOL}')
        else:
            bot.send_message(chat_id, "⚠️ La valeur doit être supérieure au minimum!")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 1000000)")

def adjust_buy_sell_ratio_bsc(message):
    global MIN_BUY_SELL_RATIO_BSC
    chat_id = message.chat.id
    try:
        new_ratio = float(message.text)
        if new_ratio > 0:
            MIN_BUY_SELL_RATIO_BSC = new_ratio
            bot.send_message(chat_id, f'✅ Ratio A/V BSC mis à jour à {MIN_BUY_SELL_RATIO_BSC}')
        else:
            bot.send_message(chat_id, "⚠️ Le ratio doit être positif!")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 1.5)")

def adjust_buy_sell_ratio_sol(message):
    global MIN_BUY_SELL_RATIO_SOL
    chat_id = message.chat.id
    try:
        new_ratio = float(message.text)
        if new_ratio > 0:
            MIN_BUY_SELL_RATIO_SOL = new_ratio
            bot.send_message(chat_id, f'✅ Ratio A/V Solana mis à jour à {MIN_BUY_SELL_RATIO_SOL}')
        else:
            bot.send_message(chat_id, "⚠️ Le ratio doit être positif!")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 1.5)")

async def buy_token_bsc(chat_id: int, contract_address: str, amount: float, session: ClientSession):
    try:
        dynamic_slippage = 10
        router = w3.eth.contract(address=PANCAKE_ROUTER_ADDRESS, abi=PANCAKE_ROUTER_ABI)
        amount_in = w3.to_wei(amount, 'ether')
        amount_out_min = int(amount_in * (1 - dynamic_slippage / 100))
        gas_price = max(gas_fee, w3.eth.gas_price / 10**9)
        tx = router.functions.swapExactETHForTokens(
            amount_out_min,
            [w3.to_checksum_address('0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c'), w3.to_checksum_address(contract_address)],
            w3.to_checksum_address(WALLET_ADDRESS),
            int(time.time()) + 60
        ).build_transaction({
            'from': WALLET_ADDRESS, 'value': amount_in, 'gas': 200000,
            'gasPrice': w3.to_wei(gas_price, 'gwei'), 'nonce': w3.eth.get_transaction_count(WALLET_ADDRESS)
        })
        signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
        bot.send_message(chat_id, f'⏳ Achat en cours de {amount} BNB de {contract_address}, TX: {tx_hash.hex()}')
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=10)
        if receipt.status == 1:
            entry_price = detected_tokens[contract_address]['price']
            portfolio[contract_address] = {
                'amount': amount, 'chain': 'bsc', 'entry_price': entry_price,
                'market_cap_at_buy': detected_tokens[contract_address]['market_cap'],
                'current_market_cap': detected_tokens[contract_address]['market_cap'],
                'price_history': [entry_price], 'highest_price': entry_price, 'profit': 0.0,
                'buy_time': time.time(), 'volume': detected_tokens[contract_address]['volume']
            }
            bot.send_message(chat_id, f'✅ Achat effectué : {amount} BNB de {contract_address}')
        else:
            bot.send_message(chat_id, f'⚠️ Échec achat {contract_address}, TX: {tx_hash.hex()}')
            daily_trades['buys'][-1]['error'] = "Transaction échouée"
    except Exception as e:
        logger.error(f"Erreur achat BSC: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Échec achat {contract_address}: {str(e)}')
        daily_trades['buys'][-1]['error'] = str(e)

async def buy_token_solana(chat_id: int, contract_address: str, amount: float, session: ClientSession):
    try:
        dynamic_slippage = 10
        amount_in = int(amount * 10**9)
        async with session.post(SOLANA_RPC, json={
            "jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash", "params": [{"commitment": "finalized"}]
        }) as resp:
            blockhash = (await resp.json())['result']['value']['blockhash']
        tx = Transaction.from_recent_blockhash(Pubkey.from_string(blockhash))
        instruction = Instruction(
            program_id=RAYDIUM_PROGRAM_ID,
            accounts=[
                {"pubkey": solana_keypair.pubkey(), "is_signer": True, "is_writable": True},
                {"pubkey": Pubkey.from_string(contract_address), "is_signer": False, "is_writable": True},
                {"pubkey": TOKEN_PROGRAM_ID, "is_signer": False, "is_writable": False}
            ],
            data=bytes([2]) + amount_in.to_bytes(8, 'little')
        )
        tx.add(instruction)
        tx.sign([solana_keypair])
        async with session.post(SOLANA_RPC, json={
            "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
            "params": [base58.b58encode(tx.serialize()).decode('utf-8')]
        }) as resp:
            tx_hash = (await resp.json())['result']
        bot.send_message(chat_id, f'⏳ Achat en cours de {amount} SOL de {contract_address}, TX: {tx_hash}')
        await asyncio.sleep(2)
        entry_price = detected_tokens[contract_address]['price']
        portfolio[contract_address] = {
            'amount': amount, 'chain': 'solana', 'entry_price': entry_price,
            'market_cap_at_buy': detected_tokens[contract_address]['market_cap'],
            'current_market_cap': detected_tokens[contract_address]['market_cap'],
            'price_history': [entry_price], 'highest_price': entry_price, 'profit': 0.0,
            'buy_time': time.time(), 'volume': detected_tokens[contract_address]['volume']
        }
        bot.send_message(chat_id, f'✅ Achat effectué : {amount} SOL de {contract_address}')
    except Exception as e:
        logger.error(f"Erreur achat Solana: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Échec achat {contract_address}: {str(e)}')
        daily_trades['buys'][-1]['error'] = str(e)

async def sell_token(chat_id: int, contract_address: str, amount: float, chain: str, current_price: float, session: ClientSession):
    global mise_depart_bsc, mise_depart_sol
    if chain == "solana":
        try:
            dynamic_slippage = 10
            amount_out = int(amount * 10**9)
            async with session.post(SOLANA_RPC, json={
                "jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash", "params": [{"commitment": "finalized"}]
            }) as resp:
                blockhash = (await resp.json())['result']['value']['blockhash']
            tx = Transaction.from_recent_blockhash(Pubkey.from_string(blockhash))
            instruction = Instruction(
                program_id=RAYDIUM_PROGRAM_ID,
                accounts=[
                    {"pubkey": solana_keypair.pubkey(), "is_signer": True, "is_writable": True},
                    {"pubkey": Pubkey.from_string(contract_address), "is_signer": False, "is_writable": True},
                    {"pubkey": TOKEN_PROGRAM_ID, "is_signer": False, "is_writable": False}
                ],
                data=bytes([3]) + amount_out.to_bytes(8, 'little')
            )
            tx.add(instruction)
            tx.sign([solana_keypair])
            async with session.post(SOLANA_RPC, json={
                "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                "params": [base58.b58encode(tx.serialize()).decode('utf-8')]
            }) as resp:
                tx_hash = (await resp.json())['result']
            bot.send_message(chat_id, f'⏳ Vente en cours de {amount} SOL de {contract_address}, TX: {tx_hash}')
            await asyncio.sleep(2)
            profit = (current_price - portfolio[contract_address]['entry_price']) * amount
            portfolio[contract_address]['profit'] += profit
            portfolio[contract_address]['amount'] -= amount
            if portfolio[contract_address]['amount'] <= 0:
                del portfolio[contract_address]
            reinvest_amount = profit * profit_reinvestment_ratio
            mise_depart_sol += reinvest_amount
            bot.send_message(chat_id, f'✅ Vente effectuée : {amount} SOL de {contract_address}, Profit: {profit:.4f} SOL, Réinvesti: {reinvest_amount:.4f} SOL')
            daily_trades['sells'].append({
                'token': contract_address, 'chain': 'solana', 'amount': amount,
                'pnl': profit, 'timestamp': datetime.now().strftime('%H:%M:%S')
            })
        except Exception as e:
            logger.error(f"Erreur vente Solana: {str(e)}")
            bot.send_message(chat_id, f'⚠️ Échec vente {contract_address}: {str(e)}')
    else:
        try:
            dynamic_slippage = 10
            token_amount = w3.to_wei(amount, 'ether')
            amount_in_max = int(token_amount * (1 + dynamic_slippage / 100))
            router = w3.eth.contract(address=PANCAKE_ROUTER_ADDRESS, abi=PANCAKE_ROUTER_ABI)
            gas_price = max(gas_fee, w3.eth.gas_price / 10**9)
            tx = router.functions.swapExactTokensForETH(
                token_amount,
                amount_in_max,
                [w3.to_checksum_address(contract_address), w3.to_checksum_address('0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c')],
                w3.to_checksum_address(WALLET_ADDRESS),
                int(time.time()) + 60
            ).build_transaction({
                'from': WALLET_ADDRESS, 'gas': 200000,
                'gasPrice': w3.to_wei(gas_price, 'gwei'), 'nonce': w3.eth.get_transaction_count(WALLET_ADDRESS)
            })
            signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
            tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
            bot.send_message(chat_id, f'⏳ Vente en cours de {amount} BNB de {contract_address}, TX: {tx_hash.hex()}')
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=10)
            if receipt.status == 1:
                profit = (current_price - portfolio[contract_address]['entry_price']) * amount
                portfolio[contract_address]['profit'] += profit
                portfolio[contract_address]['amount'] -= amount
                if portfolio[contract_address]['amount'] <= 0:
                    del portfolio[contract_address]
                reinvest_amount = profit * profit_reinvestment_ratio
                mise_depart_bsc += reinvest_amount
                bot.send_message(chat_id, f'✅ Vente effectuée : {amount} BNB de {contract_address}, Profit: {profit:.4f} BNB, Réinvesti: {reinvest_amount:.4f} BNB')
                daily_trades['sells'].append({
                    'token': contract_address, 'chain': 'bsc', 'amount': amount,
                    'pnl': profit, 'timestamp': datetime.now().strftime('%H:%M:%S')
                })
            else:
                bot.send_message(chat_id, f'⚠️ Échec vente {contract_address}, TX: {tx_hash.hex()}')
        except Exception as e:
            logger.error(f"Erreur vente BSC: {str(e)}")
            bot.send_message(chat_id, f'⚠️ Échec vente {contract_address}: {str(e)}')

def sell_token_percentage(chat_id: int, token: str, percentage: float):
    try:
        if token not in portfolio:
            bot.send_message(chat_id, f'⚠️ Vente impossible : {token} n\'est pas dans le portefeuille')
            return
        total_amount = portfolio[token]['amount']
        amount_to_sell = total_amount * (percentage / 100)
        chain = portfolio[token]['chain']
        current_price = get_current_market_cap(token) / detected_tokens[token]['supply']
        asyncio.run_coroutine_threadsafe(sell_token(chat_id, token, amount_to_sell, chain, current_price, ClientSession()), asyncio.get_event_loop())
    except Exception as e:
        logger.error(f"Erreur vente partielle: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Erreur vente partielle {token}: {str(e)}')

async def monitor_and_sell(chat_id: int, session: ClientSession):
    while trade_active:
        try:
            if not portfolio:
                await asyncio.sleep(2)
                continue
            for contract_address, data in list(portfolio.items()):
                chain = data['chain']
                amount = data['amount']
                current_data = await get_token_data(contract_address, chain, session)
                current_mc = current_data['market_cap']
                current_price = current_mc / data['supply']
                data['price_history'].append(current_price)
                if len(data['price_history']) > 10:
                    data['price_history'].pop(0)
                portfolio[contract_address]['current_market_cap'] = current_mc
                profit_pct = (current_price - data['entry_price']) / data['entry_price'] * 100
                loss_pct = -profit_pct if profit_pct < 0 else 0
                trend = mean(data['price_history'][-3:]) / data['entry_price'] if len(data['price_history']) >= 3 else profit_pct / 100
                data['highest_price'] = max(data['highest_price'], current_price)
                trailing_stop_price = data['highest_price'] * (1 - trailing_stop_percentage / 100)

                if current_data['volume_24h'] < data['volume'] * 0.5:
                    bot.send_message(chat_id, f'⚠️ Chute volume détectée pour {contract_address}, vente immédiate')
                    await sell_token(chat_id, contract_address, amount, chain, current_price, session)
                elif profit_pct >= take_profit_steps[2] * 100:
                    await sell_token(chat_id, contract_address, amount, chain, current_price, session)
                elif profit_pct >= take_profit_steps[1] * 100 and trend < 1.05:
                    sell_amount = amount / 2
                    await sell_token(chat_id, contract_address, sell_amount, chain, current_price, session)
                elif profit_pct >= take_profit_steps[0] * 100 and trend < 1.02:
                    sell_amount = amount / 3
                    await sell_token(chat_id, contract_address, sell_amount, chain, current_price, session)
                elif current_price <= trailing_stop_price:
                    await sell_token(chat_id, contract_address, amount, chain, current_price, session)
                elif loss_pct >= stop_loss_threshold:
                    await sell_token(chat_id, contract_address, amount, chain, current_price, session)
            await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"Erreur surveillance: {str(e)}")
            bot.send_message(chat_id, f'⚠️ Erreur surveillance: {str(e)}. Reprise dans 10s...')
            await asyncio.sleep(10)

def show_portfolio(chat_id: int):
    try:
        bnb_balance = w3.eth.get_balance(WALLET_ADDRESS) / 10**18
        sol_balance = get_solana_balance(WALLET_ADDRESS)
        msg = f'💰 Portefeuille:\nBNB : {bnb_balance:.4f}\nSOL : {sol_balance:.4f}\n\n'
        markup = InlineKeyboardMarkup()
        for ca, data in portfolio.items():
            chain = data['chain']
            current_mc = get_current_market_cap(ca)
            profit = (current_mc - data['market_cap_at_buy']) / data['market_cap_at_buy'] * 100
            markup.add(
                InlineKeyboardButton(f"🔄 Refresh {ca[:6]}...", callback_data=f"refresh_{ca}"),
                InlineKeyboardButton(f"💸 Sell {ca[:6]}...", callback_data=f"sell_{ca}")
            )
            msg += (
                f"Token: {ca} ({data['chain']})\nMC Achat: ${data['market_cap_at_buy']:.2f}\n"
                f"MC Actuel: ${current_mc:.2f}\nProfit: {profit:.2f}%\nProfit cumulé: {data['profit']:.4f} {data['chain'].upper()}\n\n"
            )
        bot.send_message(chat_id, msg, reply_markup=markup if portfolio else None)
    except Exception as e:
        logger.error(f"Erreur portefeuille: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Erreur portefeuille: {str(e)}')

def get_solana_balance(wallet_address: str) -> float:
    try:
        response = session.post(SOLANA_RPC, json={
            "jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [str(solana_keypair.pubkey())]
        }, timeout=10)
        result = response.json().get('result', {})
        return result.get('value', 0) / 10**9
    except Exception as e:
        logger.error(f"Erreur solde Solana: {str(e)}")
        return 0

def get_current_market_cap(contract_address: str) -> float:
    try:
        chain = portfolio[contract_address]['chain'] if contract_address in portfolio else ('bsc' if contract_address.startswith("0x") else 'solana')
        return asyncio.run_coroutine_threadsafe(get_token_data(contract_address, chain, ClientSession()), asyncio.get_event_loop()).result()['market_cap']
    except Exception as e:
        logger.error(f"Erreur market cap: {str(e)}")
        return detected_tokens.get(contract_address, {}).get('market_cap', 0)

def refresh_token(chat_id: int, token: str):
    try:
        current_mc = get_current_market_cap(token)
        profit = (current_mc - portfolio[token]['market_cap_at_buy']) / portfolio[token]['market_cap_at_buy'] * 100
        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh_{token}"),
            InlineKeyboardButton("💸 Sell All", callback_data=f"sell_{token}")
        )
        msg = (
            f"Token: {token} ({portfolio[token]['chain']})\nContrat: {token}\n"
            f"MC Achat: ${portfolio[token]['market_cap_at_buy']:.2f}\nMC Actuel: ${current_mc:.2f}\n"
            f"Profit: {profit:.2f}%\nProfit cumulé: ${portfolio[token]['profit']:.4f} {portfolio[token]['chain'].upper()}\n"
            f"Take-Profit: x{take_profit_steps[0]}, x{take_profit_steps[1]}, x{take_profit_steps[2]}\n"
            f"Trailing Stop: -{trailing_stop_percentage}% sous pic\nStop-Loss: -{stop_loss_threshold} %"
        )
        bot.send_message(chat_id, f'🔍 Portefeuille rafraîchi pour {token}:\n{msg}', reply_markup=markup)
    except Exception as e:
        logger.error(f"Erreur refresh: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Erreur rafraîchissement {token}: {str(e)}')

def sell_token_immediate(chat_id: int, token: str):
    try:
        if token not in portfolio:
            bot.send_message(chat_id, f'⚠️ Vente impossible : {token} n\'est pas dans le portefeuille')
            return
        amount = portfolio[token]["amount"]
        chain = portfolio[token]['chain']
        current_price = get_current_market_cap(token) / detected_tokens[token]['supply']
        asyncio.run_coroutine_threadsafe(sell_token(chat_id, token, amount, chain, current_price, ClientSession()), asyncio.get_event_loop())
    except Exception as e:
        logger.error(f"Erreur vente immédiate: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Erreur vente immédiate {token}: {str(e)}')

def show_daily_summary(chat_id: int):
    try:
        msg = f"📅 Récapitulatif du jour ({datetime.now().strftime('%Y-%m-%d')}):\n\n"
        msg += "📈 Achats et tentatives :\n"
        total_buys = 0
        for trade in daily_trades['buys']:
            if 'error' in trade:
                msg += f"- {trade['token']} ({trade['chain']}) : Échec à {trade['timestamp']} - {trade['error']}\n"
            else:
                msg += f"- {trade['token']} ({trade['chain']}) : {trade['amount']} {trade['chain'].upper()} à {trade['timestamp']}\n"
                total_buys += trade['amount']
        msg += f"Total investi : {total_buys:.4f} BNB/SOL\n\n"

        msg += "📉 Ventes :\n"
        total_profit = 0
        for trade in daily_trades['sells']:
            msg += f"- {trade['token']} ({trade['chain']}) : {trade['amount']} {trade['chain'].upper()} à {trade['timestamp']}, PNL: {trade['pnl']:.4f} {trade['chain'].upper()}\n"
            total_profit += trade['pnl']
        msg += f"Profit net : {total_profit:.4f} BNB/SOL\n"
        
        bot.send_message(chat_id, msg)
    except Exception as e:
        logger.error(f"Erreur récapitulatif: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Erreur récapitulatif: {str(e)}')

def show_token_management(chat_id: int) -> None:
    try:
        if not portfolio:
            bot.send_message(chat_id, "📋 Aucun token en portefeuille.")
            return
        msg = "📋 Gestion des tokens en portefeuille :\n\n"
        markup = InlineKeyboardMarkup()
        for ca, data in portfolio.items():
            chain = data['chain']
            current_mc = get_current_market_cap(ca)
            profit = (current_mc - data['market_cap_at_buy']) / data['market_cap_at_buy'] * 100
            msg += (
                f"Token: {ca} ({chain})\n"
                f"Quantité: {data['amount']} {chain.upper()}\n"
                f"Profit: {profit:.2f}%\n\n"
            )
            markup.add(
                InlineKeyboardButton(f"Vendre 25% {ca[:6]}", callback_data=f"sell_pct_{ca}_25"),
                InlineKeyboardButton(f"Vendre 50% {ca[:6]}", callback_data=f"sell_pct_{ca}_50"),
                InlineKeyboardButton(f"Vendre 100% {ca[:6]}", callback_data=f"sell_pct_{ca}_100")
            )
        bot.send_message(chat_id, msg, reply_markup=markup)
    except Exception as e:
        logger.error(f"Erreur gestion tokens: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Erreur gestion tokens: {str(e)}')

def set_webhook() -> None:
    logger.info("Configuration du webhook...")
    try:
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(url=WEBHOOK_URL)
        logger.info(f"Webhook configuré sur {WEBHOOK_URL}")
    except Exception as e:
        logger.error(f"Erreur configuration webhook: {str(e)}")
        logger.warning("Webhook non configuré, poursuite du démarrage...")

def run_bot() -> None:
    logger.info("Démarrage du bot...")
    while True:
        try:
            initialize_bot()
            set_webhook()
            # Lancement des tâches asynchrones dans une boucle événementielle séparée
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            # Remplacez 123456789 par votre chat_id réel ou une variable d'environnement
            chat_id = int(os.getenv("CHAT_ID", 123456789))
            async def start_tasks():
                async with ClientSession() as session:
                    await asyncio.gather(
                        trading_cycle(chat_id, session),
                        snipe_new_pairs_bsc(chat_id, session),
                        monitor_twitter(chat_id, session),
                        snipe_solana_pools(chat_id, session),
                        watchdog(chat_id),
                        return_exceptions=True
                    )
            # Exécute les tâches asynchrones en parallèle de Flask
            from threading import Thread
            thread = Thread(target=lambda: loop.run_until_complete(start_tasks()), daemon=True)
            thread.start()
            logger.info("Bot initialisé avec succès.")
            app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
        except Exception as e:
            logger.error(f"Erreur critique lors du démarrage: {str(e)}. Redémarrage dans 10s...")
            time.sleep(10)

if __name__ == "__main__":
    logger.info("Démarrage principal...")
    while True:
        try:
            run_bot()
        except Exception as e:
            logger.error(f"Crash système critique: {str(e)}. Redémarrage dans 30s...")
            time.sleep(30)
