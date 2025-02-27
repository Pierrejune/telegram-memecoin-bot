import os
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import logging
import requests
import time
import json
import base58
from flask import Flask, request, abort
from cachetools import TTLCache
from web3 import Web3, WebsocketProvider
from web3.middleware import geth_poa_middleware
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from solders.instruction import Instruction
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import threading
import asyncio
from telethon import TelegramClient, events
from dotenv import load_dotenv
from statistics import mean, stdev
from typing import Dict, List, Optional

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
loose_mode_bsc = False
last_valid_token_time = time.time()
twitter_last_reset = time.time()
twitter_requests_remaining = 450

logger.info("Chargement des variables d’environnement...")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY")
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")
TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN")
TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID")
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
PORT = int(os.getenv("PORT", 8080))
BSC_RPC = os.getenv("BSC_RPC", "wss://bsc-ws-node.nariox.org:443")  # Assurez-vous que c'est un wss://
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
bot = telebot.TeleBot(TELEGRAM_TOKEN)
logger.info("Bot Telegram initialisé.")
telegram_client = None
w3 = None
solana_keypair = None

# Variables globales pour le trading
mise_depart_bsc = 0.02
mise_depart_sol = 0.37
gas_fee = 5
stop_loss_threshold = 30
trailing_stop_percentage = 5
take_profit_steps = [2, 3, 5]
detected_tokens = {}
trade_active = False
cache = TTLCache(maxsize=100, ttl=300)
portfolio: Dict[str, dict] = {}
twitter_tokens = []
telegram_tokens = []
max_positions = 3
profit_reinvestment_ratio = 0.5

MIN_VOLUME_SOL = 25000
MAX_VOLUME_SOL = 750000
MIN_VOLUME_BSC = 1000
MAX_VOLUME_BSC = 1000000
MIN_LIQUIDITY = 50000
MIN_MARKET_CAP_SOL = 50000
MAX_MARKET_CAP_SOL = 1500000
MIN_MARKET_CAP_BSC = 75000
MAX_MARKET_CAP_BSC = 3000000
MAX_TAX = 10
MIN_BUY_SELL_RATIO_BSC = 1.5
MIN_BUY_SELL_RATIO_SOL = 1.5
MIN_VOLATILITY = 10

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
        logger.info("Tentative de connexion WebSocket BSC...")
        if not BSC_RPC.startswith("wss://"):
            logger.warning(f"BSC_RPC {BSC_RPC} n'est pas un WebSocket, passage à HTTP fallback")
            w3 = Web3(Web3.HTTPProvider("https://bsc-dataseed.binance.org/"))
        else:
            w3 = Web3(WebsocketProvider(BSC_RPC))
            w3.middleware_onion.inject(geth_poa_middleware, layer=0)
            if not w3.is_connected():
                logger.warning(f"Connexion WebSocket BSC échouée sur {BSC_RPC}, passage à HTTP fallback")
                w3 = Web3(Web3.HTTPProvider("https://bsc-dataseed.binance.org/"))
        if not w3.is_connected():
            logger.error("Connexion BSC échouée même en HTTP")
            raise ConnectionError("Connexion BSC échouée même en HTTP")
        logger.info(f"Connexion BSC réussie. Bloc actuel : {w3.eth.block_number}")
        logger.info("Initialisation de la clé Solana...")
        solana_keypair = Keypair.from_bytes(base58.b58decode(SOLANA_PRIVATE_KEY))
        logger.info("Clé Solana initialisée.")
    except Exception as e:
        logger.error(f"Erreur dans l'initialisation différée: {str(e)}")
        raise

def is_safe_token_bsc(token_address: str) -> bool:
    try:
        response = session.get(f"https://api.honeypot.is/v2/IsHoneypot?address={token_address}", timeout=10)
        data = response.json()
        return not data.get("isHoneypot", True) and data.get("buyTax", 0) <= MAX_TAX and data.get("sellTax", 0) <= MAX_TAX
    except Exception as e:
        logger.error(f"Erreur vérification Honeypot: {str(e)}")
        return False

def is_safe_token_solana(token_address: str) -> bool:
    try:
        response = session.get(f"https://public-api.birdeye.so/v1/token/token_overview?address={token_address}", headers=BIRDEYE_HEADERS, timeout=10)
        data = response.json()['data']
        return data.get('liquidity', 0) >= MIN_LIQUIDITY
    except Exception as e:
        logger.error(f"Erreur vérification Solana: {str(e)}")
        return False

def is_valid_token_bsc(token_address: str) -> bool:
    try:
        checksum_address = w3.to_checksum_address(token_address)
        code = w3.eth.get_code(checksum_address)
        if len(code) <= 2:
            logger.warning(f"Token {token_address} n'a pas de code valide.")
            return False
        token_contract = w3.eth.contract(address=checksum_address, abi=ERC20_ABI)
        token_contract.functions.totalSupply().call()
        return True
    except Exception as e:
        logger.error(f"Erreur validation token BSC {token_address}: {str(e)}")
        return False

def get_tx_ratio_bsc(token_address: str) -> float:
    try:
        latest_block = w3.eth.block_number
        buy_count, sell_count = 0, 0
        for block_num in range(max(latest_block - 100, 0), latest_block + 1):
            block = w3.eth.get_block(block_num, full_transactions=True)
            for tx in block['transactions']:
                if tx.get('to') == PANCAKE_ROUTER_ADDRESS or tx.get('from') == token_address:
                    data = tx.get('input', '')
                    if 'swapExactETHForTokens' in str(data):
                        buy_count += 1
                    elif 'swapExactTokensForETH' in str(data):
                        sell_count += 1
        ratio = buy_count / max(sell_count, 1)
        logger.info(f"Ratio achat/vente pour {token_address}: {ratio:.2f} (achats: {buy_count}, ventes: {sell_count})")
        return ratio
    except Exception as e:
        logger.error(f"Erreur calcul ratio BSC pour {token_address}: {str(e)}")
        return 0.0

def get_tx_ratio_solana(token_address: str) -> float:
    try:
        response = session.get(f"https://public-api.birdeye.so/public/history_trades?address={token_address}&offset=0&limit=50", headers=BIRDEYE_HEADERS, timeout=10)
        response.raise_for_status()
        trades = response.json()['data']['items']
        if not trades:
            logger.warning(f"Aucune transaction récente via Birdeye pour {token_address}")
            return 0.0
        buy_count = sum(1 for trade in trades if trade.get('type') == 'buy')
        sell_count = sum(1 for trade in trades if trade.get('type') == 'sell')
        ratio = buy_count / max(sell_count, 1)
        logger.info(f"Ratio achat/vente pour {token_address}: {ratio:.2f} (achats: {buy_count}, ventes: {sell_count})")
        return ratio
    except Exception as e:
        logger.error(f"Erreur calcul ratio Solana pour {token_address}: {str(e)}")
        return 0.0

