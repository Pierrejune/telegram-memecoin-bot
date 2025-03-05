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
import asyncio
import websockets
import concurrent.futures
from statistics import mean
from datetime import datetime
import re
from waitress import serve

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
session = requests.Session()
session.headers.update(HEADERS)
retry_strategy = Retry(total=5, backoff_factor=0.1, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("https://", adapter)

last_twitter_call = 0
twitter_last_reset = time.time()
twitter_requests_remaining = 450
daily_trades = {'buys': [], 'sells': []}
rejected_tokens = {}
trade_active = False
cross_chain_data = {}

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "default_token")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "0x0")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "dummy_key")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://default.example.com/webhook")
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY", "dummy_solana_key")
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY", "dummy_birdeye_key")
TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN", "dummy_twitter_token")
PORT = int(os.getenv("PORT", 8080))
BSC_RPC = os.getenv("BSC_RPC", "wss://bsc-ws-node.nariox.org:443")  # WebSocket BSC
SOLANA_RPC_ALT = os.getenv("SOLANA_RPC_ALT", "wss://api.mainnet-beta.solana.com")  # WebSocket Solana

BIRDEYE_HEADERS = {"X-API-KEY": BIRDEYE_API_KEY}
TWITTER_HEADERS = {"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"}

app = Flask(__name__)
bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=False)

w3 = None
solana_keypair = None

mise_depart_bsc = 0.02
mise_depart_sol = 0.37
gas_fee = 5
stop_loss_threshold = 15
trailing_stop_percentage = 5
take_profit_steps = [1.5, 2, 5]
detected_tokens = {}
portfolio = {}
max_positions = 3
profit_reinvestment_ratio = 0.5

MIN_VOLUME_SOL = 5000
MAX_VOLUME_SOL = 500000
MIN_VOLUME_BSC = 5000
MAX_VOLUME_BSC = 500000
MIN_LIQUIDITY = 5000
MIN_MARKET_CAP_SOL = 50000
MAX_MARKET_CAP_SOL = 1000000
MIN_MARKET_CAP_BSC = 50000
MAX_MARKET_CAP_BSC = 1000000
MIN_BUY_SELL_RATIO = 1.5
MAX_HOLDER_PERCENTAGE = 30.0
MIN_RECENT_TXNS = 1
MAX_TOKEN_AGE_HOURS = 72
REJECT_EXPIRATION_HOURS = 24

ERC20_ABI = json.loads('[{"constant": true, "inputs": [], "name": "totalSupply", "outputs": [{"name": "", "type": "uint256"}], "payable": false, "stateMutability": "view", "type": "function"}]')
PANCAKE_ROUTER_ADDRESS = "0x10ED43C718714eb63d5aA57B78B54704E256024E"
PANCAKE_FACTORY_ADDRESS = "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"
PANCAKE_ROUTER_ABI = json.loads('[{"inputs": [{"internalType": "uint256", "name": "amountIn", "type": "uint256"}, {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"}, {"internalType": "address[]", "name": "path", "type": "address[]"}, {"internalType": "address", "name": "to", "type": "address"}, {"internalType": "uint256", "name": "deadline", "type": "uint256"}], "name": "swapExactETHForTokens", "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}], "stateMutability": "payable", "type": "function"}, {"inputs": [{"internalType": "uint256", "name": "amountOut", "type": "uint256"}, {"internalType": "uint256", "name": "amountInMax", "type": "uint256"}, {"internalType": "address[]", "name": "path", "type": "address[]"}, {"internalType": "address", "name": "to", "type": "address"}, {"internalType": "uint256", "name": "deadline", "type": "uint256"}], "name": "swapExactTokensForETH", "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}], "stateMutability": "nonpayable", "type": "function"}]')
PANCAKE_FACTORY_ABI = json.loads('[{"anonymous":false,"inputs":[{"indexed":true,"internalType":"address","name":"token0","type":"address"},{"indexed":true,"internalType":"address","name":"token1","type":"address"},{"indexed":false,"internalType":"address","name":"pair","type":"address"},{"indexed":false,"internalType":"uint256","name":"","type":"uint256"}],"name":"PairCreated","type":"event"}]')
RAYDIUM_PROGRAM_ID = Pubkey.from_string("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8")
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")

def initialize_bot():
    global w3, solana_keypair
    logger.debug("Tentative de connexion BSC...")
    try:
        w3 = Web3(Web3.WebsocketProvider(BSC_RPC))
        w3.middleware_onion.inject(geth_poa_middleware, layer=0)
        if not w3.is_connected():
            logger.warning("Connexion BSC échouée")
        else:
            logger.info("Connexion BSC réussie via WebSocket")
    except Exception as e:
        logger.error(f"Erreur connexion BSC: {str(e)}")
        w3 = None

    logger.debug("Initialisation de la clé Solana...")
    try:
        solana_keypair = Keypair.from_bytes(base58.b58decode(SOLANA_PRIVATE_KEY))
        logger.info("Clé Solana initialisée.")
    except Exception as e:
        logger.error(f"Erreur initialisation Solana: {str(e)}")
        solana_keypair = None

