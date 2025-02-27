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
import threading
from statistics import mean
from typing import Dict, List, Optional
from datetime import datetime, timedelta
import re

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
        logger.info("Tentative de connexion BSC...")
        w3 = Web3(HTTPProvider(BSC_RPC))
        w3.middleware_onion.inject(geth_poa_middleware, layer=0)
        if not w3.is_connected():
            logger.error("Connexion BSC échouée")
            raise ConnectionError("Connexion BSC échouée")
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

def get_token_data(token_address: str, chain: str) -> Dict[str, float]:
    try:
        response = session.get(f"https://api.dexscreener.com/latest/dex/tokens/{token_address}", timeout=10)
        response.raise_for_status()
        data = response.json()['pairs'][0] if response.json()['pairs'] else {}
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
            top_holder_pct = get_holder_distribution(token_address, chain)
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

def get_holder_distribution(token_address: str, chain: str) -> float:
    if chain == 'solana':
        try:
            response = session.get(f"https://public-api.birdeye.so/v1/token/token_security?address={token_address}", headers=BIRDEYE_HEADERS, timeout=10)
            response.raise_for_status()
            data = response.json()['data']
            top_holder = max(data.get('top_10_holder_percent', 0), data.get('top_20_holder_percent', 0))
            return top_holder
        except Exception as e:
            logger.error(f"Erreur récupération distribution holders {token_address}: {str(e)}")
            return 100
    return 0

def monitor_twitter(chat_id: int) -> None:
    global twitter_requests_remaining, twitter_last_reset, last_twitter_call
    while True:
        try:
            logger.info("Surveillance Twitter démarrée...")
            bot.send_message(chat_id, "📡 Surveillance Twitter activée...")
            base_delay = 30.0  # Augmenté pour capturer plus de CA rapidement
            error_count = 0
            max_errors = 10
            query_general = '("contract address" OR CA) -is:retweet'
            while trade_active and error_count < max_errors:
                current_time = time.time()
                if current_time - twitter_last_reset >= 900:
                    twitter_requests_remaining = 450
                    twitter_last_reset = current_time
                    logger.info("Quota Twitter réinitialisé : 450 requêtes.")
                    error_count = 0

                if twitter_requests_remaining <= 1:
                    wait_time = 900 - (current_time - twitter_last_reset) + 10
                    logger.warning(f"Quota épuisé ({twitter_requests_remaining}), attente de {wait_time:.1f}s...")
                    bot.send_message(chat_id, f"⚠️ Quota Twitter épuisé, pause de {wait_time:.1f}s...")
                    time.sleep(wait_time)
                    continue

                time.sleep(base_delay)
                response = session.get(
                    f"https://api.twitter.com/2/tweets/search/recent?query={query_general}&max_results=100&expansions=author_id&user.fields=public_metrics",
                    headers=TWITTER_HEADERS, timeout=10
                )
                response.raise_for_status()
                twitter_requests_remaining -= 1
                last_twitter_call = current_time
                data = response.json()
                tweets = data.get('data', [])
                users = {u['id']: u for u in data.get('includes', {}).get('users', [])}

                for tweet in tweets:
                    user = users.get(tweet['author_id'])
                    followers = user.get('public_metrics', {}).get('followers_count', 0) if user else 0
                    if followers >= 5000:  # Réduit pour plus de signaux
                        text = tweet['text'].lower()
                        words = text.split()
                        for word in words:
                            if (len(word) == 42 and word.startswith("0x")) or (32 <= len(word) <= 44 and not word.startswith("0x")):
                                chain = 'bsc' if word.startswith("0x") else 'solana'
                                if validate_address(word, chain) and word not in portfolio:
                                    if word in rejected_tokens:
                                        age_reject = (time.time() - rejected_tokens[word]) / 3600
                                        if age_reject > REJECT_EXPIRATION_HOURS:
                                            del rejected_tokens[word]
                                        elif age_reject < (RETRY_DELAY_MINUTES / 60):
                                            continue
                                    bot.send_message(chat_id, f'🔍 Token détecté via Twitter (@{user["username"]}, {followers} abonnés): {word} ({chain})')
                                    check_token(chat_id, word, chain)
        except requests.exceptions.RequestException as e:
            if getattr(e.response, 'status_code', None) == 429:
                wait_time = 900
                logger.warning(f"429 détecté, attente de {wait_time}s")
                bot.send_message(chat_id, f"⚠️ Limite Twitter atteinte, pause de {wait_time}s...")
                time.sleep(wait_time)
            else:
                logger.error(f"Erreur Twitter: {str(e)}")
                time.sleep(60)
        except Exception as e:
            logger.error(f"Erreur Twitter inattendue: {str(e)}")
            bot.send_message(chat_id, f'⚠️ Erreur Twitter: {str(e)}. Reprise dans 60s...')
            time.sleep(60)
        logger.info("Redémarrage surveillance Twitter après erreur...")
        time.sleep(10)

