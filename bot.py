import os
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import logging
import requests
import time
import asyncio
import websockets
import json
import base58
import aiohttp
from flask import Flask, request, abort
from cachetools import TTLCache
from web3 import Web3
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from solders.system_program import TransferParams, transfer
from solders.signature import Signature
from solders.instruction import Instruction
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import threading

# Configuration du logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Headers pour API
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/91.0.4472.124",
    "Accept": "application/json",
}

# Session persistante avec retries et timeout
session = requests.Session()
session.headers.update(HEADERS)
retry_strategy = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("https://", adapter)

# Dernier appel Twitter
last_twitter_call = 0

# Chargement des variables depuis Cloud Run avec logs explicites
logger.info("Chargement des variables d'environnement...")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY")
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")
BSC_SCAN_API_KEY = os.getenv("BSC_SCAN_API_KEY")
TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN")
PORT = int(os.getenv("PORT", 8080))  # Port par défaut 8080 pour Cloud Run
BSC_RPC = os.getenv("BSC_RPC", "https://bsc-dataseed.binance.org/")
SOLANA_RPC_WS = os.getenv("SOLANA_RPC_WS")  # Pas de valeur par défaut
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
SOLANA_FALLBACK_WS = "wss://api.mainnet-beta.solana.com"

# Headers pour Twitter API
TWITTER_HEADERS = {"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"}

# Validation des variables critiques
logger.info("Validation des variables critiques...")
missing_vars = []
required_vars = {
    "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
    "WALLET_ADDRESS": WALLET_ADDRESS,
    "PRIVATE_KEY": PRIVATE_KEY,
    "SOLANA_PRIVATE_KEY": SOLANA_PRIVATE_KEY,
    "WEBHOOK_URL": WEBHOOK_URL,
    "BIRDEYE_API_KEY": BIRDEYE_API_KEY,
    "BSC_SCAN_API_KEY": BSC_SCAN_API_KEY,
    "TWITTER_BEARER_TOKEN": TWITTER_BEARER_TOKEN,
    "BSC_RPC": BSC_RPC,
    "SOLANA_RPC_WS": SOLANA_RPC_WS
}
for var_name, var_value in required_vars.items():
    if not var_value:
        missing_vars.append(var_name)
        logger.error(f"Variable manquante : {var_name}")
if missing_vars:
    logger.critical(f"Variables manquantes détectées : {missing_vars}. Le bot ne peut pas démarrer.")
    raise ValueError(f"Variables manquantes : {missing_vars}")
else:
    logger.info("Toutes les variables requises sont présentes.")

# Initialisation
logger.info("Initialisation du bot Telegram...")
bot = telebot.TeleBot(TELEGRAM_TOKEN)
app = Flask(__name__)

# BSC (PancakeSwap) avec QuickNode
logger.info("Initialisation connexion BSC...")
w3 = Web3(Web3.HTTPProvider(BSC_RPC))
if not w3.is_connected():
    logger.error("Connexion BSC échouée.")
    raise ConnectionError("Connexion BSC échouée")
logger.info("Connexion BSC réussie via QuickNode.")
PANCAKE_ROUTER_ADDRESS = "0x10ED43C718714eb63d5aA57B78B54704E256024E"
PANCAKE_FACTORY_ADDRESS = "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"

PANCAKE_FACTORY_ABI = json.loads('''
[
    {
        "anonymous": false,
        "inputs": [
            {"indexed": true, "internalType": "address", "name": "token0", "type": "address"},
            {"indexed": true, "internalType": "address", "name": "token1", "type": "address"},
            {"indexed": false, "internalType": "address", "name": "pair", "type": "address"},
            {"indexed": false, "internalType": "uint256", "name": "", "type": "uint256"}
        ],
        "name": "PairCreated",
        "type": "event"
    }
]
''')

PANCAKE_ROUTER_ABI = json.loads('''
[
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"},
            {"internalType": "address[]", "name": "path", "type": "address[]"},
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "deadline", "type": "uint256"}
        ],
        "name": "swapExactETHForTokens",
        "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}],
        "stateMutability": "payable",
        "type": "function"
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountOut", "type": "uint256"},
            {"internalType": "uint256", "name": "amountInMax", "type": "uint256"},
            {"internalType": "address[]", "name": "path", "type": "address[]"},
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "deadline", "type": "uint256"}
        ],
        "name": "swapExactTokensForETH",
        "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}],
        "stateMutability": "nonpayable",
        "type": "function"
    }
]
''')

# Solana (Raydium)
logger.info("Initialisation Solana...")
solana_keypair = Keypair.from_base58_string(SOLANA_PRIVATE_KEY)
RAYDIUM_PROGRAM_ID = Pubkey.from_string("675kPX9MHTjS2zt1qfr1NYHuzeLxfQM9H24wFSceAHj2")
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")

try:
    response = session.post(SOLANA_RPC, json={
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getLatestBlockhash",
        "params": [{"commitment": "finalized"}]
    }, timeout=5)
    response.raise_for_status()
    blockhash = response.json().get('result', {}).get('value', {}).get('blockhash')
    logger.info(f"Connexion Solana HTTP réussie, blockhash: {blockhash}")
except Exception as e:
    logger.error(f"Erreur initiale Solana RPC: {str(e)}")
    raise ConnectionError(f"Connexion Solana HTTP échouée: {str(e)}")

# Configuration de base
mise_depart_bsc = 0.02
mise_depart_sol = 0.37
slippage = 5
gas_fee = 5
stop_loss_threshold = 30
take_profit_steps = [2, 3, 5]
detected_tokens = {}
trade_active = False
cache = TTLCache(maxsize=100, ttl=300)
portfolio = {}
twitter_tokens = []

# Critères personnalisés
MIN_VOLUME_SOL = 50000
MAX_VOLUME_SOL = 500000
MIN_VOLUME_BSC = 75000
MAX_VOLUME_BSC = 750000
MIN_LIQUIDITY = 100000
MIN_LIQUIDITY_PCT = 0.02
MIN_PRICE_CHANGE = 30
MAX_PRICE_CHANGE = 200
MIN_MARKET_CAP_SOL = 100000
MAX_MARKET_CAP_SOL = 1000000
MIN_MARKET_CAP_BSC = 200000
MAX_MARKET_CAP_BSC = 2000000
MAX_TAX = 5
MAX_HOLDER_PCT = 20
MIN_TX_PER_MIN_BSC = 5
MAX_TX_PER_MIN_BSC = 75
MIN_TX_PER_MIN_SOL = 15
MAX_TX_PER_MIN_SOL = 150

# Vérification anti-rug pull BSC via Honeypot.is
def is_safe_token_bsc(token_address):
    try:
        response = session.get(f"https://api.honeypot.is/v2/IsHoneypot?address={token_address}", timeout=10)
        data = response.json()
        is_safe = (
            not data.get("isHoneypot", True) and
            data.get("buyTax", 0) <= MAX_TAX and
            data.get("sellTax", 0) <= MAX_TAX
        )
        return is_safe
    except Exception as e:
        logger.error(f"Erreur vérification Honeypot: {str(e)}")
        return False