def set_webhook():
    logger.debug("Configuration du webhook...")
    try:
        bot.remove_webhook()
        time.sleep(1)
        response = bot.set_webhook(url=WEBHOOK_URL)
        logger.info(f"Webhook configuré sur {WEBHOOK_URL}, réponse: {response}")
        return response
    except Exception as e:
        logger.error(f"Erreur configuration webhook: {str(e)}")
        return False

def is_safe_token_bsc(token_address):
    try:
        response = session.get(f"https://api.honeypot.is/v2/IsHoneypot?address={token_address}", timeout=1)
        data = response.json()
        safe = not data.get("isHoneypot", True) and data.get("buyTax", 0) <= 10 and data.get("sellTax", 0) <= 10
        logger.info(f"Vérification sécurité BSC {token_address}: {'Sûr' if safe else 'Non sûr'}")
        return safe
    except Exception as e:
        logger.error(f"Erreur vérification Honeypot BSC {token_address}: {str(e)}")
        return False

def is_safe_token_solana(token_address):
    try:
        response = session.get(f"https://public-api.birdeye.so/v1/token/token_security?address={token_address}", headers=BIRDEYE_HEADERS, timeout=1)
        data = response.json()['data']
        safe = data.get('is_open_source', False) and not data.get('is_honeypot', True)
        logger.info(f"Vérification sécurité Solana {token_address}: {'Sûr' if safe else 'Non sûr'}")
        return safe
    except Exception as e:
        logger.error(f"Erreur vérification sécurité Solana {token_address}: {str(e)}")
        return False

def validate_address(token_address, chain):
    if chain == 'bsc':
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
        response = session.get(f"https://api.dexscreener.com/latest/dex/tokens/{token_address}", timeout=1)
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
        buy_sell_ratio = buy_count_5m / max(sell_count_5m, 1)
        recent_buy_count = buy_count_5m
        pair_created_at = data.get('pairCreatedAt', 0) / 1000 if data.get('pairCreatedAt') else time.time() - 3600
        
        if chain == 'solana':
            top_holder_pct = get_holder_distribution(token_address, chain)
        else:
            top_holder_pct = 0
        
        token_data = {
            'volume_24h': volume_24h, 'liquidity': liquidity, 'market_cap': market_cap,
            'price': price, 'buy_sell_ratio': buy_sell_ratio, 'recent_buy_count': recent_buy_count,
            'pair_created_at': pair_created_at, 'top_holder_pct': top_holder_pct
        }
        
        if token_address not in cross_chain_data:
            cross_chain_data[token_address] = {}
        cross_chain_data[token_address][chain] = token_data
        return token_data
    except Exception as e:
        logger.error(f"Erreur récupération données DexScreener {token_address} ({chain}): {str(e)}")
        return {'volume_24h': 0, 'liquidity': 0, 'market_cap': 0, 'price': 0, 'buy_sell_ratio': 0, 'recent_buy_count': 0, 'pair_created_at': time.time()}

def get_holder_distribution(token_address, chain):
    if chain == 'solana':
        try:
            response = session.get(f"https://public-api.birdeye.so/v1/token/token_security?address={token_address}", headers=BIRDEYE_HEADERS, timeout=1)
            data = response.json()['data']
            return max(data.get('top_10_holder_percent', 0), data.get('top_20_holder_percent', 0))
        except Exception:
            return 100
    return 0