def snipe_new_pairs_bsc(chat_id: int) -> None:
    while True:
        try:
            logger.info("Sniping BSC démarré...")
            bot.send_message(chat_id, "🔫 Sniping BSC activé...")
            factory = w3.eth.contract(address=PANCAKE_FACTORY_ADDRESS, abi=PANCAKE_FACTORY_ABI)
            while trade_active:
                latest_block = w3.eth.block_number
                events = factory.events.PairCreated.get_logs(fromBlock=latest_block - 10, toBlock=latest_block)
                for event in events:
                    token_address = event['args']['token0'] if event['args']['token0'] != "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c" else event['args']['token1']
                    if token_address not in portfolio:
                        if token_address in rejected_tokens:
                            age_reject = (time.time() - rejected_tokens[token_address]) / 3600
                            if age_reject > REJECT_EXPIRATION_HOURS:
                                del rejected_tokens[token_address]
                            elif age_reject < (RETRY_DELAY_MINUTES / 60):
                                continue
                        bot.send_message(chat_id, f'🎯 Snipe détecté : {token_address} (BSC) dans les 10 derniers blocs')
                        check_token(chat_id, token_address, 'bsc')
                time.sleep(1)
        except Exception as e:
            logger.error(f"Erreur sniping BSC: {str(e)}")
            bot.send_message(chat_id, f'⚠️ Erreur sniping BSC: {str(e)}. Reprise dans 5s...')
            time.sleep(5)
        logger.info("Redémarrage sniping BSC après erreur...")
        time.sleep(10)