# Vérification anti-rug pull Solana via Birdeye
def is_safe_token_solana(token_address):
    try:
        response = session.get(
            f"https://public-api.birdeye.so/public/token_overview?address={token_address}",
            headers={"X-API-KEY": BIRDEYE_API_KEY},
            timeout=10
        )
        data = response.json()['data']
        is_safe = data.get('liquidity', 0) >= MIN_LIQUIDITY
        return is_safe
    except Exception as e:
        logger.error(f"Erreur vérification Solana: {str(e)}")
        return False

# Calcul réel des transactions par minute (QuickNode BSC)
def get_real_tx_per_min_bsc(token_address):
    try:
        latest_block = w3.eth.block_number
        block = w3.eth.get_block(latest_block - 1, full_transactions=True)
        tx_count = sum(1 for tx in block['transactions'] if tx['to'] == token_address or tx['from'] == token_address)
        return tx_count * 20  # Approximation pour 1 min (block time ~ 3s, 20 blocs/min)
    except Exception as e:
        logger.error(f"Erreur calcul tx/min BSC pour {token_address}: {str(e)}")
        return 0

# Surveillance Twitter/X avec polling ajusté
async def monitor_twitter(chat_id):
    global last_twitter_call
    logger.info("Surveillance Twitter/X en cours...")
    bot.send_message(chat_id, "📡 Début surveillance Twitter...")
    while trade_active:
        current_time = time.time()
        if current_time - last_twitter_call < 120:
            await asyncio.sleep(120 - (current_time - last_twitter_call))
        try:
            query_kanye = "from:kanyewest memecoin OR token OR launch OR \"contract address\" OR CA"
            response = session.get(
                f"https://api.twitter.com/2/tweets/search/recent?query={query_kanye}&max_results=10",
                headers=TWITTER_HEADERS,
                timeout=10
            )
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 900))
                logger.warning(f"Erreur 429 - Limite atteinte, attente de {retry_after} secondes")
                bot.send_message(chat_id, f"⚠️ Limite Twitter atteinte, pause de {retry_after} s...")
                await asyncio.sleep(retry_after)
                continue
            response.raise_for_status()
            remaining = int(response.headers.get("x-rate-limit-remaining", 500))
            logger.info(f"Requêtes Twitter restantes : {remaining}")
            if remaining < 10:
                await asyncio.sleep(900)
            tweets = response.json().get('data', [])
            for tweet in tweets:
                text = tweet['text'].lower()
                if 'contract address' in text or "ca" in text or "token" in text:
                    words = text.split()
                    for word in words:
                        if (len(word) == 42 and word.startswith("0x")) or len(word) == 44:
                            if word not in twitter_tokens:
                                twitter_tokens.append(word)
                            bot.send_message(chat_id, f"✅ Token détecté via X (@kanyewest): {word}")
                            check_twitter_token(chat_id, word)
            last_twitter_call = time.time()
            await asyncio.sleep(120)
        except Exception as e:
            logger.error(f"Erreur surveillance Twitter: {str(e)}")
            bot.send_message(chat_id, f"⚠️ Erreur surveillance Twitter: {str(e)}")
            await asyncio.sleep(900)

# Vérification des tokens détectés via Twitter
def check_twitter_token(chat_id, token_address):
    try:
        if token_address.startswith("0x"):  # BSC
            if w3.eth.get_code(w3.to_checksum_address(token_address)) == b'':
                return False
            token_contract = w3.eth.contract(address=w3.to_checksum_address(token_address), abi=[{
                "name": "totalSupply",
                "outputs": [{"name": "", "type": "uint256"}],
                "payable": False,
                "stateMutability": "view",
                "type": "function"
            }])
            supply = token_contract.functions.totalSupply().call() / 10**18
            volume_response = session.get(
                f"https://api.bscscan.com/api?module=stats&action=tokenbalance&contractaddress={token_address}&address={PANCAKE_ROUTER_ADDRESS}&tag=latest&apikey={BSC_SCAN_API_KEY}",
                timeout=10
            )
            volume_data = volume_response.json()
            if volume_data['status'] != '1':
                return False
            volume_24h = float(volume_data['result']) / 10**18
            liquidity = volume_24h * 0.5
            market_cap = volume_24h * supply
            tx_per_min = get_real_tx_per_min_bsc(token_address)
            if not (MIN_TX_PER_MIN_BSC <= tx_per_min <= MAX_TX_PER_MIN_BSC):
                return False
            if not (MIN_VOLUME_BSC <= volume_24h <= MAX_VOLUME_BSC):
                return False
            if liquidity < MIN_LIQUIDITY:
                return False
            if not (MIN_MARKET_CAP_BSC <= market_cap <= MAX_MARKET_CAP_BSC):
                return False
            if not is_safe_token_bsc(token_address):
                return False
            bot.send_message(chat_id,
                f"✅ Token X détecté : {token_address} (BSC) - Tx/min réel: {tx_per_min}, Vol: ${volume_24h:.2f}, Liq: ${liquidity:.2f}, MC: ${market_cap:.2f}"
            )
            detected_tokens[token_address] = {
                'address': token_address,
                'volume': volume_24h,
                'tx_per_min': tx_per_min,
                'liquidity': liquidity,
                'market_cap': market_cap,
                'supply': supply
            }
            buy_token_bsc(chat_id, token_address, mise_depart_bsc)
            return True
        else:  # Solana
            response = session.get(
                f"https://public-api.birdeye.so/public/token_overview?address={token_address}",
                headers={"X-API-KEY": BIRDEYE_API_KEY},
                timeout=10
            )
            data = response.json()['data']
            volume_24h = float(data.get('v24hUSD', 0))
            liquidity = float(data.get('liquidity', 0))
            market_cap = float(data.get('mc', 0))
            supply = float(data.get('supply', 0))
            tx_per_min = asyncio.run(get_real_tx_per_min_solana(token_address))
            if not (MIN_TX_PER_MIN_SOL <= tx_per_min <= MAX_TX_PER_MIN_SOL):
                return False
            if not (MIN_VOLUME_SOL <= volume_24h <= MAX_VOLUME_SOL):
                return False
            if liquidity < MIN_LIQUIDITY:
                return False
            if not (MIN_MARKET_CAP_SOL <= market_cap <= MAX_MARKET_CAP_SOL):
                return False
            if not is_safe_token_solana(token_address):
                return False
            bot.send_message(chat_id,
                f"✅ Token X détecté : {token_address} (Solana) - Tx/min réel: {tx_per_min}, Vol: ${volume_24h:.2f}, Liq: ${liquidity:.2f}, MC: ${market_cap:.2f}"
            )
            detected_tokens[token_address] = {
                'address': token_address,
                'volume': volume_24h,
                'tx_per_min': tx_per_min,
                'liquidity': liquidity,
                'market_cap': market_cap,
                'supply': supply
            }
            buy_token_solana(chat_id, token_address, mise_depart_sol)
            return True
    except Exception as e:
        logger.error(f"Erreur vérification token X {token_address} : {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur vérification token X {token_address} : {str(e)}")
        return False