def monitor_twitter(chat_id):
    global twitter_requests_remaining, twitter_last_reset, last_twitter_call
    if trade_active:
        logger.info("Surveillance Twitter démarrée...")
        bot.send_message(chat_id, "📡 Surveillance Twitter activée...")
    while trade_active:
        try:
            current_time = time.time()
            if current_time - twitter_last_reset >= 900:
                twitter_requests_remaining = 450
                twitter_last_reset = current_time
                logger.info("Quota Twitter réinitialisé : 450 requêtes.")
                bot.send_message(chat_id, "ℹ️ Quota Twitter réinitialisé : 450 requêtes restantes")

            if twitter_requests_remaining <= 10:
                wait_time = 900 - (current_time - twitter_last_reset) + 1
                logger.warning(f"Quota faible ({twitter_requests_remaining}), attente de {wait_time:.1f}s...")
                time.sleep(wait_time)
                continue

            response = session.get(
                "https://api.twitter.com/2/tweets/search/recent?query=\"contract address\" OR CA OR launch OR pump -is:retweet&max_results=100&expansions=author_id&user.fields=public_metrics",
                headers=TWITTER_HEADERS, timeout=5
            )
            response.raise_for_status()
            twitter_requests_remaining -= 1
            last_twitter_call = current_time
            remaining = response.headers.get('x-rate-limit-remaining', twitter_requests_remaining)
            twitter_requests_remaining = int(remaining) if remaining else max(twitter_requests_remaining - 1, 0)
            data = response.json()
            tweets = data.get('data', [])
            users = {u['id']: u for u in data.get('includes', {}).get('users', [])}

            logger.info(f"Tweets analysés, restant: {twitter_requests_remaining}")
            bot.send_message(chat_id, f"✅ Tweets analysés, {len(tweets)} détectés, restant: {twitter_requests_remaining}")
            for tweet in tweets:
                user = users.get(tweet['author_id'])
                followers = user.get('public_metrics', {}).get('followers_count', 0) if user else 0
                if followers >= 2000:
                    text = tweet['text'].lower()
                    words = text.split()
                    for word in words:
                        chain = 'bsc' if word.startswith("0x") else 'solana'
                        if validate_address(word, chain) and word not in portfolio:
                            if word in rejected_tokens and (time.time() - rejected_tokens[word]) / 3600 < REJECT_EXPIRATION_HOURS:
                                continue
                            bot.send_message(chat_id, f'🔍 Token détecté via Twitter (@{user["username"]}, {followers} abonnés): {word} ({chain})')
                            threading.Thread(target=validate_and_trade, args=(chat_id, word, chain)).start()
            time.sleep(900)  # Strictement 15 minutes
        except requests.exceptions.RequestException as e:
            if getattr(e.response, 'status_code', None) == 429:
                wait_time = 900 - (current_time - twitter_last_reset) + 1
                logger.warning(f"429 détecté, attente de {wait_time}s")
                bot.send_message(chat_id, f"⚠️ Limite Twitter atteinte, pause de {wait_time}s...")
                time.sleep(wait_time)
            else:
                logger.error(f"Erreur Twitter: {str(e)}")
                time.sleep(60)
        except Exception as e:
            logger.error(f"Erreur Twitter inattendue: {str(e)}")
            time.sleep(60)
            async def snipe_new_pairs_bsc(chat_id):
    if trade_active:
        logger.info("Sniping BSC démarré...")
        bot.send_message(chat_id, "🔫 Sniping BSC activé...")
    if w3 is None:
        initialize_bot()
    if w3 is None or not w3.is_connected():
        logger.warning("BSC non connecté, sniping désactivé")
        return
    factory = w3.eth.contract(address=PANCAKE_FACTORY_ADDRESS, abi=PANCAKE_FACTORY_ABI)
    subscription_id = w3.eth.subscribe('logs', {'address': PANCAKE_FACTORY_ADDRESS})
    async with w3.ws as ws:
        while trade_active:
            try:
                event = await ws.recv()
                if 'topics' in event and event['topics'][0].hex() == factory.events.PairCreated.signature:
                    token0 = w3.to_checksum_address(event['data'][2:66][-40:])
                    token1 = w3.to_checksum_address(event['data'][66:130][-40:])
                    token_address = token0 if token0 != "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c" else token1
                    if token_address not in portfolio and (token_address not in rejected_tokens or (time.time() - rejected_tokens[token_address]) / 3600 > REJECT_EXPIRATION_HOURS):
                        logger.info(f"Snipe détecté BSC: {token_address}")
                        bot.send_message(chat_id, f'🎯 Snipe détecté : {token_address} (BSC)')
                        threading.Thread(target=validate_and_trade, args=(chat_id, token_address, 'bsc')).start()
            except Exception as e:
                logger.error(f"Erreur sniping BSC: {str(e)}")
                await asyncio.sleep(1)

async def snipe_solana_pools(chat_id):
    if trade_active:
        logger.info("Sniping Solana démarré...")
        bot.send_message(chat_id, "🔫 Sniping Solana activé...")
    uri = SOLANA_RPC_ALT  # "wss://api.mainnet-beta.solana.com"
    async with websockets.connect(uri) as ws:
        subscription_msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "programSubscribe",
            "params": [str(RAYDIUM_PROGRAM_ID), {"encoding": "base64", "commitment": "confirmed"}]
        }
        await ws.send(json.dumps(subscription_msg))
        while trade_active:
            try:
                msg = await ws.recv()
                data = json.loads(msg)
                if 'result' in data and 'value' in data['result'] and 'account' in data['result']['value']:
                    account_data = data['result']['value']['account']['data']
                    if isinstance(account_data, list) and account_data[1] == "base64":
                        decoded = base58.b58decode(account_data[0]).decode('utf-8', errors='ignore')
                        match = re.search(r'[A-Za-z0-9]{32,44}', decoded)
                        if match:
                            token_address = match.group(0)
                            if validate_address(token_address, 'solana') and token_address not in portfolio:
                                logger.info(f"Snipe détecté Solana: {token_address}")
                                bot.send_message(chat_id, f'🎯 Snipe détecté : {token_address} (Solana)')
                                threading.Thread(target=validate_and_trade, args=(chat_id, token_address, 'solana')).start()
            except Exception as e:
                logger.error(f"Erreur sniping Solana: {str(e)}")
                await asyncio.sleep(1)