def calculate_volatility(token_address: str, chain: str) -> float:
    try:
        if chain == 'bsc':
            response = session.get(f"https://api.dexscreener.com/latest/dex/tokens/{token_address}", timeout=10)
            data = response.json()['pairs'][0]
            prices = [float(data.get(f'priceChangeh{h}', 0)) for h in [1, 6, 24]]
        else:
            response = session.get(f"https://public-api.birdeye.so/v1/token/token_overview?address={token_address}", headers=BIRDEYE_HEADERS, timeout=10)
            data = response.json()['data']
            prices = [float(data.get(f'priceChange{h}', 0)) for h in ['1h', '6h', '24h']]
        volatility = stdev(prices) if len(prices) > 1 else abs(prices[0])
        logger.info(f"Volatilité pour {token_address}: {volatility:.2f}%")
        return volatility
    except Exception as e:
        logger.error(f"Erreur calcul volatilité {token_address}: {str(e)}")
        return 0.0

def calculate_dynamic_slippage(token_address: str, chain: str) -> float:
    volatility = calculate_volatility(token_address, chain)
    slippage = min(max(volatility * 0.5, 5), 20)
    logger.info(f"Slippage dynamique pour {token_address}: {slippage:.2f}%")
    return slippage

def monitor_twitter(chat_id: int) -> None:
    global twitter_requests_remaining, twitter_last_reset, last_twitter_call
    logger.info("Surveillance Twitter démarrée...")
    bot.send_message(chat_id, "📡 Surveillance Twitter activée...")
    base_delay = 15.0  # Augmenté pour éviter 429
    error_count = 0
    max_errors = 10

    query_general = '("contract address" OR CA) -is:retweet'
    query_kanye = 'from:kanyewest "contract address" OR CA OR token OR memecoin OR launch'

    while trade_active and error_count < max_errors:
        try:
            current_time = time.time()
            if current_time - twitter_last_reset >= 900:
                twitter_requests_remaining = 450
                twitter_last_reset = current_time
                logger.info("Quota Twitter réinitialisé : 450 requêtes.")
                error_count = 0

            if twitter_requests_remaining < 20:
                wait_time = 900 - (current_time - twitter_last_reset) + 10
                logger.warning(f"Quota faible ({twitter_requests_remaining}), attente de {wait_time:.1f}s...")
                bot.send_message(chat_id, f"⚠️ Quota Twitter bas ({twitter_requests_remaining}), pause de {wait_time:.1f}s...")
                time.sleep(wait_time)
                continue

            delay = max(base_delay, 900 / max(twitter_requests_remaining - 1, 1))
            time.sleep(max(0, delay - (current_time - last_twitter_call)))

            response = session.get(
                f"https://api.twitter.com/2/tweets/search/recent?query={query_general}&max_results=10&expansions=author_id&user.fields=public_metrics",
                headers=TWITTER_HEADERS, timeout=10
            )
            response.raise_for_status()
            twitter_requests_remaining -= 1
            last_twitter_call = current_time
            error_count = 0
            data = response.json()
            tweets = data.get('data', [])
            users = {u['id']: u for u in data.get('includes', {}).get('users', [])}

            for tweet in tweets:
                user = users.get(tweet['author_id'])
                followers = user.get('public_metrics', {}).get('followers_count', 0) if user else 0
                if followers >= 10000 or user['username'] == 'kanyewest':
                    text = tweet['text'].lower()
                    words = text.split()
                    for word in words:
                        if (len(word) == 42 and word.startswith("0x")) or len(word) == 44:
                            if word not in twitter_tokens:
                                twitter_tokens.append(word)
                                bot.send_message(chat_id, f'🔍 Token détecté via X (@{user["username"]}, {followers} abonnés): {word}')
                                check_twitter_token(chat_id, word)
        except requests.exceptions.RequestException as e:
            if getattr(e.response, 'status_code', None) == 429:
                wait_time = int(e.response.headers.get('Retry-After', 900)) + 5
                logger.warning(f"429 détecté, attente de {wait_time}s")
                bot.send_message(chat_id, f"⚠️ Limite Twitter atteinte, pause de {wait_time}s...")
                time.sleep(wait_time)
                error_count += 1
            else:
                logger.error(f"Erreur Twitter: {str(e)}")
                bot.send_message(chat_id, f'⚠️ Erreur Twitter: {str(e)}. Reprise dans 60s...')
                time.sleep(60)
                error_count += 1
        except Exception as e:
            logger.error(f"Erreur Twitter inattendue: {str(e)}")
            bot.send_message(chat_id, f'⚠️ Erreur Twitter: {str(e)}. Reprise dans 60s...')
            time.sleep(60)
            error_count += 1
    logger.info("Surveillance Twitter arrêtée.")
    if error_count >= max_errors:
        bot.send_message(chat_id, "⚠️ Surveillance Twitter désactivée après trop d'erreurs.")