# Calcul réel des transactions par minute (QuickNode Solana)
async def get_real_tx_per_min_solana(token_address):
    ws_url = SOLANA_RPC_WS
    try:
        async with websockets.connect(ws_url) as websocket:
            block_request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getBlock",
                "params": ["latest", {"transactionDetails": "full", "commitment": "finalized"}]
            }
            await asyncio.wait_for(websocket.send(json.dumps(block_request)), timeout=30)
            response = await asyncio.wait_for(websocket.recv(), timeout=30)
            block_data = json.loads(response)
            if 'result' not in block_data:
                return 0
            tx_count = sum(1 for tx in block_data['result']['transactions'] if token_address in str(tx))
            return tx_count * 150  # Approximation pour 1 min (block time ~ 0.4s)
    except Exception as e:
        logger.error(f"Erreur calcul tx/min Solana pour {token_address}: {str(e)}")
        if ws_url != SOLANA_FALLBACK_WS:
            logger.info(f"Tentative avec RPC public : {SOLANA_FALLBACK_WS}")
            ws_url = SOLANA_FALLBACK_WS
            try:
                async with websockets.connect(ws_url) as websocket:
                    await asyncio.wait_for(websocket.send(json.dumps(block_request)), timeout=30)
                    response = await asyncio.wait_for(websocket.recv(), timeout=30)
                    block_data = json.loads(response)
                    if 'result' not in block_data:
                        return 0
                    tx_count = sum(1 for tx in block_data['result']['transactions'] if token_address in str(tx))
                    return tx_count * 150
            except Exception as fallback_e:
                logger.error(f"Échec avec RPC public pour {token_address}: {str(fallback_e)}")
                return 0
        return 0

# Détection des nouveaux tokens BSC
def detect_new_tokens_bsc(chat_id):
    bot.send_message(chat_id, "📡 Début détection BSC...")
    try:
        factory = w3.eth.contract(address=PANCAKE_FACTORY_ADDRESS, abi=PANCAKE_FACTORY_ABI)
        latest_block = w3.eth.block_number
        logger.info(f"Bloc actuel : {latest_block}")
        events = factory.events.PairCreated.get_logs(fromBlock=latest_block - 50, toBlock=latest_block)
        logger.info(f"Événements trouvés : {len(events)}")
        bot.send_message(chat_id, f"📡 {len(events)} nouvelles paires détectées sur BSC")
        if not events:
            logger.info("Aucun événement PairCreated trouvé dans les 50 derniers blocs.")
            bot.send_message(chat_id, "ℹ️ Aucun événement PairCreated détecté dans les 50 derniers blocs.")
        rejected_count = 0
        valid_token_found = False
        for event in events:
            logger.info(f"Événement : {event}")
            token_address = event['args']['token0'] if event['args']['token0'] != "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c" else event['args']['token1']
            pair = event['args']['pair']
            logger.info(f"Token : {token_address}, Pair : {pair}")
            if w3.eth.get_code(w3.to_checksum_address(token_address)) == b'':
                logger.warning(f"Token {token_address} n'a pas de code de contrat valide.")
                rejected_count += 1
                continue
            try:
                token_contract = w3.eth.contract(address=w3.to_checksum_address(token_address), abi=[{
                    "name": "totalSupply",
                    "outputs": [{"name": "", "type": "uint256"}],
                    "payable": False,
                    "stateMutability": "view",
                    "type": "function"
                }])
                supply = token_contract.functions.totalSupply().call() / 10**18
            except Exception as e:
                logger.error(f"Erreur lors de l'appel à totalSupply pour {token_address}: {str(e)}")
                rejected_count += 1
                continue
            volume_response = session.get(
                f"https://api.bscscan.com/api?module=stats&action=tokenbalance&contractaddress={token_address}&address={pair}&tag=latest&apikey={BSC_SCAN_API_KEY}",
                timeout=10
            )
            volume_data = volume_response.json()
            if volume_data['status'] != '1':
                rejected_count += 1
                continue
            volume_24h = float(volume_data['result']) / 10**18
            liquidity = volume_24h * 0.5
            market_cap = volume_24h * supply
            tx_per_min = get_real_tx_per_min_bsc(token_address)
            if not (MIN_TX_PER_MIN_BSC <= tx_per_min <= MAX_TX_PER_MIN_BSC):
                rejected_count += 1
                continue
            if not (MIN_VOLUME_BSC <= volume_24h <= MAX_VOLUME_BSC):
                rejected_count += 1
                continue
            if liquidity < MIN_LIQUIDITY:
                rejected_count += 1
                continue
            if not (MIN_MARKET_CAP_BSC <= market_cap <= MAX_MARKET_CAP_BSC):
                rejected_count += 1
                continue
            if not is_safe_token_bsc(token_address):
                rejected_count += 1
                continue
            bot.send_message(chat_id,
                f"✅ Token détecté : {token_address} (BSC) - Tx/min réel: {tx_per_min}, Vol 24h: ${volume_24h:.2f}, Liq: ${liquidity:.2f}, MC: ${market_cap:.2f}"
            )
            detected_tokens[token_address] = {
                'address': token_address,
                'volume': volume_24h,
                'tx_per_min': tx_per_min,
                'liquidity': liquidity,
                'market_cap': market_cap,
                'supply': supply
            }
            buy_token_bsc(chat_id, token_address, mise_depart_bsc)
            valid_token_found = True
        if not valid_token_found:
            bot.send_message(chat_id, f"ℹ️ Aucun token BSC ne correspond aux critères ({rejected_count} rejetés).")
        bot.send_message(chat_id, "✔️ Détection BSC terminée.")
    except Exception as e:
        logger.error(f"Erreur détection BSC: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur détection BSC: {str(e)}")

# Test préalable de la connexion WebSocket Solana
async def test_solana_websocket(chat_id):
    ws_urls = [SOLANA_RPC_WS, SOLANA_FALLBACK_WS]
    for ws_url in ws_urls:
        try:
            async with websockets.connect(ws_url) as websocket:
                test_request = {"jsonrpc": "2.0", "id": 1, "method": "getHealth"}
                await asyncio.wait_for(websocket.send(json.dumps(test_request)), timeout=30)
                response = await asyncio.wait_for(websocket.recv(), timeout=30)
                logger.info(f"Test WebSocket Solana réussi avec {ws_url}: {response}")
                return ws_url
        except Exception as e:
            logger.error(f"Échec test WebSocket avec {ws_url}: {str(e)}")
    bot.send_message(chat_id, "⚠️ Tous les endpoints WebSocket Solana ont échoué. Vérifiez SOLANA_RPC_WS.")
    return None