def check_token(chat_id: int, token_address: str, chain: str) -> bool:
    try:
        if len(portfolio) >= max_positions:
            bot.send_message(chat_id, f'⚠️ Limite de {max_positions} positions atteinte, token {token_address} ignoré.')
            return False

        data = get_token_data(token_address, chain)
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
            min_ratio = MIN_BUY_SELL_RATIO_BSC
            min_volume = MIN_VOLUME_BSC
            max_volume = MAX_VOLUME_BSC
            min_market_cap = MIN_MARKET_CAP_BSC
            max_market_cap = MAX_MARKET_CAP_BSC
        else:
            min_ratio = MIN_BUY_SELL_RATIO_SOL
            min_volume = MIN_VOLUME_SOL
            max_volume = MAX_VOLUME_SOL
            min_market_cap = MIN_MARKET_CAP_SOL
            max_market_cap = MAX_MARKET_CAP_SOL

        if age_hours > MAX_TOKEN_AGE_HOURS or age_hours < 0:
            bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : âge {age_hours:.2f}h > {MAX_TOKEN_AGE_HOURS}h ou invalide')
            rejected_tokens[token_address] = time.time()
            return False
        if buy_sell_ratio < min_ratio:
            bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : ratio achat/vente 5min {buy_sell_ratio:.2f} < {min_ratio}')
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
            bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : {recent_buy_count} achats récents < {MIN_RECENT_TXNS} en 5 min')
            rejected_tokens[token_address] = time.time()
            return False
        if chain == 'solana' and top_holder_pct > MAX_HOLDER_PERCENTAGE:
            bot.send_message(chat_id, f'⚠️ Token {token_address} rejeté : top holder détient {top_holder_pct}% > {MAX_HOLDER_PERCENTAGE}%')
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

        bot.send_message(chat_id, f'🔍 Token détecté : {token_address} ({chain}) - Ratio A/V 5min: {buy_sell_ratio:.2f}, Vol: ${volume_24h:.2f}, Liq: ${liquidity:.2f}, MC: ${market_cap:.2f}, Achats 5min: {recent_buy_count}, Âge: {age_hours:.2f}h')
        detected_tokens[token_address] = {
            'address': token_address, 'volume': volume_24h, 'liquidity': liquidity,
            'market_cap': market_cap, 'supply': market_cap / price, 'price': price,
            'buy_sell_ratio': buy_sell_ratio, 'recent_buy_count': recent_buy_count
        }
        if chain == 'bsc':
            buy_token_bsc(chat_id, token_address, mise_depart_bsc)
        else:
            buy_token_solana(chat_id, token_address, mise_depart_sol)
        daily_trades['buys'].append({'token': token_address, 'chain': chain, 'amount': mise_depart_bsc if chain == 'bsc' else mise_depart_sol, 'timestamp': datetime.now().strftime('%H:%M:%S')})
        return True
    except Exception as e:
        logger.error(f"Erreur vérification token {token_address}: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Erreur vérification token {token_address}: {str(e)}')
        daily_trades['buys'].append({'token': token_address, 'chain': chain, 'amount': 0, 'timestamp': datetime.now().strftime('%H:%M:%S'), 'error': str(e)})
        rejected_tokens[token_address] = time.time()
        return False

def detect_new_tokens_bsc(chat_id: int) -> None:
    global last_valid_token_time
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

        for event in events:
            token_address = event['args']['token0'] if event['args']['token0'] != "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c" else event['args']['token1']
            if token_address in rejected_tokens:
                age_reject = (time.time() - rejected_tokens[token_address]) / 3600
                if age_reject > REJECT_EXPIRATION_HOURS:
                    del rejected_tokens[token_address]
                elif age_reject < (RETRY_DELAY_MINUTES / 60):
                    continue
            result = check_token(chat_id, token_address, 'bsc')
            if isinstance(result, Exception):
                rejection_reasons.append(f"{token_address}: erreur - {str(result)}")
                rejected_count += 1
            elif result is False:
                rejection_reasons.append(f"{token_address}: critères non remplis")
                rejected_count += 1
            elif result:
                valid_token_found = True
                last_valid_token_time = time.time()

        if not valid_token_found:
            bot.send_message(chat_id, f'⚠️ Aucun token BSC ne correspond aux critères ({rejected_count} rejetés).')
            if rejection_reasons:
                bot.send_message(chat_id, "Raisons de rejet :\n" + "\n".join(rejection_reasons[:5]))
        bot.send_message(chat_id, "✅ Détection BSC terminée.")
    except Exception as e:
        logger.error(f"Erreur détection BSC: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Erreur détection BSC: {str(e)}')

def detect_new_tokens_solana(chat_id: int) -> None:
    bot.send_message(chat_id, "🔍 Début détection Solana...")
    try:
        # Tentative DexScreener
        response = session.get("https://api.dexscreener.com/latest/dex/pairs", timeout=10)
        response.raise_for_status()
        json_response = response.json()
        tokens = [token for token in json_response.get('pairs', []) if token.get('chainId') == 'solana']
        logger.info(f"DexScreener: {len(tokens)} paires détectées sur Solana")
        
        # Fallback Birdeye si DexScreener échoue ou manque de données
        if not tokens:
            response = session.get("https://public-api.birdeye.so/v1/pairs/list?sort_by=time&sort_type=desc&limit=50", headers=BIRDEYE_HEADERS, timeout=10)
            response.raise_for_status()
            tokens = response.json().get('data', {}).get('pairs', [])
            logger.info(f"Birdeye: {len(tokens)} paires détectées sur Solana")

        bot.send_message(chat_id, f"⬇️ {len(tokens)} paires détectées sur Solana")
        tokens.sort(key=lambda x: x.get('pairCreatedAt', 0) if 'pairCreatedAt' in x else x.get('created_at', 0), reverse=True)
        
        for token in tokens[:50]:  # Limite à 50 pour performance
            token_address = token['baseToken']['address'] if 'baseToken' in token else token['address']
            if token_address in rejected_tokens:
                age_reject = (time.time() - rejected_tokens[token_address]) / 3600
                if age_reject > REJECT_EXPIRATION_HOURS:
                    del rejected_tokens[token_address]
                elif age_reject < (RETRY_DELAY_MINUTES / 60):
                    continue
            volume_24h = float(token.get('volume', {}).get('h24', 0) if 'volume' in token else token.get('volume_24h', 0))
            pair_created_at = token.get('pairCreatedAt', 0) / 1000 if token.get('pairCreatedAt') else token.get('created_at', time.time() - 3600) / 1000
            age_hours = (time.time() - pair_created_at) / 3600
            
            if age_hours <= MAX_TOKEN_AGE_HOURS and volume_24h > MIN_VOLUME_SOL:
                bot.send_message(chat_id, f'🆕 Token Solana détecté : {token_address} (Vol: ${volume_24h:.2f}, Âge: {age_hours:.2f}h)')
                check_token(chat_id, token_address, 'solana')
    except Exception as e:
        logger.error(f"Erreur détection Solana: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Erreur détection Solana: {str(e)}. Tentative de récupération...')
        time.sleep(10)

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
                threading.Thread(target=trading_cycle, args=(chat_id,), daemon=True).start()
                threading.Thread(target=snipe_new_pairs_bsc, args=(chat_id,), daemon=True).start()
                threading.Thread(target=monitor_twitter, args=(chat_id,), daemon=True).start()
                threading.Thread(target=watchdog, args=(chat_id,), daemon=True).start()
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

def trading_cycle(chat_id: int) -> None:
    global trade_active
    cycle_count = 0
    threading.Thread(target=monitor_and_sell, args=(chat_id,), daemon=True).start()
    while True:
        try:
            if not trade_active:
                time.sleep(10)
                continue
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

def watchdog(chat_id: int) -> None:
    logger.info("Watchdog démarré...")
    while True:
        try:
            if not trade_active:
                time.sleep(60)
                continue
            # Vérifier si les threads principaux sont actifs
            threads = threading.enumerate()
            required_threads = ['trading_cycle', 'snipe_new_pairs_bsc', 'monitor_twitter']
            active_threads = [t.name for t in threads]
            for thread_name in required_threads:
                if thread_name not in active_threads:
                    logger.warning(f"Thread {thread_name} inactif, redémarrage...")
                    bot.send_message(chat_id, f'⚠️ Thread {thread_name} inactif, redémarrage...')
                    if thread_name == 'trading_cycle':
                        threading.Thread(target=trading_cycle, args=(chat_id,), daemon=True, name='trading_cycle').start()
                    elif thread_name == 'snipe_new_pairs_bsc':
                        threading.Thread(target=snipe_new_pairs_bsc, args=(chat_id,), daemon=True, name='snipe_new_pairs_bsc').start()
                    elif thread_name == 'monitor_twitter':
                        threading.Thread(target=monitor_twitter, args=(chat_id,), daemon=True, name='monitor_twitter').start()
            time.sleep(60)
        except Exception as e:
            logger.error(f"Erreur dans watchdog: {str(e)}")
            bot.send_message(chat_id, f'⚠️ Erreur watchdog: {str(e)}. Reprise dans 60s...')
            time.sleep(60)

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

def buy_token_bsc(chat_id: int, contract_address: str, amount: float) -> None:
    try:
        dynamic_slippage = 10
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
            entry_price = detected_tokens[contract_address]['price']
            portfolio[contract_address] = {
                'amount': amount, 'chain': 'bsc', 'entry_price': entry_price,
                'market_cap_at_buy': detected_tokens[contract_address]['market_cap'],
                'current_market_cap': detected_tokens[contract_address]['market_cap'],
                'price_history': [entry_price], 'highest_price': entry_price, 'profit': 0.0,
                'buy_time': time.time()
            }
            bot.send_message(chat_id, f'✅ Achat effectué : {amount} BNB de {contract_address}')
        else:
            bot.send_message(chat_id, f'⚠️ Échec achat {contract_address}, TX: {tx_hash.hex()}')
            daily_trades['buys'][-1]['error'] = "Transaction échouée"
    except Exception as e:
        logger.error(f"Erreur achat BSC: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Échec achat {contract_address}: {str(e)}')
        if daily_trades['buys'] and daily_trades['buys'][-1]['token'] == contract_address:
            daily_trades['buys'][-1]['error'] = str(e)

def buy_token_solana(chat_id: int, contract_address: str, amount: float) -> None:
    try:
        dynamic_slippage = 10
        amount_in = int(amount * 10**9)
        response = session.post(SOLANA_RPC, json={
            "jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash", "params": [{"commitment": "finalized"}]
        }, timeout=5)
        blockhash = response.json()['result']['value']['blockhash']
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
        tx_hash = session.post(SOLANA_RPC, json={
            "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
            "params": [base58.b58encode(tx.serialize()).decode('utf-8')]
        }, timeout=5).json()['result']
        bot.send_message(chat_id, f'⏳ Achat en cours de {amount} SOL de {contract_address}, TX: {tx_hash}')
        time.sleep(2)
        entry_price = detected_tokens[contract_address]['price']
        portfolio[contract_address] = {
            'amount': amount, 'chain': 'solana', 'entry_price': entry_price,
            'market_cap_at_buy': detected_tokens[contract_address]['market_cap'],
            'current_market_cap': detected_tokens[contract_address]['market_cap'],
            'price_history': [entry_price], 'highest_price': entry_price, 'profit': 0.0,
            'buy_time': time.time()
        }
        bot.send_message(chat_id, f'✅ Achat effectué : {amount} SOL de {contract_address}')
    except Exception as e:
        logger.error(f"Erreur achat Solana: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Échec achat {contract_address}: {str(e)}')
        if daily_trades['buys'] and daily_trades['buys'][-1]['token'] == contract_address:
            daily_trades['buys'][-1]['error'] = str(e)

def sell_token(chat_id: int, contract_address: str, amount: float, chain: str, current_price: float) -> None:
    global mise_depart_bsc, mise_depart_sol
    if chain == "solana":
        try:
            dynamic_slippage = 10
            amount_out = int(amount * 10**9)
            response = session.post(SOLANA_RPC, json={
                "jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash", "params": [{"commitment": "finalized"}]
            }, timeout=5)
            blockhash = response.json()['result']['value']['blockhash']
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
            tx_hash = session.post(SOLANA_RPC, json={
                "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                "params": [base58.b58encode(tx.serialize()).decode('utf-8')]
            }, timeout=5).json()['result']
            bot.send_message(chat_id, f'⏳ Vente en cours de {amount} SOL de {contract_address}, TX: {tx_hash}')
            time.sleep(2)
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

def sell_token_percentage(chat_id: int, token: str, percentage: float) -> None:
    try:
        if token not in portfolio:
            bot.send_message(chat_id, f'⚠️ Vente impossible : {token} n\'est pas dans le portefeuille')
            return
        total_amount = portfolio[token]['amount']
        amount_to_sell = total_amount * (percentage / 100)
        chain = portfolio[token]['chain']
        current_price = get_current_market_cap(token) / detected_tokens[token]['supply']
        sell_token(chat_id, token, amount_to_sell, chain, current_price)
    except Exception as e:
        logger.error(f"Erreur vente partielle: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Erreur vente partielle {token}: {str(e)}')

def monitor_and_sell(chat_id: int) -> None:
    while True:
        try:
            if not trade_active or not portfolio:
                time.sleep(2)
                continue
            for contract_address, data in list(portfolio.items()):
                chain = data['chain']
                amount = data['amount']
                current_mc = get_token_data(contract_address, chain)['market_cap']
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

                if profit_pct >= take_profit_steps[2] * 100:
                    sell_token(chat_id, contract_address, amount, chain, current_price)
                elif profit_pct >= take_profit_steps[1] * 100 and trend < 1.05:
                    sell_amount = amount / 2
                    sell_token(chat_id, contract_address, sell_amount, chain, current_price)
                elif profit_pct >= take_profit_steps[0] * 100 and trend < 1.02:
                    sell_amount = amount / 3
                    sell_token(chat_id, contract_address, sell_amount, chain, current_price)
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
            chain = data['chain']
            current_mc = get_token_data(ca, chain)['market_cap']
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
        return get_token_data(contract_address, chain)['market_cap']
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
            f"Profit: {profit:.2f}%\nProfit cumulé: ${portfolio[token]['profit']:.4f} {portfolio[token]['chain'].upper()}\n"
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
        chain = portfolio[token]['chain']
        current_price = get_current_market_cap(token) / detected_tokens[token]['supply']
        sell_token(chat_id, token, amount, chain, current_price)
    except Exception as e:
        logger.error(f"Erreur vente immédiate: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Erreur vente immédiate {token}: {str(e)}')

def show_daily_summary(chat_id: int) -> None:
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
            current_mc = get_token_data(ca, chain)['market_cap']
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
            logger.info("Appel de initialize_bot...")
            initialize_bot()
            logger.info("Configuration du webhook...")
            set_webhook()
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
            