async def monitor_telegram(chat_id: int) -> None:
    global telegram_client
    logger.info("Surveillance Telegram démarrée...")
    bot.send_message(chat_id, "📡 Surveillance Telegram activée...")
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        logger.warning("TELEGRAM_API_ID ou TELEGRAM_API_HASH manquant, surveillance Telegram désactivée.")
        bot.send_message(chat_id, "⚠️ Surveillance Telegram désactivée : clés API manquantes.")
        return
    max_retries = 3
    for attempt in range(max_retries):
        try:
            # Supprimer le fichier de session local s'il existe pour éviter les conflits SQLite
            if os.path.exists('bot_session.session'):
                os.remove('bot_session.session')
            telegram_client = TelegramClient('bot_session', int(TELEGRAM_API_ID), TELEGRAM_API_HASH)
            async with telegram_client:
                @telegram_client.on(events.NewMessage(chats=['memecoin_group']))  # Remplacez par un vrai groupe
                async def handler(event):
                    try:
                        text = event.message.text.lower()
                        words = text.split()
                        for word in words:
                            if (len(word) == 42 and word.startswith("0x")) or len(word) == 44:
                                if word not in telegram_tokens:
                                    telegram_tokens.append(word)
                                    bot.send_message(chat_id, f'🔍 Token détecté via Telegram: {word}')
                                    check_twitter_token(chat_id, word)
                    except Exception as e:
                        logger.error(f"Erreur dans le handler Telegram: {str(e)}")
                logger.info("Surveillance Telegram active.")
                await telegram_client.run_until_disconnected()
            break  # Si succès, sortir de la boucle
        except Exception as e:
            logger.error(f"Erreur dans monitor_telegram (tentative {attempt + 1}/{max_retries}): {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(5)  # Attendre avant de réessayer
            else:
                bot.send_message(chat_id, f'⚠️ Surveillance Telegram échouée après {max_retries} tentatives: {str(e)}')

def check_twitter_token(chat_id: int, token_address: str) -> bool:
    try:
        if len(portfolio) >= max_positions:
            bot.send_message(chat_id, f'⚠️ Limite de {max_positions} positions atteinte, token {token_address} ignoré.')
            return False

        if token_address.startswith("0x"):
            if not is_valid_token_bsc(token_address):
                bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : pas de code valide ou totalSupply inaccessible')
                return False
            token_contract = w3.eth.contract(address=w3.to_checksum_address(token_address), abi=ERC20_ABI)
            supply = token_contract.functions.totalSupply().call() / 10**18
            response = session.get(f"https://api.dexscreener.com/latest/dex/tokens/{token_address}", timeout=10)
            data = response.json()['pairs'][0] if response.json()['pairs'] else {}
            volume_24h = float(data.get('volume', {}).get('h24', 0))
            liquidity = float(data.get('liquidity', {}).get('usd', 0))
            market_cap = volume_24h * supply
            buy_sell_ratio = get_tx_ratio_bsc(token_address)
            volatility = calculate_volatility(token_address, 'bsc')

            if buy_sell_ratio < MIN_BUY_SELL_RATIO_BSC:
                bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : ratio achat/vente {buy_sell_ratio:.2f} < {MIN_BUY_SELL_RATIO_BSC}')
                return False
            if not (MIN_VOLUME_BSC <= volume_24h <= MAX_VOLUME_BSC):
                bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : volume ${volume_24h} hors plage [{MIN_VOLUME_BSC}, {MAX_VOLUME_BSC}]')
                return False
            if liquidity < MIN_LIQUIDITY:
                bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : liquidité ${liquidity} < ${MIN_LIQUIDITY}')
                return False
            if not (MIN_MARKET_CAP_BSC <= market_cap <= MAX_MARKET_CAP_BSC):
                bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : market cap ${market_cap} hors plage [{MIN_MARKET_CAP_BSC}, {MAX_MARKET_CAP_BSC}]')
                return False
            if not is_safe_token_bsc(token_address):
                bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : possible rug ou taxes élevées')
                return False
            if volatility < MIN_VOLATILITY:
                bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : volatilité {volatility:.2f}% < {MIN_VOLATILITY}%')
                return False
            bot.send_message(chat_id, f'🔍 Token détecté : {token_address} (BSC) - Ratio A/V: {buy_sell_ratio:.2f}, Vol: ${volume_24h:.2f}, Liq: ${liquidity:.2f}, MC: ${market_cap:.2f}, Volatilité: {volatility:.2f}%')
            detected_tokens[token_address] = {
                'address': token_address, 'volume': volume_24h, 'buy_sell_ratio': buy_sell_ratio,
                'liquidity': liquidity, 'market_cap': market_cap, 'supply': supply, 'volatility': volatility
            }
            buy_token_bsc(chat_id, token_address, mise_depart_bsc)
            return True
        else:
            if token_address == "So11111111111111111111111111111111111111112":
                logger.info(f"Ignorer Wrapped SOL: {token_address}")
                return False
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    response = session.get(f"https://public-api.birdeye.so/v1/token/token_overview?address={token_address}", headers=BIRDEYE_HEADERS, timeout=10)
                    response.raise_for_status()
                    data = response.json().get('data', {})
                    if not data:
                        logger.warning(f"Réponse vide de Birdeye pour {token_address} (tentative {attempt + 1}/{max_retries})")
                        if attempt < max_retries - 1:
                            time.sleep(5)
                            continue
                        return False
                    volume_24h = float(data.get('v24hUSD', 0))
                    liquidity = float(data.get('liquidity', 0))
                    market_cap = float(data.get('mc', 0))
                    supply = float(data.get('supply', 0))
                    buy_sell_ratio = get_tx_ratio_solana(token_address)
                    volatility = calculate_volatility(token_address, 'solana')

                    if buy_sell_ratio < MIN_BUY_SELL_RATIO_SOL:
                        bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : ratio achat/vente {buy_sell_ratio:.2f} < {MIN_BUY_SELL_RATIO_SOL}')
                        return False
                    if not (MIN_VOLUME_SOL <= volume_24h <= MAX_VOLUME_SOL):
                        bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : volume ${volume_24h} hors plage [{MIN_VOLUME_SOL}, {MAX_VOLUME_SOL}]')
                        return False
                    if liquidity < MIN_LIQUIDITY:
                        bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : liquidité ${liquidity} < ${MIN_LIQUIDITY}')
                        return False
                    if not (MIN_MARKET_CAP_SOL <= market_cap <= MAX_MARKET_CAP_SOL):
                        bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : market cap ${market_cap} hors plage [{MIN_MARKET_CAP_SOL}, {MAX_MARKET_CAP_SOL}]')
                        return False
                    if not is_safe_token_solana(token_address):
                        bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : possible rug ou liquidité insuffisante')
                        return False
                    if volatility < MIN_VOLATILITY:
                        bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : volatilité {volatility:.2f}% < {MIN_VOLATILITY}%')
                        return False
                    bot.send_message(chat_id, f'🔍 Token détecté : {token_address} (Solana) - Ratio A/V: {buy_sell_ratio:.2f}, Vol: ${volume_24h:.2f}, Liq: ${liquidity:.2f}, MC: ${market_cap:.2f}, Volatilité: {volatility:.2f}%')
                    detected_tokens[token_address] = {
                        'address': token_address, 'volume': volume_24h, 'buy_sell_ratio': buy_sell_ratio,
                        'liquidity': liquidity, 'market_cap': market_cap, 'supply': supply, 'volatility': volatility
                    }
                    buy_token_solana(chat_id, token_address, mise_depart_sol)
                    return True
                except Exception as e:
                    logger.error(f"Erreur vérification Solana {token_address} (tentative {attempt + 1}/{max_retries}): {str(e)}")
                    if attempt < max_retries - 1:
                        time.sleep(5)
                    else:
                        bot.send_message(chat_id, f'⚠️ Erreur vérification Solana {token_address} après {max_retries} tentatives: {str(e)}')
                        return False
    except Exception as e:
        logger.error(f"Erreur vérification token {token_address}: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Erreur vérification token {token_address}: {str(e)}')
        return False

def snipe_new_pairs_bsc(chat_id: int) -> None:
    logger.info("Sniping BSC démarré...")
    bot.send_message(chat_id, "🔫 Sniping BSC activé...")
    factory = w3.eth.contract(address=PANCAKE_FACTORY_ADDRESS, abi=PANCAKE_FACTORY_ABI)
    while trade_active:
        try:
            latest_block = w3.eth.block_number
            events = factory.events.PairCreated.get_logs(fromBlock=latest_block - 10, toBlock=latest_block)
            for event in events:
                token_address = event['args']['token0'] if event['args']['token0'] != "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c" else event['args']['token1']
                if token_address not in detected_tokens:
                    bot.send_message(chat_id, f'🎯 Snipe détecté : {token_address} (BSC) dans les 10 derniers blocs')
                    check_bsc_token(chat_id, token_address, loose_mode_bsc)
            time.sleep(1)
        except Exception as e:
            logger.error(f"Erreur sniping BSC: {str(e)}")
            bot.send_message(chat_id, f'⚠️ Erreur sniping BSC: {str(e)}. Reprise dans 5s...')
            time.sleep(5)

def detect_new_tokens_bsc(chat_id: int) -> None:
    global loose_mode_bsc, last_valid_token_time
    bot.send_message(chat_id, "🔍 Début détection BSC...")
    try:
        if not w3.is_connected():
            raise ConnectionError("Connexion au nœud BSC perdue")
        factory = w3.eth.contract(address=PANCAKE_FACTORY_ADDRESS, abi=PANCAKE_FACTORY_ABI)
        latest_block = w3.eth.block_number
        logger.info(f"Bloc actuel : {latest_block}")
        events = factory.events.PairCreated.get_logs(fromBlock=latest_block - 200, toBlock=latest_block)
        logger.info(f"Événements trouvés : {len(events)}")
        bot.send_message(chat_id, f"⬇️ {len(events)} nouvelles paires détectées sur BSC")
        if not events:
            bot.send_message(chat_id, "ℹ️ Aucun événement PairCreated détecté dans les 200 derniers blocs.")
            return

        rejected_count = 0
        valid_token_found = False
        rejection_reasons = []

        if time.time() - last_valid_token_time > 3600 and not loose_mode_bsc:
            loose_mode_bsc = True
            bot.send_message(chat_id, "⚠️ Aucun token valide depuis 1h, mode souple activé.")

        for event in events:
            token_address = event['args']['token0'] if event['args']['token0'] != "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c" else event['args']['token1']
            result = check_bsc_token(chat_id, token_address, loose_mode_bsc)
            if isinstance(result, Exception):
                rejection_reasons.append(f"{token_address}: erreur - {str(result)}")
                rejected_count += 1
            elif result is False:
                rejection_reasons.append(f"{token_address}: critères non remplis")
                rejected_count += 1
            elif result:
                valid_token_found = True
                last_valid_token_time = time.time()
                loose_mode_bsc = False

        if not valid_token_found:
            bot.send_message(chat_id, f'⚠️ Aucun token BSC ne correspond aux critères ({rejected_count} rejetés).')
            if rejection_reasons:
                bot.send_message(chat_id, "Raisons de rejet :\n" + "\n".join(rejection_reasons[:5]))
        bot.send_message(chat_id, "✅ Détection BSC terminée.")
    except Exception as e:
        logger.error(f"Erreur détection BSC: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Erreur détection BSC: {str(e)}')

def check_bsc_token(chat_id: int, token_address: str, loose_mode: bool) -> bool:
    try:
        if len(portfolio) >= max_positions:
            bot.send_message(chat_id, f'⚠️ Limite de {max_positions} positions atteinte, token {token_address} ignoré.')
            return False

        if not is_valid_token_bsc(token_address):
            bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : pas de code valide ou totalSupply inaccessible')
            return False
        token_contract = w3.eth.contract(address=w3.to_checksum_address(token_address), abi=ERC20_ABI)
        supply = token_contract.functions.totalSupply().call() / 10**18
        response = session.get(f"https://api.dexscreener.com/latest/dex/tokens/{token_address}", timeout=10)
        data = response.json()['pairs'][0] if response.json()['pairs'] else {}
        volume_24h = float(data.get('volume', {}).get('h24', 0))
        liquidity = float(data.get('liquidity', {}).get('usd', 0))
        market_cap = volume_24h * supply
        buy_sell_ratio = get_tx_ratio_bsc(token_address)
        volatility = calculate_volatility(token_address, 'bsc')

        min_volume = MIN_VOLUME_BSC * 0.5 if loose_mode else MIN_VOLUME_BSC
        max_volume = MAX_VOLUME_BSC
        min_liquidity = MIN_LIQUIDITY * 0.5 if loose_mode else MIN_LIQUIDITY
        min_market_cap = MIN_MARKET_CAP_BSC * 0.5 if loose_mode else MIN_MARKET_CAP_BSC
        max_market_cap = MAX_MARKET_CAP_BSC
        min_ratio = MIN_BUY_SELL_RATIO_BSC * 0.5 if loose_mode else MIN_BUY_SELL_RATIO_BSC

        if buy_sell_ratio < min_ratio:
            bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : ratio achat/vente {buy_sell_ratio:.2f} < {min_ratio}')
            return False
        if not (min_volume <= volume_24h <= max_volume):
            bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : volume ${volume_24h} hors plage [{min_volume}, {max_volume}]')
            return False
        if liquidity < min_liquidity:
            bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : liquidité ${liquidity} < ${min_liquidity}')
            return False
        if not (min_market_cap <= market_cap <= max_market_cap):
            bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : market cap ${market_cap} hors plage [{min_market_cap}, {max_market_cap}]')
            return False
        if not is_safe_token_bsc(token_address):
            bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : possible rug ou taxes élevées')
            return False
        if volatility < MIN_VOLATILITY:
            bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : volatilité {volatility:.2f}% < {MIN_VOLATILITY}%')
            return False
        
        bot.send_message(chat_id, f'🔍 Token détecté : {token_address} (BSC) - Ratio A/V: {buy_sell_ratio:.2f}, Vol: ${volume_24h:.2f}, Liq: ${liquidity:.2f}, MC: ${market_cap:.2f}, Volatilité: {volatility:.2f}%')
        detected_tokens[token_address] = {
            'address': token_address, 'volume': volume_24h, 'buy_sell_ratio': buy_sell_ratio,
            'liquidity': liquidity, 'market_cap': market_cap, 'supply': supply, 'volatility': volatility
        }
        buy_token_bsc(chat_id, token_address, mise_depart_bsc)
        return True
    except Exception as e:
        logger.error(f"Erreur vérification BSC {token_address}: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Erreur vérification BSC {token_address}: {str(e)}')
        return False

def detect_new_tokens_solana(chat_id: int) -> None:
    bot.send_message(chat_id, "🔍 Début détection Solana via Birdeye...")
    try:
        response = session.get("https://public-api.birdeye.so/defi/tokenlist?sort_by=v24hUSD&sort_type=desc&offset=0&limit=10", headers=BIRDEYE_HEADERS, timeout=10)
        response.raise_for_status()
        json_response = response.json()
        if 'data' not in json_response or 'tokens' not in json_response.get('data', {}):
            raise ValueError(f"Réponse Birdeye invalide ou vide : {json_response}")
        tokens = json_response['data']['tokens']
        bot.send_message(chat_id, f"⬇️ {len(tokens)} tokens détectés sur Solana via Birdeye")
        for token in tokens:
            token_address = token['address']
            if token_address not in detected_tokens:
                volume_24h = float(token.get('v24hUSD', 0))
                if volume_24h > MIN_VOLUME_SOL:
                    bot.send_message(chat_id, f'🆕 Token Solana détecté via Birdeye : {token_address} (Vol: ${volume_24h:.2f})')
                    check_solana_token(chat_id, token_address)
    except Exception as e:
        logger.error(f"Erreur détection Solana via Birdeye: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Erreur détection Solana: {str(e)}')

def check_solana_token(chat_id: int, token_address: str) -> None:
    try:
        if len(portfolio) >= max_positions:
            bot.send_message(chat_id, f'⚠️ Limite de {max_positions} positions atteinte, token {token_address} ignoré.')
            return

        if token_address == "So11111111111111111111111111111111111111112":
            logger.info(f"Ignorer Wrapped SOL: {token_address}")
            return

        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = session.get(f"https://public-api.birdeye.so/v1/token/token_overview?address={token_address}", headers=BIRDEYE_HEADERS, timeout=10)
                response.raise_for_status()
                data = response.json().get('data', {})
                if not data:
                    logger.warning(f"Réponse vide de Birdeye pour {token_address} (tentative {attempt + 1}/{max_retries})")
                    if attempt < max_retries - 1:
                        time.sleep(5)
                        continue
                    return
                volume_24h = float(data.get('v24hUSD', 0))
                liquidity = float(data.get('liquidity', 0))
                market_cap = float(data.get('mc', 0))
                supply = float(data.get('supply', 0))
                buy_sell_ratio = get_tx_ratio_solana(token_address)
                volatility = calculate_volatility(token_address, 'solana')

                logger.info(f"Validation {token_address} : Ratio A/V={buy_sell_ratio:.2f}, Vol=${volume_24h}, Liq=${liquidity}, MC=${market_cap}, Volatilité={volatility:.2f}%")

                if buy_sell_ratio < MIN_BUY_SELL_RATIO_SOL:
                    bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : ratio achat/vente {buy_sell_ratio:.2f} < {MIN_BUY_SELL_RATIO_SOL}')
                    return
                if not (MIN_VOLUME_SOL <= volume_24h <= MAX_VOLUME_SOL):
                    bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : volume ${volume_24h} hors plage [{MIN_VOLUME_SOL}, {MAX_VOLUME_SOL}]')
                    return
                if liquidity < MIN_LIQUIDITY:
                    bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : liquidité ${liquidity} < ${MIN_LIQUIDITY}')
                    return
                if not (MIN_MARKET_CAP_SOL <= market_cap <= MAX_MARKET_CAP_SOL):
                    bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : market cap ${market_cap} hors plage [{MIN_MARKET_CAP_SOL}, {MAX_MARKET_CAP_SOL}]')
                    return
                if not is_safe_token_solana(token_address):
                    bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : possible rug ou liquidité insuffisante')
                    return
                if volatility < MIN_VOLATILITY:
                    bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : volatilité {volatility:.2f}% < {MIN_VOLATILITY}%')
                    return
                bot.send_message(chat_id, f'🔍 Token détecté : {token_address} (Solana) - Ratio A/V: {buy_sell_ratio:.2f}, Vol: ${volume_24h:.2f}, Liq: ${liquidity:.2f}, MC: ${market_cap:.2f}, Volatilité: {volatility:.2f}%')
                detected_tokens[token_address] = {
                    'address': token_address, 'volume': volume_24h, 'buy_sell_ratio': buy_sell_ratio,
                    'liquidity': liquidity, 'market_cap': market_cap, 'supply': supply, 'volatility': volatility
                }
                buy_token_solana(chat_id, token_address, mise_depart_sol)
                break
            except Exception as e:
                logger.error(f"Erreur vérification Solana {token_address} (tentative {attempt + 1}/{max_retries}): {str(e)}")
                if attempt < max_retries - 1:
                    time.sleep(5)
                else:
                    bot.send_message(chat_id, f'⚠️ Erreur vérification Solana {token_address} après {max_retries} tentatives: {str(e)}')
    except Exception as e:
        logger.error(f"Erreur vérification Solana {token_address}: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Erreur vérification Solana {token_address}: {str(e)}')

@app.route("/webhook", methods=['POST'])
def webhook():
    logger.info("Webhook reçu")
    try:
        if request.method == "POST" and request.headers.get("content-type") == "application/json":
            update = telebot.types.Update.de_json(request.get_json())
            bot.process_new_updates([update])
            logger.info("Update traité avec succès")
            return 'OK', 200
        logger.warning("Requête webhook invalide")
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
        InlineKeyboardButton("🔧 Réglages", callback_data="settings"),
        InlineKeyboardButton("📈 TP/SL", callback_data="tp_sl_settings"),
        InlineKeyboardButton("📊 Seuils", callback_data="threshold_settings")
    )
    bot.send_message(chat_id, "Voici le menu principal:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    global mise_depart_bsc, mise_depart_sol, trade_active, gas_fee, stop_loss_threshold, take_profit_steps
    global MIN Volume_BSC, MAX_VOLUME_BSC, MIN_LIQUIDITY, MIN_MARKET_CAP_BSC, MAX_MARKET_CAP_BSC
    global MIN_VOLUME_SOL, MAX_VOLUME_SOL, MIN_MARKET_CAP_SOL, MAX_MARKET_CAP_SOL
    chat_id = call.message.chat.id
    logger.info(f"Callback reçu: {call.data}")
    try:
        if call.data == "status":
            bot.send_message(chat_id, (
                f"ℹ️ Statut actuel :\nTrading actif: {'Oui' if trade_active else 'Non'}\n"
                f"Mise BSC: {mise_depart_bsc} BNB\nMise Solana: {mise_depart_sol} SOL\n"
                f"Slippage: Dynamique (5-20%)\nGas Fee: {gas_fee} Gwei\nPositions: {len(portfolio)}/{max_positions}"
            ))
        elif call.data == "config":
            show_config_menu(chat_id)
        elif call.data == "launch":
            if not trade_active:
                trade_active = True
                bot.send_message(chat_id, "▶️ Trading lancé avec succès!")
                threading.Thread(target=trading_cycle, args=(chat_id,), daemon=True).start()
                threading.Thread(target=snipe_new_pairs_bsc, args=(chat_id,), daemon=True).start()
                threading.Thread(target=lambda: asyncio.run(monitor_telegram(chat_id)), daemon=True).start()
            else:
                bot.send_message(chat_id, "⚠️ Trading déjà en cours.")
        elif call.data == "stop":
            trade_active = False
            bot.send_message(chat_id, "⏹️ Trading arrêté.")
        elif call.data == "portfolio":
            show_portfolio(chat_id)
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
            bot.send_message(chat_id, "Entrez le nouveau seuil de Stop-Loss (en %, ex. : 30) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_stop_loss)
        elif call.data == "adjust_take_profit":
            bot.send_message(chat_id, "Entrez les nouveaux seuils de Take-Profit (3 valeurs séparées par des virgules, ex. : 2,3,5) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_take_profit)
        elif call.data == "adjust_min_volume_bsc":
            bot.send_message(chat_id, "Entrez le nouveau seuil min de volume BSC (en $, ex. : 1000) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_min_volume_bsc)
        elif call.data == "adjust_max_volume_bsc":
            bot.send_message(chat_id, "Entrez le nouveau seuil max de volume BSC (en $, ex. : 1000000) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_max_volume_bsc)
        elif call.data == "adjust_min_liquidity":
            bot.send_message(chat_id, "Entrez le nouveau seuil min de liquidité (en $, ex. : 50000) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_min_liquidity)
        elif call.data == "adjust_min_market_cap_bsc":
            bot.send_message(chat_id, "Entrez le nouveau seuil min de market cap BSC (en $, ex. : 75000) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_min_market_cap_bsc)
        elif call.data == "adjust_max_market_cap_bsc":
            bot.send_message(chat_id, "Entrez le nouveau seuil max de market cap BSC (en $, ex. : 3000000) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_max_market_cap_bsc)
        elif call.data == "adjust_min_volume_sol":
            bot.send_message(chat_id, "Entrez le nouveau seuil min de volume Solana (en $, ex. : 25000) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_min_volume_sol)
        elif call.data == "adjust_max_volume_sol":
            bot.send_message(chat_id, "Entrez le nouveau seuil max de volume Solana (en $, ex. : 750000) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_max_volume_sol)
        elif call.data == "adjust_min_market_cap_sol":
            bot.send_message(chat_id, "Entrez le nouveau seuil min de market cap Solana (en $, ex. : 50000) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_min_market_cap_sol)
        elif call.data == "adjust_max_market_cap_sol":
            bot.send_message(chat_id, "Entrez le nouveau seuil max de market cap Solana (en $, ex. : 1500000) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_max_market_cap_sol)
        elif call.data.startswith("refresh_"):
            token = call.data.split("_")[1]
            refresh_token(chat_id, token)
        elif call.data.startswith("sell_"):
            token = call.data.split("_")[1]
            sell_token_immediate(chat_id, token)
    except Exception as e:
        logger.error(f"Erreur dans callback_query: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Erreur générale: {str(e)}')

def trading_cycle(chat_id: int) -> None:
    global trade_active
    cycle_count = 0
    threading.Thread(target=monitor_twitter, args=(chat_id,), daemon=True).start()
    threading.Thread(target=monitor_and_sell, args=(chat_id,), daemon=True).start()
    threading.Thread(target=snipe_new_pairs_bsc, args=(chat_id,), daemon=True).start()
    threading.Thread(target=lambda: asyncio.run(monitor_telegram(chat_id)), daemon=True).start()
    while trade_active:
        try:
            cycle_count += 1
            bot.send_message(chat_id, f'🔍 Début du cycle de détection #{cycle_count}...')
            logger.info(f"Cycle {cycle_count} démarré")
            detect_new_tokens_bsc(chat_id)
            detect_new_tokens_solana(chat_id)
            time.sleep(30)
        except Exception as e:
            logger.error(f"Erreur dans trading_cycle: {str(e)}")
            bot.send_message(chat_id, f'⚠️ Erreur dans le cycle: {str(e)}. Reprise dans 30s...')
            time.sleep(30)
    logger.info("Trading_cycle arrêté.")
    bot.send_message(chat_id, "ℹ️ Cycle de trading terminé.")

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
        InlineKeyboardButton("📊 Max Market Cap Solana", callback_data="adjust_max_market_cap_sol")
    )
    bot.send_message(chat_id, (
        f'📊 Seuils de détection :\n- BSC Volume: {MIN_VOLUME_BSC} $ - {MAX_VOLUME_BSC} $\n'
        f'- BSC Ratio A/V: {MIN_BUY_SELL_RATIO_BSC}\n'
        f'- BSC Market Cap : {MIN_MARKET_CAP_BSC} $ - {MAX_MARKET_CAP_BSC} $\n'
        f'- Min Liquidité : {MIN_LIQUIDITY} $\n- Solana Volume: {MIN_VOLUME_SOL} $ - {MAX_VOLUME_SOL} $\n'
        f'- Solana Ratio A/V: {MIN_BUY_SELL_RATIO_SOL}\n'
        f'- Solana Market Cap : {MIN_MARKET_CAP_SOL} $ - {MAX_MARKET_CAP_SOL} $'
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
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un pourcentage valide (ex. : 30)")

def adjust_take_profit(message):
    global take_profit_steps
    chat_id = message.chat.id
    try:
        new_tp = [float(x) for x in message.text.split(",")]
        if len(new_tp) == 3 and all(x > 0 for x in new_tp):
            take_profit_steps = new_tp
            bot.send_message(chat_id, f'✅ Take-Profit mis à jour à x{take_profit_steps[0]}, x{take_profit_steps[1]}, x{take_profit_steps[2]}')
        else:
            bot.send_message(chat_id, "⚠️ Entrez 3 valeurs positives séparées par des virgules (ex. : 2,3,5)")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Erreur : Entrez des nombres valides (ex. : 2,3,5)")

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
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 1000)")

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
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 1000000)")

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
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 50000)")

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
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 75000)")

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
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 3000000)")

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
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 25000)")

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
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 750000)")

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
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 1500000)")

def buy_token_bsc(chat_id: int, contract_address: str, amount: float) -> None:
    try:
        dynamic_slippage = calculate_dynamic_slippage(contract_address, 'bsc')
        router = w3.eth.contract(address=PANCAKE_ROUTER_ADDRESS, abi=PANCAKE_ROUTER_ABI)
        amount_in = w3.to_wei(amount, 'ether')
        amount_out_min = int(amount_in * (1 - dynamic_slippage / 100))
        tx = router.functions.swapExactETHForTokens(
            amount_out_min,
            [w3.to_checksum_address('0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c'), w3.to_checksum_address(contract_address)],
            w3.to_checksum_address(WALLET_ADDRESS),
            int(time.time()) + 60
        ).build_transaction({
            'from': WALLET_ADDRESS, 'value': amount_in, 'gas': 200000,
            'gasPrice': w3.to_wei(gas_fee, 'gwei'), 'nonce': w3.eth.get_transaction_count(WALLET_ADDRESS)
        })
        signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
        bot.send_message(chat_id, f'⏳ Achat en cours de {amount} BNB de {contract_address}, TX: {tx_hash.hex()}')
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
        if receipt.status == 1:
            entry_price = amount / (detected_tokens[contract_address]['supply'] * get_current_market_cap(contract_address) / detected_tokens[contract_address]['supply'])
            portfolio[contract_address] = {
                'amount': amount, 'chain': 'bsc', 'entry_price': entry_price,
                'market_cap_at_buy': detected_tokens[contract_address]['market_cap'],
                'current_market_cap': detected_tokens[contract_address]['market_cap'],
                'price_history': [entry_price], 'highest_price': entry_price, 'profit': 0.0
            }
            bot.send_message(chat_id, f'✅ Achat effectué : {amount} BNB de {contract_address}')
        else:
            bot.send_message(chat_id, f'⚠️ Échec achat {contract_address}, TX: {tx_hash.hex()}')
    except Exception as e:
        logger.error(f"Erreur achat BSC: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Échec achat {contract_address}: {str(e)}')

def buy_token_solana(chat_id: int, contract_address: str, amount: float) -> None:
    try:
        dynamic_slippage = calculate_dynamic_slippage(contract_address, 'solana')
        amount_in = int(amount * 10**9)
        response = session.post(SOLANA_RPC, json={
            "jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash", "params": [{"commitment": "finalized"}]
        }, timeout=5)
        blockhash = response.json()['result']['value']['blockhash']
        tx = Transaction()
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
        tx.recent_blockhash = Pubkey.from_string(blockhash)
        tx.sign(solana_keypair)
        tx_hash = session.post(SOLANA_RPC, json={
            "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
            "params": [base58.b58encode(tx.serialize()).decode('utf-8')]
        }, timeout=5).json()['result']
        bot.send_message(chat_id, f'⏳ Achat en cours de {amount} SOL de {contract_address}, TX: {tx_hash}')
        time.sleep(2)
        entry_price = amount / (detected_tokens[contract_address]['supply'] * get_current_market_cap(contract_address) / detected_tokens[contract_address]['supply'])
        portfolio[contract_address] = {
            'amount': amount, 'chain': 'solana', 'entry_price': entry_price,
            'market_cap_at_buy': detected_tokens[contract_address]['market_cap'],
            'current_market_cap': detected_tokens[contract_address]['market_cap'],
            'price_history': [entry_price], 'highest_price': entry_price, 'profit': 0.0
        }
        bot.send_message(chat_id, f'✅ Achat effectué : {amount} SOL de {contract_address}')
    except Exception as e:
        logger.error(f"Erreur achat Solana: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Échec achat {contract_address}: {str(e)}')

def sell_token(chat_id: int, contract_address: str, amount: float, chain: str, current_price: float) -> None:
    global mise_depart_bsc, mise_depart_sol
    if chain == "solana":
        try:
            dynamic_slippage = calculate_dynamic_slippage(contract_address, 'solana')
            amount_out = int(amount * 10**9)
            response = session.post(SOLANA_RPC, json={
                "jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash", "params": [{"commitment": "finalized"}]
            }, timeout=5)
            blockhash = response.json()['result']['value']['blockhash']
            tx = Transaction()
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
            tx.recent_blockhash = Pubkey.from_string(blockhash)
            tx.sign(solana_keypair)
            tx_hash = session.post(SOLANA_RPC, json={
                "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                "params": [base58.b58encode(tx.serialize()).decode('utf-8')]
            }, timeout=5).json()['result']
            bot.send_message(chat_id, f'⏳ Vente en cours de {amount} SOL de {contract_address}, TX: {tx_hash}')
            time.sleep(2)
            profit = (current_price - portfolio[contract_address]['entry_price']) * amount
            portfolio[contract_address]['profit'] += profit
            del portfolio[contract_address]
            reinvest_amount = profit * profit_reinvestment_ratio
            mise_depart_sol += reinvest_amount
            bot.send_message(chat_id, f'✅ Vente effectuée : {amount} SOL de {contract_address}, Profit: {profit:.4f} SOL, Réinvesti: {reinvest_amount:.4f} SOL')
        except Exception as e:
            logger.error(f"Erreur vente Solana: {str(e)}")
            bot.send_message(chat_id, f'⚠️ Échec vente {contract_address}: {str(e)}')
    else:
        try:
            dynamic_slippage = calculate_dynamic_slippage(contract_address, 'bsc')
            router = w3.eth.contract(address=PANCAKE_ROUTER_ADDRESS, abi=PANCAKE_ROUTER_ABI)
            token_amount = w3.to_wei(amount, 'ether')
            amount_in_max = int(token_amount * (1 + dynamic_slippage / 100))
            tx = router.functions.swapExactTokensForETH(
                token_amount,
                amount_in_max,
                [w3.to_checksum_address(contract_address), w3.to_checksum_address('0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c')],
                w3.to_checksum_address(WALLET_ADDRESS),
                int(time.time()) + 60
            ).build_transaction({
                'from': WALLET_ADDRESS, 'gas': 200000,
                'gasPrice': w3.to_wei(gas_fee, 'gwei'), 'nonce': w3.eth.get_transaction_count(WALLET_ADDRESS)
            })
            signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
            tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
            bot.send_message(chat_id, f'⏳ Vente en cours de {amount} BNB de {contract_address}, TX: {tx_hash.hex()}')
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
            if receipt.status == 1:
                profit = (current_price - portfolio[contract_address]['entry_price']) * amount
                portfolio[contract_address]['profit'] += profit
                del portfolio[contract_address]
                reinvest_amount = profit * profit_reinvestment_ratio
                mise_depart_bsc += reinvest_amount
                bot.send_message(chat_id, f'✅ Vente effectuée : {amount} BNB de {contract_address}, Profit: {profit:.4f} BNB, Réinvesti: {reinvest_amount:.4f} BNB')
            else:
                bot.send_message(chat_id, f'⚠️ Échec vente {contract_address}, TX: {tx_hash.hex()}')
        except Exception as e:
            logger.error(f"Erreur vente BSC: {str(e)}")
            bot.send_message(chat_id, f'⚠️ Échec vente {contract_address}: {str(e)}')

def monitor_and_sell(chat_id: int) -> None:
    while trade_active:
        try:
            if not portfolio:
                time.sleep(2)
                continue
            for contract_address, data in list(portfolio.items()):
                chain = data['chain']
                amount = data['amount']
                current_mc = get_current_market_cap(contract_address)
                current_price = current_mc / detected_tokens[contract_address]['supply']
                data['price_history'].append(current_price)
                if len(data['price_history']) > 10:
                    data['price_history'].pop(0)
                portfolio[contract_address]['current_market_cap'] = current_mc
                profit_pct = (current_mc - data['market_cap_at_buy']) / data['market_cap_at_buy'] * 100
                loss_pct = -profit_pct if profit_pct < 0 else 0
                trend = mean(data['price_history'][-3:]) / data['entry_price'] if len(data['price_history']) >= 3 else profit_pct / 100
                data['highest_price'] = max(data['highest_price'], current_price)
                trailing_stop_price = data['highest_price'] * (1 - trailing_stop_percentage / 100)

                if profit_pct >= take_profit_steps[2] * 100:
                    sell_token(chat_id, contract_address, amount, chain, current_price)
                elif profit_pct >= take_profit_steps[1] * 100 and trend < 1.05:
                    sell_amount = amount / 2
                    sell_token(chat_id, contract_address, sell_amount, chain, current_price)
                    portfolio[contract_address]['amount'] -= sell_amount
                elif profit_pct >= take_profit_steps[0] * 100 and trend < 1.02:
                    sell_amount = amount / 3
                    sell_token(chat_id, contract_address, sell_amount, chain, current_price)
                    portfolio[contract_address]['amount'] -= sell_amount
                elif current_price <= trailing_stop_price:
                    sell_token(chat_id, contract_address, amount, chain, current_price)
                elif loss_pct >= stop_loss_threshold:
                    sell_token(chat_id, contract_address, amount, chain, current_price)
            time.sleep(2)
        except Exception as e:
            logger.error(f"Erreur surveillance globale: {str(e)}")
            bot.send_message(chat_id, f'⚠️ Erreur surveillance: {str(e)}. Reprise dans 10s...')
            time.sleep(10)

def show_portfolio(chat_id: int) -> None:
    try:
        bnb_balance = w3.eth.get_balance(WALLET_ADDRESS) / 10**18
        sol_balance = get_solana_balance(WALLET_ADDRESS)
        msg = f'💰 Portefeuille:\nBNB : {bnb_balance:.4f}\nSOL : {sol_balance:.4f}\n\n'
        markup = InlineKeyboardMarkup()
        for ca, data in portfolio.items():
            current_mc = get_current_market_cap(ca)
            profit = (current_mc - data['market_cap_at_buy']) / data['market_cap_at_buy'] * 100
            markup.add(
                InlineKeyboardButton(f"🔄 Refresh {ca[:6]}...", callback_data=f"refresh_{ca}"),
                InlineKeyboardButton(f"💸 Sell {ca[:6]}...", callback_data=f"sell_{ca}")
            )
            msg += (
                f"Token: {ca} ({data['chain']})\nContrat: {ca}\nMC Achat: ${data['market_cap_at_buy']:.2f}\n"
                f"MC Actuel: ${current_mc:.2f}\nProfit: {profit:.2f}%\nProfit cumulé: {data['profit']:.4f} {data['chain'].upper()}\n"
                f"Take-Profit: x{take_profit_steps[0]}, x{take_profit_steps[1]}, x{take_profit_steps[2]}\n"
                f"Trailing Stop: -{trailing_stop_percentage}% sous pic\nStop-Loss: -{stop_loss_threshold} %\n\n"
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
        if contract_address in portfolio and portfolio[contract_address]['chain'] == 'solana':
            response = session.get(f"https://public-api.birdeye.so/public/price?address={contract_address}", headers=BIRDEYE_HEADERS, timeout=10)
            price = response.json()['data']['value']
            supply = detected_tokens[contract_address]['supply']
            return price * supply
        else:
            token_contract = w3.eth.contract(address=w3.to_checksum_address(contract_address), abi=ERC20_ABI)
            supply = token_contract.functions.totalSupply().call() / 10**18
            response = session.get(f"https://api.dexscreener.com/latest/dex/tokens/{contract_address}", timeout=10)
            data = response.json()['pairs'][0] if response.json()['pairs'] else {}
            price = float(data.get('priceUsd', 0))
            return price * supply
    except Exception as e:
        logger.error(f"Erreur market cap: {str(e)}")
        return detected_tokens.get(contract_address, {}).get('market_cap', 0)

def refresh_token(chat_id: int, token: str) -> None:
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
            f"Profit: {profit:.2f}%\nProfit cumulé: {portfolio[token]['profit']:.4f} {portfolio[token]['chain'].upper()}\n"
            f"Take-Profit: x{take_profit_steps[0]}, x{take_profit_steps[1]}, x{take_profit_steps[2]}\n"
            f"Trailing Stop: -{trailing_stop_percentage}% sous pic\nStop-Loss: -{stop_loss_threshold} %"
        )
        bot.send_message(chat_id, f'🔍 Portefeuille rafraîchi pour {token}:\n{msg}', reply_markup=markup)
    except Exception as e:
        logger.error(f"Erreur refresh: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Erreur rafraîchissement {token}: {str(e)}')

def sell_token_immediate(chat_id: int, token: str) -> None:
    try:
        if token not in portfolio:
            bot.send_message(chat_id, f'⚠️ Vente impossible : {token} n\'est pas dans le portefeuille')
            return
        amount = portfolio[token]["amount"]
        chain = portfolio[token]["chain"]
        current_price = get_current_market_cap(token) / detected_tokens[token]['supply']
        sell_token(chat_id, token, amount, chain, current_price)
    except Exception as e:
        logger.error(f"Erreur vente immédiate: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Erreur vente immédiate {token}: {str(e)}')

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
    try:
        logger.info("Appel de initialize_bot...")
        initialize_bot()
        logger.info("Configuration du webhook...")
        set_webhook()
        logger.info("Bot initialisé avec succès.")
        logger.info(f"Démarrage de Flask sur 0.0.0.0:{PORT}...")
        app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
    except Exception as e:
        logger.error(f"Erreur fatale lors du démarrage du bot: {str(e)}")
        logger.warning("Démarrage de Flask malgré l'erreur pour répondre aux sondes Cloud Run...")
        app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)

if __name__ == "__main__":
    logger.info("Démarrage principal...")
    run_bot()
    