# Détection des nouveaux tokens Solana avec QuickNode et retry
async def detect_new_tokens_solana(chat_id):
    bot.send_message(chat_id, "📡 Début détection Solana via QuickNode...")
    active_ws_url = await test_solana_websocket(chat_id)
    if not active_ws_url:
        return
    max_retries = 3
    for attempt in range(max_retries):
        try:
            async with websockets.connect(active_ws_url) as websocket:
                subscription = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "logsSubscribe",
                    "params": [{"mentions": [str(RAYDIUM_PROGRAM_ID)]}, {"commitment": "finalized"}]
                }
                await asyncio.wait_for(websocket.send(json.dumps(subscription)), timeout=30)
                response = await asyncio.wait_for(websocket.recv(), timeout=30)
                sub_id = json.loads(response).get('result')
                logger.info(f"Abonnement WebSocket Solana actif : {sub_id}")
                while trade_active:
                    message = await asyncio.wait_for(websocket.recv(), timeout=30)
                    data = json.loads(message)
                    if 'params' in data and 'result' in data['params']:
                        logs = data['params']['result']['value']['logs']
                        token_address = None
                        for log in logs:
                            if "Mint" in log:
                                words = log.split()
                                for word in words:
                                    if len(word) == 44:
                                        token_address = word
                                        break
                        if token_address and token_address not in detected_tokens:
                            bot.send_message(chat_id, f"✅ Nouveau pool détecté sur Raydium : {token_address}")
                            await check_solana_token(chat_id, token_address)
                break
        except Exception as e:
            logger.error(f"Erreur détection Solana (tentative {attempt + 1}/{max_retries}) : {str(e)}")
            bot.send_message(chat_id, f"⚠️ Erreur détection Solana (tentative {attempt + 1}/{max_retries}) : {str(e)}")
            if attempt < max_retries - 1:
                await asyncio.sleep(5)
            else:
                bot.send_message(chat_id, "❌ Échec définitif de la connexion Solana après plusieurs tentatives.")

async def check_solana_token(chat_id, token_address):
    try:
        response = session.get(
            f"https://public-api.birdeye.so/public/token_overview?address={token_address}",
            headers={"X-API-KEY": BIRDEYE_API_KEY},
            timeout=10
        )
        data = response.json()['data']
        volume_24h = float(data.get('v24hUSD', 0))
        liquidity = float(data.get('liquidity', 0))
        market_cap = float(data.get('mc', 0))
        supply = float(data.get('supply', 0))
        tx_per_min = await get_real_tx_per_min_solana(token_address)
        if not (MIN_TX_PER_MIN_SOL <= tx_per_min <= MAX_TX_PER_MIN_SOL):
            return
        if not (MIN_VOLUME_SOL <= volume_24h <= MAX_VOLUME_SOL):
            return
        if liquidity < MIN_LIQUIDITY:
            return
        if not (MIN_MARKET_CAP_SOL <= market_cap <= MAX_MARKET_CAP_SOL):
            return
        if not is_safe_token_solana(token_address):
            return
        bot.send_message(chat_id,
            f"✅ Token détecté : {token_address} (Solana) - Tx/min réel: {tx_per_min}, Vol: ${volume_24h:.2f}, Liq: ${liquidity:.2f}, MC: ${market_cap:.2f}"
        )
        detected_tokens[token_address] = {
            'address': token_address,
            'volume': volume_24h,
            'tx_per_min': tx_per_min,
            'liquidity': liquidity,
            'market_cap': market_cap,
            'supply': supply
        }
        buy_token_solana(chat_id, token_address, mise_depart_sol)
    except Exception as e:
        logger.error(f"Erreur vérification token Solana {token_address}: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur vérification token Solana {token_address}: {str(e)}")

# Webhook Telegram
@app.route("/webhook", methods=['POST'])
def webhook():
    logger.info("Webhook reçu")
    try:
        if request.method == "POST" and request.headers.get("content-type") == "application/json":
            update = telebot.types.Update.de_json(request.get_json())
            bot.process_new_updates([update])
            logger.info('Update traité avec succès')
            return 'OK', 200
        logger.warning("Requête webhook invalide")
        return abort(403)
    except Exception as e:
        logger.error(f"Erreur dans webhook: {str(e)}")
        return abort(500)

# Commandes Telegram
@bot.message_handler(commands=['start'])
def start_message(message):
    logger.info("Commande /start reçue")
    try:
        bot.send_message(message.chat.id, "🚀 Bot démarré! Bienvenue sur ton bot de trading de memecoins.")
        show_main_menu(message.chat.id)
    except Exception as e:
        logger.error(f"Erreur dans start_message: {str(e)}")
        bot.send_message(message.chat.id, f"⚠️ Erreur au démarrage: {str(e)}")

@bot.message_handler(commands=['menu'])
def menu_message(message):
    logger.info("Commande /menu reçue")
    try:
        bot.send_message(message.chat.id, "📋 Menu affiché!")
        show_main_menu(message.chat.id)
    except Exception as e:
        logger.error(f"Erreur dans menu_message: {str(e)}")
        bot.send_message(message.chat.id, f"⚠️ Erreur affichage menu: {str(e)}")

# Menu principal
def show_main_menu(chat_id):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("📊 Statut", callback_data="status"),
        InlineKeyboardButton("⚙️ Configure", callback_data="config"),
        InlineKeyboardButton("🚀 Lancer", callback_data="launch"),
        InlineKeyboardButton("🛑 Arrêter", callback_data="stop"),
        InlineKeyboardButton("💼 Portefeuille", callback_data="portfolio"),
        InlineKeyboardButton("🔧 Réglages", callback_data="settings"),
        InlineKeyboardButton("📈 TP/SL", callback_data="tp_sl_settings"),
        InlineKeyboardButton("📉 Seuils", callback_data="threshold_settings")
    )
    try:
        bot.send_message(chat_id, "📋 Voici le menu principal :", reply_markup=markup)
    except Exception as e:
        logger.error(f"Erreur dans show_main_menu: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur affichage menu: {str(e)}")