def validate_and_trade(chat_id, token_address, chain):
    try:
        start_time = time.time()
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
        min_volume = MIN_VOLUME_BSC if chain == 'bsc' else MIN_VOLUME_SOL
        max_volume = MAX_VOLUME_BSC if chain == 'bsc' else MAX_VOLUME_SOL
        min_market_cap = MIN_MARKET_CAP_BSC if chain == 'bsc' else MIN_MARKET_CAP_SOL
        max_market_cap = MAX_MARKET_CAP_BSC if chain == 'bsc' else MAX_MARKET_CAP_SOL

        if len(portfolio) >= max_positions:
            bot.send_message(chat_id, f'⚠️ {token_address} rejeté : Limite de {max_positions} positions atteinte')
            return
        if age_hours > MAX_TOKEN_AGE_HOURS or age_hours < 0:
            bot.send_message(chat_id, f'⚠️ {token_address} rejeté : Âge {age_hours:.2f}h > {MAX_TOKEN_AGE_HOURS}h')
            rejected_tokens[token_address] = time.time()
            return
        if liquidity < MIN_LIQUIDITY:
            bot.send_message(chat_id, f'⚠️ {token_address} rejeté : Liquidité ${liquidity:.2f} < ${MIN_LIQUIDITY}')
            rejected_tokens[token_address] = time.time()
            return
        if volume_24h < min_volume or volume_24h > max_volume:
            bot.send_message(chat_id, f'⚠️ {token_address} rejeté : Volume ${volume_24h:.2f} hors plage [${min_volume}, ${max_volume}]')
            rejected_tokens[token_address] = time.time()
            return
        if market_cap < min_market_cap or market_cap > max_market_cap:
            bot.send_message(chat_id, f'⚠️ {token_address} rejeté : Market Cap ${market_cap:.2f} hors plage [${min_market_cap}, ${max_market_cap}]')
            rejected_tokens[token_address] = time.time()
            return
        if buy_sell_ratio < MIN_BUY_SELL_RATIO:
            bot.send_message(chat_id, f'⚠️ {token_address} rejeté : Ratio A/V {buy_sell_ratio:.2f} < {MIN_BUY_SELL_RATIO}')
            rejected_tokens[token_address] = time.time()
            return
        if recent_buy_count < MIN_RECENT_TXNS:
            bot.send_message(chat_id, f'⚠️ {token_address} rejeté : {recent_buy_count} achats récents < {MIN_RECENT_TXNS}')
            rejected_tokens[token_address] = time.time()
            return
        if top_holder_pct > MAX_HOLDER_PERCENTAGE:
            bot.send_message(chat_id, f'⚠️ {token_address} rejeté : Top holders {top_holder_pct}% > {MAX_HOLDER_PERCENTAGE}%')
            rejected_tokens[token_address] = time.time()
            return
        if chain == 'bsc' and not is_safe_token_bsc(token_address):
            bot.send_message(chat_id, f'⚠️ {token_address} rejeté : Possible rug ou taxes élevées (BSC)')
            rejected_tokens[token_address] = time.time()
            return
        if chain == 'solana' and not is_safe_token_solana(token_address):
            bot.send_message(chat_id, f'⚠️ {token_address} rejeté : Possible rug ou non open-source (Solana)')
            rejected_tokens[token_address] = time.time()
            return

        logger.info(f"Token validé {token_address} en {time.time() - start_time:.3f}s")
        bot.send_message(chat_id, f'✅ Token validé : {token_address} ({chain}) - Vol: ${volume_24h:.2f}, Liq: ${liquidity:.2f}, MC: ${market_cap:.2f}')
        detected_tokens[token_address] = {
            'address': token_address, 'volume': volume_24h, 'liquidity': liquidity,
            'market_cap': market_cap, 'supply': market_cap / price if price > 0 else 0, 'price': price,
            'buy_sell_ratio': buy_sell_ratio, 'recent_buy_count': recent_buy_count
        }
        if chain == 'bsc':
            buy_token_bsc(chat_id, token_address, mise_depart_bsc)
        else:
            buy_token_solana(chat_id, token_address, mise_depart_sol)
    except Exception as e:
        logger.error(f"Erreur validation/trade {token_address}: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Erreur validation {token_address}: {str(e)}')

def buy_token_bsc(chat_id, contract_address, amount):
    try:
        if w3 is None:
            initialize_bot()
        if not w3.is_connected():
            raise Exception("BSC non connecté")
        router = w3.eth.contract(address=PANCAKE_ROUTER_ADDRESS, abi=PANCAKE_ROUTER_ABI)
        amount_in = w3.to_wei(amount, 'ether')
        amount_out_min = int(amount_in * 0.95)  # Slippage 5%
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
        bot.send_message(chat_id, f'⏳ Achat BSC {amount} BNB de {contract_address}, TX: {tx_hash.hex()}')
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
            bot.send_message(chat_id, f'✅ Achat réussi : {amount} BNB de {contract_address}')
            daily_trades['buys'].append({'token': contract_address, 'chain': 'bsc', 'amount': amount, 'timestamp': datetime.now().strftime('%H:%M:%S')})
        else:
            bot.send_message(chat_id, f'⚠️ Échec achat BSC {contract_address}, TX: {tx_hash.hex()}')
    except Exception as e:
        logger.error(f"Erreur achat BSC {contract_address}: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Échec achat BSC {contract_address}: {str(e)}')

def buy_token_solana(chat_id, contract_address, amount):
    try:
        if solana_keypair is None:
            initialize_bot()
        if not solana_keypair:
            raise Exception("Solana non initialisé")
        amount_in = int(amount * 10**9)
        response = session.post(SOLANA_RPC_ALT.replace("wss", "https"), json={
            "jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash", "params": [{"commitment": "finalized"}]
        }, timeout=1)
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
        tx_hash = session.post(SOLANA_RPC_ALT.replace("wss", "https"), json={
            "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
            "params": [base58.b58encode(tx.serialize()).decode('utf-8')]
        }, timeout=1).json()['result']
        bot.send_message(chat_id, f'⏳ Achat Solana {amount} SOL de {contract_address}, TX: {tx_hash}')
        entry_price = detected_tokens[contract_address]['price']
        portfolio[contract_address] = {
            'amount': amount, 'chain': 'solana', 'entry_price': entry_price,
            'market_cap_at_buy': detected_tokens[contract_address]['market_cap'],
            'current_market_cap': detected_tokens[contract_address]['market_cap'],
            'price_history': [entry_price], 'highest_price': entry_price, 'profit': 0.0,
            'buy_time': time.time()
        }
        bot.send_message(chat_id, f'✅ Achat réussi : {amount} SOL de {contract_address}')
        daily_trades['buys'].append({'token': contract_address, 'chain': 'solana', 'amount': amount, 'timestamp': datetime.now().strftime('%H:%M:%S')})
    except Exception as e:
        logger.error(f"Erreur achat Solana {contract_address}: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Échec achat Solana {contract_address}: {str(e)}')

def sell_token(chat_id, contract_address, amount, chain, current_price):
    global mise_depart_bsc, mise_depart_sol
    if chain == "solana":
        try:
            if solana_keypair is None:
                initialize_bot()
            if not solana_keypair:
                raise Exception("Solana non initialisé")
            amount_out = int(amount * 10**9)
            response = session.post(SOLANA_RPC_ALT.replace("wss", "https"), json={
                "jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash", "params": [{"commitment": "finalized"}]
            }, timeout=1)
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
            tx_hash = session.post(SOLANA_RPC_ALT.replace("wss", "https"), json={
                "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                "params": [base58.b58encode(tx.serialize()).decode('utf-8')]
            }, timeout=1).json()['result']
            bot.send_message(chat_id, f'⏳ Vente Solana {amount} SOL de {contract_address}, TX: {tx_hash}')
            profit = (current_price - portfolio[contract_address]['entry_price']) * amount
            portfolio[contract_address]['profit'] += profit
            portfolio[contract_address]['amount'] -= amount
            if portfolio[contract_address]['amount'] <= 0:
                del portfolio[contract_address]
            reinvest_amount = profit * profit_reinvestment_ratio
            mise_depart_sol += reinvest_amount
            bot.send_message(chat_id, f'✅ Vente Solana réussie : {amount} SOL, Profit: {profit:.4f} SOL, Réinvesti: {reinvest_amount:.4f} SOL')
            daily_trades['sells'].append({'token': contract_address, 'chain': 'solana', 'amount': amount, 'pnl': profit, 'timestamp': datetime.now().strftime('%H:%M:%S')})
        except Exception as e:
            logger.error(f"Erreur vente Solana {contract_address}: {str(e)}")
            bot.send_message(chat_id, f'⚠️ Échec vente Solana {contract_address}: {str(e)}')
    else:
        try:
            if w3 is None:
                initialize_bot()
            if not w3.is_connected():
                raise Exception("BSC non connecté")
            token_amount = w3.to_wei(amount, 'ether')
            amount_in_max = int(token_amount * 1.05)  # Slippage 5%
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
            bot.send_message(chat_id, f'⏳ Vente BSC {amount} BNB de {contract_address}, TX: {tx_hash.hex()}')
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
            if receipt.status == 1:
                profit = (current_price - portfolio[contract_address]['entry_price']) * amount
                portfolio[contract_address]['profit'] += profit
                portfolio[contract_address]['amount'] -= amount
                if portfolio[contract_address]['amount'] <= 0:
                    del portfolio[contract_address]
                reinvest_amount = profit * profit_reinvestment_ratio
                mise_depart_bsc += reinvest_amount
                bot.send_message(chat_id, f'✅ Vente BSC réussie : {amount} BNB, Profit: {profit:.4f} BNB, Réinvesti: {reinvest_amount:.4f} BNB')
                daily_trades['sells'].append({'token': contract_address, 'chain': 'bsc', 'amount': amount, 'pnl': profit, 'timestamp': datetime.now().strftime('%H:%M:%S')})
            else:
                bot.send_message(chat_id, f'⚠️ Échec vente BSC {contract_address}, TX: {tx_hash.hex()}')
        except Exception as e:
            logger.error(f"Erreur vente BSC {contract_address}: {str(e)}")
            bot.send_message(chat_id, f'⚠️ Échec vente BSC {contract_address}: {str(e)}')

def sell_token_percentage(chat_id, token, percentage):
    try:
        if token not in portfolio:
            bot.send_message(chat_id, f'⚠️ Vente impossible : {token} n\'est pas dans le portefeuille')
            return
        total_amount = portfolio[token]['amount']
        amount_to_sell = total_amount * (percentage / 100)
        chain = portfolio[token]['chain']
        market_cap = get_current_market_cap(token)
        supply = detected_tokens.get(token, {}).get('supply', 0)
        current_price = market_cap / supply if supply > 0 else 0
        sell_token(chat_id, token, amount_to_sell, chain, current_price)
    except Exception as e:
        logger.error(f"Erreur vente partielle {token}: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Erreur vente partielle {token}: {str(e)}')

def monitor_and_sell(chat_id):
    while trade_active:
        try:
            if not portfolio:
                time.sleep(1)
                continue
            for contract_address, data in list(portfolio.items()):
                chain = data['chain']
                amount = data['amount']
                current_mc = get_token_data(contract_address, chain)['market_cap']
                current_price = current_mc / data['supply'] if data['supply'] > 0 else 0
                data['price_history'].append(current_price)
                if len(data['price_history']) > 10:
                    data['price_history'].pop(0)
                portfolio[contract_address]['current_market_cap'] = current_mc
                profit_pct = (current_price - data['entry_price']) / data['entry_price'] * 100 if data['entry_price'] > 0 else 0
                loss_pct = -profit_pct if profit_pct < 0 else 0
                trend = mean(data['price_history'][-3:]) / data['entry_price'] if len(data['price_history']) >= 3 and data['entry_price'] > 0 else profit_pct / 100
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
            time.sleep(1)
        except Exception as e:
            logger.error(f"Erreur surveillance globale: {str(e)}")
            bot.send_message(chat_id, f'⚠️ Erreur surveillance: {str(e)}. Reprise dans 5s...')
            time.sleep(5)

def show_portfolio(chat_id):
    try:
        if w3 is None or solana_keypair is None:
            initialize_bot()
        bnb_balance = w3.eth.get_balance(WALLET_ADDRESS) / 10**18 if w3 and w3.is_connected() else 0
        sol_balance = get_solana_balance(WALLET_ADDRESS)
        msg = f'💰 Portefeuille:\nBNB : {bnb_balance:.4f}\nSOL : {sol_balance:.4f}\n\n'
        markup = InlineKeyboardMarkup()
        for ca, data in portfolio.items():
            chain = data['chain']
            current_mc = get_token_data(ca, chain)['market_cap']
            profit = (current_mc - data['market_cap_at_buy']) / data['market_cap_at_buy'] * 100 if data['market_cap_at_buy'] > 0 else 0
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

def get_solana_balance(wallet_address):
    try:
        if solana_keypair is None:
            initialize_bot()
        if not solana_keypair:
            return 0
        response = session.post(SOLANA_RPC_ALT.replace("wss", "https"), json={
            "jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [str(solana_keypair.pubkey())]
        }, timeout=1)
        result = response.json().get('result', {})
        return result.get('value', 0) / 10**9
    except Exception as e:
        logger.error(f"Erreur solde Solana: {str(e)}")
        return 0

def get_current_market_cap(contract_address):
    try:
        chain = portfolio[contract_address]['chain'] if contract_address in portfolio else ('bsc' if contract_address.startswith("0x") else 'solana')
        return get_token_data(contract_address, chain)['market_cap']
    except Exception as e:
        logger.error(f"Erreur market cap: {str(e)}")
        return detected_tokens.get(contract_address, {}).get('market_cap', 0)

def refresh_token(chat_id, token):
    try:
        current_mc = get_current_market_cap(token)
        profit = (current_mc - portfolio[token]['market_cap_at_buy']) / portfolio[token]['market_cap_at_buy'] * 100 if portfolio[token]['market_cap_at_buy'] > 0 else 0
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

def sell_token_immediate(chat_id, token):
    try:
        if token not in portfolio:
            bot.send_message(chat_id, f'⚠️ Vente impossible : {token} n\'est pas dans le portefeuille')
            return
        amount = portfolio[token]["amount"]
        chain = portfolio[token]['chain']
        current_price = get_current_market_cap(token) / detected_tokens.get(token, {}).get('supply', 1)
        sell_token(chat_id, token, amount, chain, current_price)
    except Exception as e:
        logger.error(f"Erreur vente immédiate: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Erreur vente immédiate {token}: {str(e)}')

def show_daily_summary(chat_id):
    try:
        msg = f"📅 Récapitulatif du jour ({datetime.now().strftime('%Y-%m-%d')}):\n\n"
        msg += "📈 Achats :\n"
        total_buys = 0
        for trade in daily_trades['buys']:
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

def show_token_management(chat_id):
    try:
        if not portfolio:
            bot.send_message(chat_id, "📋 Aucun token en portefeuille.")
            return
        msg = "📋 Gestion des tokens en portefeuille :\n\n"
        markup = InlineKeyboardMarkup()
        for ca, data in portfolio.items():
            chain = data['chain']
            current_mc = get_token_data(ca, chain)['market_cap']
            profit = (current_mc - data['market_cap_at_buy']) / data['market_cap_at_buy'] * 100 if data['market_cap_at_buy'] > 0 else 0
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

@app.route("/")
def health_check():
    logger.debug("Health check appelé")
    return "Bot is running", 200

@app.route("/webhook", methods=['POST'])
def webhook():
    global trade_active
    logger.debug("Webhook reçu")
    try:
        if request.method == "POST" and request.headers.get("content-type") == "application/json":
            update = telebot.types.Update.de_json(request.get_json())
            logger.debug(f"Update reçu: {update}")
            bot.process_new_updates([update])
            logger.info("Update traité avec succès")
            if not trade_active and update.message:
                trade_active = True
                threading.Thread(target=initialize_and_run_threads, args=(update.message.chat.id,), daemon=True).start()
            return 'OK', 200
        logger.warning("Requête webhook invalide")
        return abort(403)
    except Exception as e:
        logger.error(f"Erreur dans webhook: {str(e)}")
        return 'ERROR', 500

@app.route("/setup-webhook", methods=['GET'])
def setup_webhook_endpoint():
    logger.debug("Tentative de configuration du webhook via endpoint")
    success = set_webhook()
    return "Webhook configuré avec succès" if success else "Échec de la configuration du webhook", 200 if success else 500

@bot.message_handler(commands=['start'])
def start_message(message):
    logger.info(f"Commande /start reçue de {message.chat.id}")
    try:
        bot.send_message(message.chat.id, "✅ Bot démarré!")
        show_main_menu(message.chat.id)
    except Exception as e:
        logger.error(f"Erreur dans start_message: {str(e)}")
        bot.send_message(message.chat.id, f'⚠️ Erreur au démarrage: {str(e)}')

@bot.message_handler(commands=['menu'])
def menu_message(message):
    logger.info(f"Commande /menu reçue de {message.chat.id}")
    try:
        bot.send_message(message.chat.id, "📋 Menu affiché!")
        show_main_menu(message.chat.id)
    except Exception as e:
        logger.error(f"Erreur dans menu_message: {str(e)}")
        bot.send_message(message.chat.id, f'⚠️ Erreur affichage menu: {str(e)}')

def show_main_menu(chat_id):
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
    global MIN_VOLUME_SOL, MAX_VOLUME_SOL, MIN_MARKET_CAP_SOL, MAX_MARKET_CAP_SOL, MIN_BUY_SELL_RATIO
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
                threading.Thread(target=initialize_and_run_threads, args=(chat_id,), daemon=True).start()
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
            bot.send_message(chat_id, "Entrez le nouveau seuil min de liquidité (en $, ex. : 5000) :")
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
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_buy_sell_ratio)
        elif call.data == "adjust_buy_sell_ratio_sol":
            bot.send_message(chat_id, "Entrez le nouveau ratio achat/vente min pour Solana (ex. : 1.5) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_buy_sell_ratio)
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

def show_config_menu(chat_id):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("➕ Augmenter mise BSC (+0.01 BNB)", callback_data="increase_mise_bsc"),
        InlineKeyboardButton("➕ Augmenter mise SOL (+0.01 SOL)", callback_data="increase_mise_sol")
    )
    bot.send_message(chat_id, "⚙️ Configuration:", reply_markup=markup)

def show_settings_menu(chat_id):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("🔧 Ajuster Mise BSC", callback_data="adjust_mise_bsc"),
        InlineKeyboardButton("🔧 Ajuster Mise Solana", callback_data="adjust_mise_sol"),
        InlineKeyboardButton("🔧 Ajuster Gas Fee (BSC)", callback_data="adjust_gas")
    )
    bot.send_message(chat_id, "🔧 Réglages:", reply_markup=markup)

def show_tp_sl_menu(chat_id):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("📉 Ajuster Stop-Loss", callback_data="adjust_stop_loss"),
        InlineKeyboardButton("📈 Ajuster Take-Profit", callback_data="adjust_take_profit")
    )
    bot.send_message(chat_id, "📈 Configuration TP/SL", reply_markup=markup)

def show_threshold_menu(chat_id):
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
        InlineKeyboardButton("📊 Ratio A/V", callback_data="adjust_buy_sell_ratio_bsc")
    )
    bot.send_message(chat_id, (
        f'📊 Seuils de détection :\n'
        f'- BSC Volume: {MIN_VOLUME_BSC} $ - {MAX_VOLUME_BSC} $\n'
        f'- Ratio A/V: {MIN_BUY_SELL_RATIO}\n'
        f'- BSC Market Cap: {MIN_MARKET_CAP_BSC} $ - {MAX_MARKET_CAP_BSC} $\n'
        f'- Min Liquidité: {MIN_LIQUIDITY} $\n'
        f'- Solana Volume: {MIN_VOLUME_SOL} $ - {MAX_VOLUME_SOL} $\n'
        f'- Solana Market Cap: {MIN_MARKET_CAP_SOL} $ - {MAX_MARKET_CAP_SOL} $\n'
        f'- Âge max: {MAX_TOKEN_AGE_HOURS}h'
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
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 5000)")

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

def adjust_buy_sell_ratio(message):
    global MIN_BUY_SELL_RATIO
    chat_id = message.chat.id
    try:
        new_ratio = float(message.text)
        if new_ratio > 0:
            MIN_BUY_SELL_RATIO = new_ratio
            bot.send_message(chat_id, f'✅ Ratio A/V mis à jour à {MIN_BUY_SELL_RATIO}')
        else:
            bot.send_message(chat_id, "⚠️ Le ratio doit être positif!")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 1.5)")

def initialize_and_run_threads(chat_id):
    logger.debug("Initialisation et lancement des threads...")
    try:
        initialize_bot()
        logger.info("Bot initialisé avec succès.")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        tasks = [
            snipe_new_pairs_bsc(chat_id),
            snipe_solana_pools(chat_id),
            asyncio.to_thread(monitor_twitter, chat_id),
            asyncio.to_thread(monitor_and_sell, chat_id)
        ]
        loop.run_until_complete(asyncio.gather(*tasks))
    except Exception as e:
        logger.error(f"Erreur dans l'initialisation ou les threads: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur threads: {str(e)}. Certaines fonctionnalités peuvent être indisponibles.")

def run_polling():
    logger.info("Démarrage du mode polling pour Telegram...")
    while True:
        try:
            bot.polling(none_stop=True, interval=0, timeout=20)
        except Exception as e:
            logger.error(f"Erreur polling: {str(e)}. Reprise dans 5s...")
            time.sleep(5)

if __name__ == "__main__":
    logger.info("Démarrage principal...")
    try:
        logger.debug("Lancement de Waitress...")
        serve(app, host="0.0.0.0", port=PORT, threads=8)
        logger.info(f"Waitress démarré sur le port {PORT}")

        def startup_tasks():
            logger.debug("Début des tâches de démarrage...")
            try:
                if set_webhook():
                    logger.info("Webhook configuré avec succès.")
                else:
                    logger.warning("Échec du webhook, démarrage du polling.")
                    threading.Thread(target=run_polling, daemon=True).start()
                initialize_bot()
            except Exception as e:
                logger.error(f"Erreur dans startup_tasks: {str(e)}. Passage en mode polling.")
                threading.Thread(target=run_polling, daemon=True).start()

        threading.Thread(target=startup_tasks, daemon=True).start()

        while True:
            time.sleep(60)
            logger.debug("Bot en cours d'exécution...")
    except Exception as e:
        logger.critical(f"Erreur critique au démarrage: {str(e)}. Tentative de survie avec polling...")
        threading.Thread(target=run_polling, daemon=True).start()
        while True:
            time.sleep(60)

logger.info("Fin du chargement du module bot.py")
        