# Gestion des callbacks
@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    global mise_depart_bsc, mise_depart_sol, trade_active, slippage, gas_fee, stop_loss_threshold, take_profit_steps
    global MIN_VOLUME_BSC, MAX_VOLUME_BSC, MIN_LIQUIDITY, MIN_MARKET_CAP_BSC, MAX_MARKET_CAP_BSC
    global MIN_VOLUME_SOL, MAX_VOLUME_SOL, MIN_MARKET_CAP_SOL, MAX_MARKET_CAP_SOL
    chat_id = call.message.chat.id
    logger.info(f"Callback reçu: {call.data}")
    try:
        if call.data == "status":
            bot.send_message(chat_id, (
                f"📊 Statut actuel :\n"
                f"- Trading actif : {'Oui' if trade_active else 'Non'}\n"
                f"- Mise BSC : {mise_depart_bsc} BNB\n"
                f"- Mise Solana : {mise_depart_sol} SOL\n"
                f"- Slippage : {slippage}%\n"
                f"- Gas Fee : {gas_fee} Gwei"
            ))
        elif call.data == "config":
            show_config_menu(chat_id)
        elif call.data == "launch":
            if not trade_active:
                trade_active = True
                bot.send_message(chat_id, "🚀 Trading lancé avec succès!")
                logger.info("Lancement du trading cycle...")
                asyncio.run(trading_cycle(chat_id))
            else:
                bot.send_message(chat_id, "⚠️ Trading déjà en cours.")
        elif call.data == "stop":
            trade_active = False
            bot.send_message(chat_id, "🛑 Trading arrêté.")
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
            bot.send_message(chat_id, f"✅ Mise BSC augmentée à {mise_depart_bsc} BNB")
        elif call.data == "increase_mise_sol":
            mise_depart_sol += 0.01
            bot.send_message(chat_id, f"✅ Mise Solana augmentée à {mise_depart_sol} SOL")
        elif call.data == "adjust_mise_bsc":
            bot.send_message(chat_id, "Entrez la nouvelle mise BSC (en BNB, ex. : 0.02) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_mise_bsc)
        elif call.data == "adjust_mise_sol":
            bot.send_message(chat_id, "Entrez la nouvelle mise Solana (en SOL, ex. : 0.37) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_mise_sol)
        elif call.data == "adjust_slippage":
            bot.send_message(chat_id, "Entrez le nouveau slippage (en %, ex. : 5) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_slippage)
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
            bot.send_message(chat_id, "Entrez le nouveau seuil min de volume BSC (en $, ex. : 75000) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_min_volume_bsc)
        elif call.data == "adjust_max_volume_bsc":
            bot.send_message(chat_id, "Entrez le nouveau seuil max de volume BSC (en $, ex. : 750000) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_max_volume_bsc)
        elif call.data == "adjust_min_liquidity":
            bot.send_message(chat_id, "Entrez le nouveau seuil min de liquidité (en $, ex. : 100000) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_min_liquidity)
        elif call.data == "adjust_min_market_cap_bsc":
            bot.send_message(chat_id, "Entrez le nouveau seuil min de market cap BSC (en $, ex. : 200000) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_min_market_cap_bsc)
        elif call.data == "adjust_max_market_cap_bsc":
            bot.send_message(chat_id, "Entrez le nouveau seuil max de market cap BSC (en $, ex. : 2000000) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_max_market_cap_bsc)
        elif call.data == "adjust_min_volume_sol":
            bot.send_message(chat_id, "Entrez le nouveau seuil min de volume Solana (en $, ex. : 50000) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_min_volume_sol)
        elif call.data == "adjust_max_volume_sol":
            bot.send_message(chat_id, "Entrez le nouveau seuil max de volume Solana (en $, ex. : 500000) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_max_volume_sol)
        elif call.data == "adjust_min_market_cap_sol":
            bot.send_message(chat_id, "Entrez le nouveau seuil min de market cap Solana (en $, ex. : 100000) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_min_market_cap_sol)
        elif call.data == "adjust_max_market_cap_sol":
            bot.send_message(chat_id, "Entrez le nouveau seuil max de market cap Solana (en $, ex. : 1000000) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_max_market_cap_sol)
        elif call.data.startswith("refresh_"):
            token = call.data.split("_")[1]
            refresh_token(chat_id, token)
        elif call.data.startswith("sell_"):
            token = call.data.split("_")[1]
            sell_token_immediate(chat_id, token)
    except Exception as e:
        logger.error(f"Erreur dans callback_query: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur générale: {str(e)}")

# Cycle de trading optimisé pour la rapidité
async def trading_cycle(chat_id):
    global trade_active
    cycle_count = 0
    solana_task = asyncio.create_task(detect_new_tokens_solana(chat_id))
    twitter_task = asyncio.create_task(monitor_twitter(chat_id))
    while trade_active:
        cycle_count += 1
        bot.send_message(chat_id, f"🔍 Début du cycle de détection #{cycle_count}...")
        logger.info(f"Cycle {cycle_count} démarré")
        try:
            detect_new_tokens_bsc(chat_id)
            bot.send_message(chat_id, "📡 Surveillance Solana en cours via QuickNode (WebSocket)...")
            bot.send_message(chat_id, "⏳ Attente de 10 secondes avant le prochain cycle...")
            await asyncio.sleep(10)
        except Exception as e:
            logger.error(f"Erreur dans trading_cycle: {str(e)}")
            bot.send_message(chat_id, f"⚠️ Erreur dans le cycle: {str(e)}")
    trade_active = False
    solana_task.cancel()
    twitter_task.cancel()

# Menu de configuration
def show_config_menu(chat_id):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("Augmenter mise BSC (+0.01 BNB)", callback_data="increase_mise_bsc"),
        InlineKeyboardButton("Augmenter mise SOL (+0.01 SOL)", callback_data="increase_mise_sol")
    )
    try:
        bot.send_message(chat_id, "⚙️ Configuration:", reply_markup=markup)
    except Exception as e:
        logger.error(f"Erreur dans show_config_menu: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur configuration: {str(e)}")

# Menu des réglages
def show_settings_menu(chat_id):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("🔧 Ajuster Mise BSC", callback_data="adjust_mise_bsc"),
        InlineKeyboardButton("🔧 Ajuster Mise Solana", callback_data="adjust_mise_sol"),
        InlineKeyboardButton("🔧 Ajuster Slippage", callback_data="adjust_slippage"),
        InlineKeyboardButton("🔧 Ajuster Gas Fee (BSC)", callback_data="adjust_gas")
    )
    try:
        bot.send_message(chat_id, "🔧 Réglages:", reply_markup=markup)
    except Exception as e:
        logger.error(f"Erreur dans show_settings_menu: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur réglages: {str(e)}")

# Menu TP/SL
def show_tp_sl_menu(chat_id):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("📈 Ajuster Stop-Loss", callback_data="adjust_stop_loss"),
        InlineKeyboardButton("📈 Ajuster Take-Profit", callback_data="adjust_take_profit")
    )
    try:
        bot.send_message(chat_id, "📈 Configuration TP/SL:", reply_markup=markup)
    except Exception as e:
        logger.error(f"Erreur dans show_tp_sl_menu: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur TP/SL: {str(e)}")

# Menu Seuils
def show_threshold_menu(chat_id):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("📉 Min Volume BSC", callback_data="adjust_min_volume_bsc"),
        InlineKeyboardButton("📉 Max Volume BSC", callback_data="adjust_max_volume_bsc"),
        InlineKeyboardButton("📉 Min Liquidité", callback_data="adjust_min_liquidity"),
        InlineKeyboardButton("📉 Min Market Cap BSC", callback_data="adjust_min_market_cap_bsc"),
        InlineKeyboardButton("📉 Max Market Cap BSC", callback_data="adjust_max_market_cap_bsc"),
        InlineKeyboardButton("📉 Min Volume Solana", callback_data="adjust_min_volume_sol"),
        InlineKeyboardButton("📉 Max Volume Solana", callback_data="adjust_max_volume_sol"),
        InlineKeyboardButton("📉 Min Market Cap Solana", callback_data="adjust_min_market_cap_sol"),
        InlineKeyboardButton("📉 Max Market Cap Solana", callback_data="adjust_max_market_cap_sol")
    )
    try:
        bot.send_message(chat_id, (
            f"📉 Seuils de détection :\n"
            f"- BSC Volume : {MIN_VOLUME_BSC} $ - {MAX_VOLUME_BSC} $\n"
            f"- BSC Tx/min : {MIN_TX_PER_MIN_BSC} - {MAX_TX_PER_MIN_BSC}\n"
            f"- BSC Market Cap : {MIN_MARKET_CAP_BSC} $ - {MAX_MARKET_CAP_BSC} $\n"
            f"- Min Liquidité : {MIN_LIQUIDITY} $\n"
            f"- Solana Volume : {MIN_VOLUME_SOL} $ - {MAX_VOLUME_SOL} $\n"
            f"- Solana Tx/min : {MIN_TX_PER_MIN_SOL} - {MAX_TX_PER_MIN_SOL}\n"
            f"- Solana Market Cap : {MIN_MARKET_CAP_SOL} $ - {MAX_MARKET_CAP_SOL} $"
        ), reply_markup=markup)
    except Exception as e:
        logger.error(f"Erreur dans show_threshold_menu: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur seuils: {str(e)}")

# Ajustements avec notifications
def adjust_mise_bsc(message):
    global mise_depart_bsc
    chat_id = message.chat.id
    try:
        new_mise = float(message.text)
        if new_mise > 0:
            mise_depart_bsc = new_mise
            bot.send_message(chat_id, f"✅ Mise BSC mise à jour avec succès à {mise_depart_bsc} BNB")
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
            bot.send_message(chat_id, f"✅ Mise Solana mise à jour avec succès à {mise_depart_sol} SOL")
        else:
            bot.send_message(chat_id, "⚠️ La mise doit être positive!")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 0.37)")

def adjust_slippage(message):
    global slippage
    chat_id = message.chat.id
    try:
        new_slippage = float(message.text)
        if 0 <= new_slippage <= 100:
            slippage = new_slippage
            bot.send_message(chat_id, f"✅ Slippage mis à jour avec succès à {slippage}%")
        else:
            bot.send_message(chat_id, "⚠️ Le slippage doit être entre 0 et 100%!")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un pourcentage valide (ex. : 5)")

def adjust_gas_fee(message):
    global gas_fee
    chat_id = message.chat.id
    try:
        new_gas_fee = float(message.text)
        if new_gas_fee > 0:
            gas_fee = new_gas_fee
            bot.send_message(chat_id, f"✅ Frais de gas mis à jour avec succès à {gas_fee} Gwei")
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
            bot.send_message(chat_id, f"✅ Stop-Loss mis à jour avec succès à {stop_loss_threshold}%")
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
            bot.send_message(chat_id, f"✅ Take-Profit mis à jour avec succès à x{take_profit_steps[0]}, x{take_profit_steps[1]}, x{take_profit_steps[2]}")
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
            bot.send_message(chat_id, f"✅ Min Volume BSC mis à jour avec succès à ${MIN_VOLUME_BSC}")
        else:
            bot.send_message(chat_id, "⚠️ La valeur doit être positive!")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 75000)")

def adjust_max_volume_bsc(message):
    global MAX_VOLUME_BSC
    chat_id = message.chat.id
    try:
        new_value = float(message.text)
        if new_value >= MIN_VOLUME_BSC:
            MAX_VOLUME_BSC = new_value
            bot.send_message(chat_id, f"✅ Max Volume BSC mis à jour avec succès à ${MAX_VOLUME_BSC}")
        else:
            bot.send_message(chat_id, "⚠️ La valeur doit être supérieure au minimum!")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 750000)")

def adjust_min_liquidity(message):
    global MIN_LIQUIDITY
    chat_id = message.chat.id
    try:
        new_value = float(message.text)
        if new_value >= 0:
            MIN_LIQUIDITY = new_value
            bot.send_message(chat_id, f"✅ Min Liquidité mis à jour avec succès à ${MIN_LIQUIDITY}")
        else:
            bot.send_message(chat_id, "⚠️ La valeur doit être positive!")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 100000)")

def adjust_min_market_cap_bsc(message):
    global MIN_MARKET_CAP_BSC
    chat_id = message.chat.id
    try:
        new_value = float(message.text)
        if new_value >= 0:
            MIN_MARKET_CAP_BSC = new_value
            bot.send_message(chat_id, f"✅ Min Market Cap BSC mis à jour avec succès à ${MIN_MARKET_CAP_BSC}")
        else:
            bot.send_message(chat_id, "⚠️ La valeur doit être positive!")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 200000)")

def adjust_max_market_cap_bsc(message):
    global MAX_MARKET_CAP_BSC
    chat_id = message.chat.id
    try:
        new_value = float(message.text)
        if new_value >= MIN_MARKET_CAP_BSC:
            MAX_MARKET_CAP_BSC = new_value
            bot.send_message(chat_id, f"✅ Max Market Cap BSC mis à jour avec succès à ${MAX_MARKET_CAP_BSC}")
        else:
            bot.send_message(chat_id, "⚠️ La valeur doit être supérieure au minimum!")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 2000000)")

def adjust_min_volume_sol(message):
    global MIN_VOLUME_SOL
    chat_id = message.chat.id
    try:
        new_value = float(message.text)
        if new_value >= 0:
            MIN_VOLUME_SOL = new_value
            bot.send_message(chat_id, f"✅ Min Volume Solana mis à jour avec succès à ${MIN_VOLUME_SOL}")
        else:
            bot.send_message(chat_id, "⚠️ La valeur doit être positive!")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 50000)")

def adjust_max_volume_sol(message):
    global MAX_VOLUME_SOL
    chat_id = message.chat.id
    try:
        new_value = float(message.text)
        if new_value >= MIN_VOLUME_SOL:
            MAX_VOLUME_SOL = new_value
            bot.send_message(chat_id, f"✅ Max Volume Solana mis à jour avec succès à ${MAX_VOLUME_SOL}")
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
            bot.send_message(chat_id, f"✅ Min Market Cap Solana mis à jour avec succès à ${MIN_MARKET_CAP_SOL}")
        else:
            bot.send_message(chat_id, "⚠️ La valeur doit être positive!")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 100000)")

def adjust_max_market_cap_sol(message):
    global MAX_MARKET_CAP_SOL
    chat_id = message.chat.id
    try:
        new_value = float(message.text)
        if new_value >= MIN_MARKET_CAP_SOL:
            MAX_MARKET_CAP_SOL = new_value
            bot.send_message(chat_id, f"✅ Max Market Cap Solana mis à jour avec succès à ${MAX_MARKET_CAP_SOL}")
        else:
            bot.send_message(chat_id, "⚠️ La valeur doit être supérieure au minimum!")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 1000000)")

# Achat BSC
def buy_token_bsc(chat_id, contract_address, amount):
    try:
        router = w3.eth.contract(address=PANCAKE_ROUTER_ADDRESS, abi=PANCAKE_ROUTER_ABI)
        amount_in = w3.to_wei(amount, 'ether')
        amount_out_min = int(amount_in * (1 - slippage / 100))
        tx = router.functions.swapExactETHForTokens(
            amount_out_min,
            [w3.to_checksum_address("0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"), w3.to_checksum_address(contract_address)],
            w3.to_checksum_address(WALLET_ADDRESS),
            int(time.time()) + 60
        ).build_transaction({
            'from': WALLET_ADDRESS,
            'value': amount_in,
            'gas': 200000,
            'gasPrice': w3.to_wei(gas_fee * 2, 'gwei'),
            'nonce': w3.eth.get_transaction_count(WALLET_ADDRESS)
        })
        signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
        bot.send_message(chat_id, f"⏳ Achat en cours de {amount} BNB de {contract_address}, TX: {tx_hash.hex()}")
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
        if receipt.status == 1:
            entry_price = amount / (detected_tokens[contract_address]['supply'] * get_current_market_cap(contract_address) / detected_tokens[contract_address]['supply'])
            portfolio[contract_address] = {
                'amount': amount,
                'chain': 'bsc',
                'entry_price': entry_price,
                'market_cap_at_buy': detected_tokens[contract_address]['market_cap'],
                'current_market_cap': detected_tokens[contract_address]['market_cap']
            }
            bot.send_message(chat_id, f"✅ Achat effectué avec succès : {amount} BNB de {contract_address}")
            monitor_and_sell(chat_id, contract_address, amount, 'bsc')
        else:
            bot.send_message(chat_id, f"⚠️ Échec achat {contract_address}, TX: {tx_hash.hex()}")
    except Exception as e:
        logger.error(f"Erreur achat BSC: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Échec achat {contract_address}: {str(e)}")

# Achat Solana
def buy_token_solana(chat_id, contract_address, amount):
    try:
        amount_in = int(amount * 10**9)
        response = session.post(SOLANA_RPC, json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getLatestBlockhash",
            "params": [{"commitment": "finalized"}]
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
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [base58.b58encode(tx.serialize()).decode('utf-8')]
        }, timeout=5).json()['result']
        bot.send_message(chat_id, f"⏳ Achat en cours de {amount} SOL de {contract_address}, TX: {tx_hash}")
        time.sleep(2)
        entry_price = amount / (detected_tokens[contract_address]['supply'] * get_current_market_cap(contract_address) / detected_tokens[contract_address]['supply'])
        portfolio[contract_address] = {
            'amount': amount,
            'chain': 'solana',
            'entry_price': entry_price,
            'market_cap_at_buy': detected_tokens[contract_address]['market_cap'],
            'current_market_cap': detected_tokens[contract_address]['market_cap']
        }
        bot.send_message(chat_id, f"✅ Achat effectué avec succès : {amount} SOL de {contract_address}")
        monitor_and_sell(chat_id, contract_address, amount, 'solana')
    except Exception as e:
        logger.error(f"Erreur achat Solana: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Échec achat {contract_address}: {str(e)}")

# Vente avec notifications
def sell_token(chat_id, contract_address, amount, chain, current_price):
    if chain == "solana":
        try:
            amount_out = int(amount * 10**9)
            response = session.post(SOLANA_RPC, json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getLatestBlockhash",
                "params": [{"commitment": "finalized"}]
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
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sendTransaction",
                "params": [base58.b58encode(tx.serialize()).decode('utf-8')]
            }, timeout=5).json()['result']
            bot.send_message(chat_id, f"⏳ Vente en cours de {amount} SOL de {contract_address}, TX: {tx_hash}")
            time.sleep(2)
            del portfolio[contract_address]
            bot.send_message(chat_id, f"✅ Vente effectuée avec succès : {amount} SOL de {contract_address}")
        except Exception as e:
            logger.error(f"Erreur vente Solana: {str(e)}")
            bot.send_message(chat_id, f"⚠️ Échec vente {contract_address}: {str(e)}")
    else:  # BSC
        try:
            router = w3.eth.contract(address=PANCAKE_ROUTER_ADDRESS, abi=PANCAKE_ROUTER_ABI)
            token_amount = w3.to_wei(amount, 'ether')
            amount_in_max = int(token_amount * (1 + slippage / 100))
            tx = router.functions.swapExactTokensForETH(
                token_amount,
                amount_in_max,
                [w3.to_checksum_address(contract_address), w3.to_checksum_address("0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c")],
                w3.to_checksum_address(WALLET_ADDRESS),
                int(time.time()) + 60
            ).build_transaction({
                'from': WALLET_ADDRESS,
                'gas': 200000,
                'gasPrice': w3.to_wei(gas_fee * 2, 'gwei'),
                'nonce': w3.eth.get_transaction_count(WALLET_ADDRESS)
            })
            signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
            tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
            bot.send_message(chat_id, f"⏳ Vente en cours de {amount} BNB de {contract_address}, TX: {tx_hash.hex()}")
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
            if receipt.status == 1:
                del portfolio[contract_address]
                bot.send_message(chat_id, f"✅ Vente effectuée avec succès : {amount} BNB de {contract_address}")
            else:
                bot.send_message(chat_id, f"⚠️ Échec vente {contract_address}, TX: {tx_hash.hex()}")
        except Exception as e:
            logger.error(f"Erreur vente BSC: {str(e)}")
            bot.send_message(chat_id, f"⚠️ Échec vente {contract_address}: {str(e)}")

# Surveillance et vente automatique TP/SL
def monitor_and_sell(chat_id, contract_address, amount, chain):
    try:
        while contract_address in portfolio:
            current_mc = get_current_market_cap(contract_address)
            portfolio[contract_address]['current_market_cap'] = current_mc
            profit_pct = (current_mc - portfolio[contract_address]['market_cap_at_buy']) / portfolio[contract_address]['market_cap_at_buy'] * 100
            loss_pct = -profit_pct if profit_pct < 0 else 0
            if profit_pct >= take_profit_steps[0] * 100:
                sell_amount = amount / 3
                current_price = current_mc / detected_tokens[contract_address]['supply']
                sell_token(chat_id, contract_address, sell_amount, chain, current_price)
                amount -= sell_amount
            elif profit_pct >= take_profit_steps[1] * 100:
                sell_amount = amount / 2
                current_price = current_mc / detected_tokens[contract_address]['supply']
                sell_token(chat_id, contract_address, sell_amount, chain, current_price)
                amount -= sell_amount
            elif profit_pct >= take_profit_steps[2] * 100:
                current_price = current_mc / detected_tokens[contract_address]['supply']
                sell_token(chat_id, contract_address, amount, chain, current_price)
                break
            elif loss_pct >= stop_loss_threshold:
                current_price = current_mc / detected_tokens[contract_address]['supply']
                sell_token(chat_id, contract_address, amount, chain, current_price)
                break
            time.sleep(5)
    except Exception as e:
        logger.error(f"Erreur surveillance {contract_address}: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur surveillance {contract_address}: {str(e)}")

# Affichage du portefeuille
def show_portfolio(chat_id):
    try:
        bnb_balance = w3.eth.get_balance(WALLET_ADDRESS) / 10**18
        sol_balance = get_solana_balance(WALLET_ADDRESS)
        msg = f"💼 Portefeuille:\nBNB : {bnb_balance:.4f}\nSOL : {sol_balance:.4f}\n\n"
        markup = InlineKeyboardMarkup()
        for ca, data in portfolio.items():
            current_mc = get_current_market_cap(ca)
            profit = (current_mc - data['market_cap_at_buy']) / data['market_cap_at_buy'] * 100
            markup.add(
                InlineKeyboardButton(f"🔄 Refresh {ca[:6]}...", callback_data=f"refresh_{ca}"),
                InlineKeyboardButton(f"💰 Sell {ca[:6]}...", callback_data=f"sell_{ca}")
            )
            msg += (
                f"Token: {ca} ({data['chain']})\n"
                f"Contrat: {ca}\n"
                f"MC Achat: ${data['market_cap_at_buy']:.2f}\n"
                f"MC Actuel: ${current_mc:.2f}\n"
                f"Profit: {profit:.2f}%\n"
                f"Take-Profit: x{take_profit_steps[0]}, x{take_profit_steps[1]}, x{take_profit_steps[2]}\n"
                f"Stop-Loss: -{stop_loss_threshold}%\n\n"
            )
        bot.send_message(chat_id, msg, reply_markup=markup if portfolio else None)
    except Exception as e:
        logger.error(f"Erreur portefeuille: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur portefeuille: {str(e)}")

# Solde Solana réel
def get_solana_balance(wallet_address):
    try:
        response = session.post(SOLANA_RPC, json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getBalance",
            "params": [str(solana_keypair.pubkey())]
        }, timeout=10)
        result = response.json().get('result', {})
        return result.get('value', 0) / 10**9
    except Exception as e:
        logger.error(f"Erreur solde Solana: {str(e)}")
        return 0

# Market cap en temps réel
def get_current_market_cap(contract_address):
    try:
        if contract_address in portfolio and portfolio[contract_address]['chain'] == 'solana':
            response = session.get(
                f"https://public-api.birdeye.so/public/price?address={contract_address}",
                headers={"X-API-KEY": BIRDEYE_API_KEY},
                timeout=10
            )
            price = response.json()['data']['value']
            supply = detected_tokens[contract_address]['supply']
            return price * supply
        else:
            token_contract = w3.eth.contract(address=w3.to_checksum_address(contract_address), abi=[{
                "name": "totalSupply",
                "outputs": [{"name": "", "type": "uint256"}],
                "payable": False,
                "stateMutability": "view",
                "type": "function"
            }])
            supply = token_contract.functions.totalSupply().call() / 10**18
            volume_response = session.get(
                f"https://api.bscscan.com/api?module=stats&action=tokenbalance&contractaddress={contract_address}&address={PANCAKE_ROUTER_ADDRESS}&tag=latest&apikey={BSC_SCAN_API_KEY}",
                timeout=10
            )
            volume = float(volume_response.json()['result']) / 10**18
            return volume * supply
    except Exception as e:
        logger.error(f"Erreur market cap: {str(e)}")
        return detected_tokens[contract_address]['market_cap']

# Rafraîchir un token
def refresh_token(chat_id, token):
    try:
        current_mc = get_current_market_cap(token)
        profit = (current_mc - portfolio[token]['market_cap_at_buy']) / portfolio[token]['market_cap_at_buy'] * 100
        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh_{token}"),
            InlineKeyboardButton("💰 Sell All", callback_data=f"sell_{token}")
        )
        msg = (
            f"Token: {token} ({portfolio[token]['chain']})\n"
            f"Contrat: {token}\n"
            f"MC Achat: ${portfolio[token]['market_cap_at_buy']:.2f}\n"
            f"MC Actuel: ${current_mc:.2f}\n"
            f"Profit: {profit:.2f}%\n"
            f"Take-Profit: x{take_profit_steps[0]}, x{take_profit_steps[1]}, x{take_profit_steps[2]}\n"
            f"Stop-Loss: -{stop_loss_threshold}%"
        )
        bot.send_message(chat_id, f"🔄 Portefeuille rafraîchi pour {token}", reply_markup=markup)
    except Exception as e:
        logger.error(f"Erreur refresh: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur rafraîchissement {token}: {str(e)}")

# Vente immédiate
def sell_token_immediate(chat_id, token):
    try:
        if token not in portfolio:
            bot.send_message(chat_id, f"⚠️ Vente impossible : {token} n'est pas dans le portefeuille")
            return
        amount = portfolio[token]["amount"]
        chain = portfolio[token]["chain"]
        current_price = get_current_market_cap(token) / detected_tokens[token]['supply']
        sell_token(chat_id, token, amount, chain, current_price)
    except Exception as e:
        logger.error(f"Erreur vente immédiate: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur vente immédiate {token}: {str(e)}")

# Configuration du webhook dans un thread séparé
def set_webhook():
    logger.info("Configuration du webhook...")
    try:
        if WEBHOOK_URL:
            bot.remove_webhook()
            time.sleep(1)  # Délai pour éviter les conflits
            bot.set_webhook(url=WEBHOOK_URL)
            logger.info(f"Webhook configuré avec succès sur {WEBHOOK_URL}")
        else:
            logger.warning("WEBHOOK_URL non défini, exécution en mode polling")
            bot.polling()
    except Exception as e:
        logger.error(f"Erreur configuration webhook: {str(e)}")
        raise

# Point d'entrée principal
if __name__ == "__main__":
    logger.info("Démarrage du bot...")
    try:
        # Lancer la configuration du webhook dans un thread séparé
        webhook_thread = threading.Thread(target=set_webhook)
        webhook_thread.start()

        # Démarrer Flask pour Cloud Run
        logger.info(f"Démarrage de Flask sur 0.0.0.0:{PORT}...")
        app.run(host="0.0.0.0", port=PORT, debug=False)
    except Exception as e:
        logger.error(f"Erreur critique au démarrage: {str(e)}")
        raise
