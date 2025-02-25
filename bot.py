import os
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import logging
import requests
import time
import json
import base58
import aiohttp
from flask import Flask, request, abort
from cachetools import TTLCache
from web3 import Web3
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from solders.instruction import Instruction
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import asyncio
import threading

# Configuration des logs
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Variables globales
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/91.0.4472.124", "Accept": "application/json"}
session = requests.Session()
session.headers.update(HEADERS)
retry_strategy = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("https://", adapter)

last_twitter_call = 0
loose_mode_bsc = False
last_valid_token_time = time.time()

# Variables d’environnement
logger.info("Chargement des variables d’environnement...")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY")
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")
PORT = int(os.getenv("PORT", 8080))
BSC_RPC = os.getenv("BSC_RPC", "https://bsc-dataseed.binance.org/")
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")

BIRDEYE_HEADERS = {"X-API-KEY": BIRDEYE_API_KEY}
TWITTER_HEADERS = {"Authorization": f"Bearer {os.getenv('TWITTER_BEARER_TOKEN')}"}

missing_vars = [var for var, val in {
    "TELEGRAM_TOKEN": TELEGRAM_TOKEN, "WALLET_ADDRESS": WALLET_ADDRESS, "PRIVATE_KEY": PRIVATE_KEY,
    "SOLANA_PRIVATE_KEY": SOLANA_PRIVATE_KEY, "WEBHOOK_URL": WEBHOOK_URL, "BIRDEYE_API_KEY": BIRDEYE_API_KEY
}.items() if not val]
if missing_vars:
    logger.critical(f"Variables manquantes: {missing_vars}")
    raise ValueError(f"Variables manquantes: {missing_vars}")
logger.info("Variables chargées.")

# Initialisation de Flask et Telegram
bot = telebot.TeleBot(TELEGRAM_TOKEN)
app = Flask(__name__)

# Variables globales pour le trading
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

MIN_VOLUME_SOL = 25000
MAX_VOLUME_SOL = 750000
MIN_VOLUME_BSC = 30000
MAX_VOLUME_BSC = 1000000
MIN_LIQUIDITY = 50000
MIN_MARKET_CAP_SOL = 50000
MAX_MARKET_CAP_SOL = 1500000
MIN_MARKET_CAP_BSC = 75000
MAX_MARKET_CAP_BSC = 3000000
MAX_TAX = 10
MIN_TX_PER_MIN_BSC = 5
MAX_TX_PER_MIN_BSC = 75
MIN_TX_PER_MIN_SOL = 15
MAX_TX_PER_MIN_SOL = 150

# Nouvelles variables pour les améliorations
momentum_threshold = 100
simulation_mode = False
BSC_WS_URI = "wss://bsc-ws-node.nariox.org:443"

ERC20_ABI = json.loads('[{"constant": true, "inputs": [], "name": "totalSupply", "outputs": [{"name": "", "type": "uint256"}], "payable": false, "stateMutability": "view", "type": "function"}]')
PANCAKE_ROUTER_ADDRESS = "0x10ED43C718714eb63d5aA57B78B54704E256024E"
PANCAKE_FACTORY_ADDRESS = "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"
PANCAKE_FACTORY_ABI = [json.loads('{"anonymous": false, "inputs": [{"indexed": true, "internalType": "address", "name": "token0", "type": "address"}, {"indexed": true, "internalType": "address", "name": "token1", "type": "address"}, {"indexed": false, "internalType": "address", "name": "pair", "type": "address"}, {"indexed": false, "internalType": "uint256", "name": "type", "type": "uint256"}], "name": "PairCreated", "type": "event"}')]
PANCAKE_ROUTER_ABI = json.loads('[{"inputs": [{"internalType": "uint256", "name": "amountIn", "type": "uint256"}, {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"}, {"internalType": "address[]", "name": "path", "type": "address[]"}, {"internalType": "address", "name": "to", "type": "address"}, {"internalType": "uint256", "name": "deadline", "type": "uint256"}], "name": "swapExactETHForTokens", "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}], "stateMutability": "payable", "type": "function"}, {"inputs": [{"internalType": "uint256", "name": "amountOut", "type": "uint256"}, {"internalType": "uint256", "name": "amountInMax", "type": "uint256"}, {"internalType": "address[]", "name": "path", "type": "address[]"}, {"internalType": "address", "name": "to", "type": "address"}, {"internalType": "uint256", "name": "deadline", "type": "uint256"}], "name": "swapExactTokensForETH", "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}], "stateMutability": "nonpayable", "type": "function"}]')

# Initialisations différées
w3 = None
solana_keypair = None
RAYDIUM_PROGRAM_ID = Pubkey.from_string("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8")
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")

def initialize_bot():
    global w3, solana_keypair
    logger.info("Initialisation différée du bot...")
    try:
        w3 = Web3(Web3.HTTPProvider(BSC_RPC))
        if not w3.is_connected():
            raise ConnectionError("Connexion BSC échouée")
        logger.info("Connexion BSC réussie.")
        solana_keypair = Keypair.from_bytes(base58.b58decode(SOLANA_PRIVATE_KEY))
        logger.info("Clé Solana initialisée.")
    except Exception as e:
        logger.error(f"Erreur dans l'initialisation différée: {str(e)}")
        raise

def is_safe_token_bsc(token_address):
    try:
        response = session.get(f"https://api.honeypot.is/v2/IsHoneypot?address={token_address}", timeout=10)
        data = response.json()
        return not data.get("isHoneypot", True) and data.get("buyTax", 0) <= MAX_TAX and data.get("sellTax", 0) <= MAX_TAX
    except Exception as e:
        logger.error(f"Erreur vérification Honeypot: {str(e)}")
        return False

def is_safe_token_solana(token_address):
    try:
        response = session.get(f"https://public-api.birdeye.so/public/token_overview?address={token_address}", headers=BIRDEYE_HEADERS, timeout=10)
        data = response.json()['data']
        return data.get('liquidity', 0) >= MIN_LIQUIDITY
    except Exception as e:
        logger.error(f"Erreur vérification Solana: {str(e)}")
        return False

def is_valid_token_bsc(token_address):
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

def get_real_tx_per_min_bsc(token_address):
    try:
        latest_block = w3.eth.block_number
        tx_count = 0
        for block_num in range(max(latest_block - 10, 0), latest_block + 1):
            block = w3.eth.get_block(block_num, full_transactions=True)
            tx_count += sum(1 for tx in block['transactions'] if tx['to'] == token_address or tx['from'] == token_address)
        return tx_count * 2
    except Exception as e:
        logger.error(f"Erreur calcul tx/min BSC pour {token_address}: {str(e)}")
        return 0

async def monitor_twitter(chat_id):
    bot.send_message(chat_id, "⚠️ Surveillance Twitter désactivée (quota API gratuit dépassé).")
    logger.info("Surveillance Twitter désactivée.")

def check_twitter_token(chat_id, token_address):
    try:
        if token_address.startswith("0x"):
            if not is_valid_token_bsc(token_address):
                return False
            token_contract = w3.eth.contract(address=w3.to_checksum_address(token_address), abi=ERC20_ABI)
            supply = token_contract.functions.totalSupply().call() / 10**18
            response = session.get(f"https://api.dexscreener.com/latest/dex/tokens/{token_address}", timeout=10)
            data = response.json()['pairs'][0] if response.json()['pairs'] else {}
            volume_24h = float(data.get('volume', {}).get('h24', 0))
            liquidity = float(data.get('liquidity', {}).get('usd', 0))
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
                f'🔍 Token X détecté : {token_address} (BSC) - Tx/min: {tx_per_min}, Vol: ${volume_24h:.2f}, Liq: ${liquidity:.2f}, MC: ${market_cap:.2f}'
            )
            detected_tokens[token_address] = {
                'address': token_address, 'volume': volume_24h, 'tx_per_min': tx_per_min,
                'liquidity': liquidity, 'market_cap': market_cap, 'supply': supply
            }
            buy_token_bsc(chat_id, token_address, mise_depart_bsc)
            return True
        else:
            response = session.get(f"https://public-api.birdeye.so/public/token_overview?address={token_address}", headers=BIRDEYE_HEADERS, timeout=10)
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
                f'🔍 Token X détecté : {token_address} (Solana) - Tx/min: {tx_per_min}, Vol: ${volume_24h:.2f}, Liq: ${liquidity:.2f}, MC: ${market_cap:.2f}'
            )
            detected_tokens[token_address] = {
                'address': token_address, 'volume': volume_24h, 'tx_per_min': tx_per_min,
                'liquidity': liquidity, 'market_cap': market_cap, 'supply': supply
            }
            buy_token_solana(chat_id, token_address, mise_depart_sol)
            return True
    except Exception as e:
        logger.error(f"Erreur vérification token X {token_address} : {str(e)}")
        return False

async def get_real_tx_per_min_solana(token_address):
    try:
        response = session.get(f"https://public-api.birdeye.so/public/history_trades?address={token_address}&offset=0&limit=50", headers=BIRDEYE_HEADERS, timeout=10)
        trades = response.json()['data']['items']
        if not trades:
            logger.warning(f"Aucune transaction récente via Birdeye pour {token_address}")
            return 0
        return min(len(trades) * 2, MAX_TX_PER_MIN_SOL)
    except Exception as e:
        logger.error(f"Erreur calcul tx/min Solana pour {token_address}: {str(e)}")
        return 0

async def detect_new_tokens_bsc(chat_id):
    global loose_mode_bsc, last_valid_token_time
    bot.send_message(chat_id, "🔍 Début détection BSC...")
    try:
        factory = w3.eth.contract(address=PANCAKE_FACTORY_ADDRESS, abi=PANCAKE_FACTORY_ABI)
        latest_block = w3.eth.block_number
        logger.info(f"Bloc actuel : {latest_block}")
        events = factory.events.PairCreated.get_logs(fromBlock=latest_block - 50, toBlock=latest_block)
        logger.info(f"Événements trouvés : {len(events)}")
        bot.send_message(chat_id, f"⬇️ {len(events)} nouvelles paires détectées sur BSC")
        if not events:
            logger.info("Aucun événement PairCreated trouvé dans les 50 derniers blocs.")
            bot.send_message(chat_id, "ℹ️ Aucun événement PairCreated détecté dans les 50 derniers blocs.")
            return
        
        rejected_count = 0
        valid_token_found = False
        rejection_reasons = []

        if time.time() - last_valid_token_time > 3600 and not loose_mode_bsc:
            loose_mode_bsc = True
            bot.send_message(chat_id, "⚠️ Aucun token valide depuis 1h, mode souple activé.")

        tasks = []
        for event in events:
            token_address = event['args']['token0'] if event['args']['token0'] != "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c" else event['args']['token1']
            tasks.append(check_bsc_token(chat_id, token_address, loose_mode_bsc))
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for i, result in enumerate(results):
            token_addr = events[i]['args']['token0'] if events[i]['args']['token0'] != "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c" else events[i]['args']['token1']
            if isinstance(result, Exception):
                rejection_reasons.append(f"{token_addr}: erreur - {str(result)}")
                rejected_count += 1
            elif result is False:
                rejection_reasons.append(f"{token_addr}: critères non remplis")
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

async def check_bsc_token(chat_id, token_address, loose_mode):
    try:
        if not is_valid_token_bsc(token_address):
            logger.info(f"{token_address}: pas de code valide ou totalSupply inaccessible")
            return False
        token_contract = w3.eth.contract(address=w3.to_checksum_address(token_address), abi=ERC20_ABI)
        supply = token_contract.functions.totalSupply().call() / 10**18
        response = session.get(f"https://api.dexscreener.com/latest/dex/tokens/{token_address}", timeout=10)
        data = response.json()['pairs'][0] if response.json()['pairs'] else {}
        volume_24h = float(data.get('volume', {}).get('h24', 0))
        liquidity = float(data.get('liquidity', {}).get('usd', 0))
        market_cap = volume_24h * supply
        tx_per_min = get_real_tx_per_min_bsc(token_address)

        min_volume = MIN_VOLUME_BSC * 0.5 if loose_mode else MIN_VOLUME_BSC
        max_volume = MAX_VOLUME_BSC if loose_mode else MAX_VOLUME_BSC
        min_liquidity = MIN_LIQUIDITY * 0.5 if loose_mode else MIN_LIQUIDITY
        min_market_cap = MIN_MARKET_CAP_BSC * 0.5 if loose_mode else MIN_MARKET_CAP_BSC
        max_market_cap = MAX_MARKET_CAP_BSC if loose_mode else MAX_MARKET_CAP_BSC
        min_tx = MIN_TX_PER_MIN_BSC
        max_tx = MAX_TX_PER_MIN_BSC

        if not (min_tx <= tx_per_min <= max_tx):
            logger.info(f"{token_address}: tx/min {tx_per_min} hors plage [{min_tx}, {max_tx}]")
            return False
        if not (min_volume <= volume_24h <= max_volume):
            logger.info(f"{token_address}: volume {volume_24h} hors plage [{min_volume}, {max_volume}]")
            return False
        if liquidity < min_liquidity:
            logger.info(f"{token_address}: liquidité {liquidity} < {min_liquidity}")
            return False
        if not (min_market_cap <= market_cap <= max_market_cap):
            logger.info(f"{token_address}: market cap {market_cap} hors plage [{min_market_cap}, {max_market_cap}]")
            return False
        if not is_safe_token_bsc(token_address):
            logger.info(f"{token_address}: non sécurisé (Honeypot)")
            return False
        
        bot.send_message(chat_id,
            f'🔍 Token détecté : {token_address} (BSC) - Tx/min: {tx_per_min}, Vol 24h: ${volume_24h:.2f}, Liq: ${liquidity:.2f}, MC: ${market_cap:.2f}'
        )
        detected_tokens[token_address] = {
            'address': token_address, 'volume': volume_24h, 'tx_per_min': tx_per_min,
            'liquidity': liquidity, 'market_cap': market_cap, 'supply': supply
        }
        buy_token_bsc(chat_id, token_address, mise_depart_bsc)
        return True
    except Exception as e:
        logger.error(f"Erreur vérification BSC {token_address}: {str(e)}")
        return e

async def detect_new_tokens_solana(chat_id):
    bot.send_message(chat_id, "🔍 Début détection Solana via Birdeye...")
    try:
        response = session.get("https://public-api.birdeye.so/defi/tokenlist?sort_by=v24hUSD&sort_type=desc&offset=0&limit=10", headers=BIRDEYE_HEADERS, timeout=10)
        response.raise_for_status()
        json_response = response.json()
        logger.info(f"Réponse Birdeye : {json_response}")
        if 'data' not in json_response or 'tokens' not in json_response['data']:
            raise ValueError(f"Réponse Birdeye invalide : {json_response}")
        tokens = json_response['data']['tokens']
        for token in tokens:
            token_address = token['address']
            if token_address not in detected_tokens:
                response = session.get(f"https://public-api.birdeye.so/public/token_overview?address={token_address}", headers=BIRDEYE_HEADERS, timeout=10)
                data = response.json()['data']
                volume_24h = float(data.get('v24hUSD', 0))
                if volume_24h > MIN_VOLUME_SOL:
                    bot.send_message(chat_id, f'🆕 Token Solana détecté via Birdeye : {token_address} (Vol: ${volume_24h:.2f})')
                    await check_solana_token(chat_id, token_address)
                    break
                else:
                    logger.info(f"Token {token_address} rejeté, volume insuffisant: ${volume_24h:.2f}")
    except Exception as e:
        logger.error(f"Erreur détection Solana via Birdeye: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Erreur détection Solana: {str(e)}')

async def check_solana_token(chat_id, token_address):
    try:
        response = session.get(f"https://public-api.birdeye.so/public/token_overview?address={token_address}", headers=BIRDEYE_HEADERS, timeout=10)
        data = response.json()['data']
        volume_24h = float(data.get('v24hUSD', 0))
        liquidity = float(data.get('liquidity', 0))
        market_cap = float(data.get('mc', 0))
        supply = float(data.get('supply', 0))
        tx_per_min = await get_real_tx_per_min_solana(token_address)
        if not (MIN_TX_PER_MIN_SOL <= tx_per_min <= MAX_TX_PER_MIN_SOL):
            logger.info(f"{token_address}: tx/min {tx_per_min} hors plage [{MIN_TX_PER_MIN_SOL}, {MAX_TX_PER_MIN_SOL}]")
            return
        if not (MIN_VOLUME_SOL <= volume_24h <= MAX_VOLUME_SOL):
            logger.info(f"{token_address}: volume {volume_24h} hors plage [{MIN_VOLUME_SOL}, {MAX_VOLUME_SOL}]")
            return
        if liquidity < MIN_LIQUIDITY:
            logger.info(f"{token_address}: liquidité {liquidity} < {MIN_LIQUIDITY}")
            return
        if not (MIN_MARKET_CAP_SOL <= market_cap <= MAX_MARKET_CAP_SOL):
            logger.info(f"{token_address}: market cap {market_cap} hors plage [{MIN_MARKET_CAP_SOL}, {MAX_MARKET_CAP_SOL}]")
            return
        if not is_safe_token_solana(token_address):
            logger.info(f"{token_address}: non sécurisé")
            return
        bot.send_message(chat_id,
            f'🔍 Token détecté : {token_address} (Solana) - Tx/min: {tx_per_min}, Vol: ${volume_24h:.2f}, Liq: ${liquidity:.2f}, MC: ${market_cap:.2f}'
        )
        detected_tokens[token_address] = {
            'address': token_address, 'volume': volume_24h, 'tx_per_min': tx_per_min,
            'liquidity': liquidity, 'market_cap': market_cap, 'supply': supply
        }
        buy_token_solana(chat_id, token_address, mise_depart_sol)
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

def show_main_menu(chat_id):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("ℹ️ Statut", callback_data="status"),
        InlineKeyboardButton("⚙️ Configure", callback_data="config"),
        InlineKeyboardButton("▶️ Lancer", callback_data="launch"),
        InlineKeyboardButton("⏹️ Arrêter", callback_data="stop"),
        InlineKeyboardButton("💰 Portefeuille", callback_data="portfolio"),
        InlineKeyboardButton("🔧 Réglages", callback_data="settings"),
        InlineKeyboardButton("📈 TP/SL", callback_data="tp_sl_settings"),
        InlineKeyboardButton("📊 Seuils", callback_data="threshold_settings"),
        InlineKeyboardButton(f"🧪 Simulation: {'ON' if simulation_mode else 'OFF'}", callback_data="toggle_simulation")
    )
    try:
        bot.send_message(chat_id, "Voici le menu principal:", reply_markup=markup)
    except Exception as e:
        logger.error(f"Erreur dans show_main_menu: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Erreur affichage menu: {str(e)}')

@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    global mise_depart_bsc, mise_depart_sol, trade_active, slippage, gas_fee, stop_loss_threshold, take_profit_steps
    global MIN_VOLUME_BSC, MAX_VOLUME_BSC, MIN_LIQUIDITY, MIN_MARKET_CAP_BSC, MAX_MARKET_CAP_BSC
    global MIN_VOLUME_SOL, MAX_VOLUME_SOL, MIN_MARKET_CAP_SOL, MAX_MARKET_CAP_SOL
    global simulation_mode, momentum_threshold
    chat_id = call.message.chat.id
    logger.info(f"Callback reçu: {call.data}")
    try:
        if call.data == "status":
            bot.send_message(chat_id, (
                f"ℹ️ Statut actuel :\nTrading actif: {'Oui' if trade_active else 'Non'}\n"
                f"Mise BSC: {mise_depart_bsc} BNB\nMise Solana: {mise_depart_sol} SOL\n"
                f"Slippage: {slippage} %\nGas Fee: {gas_fee} Gwei"
            ))
        elif call.data == "config":
            show_config_menu(chat_id)
        elif call.data == "launch":
            if not trade_active:
                trade_active = True
                bot.send_message(chat_id, "▶️ Trading lancé avec succès!")
                logger.info("Lancement du trading cycle...")
                threading.Thread(target=lambda: asyncio.run(trading_cycle(chat_id)), daemon=True).start()
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
            bot.send_message(chat_id, "Entrez le nouveau seuil min de volume BSC (en $, ex. : 30000) :")
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
        elif call.data == "toggle_simulation":
            simulation_mode = not simulation_mode
            bot.send_message(chat_id, f"🧪 Mode simulation : {'activé' if simulation_mode else 'désactivé'}")
            show_main_menu(chat_id)
        elif call.data == "adjust_momentum":
            bot.send_message(chat_id, "Entrez le nouveau seuil de momentum (tx/min, ex. : 100) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_momentum)
    except Exception as e:
        logger.error(f"Erreur dans callback_query: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Erreur générale: {str(e)}')

async def trading_cycle(chat_id):
    global trade_active
    cycle_count = 0
    solana_task = asyncio.create_task(detect_new_tokens_solana(chat_id))
    twitter_task = asyncio.create_task(monitor_twitter_free(chat_id))
    trending_task = asyncio.create_task(monitor_trending_free(chat_id))
    bsc_ws_task = asyncio.create_task(monitor_bsc_websocket(chat_id))
    while trade_active:
        cycle_count += 1
        bot.send_message(chat_id, f'🔍 Début du cycle de détection #{cycle_count}...')
        logger.info(f"Cycle {cycle_count} démarré")
        try:
            await asyncio.gather(detect_new_tokens_bsc(chat_id), detect_new_tokens_solana(chat_id))
            await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"Erreur dans trading_cycle: {str(e)}")
            bot.send_message(chat_id, f'⚠️ Erreur dans le cycle: {str(e)}')
            trade_active = False
            bot.send_message(chat_id, "⏹️ Trading arrêté suite à une erreur. Relancez avec /start ou 'Lancer'.")
    if not trade_active:
        logger.info("Trading_cycle arrêté.")
        bot.send_message(chat_id, "ℹ️ Cycle de trading terminé.")
    try:
        solana_task.cancel()
        twitter_task.cancel()
        trending_task.cancel()
        bsc_ws_task.cancel()
        await asyncio.gather(solana_task, twitter_task, trending_task, bsc_ws_task, return_exceptions=True)
    except Exception as e:
        logger.error(f"Erreur lors de l'annulation des tâches : {str(e)}")

def show_config_menu(chat_id):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("➕ Augmenter mise BSC (+0.01 BNB)", callback_data="increase_mise_bsc"),
        InlineKeyboardButton("➕ Augmenter mise SOL (+0.01 SOL)", callback_data="increase_mise_sol")
    )
    try:
        bot.send_message(chat_id, "⚙️ Configuration:", reply_markup=markup)
    except Exception as e:
        logger.error(f"Erreur dans show_config_menu: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Erreur configuration: {str(e)}')

def show_settings_menu(chat_id):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("🔧 Ajuster Mise BSC", callback_data="adjust_mise_bsc"),
        InlineKeyboardButton("🔧 Ajuster Mise Solana", callback_data="adjust_mise_sol"),
        InlineKeyboardButton("🔧 Ajuster Slippage", callback_data="adjust_slippage"),
        InlineKeyboardButton("🔧 Ajuster Gas Fee (BSC)", callback_data="adjust_gas"),
        InlineKeyboardButton("🔧 Ajuster Momentum", callback_data="adjust_momentum")
    )
    try:
        bot.send_message(chat_id, "🔧 Réglages:", reply_markup=markup)
    except Exception as e:
        logger.error(f"Erreur dans show_settings_menu: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Erreur réglages: {str(e)}')

def show_tp_sl_menu(chat_id):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("📉 Ajuster Stop-Loss", callback_data="adjust_stop_loss"),
        InlineKeyboardButton("📈 Ajuster Take-Profit", callback_data="adjust_take_profit")
    )
    try:
        bot.send_message(chat_id, "📈 Configuration TP/SL", reply_markup=markup)
    except Exception as e:
        logger.error(f"Erreur dans show_tp_sl_menu: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Erreur TP/SL: {str(e)}')

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
        InlineKeyboardButton("📊 Max Market Cap Solana", callback_data="adjust_max_market_cap_sol")
    )
    try:
        bot.send_message(chat_id, (
            f'📊 Seuils de détection :\n- BSC Volume: {MIN_VOLUME_BSC} $ - {MAX_VOLUME_BSC} $\n'
            f'- BSC Tx/min : {MIN_TX_PER_MIN_BSC} - {MAX_TX_PER_MIN_BSC}\n'
            f'- BSC Market Cap : {MIN_MARKET_CAP_BSC} $ - {MAX_MARKET_CAP_BSC} $\n'
            f'- Min Liquidité : {MIN_LIQUIDITY} $\n- Solana Volume: {MIN_VOLUME_SOL} $ - {MAX_VOLUME_SOL} $\n'
            f'- Solana Tx/min : {MIN_TX_PER_MIN_SOL} - {MAX_TX_PER_MIN_SOL}\n'
            f'- Solana Market Cap : {MIN_MARKET_CAP_SOL} $ - {MAX_MARKET_CAP_SOL} $'
        ), reply_markup=markup)
    except Exception as e:
        logger.error(f"Erreur dans show_threshold_menu: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Erreur seuils: {str(e)}')

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

def adjust_slippage(message):
    global slippage
    chat_id = message.chat.id
    try:
        new_slippage = float(message.text)
        if 0 <= new_slippage <= 100:
            slippage = new_slippage
            bot.send_message(chat_id, f'✅ Slippage mis à jour à {slippage} %')
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
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 30000)")

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

def adjust_momentum(message):
    global momentum_threshold
    chat_id = message.chat.id
    try:
        new_value = int(message.text)
        if new_value > 0:
            momentum_threshold = new_value
            bot.send_message(chat_id, f'✅ Seuil de momentum mis à jour à {momentum_threshold} tx/min')
        else:
            bot.send_message(chat_id, "⚠️ La valeur doit être positive!")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 100)")

def buy_token_bsc(chat_id, contract_address, amount):
    global simulation_mode
    if simulation_mode:
        bot.send_message(chat_id, f"🧪 [SIMULATION] Achat simulé de {amount} BNB de {contract_address}")
        entry_price = amount / (detected_tokens[contract_address]['supply'] * get_current_market_cap(contract_address) / detected_tokens[contract_address]['supply'])
        portfolio[contract_address] = {
            'amount': amount, 'chain': 'bsc', 'entry_price': entry_price,
            'market_cap_at_buy': detected_tokens[contract_address]['market_cap'],
            'current_market_cap': detected_tokens[contract_address]['market_cap']
        }
        monitor_and_sell_dynamic(chat_id, contract_address, amount, 'bsc')
        return
    try:
        router = w3.eth.contract(address=PANCAKE_ROUTER_ADDRESS, abi=PANCAKE_ROUTER_ABI)
        amount_in = w3.to_wei(amount, 'ether')
        amount_out_min = int(amount_in * (1 - slippage / 100))
        tx = router.functions.swapExactETHForTokens(
            amount_out_min,
            [w3.to_checksum_address('0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c'), w3.to_checksum_address(contract_address)],
            w3.to_checksum_address(WALLET_ADDRESS),
            int(time.time()) + 60
        ).build_transaction({
            'from': WALLET_ADDRESS, 'value': amount_in, 'gas': 200000,
            'gasPrice': w3.to_wei(gas_fee * 2, 'gwei'), 'nonce': w3.eth.get_transaction_count(WALLET_ADDRESS)
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
                'current_market_cap': detected_tokens[contract_address]['market_cap']
            }
            bot.send_message(chat_id, f'✅ Achat effectué : {amount} BNB de {contract_address}')
            monitor_and_sell_dynamic(chat_id, contract_address, amount, 'bsc')
        else:
            bot.send_message(chat_id, f'⚠️ Échec achat {contract_address}, TX: {tx_hash.hex()}')
    except Exception as e:
        logger.error(f"Erreur achat BSC: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Échec achat {contract_address}: {str(e)}')

def buy_token_solana(chat_id, contract_address, amount):
    global simulation_mode
    if simulation_mode:
        bot.send_message(chat_id, f"🧪 [SIMULATION] Achat simulé de {amount} SOL de {contract_address}")
        entry_price = amount / (detected_tokens[contract_address]['supply'] * get_current_market_cap(contract_address) / detected_tokens[contract_address]['supply'])
        portfolio[contract_address] = {
            'amount': amount, 'chain': 'solana', 'entry_price': entry_price,
            'market_cap_at_buy': detected_tokens[contract_address]['market_cap'],
            'current_market_cap': detected_tokens[contract_address]['market_cap']
        }
        monitor_and_sell_dynamic(chat_id, contract_address, amount, 'solana')
        return
    try:
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
            'current_market_cap': detected_tokens[contract_address]['market_cap']
        }
        bot.send_message(chat_id, f'✅ Achat effectué : {amount} SOL de {contract_address}')
        monitor_and_sell_dynamic(chat_id, contract_address, amount, 'solana')
    except Exception as e:
        logger.error(f"Erreur achat Solana: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Échec achat {contract_address}: {str(e)}')

def sell_token(chat_id, contract_address, amount, chain, current_price):
    if chain == "solana":
        try:
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
            del portfolio[contract_address]
            bot.send_message(chat_id, f'✅ Vente effectuée : {amount} SOL de {contract_address}')
        except Exception as e:
            logger.error(f"Erreur vente Solana: {str(e)}")
            bot.send_message(chat_id, f'⚠️ Échec vente {contract_address}: {str(e)}')
    else:
        try:
            router = w3.eth.contract(address=PANCAKE_ROUTER_ADDRESS, abi=PANCAKE_ROUTER_ABI)
            token_amount = w3.to_wei(amount, 'ether')
            amount_in_max = int(token_amount * (1 + slippage / 100))
            tx = router.functions.swapExactTokensForETH(
                token_amount,
                amount_in_max,
                [w3.to_checksum_address(contract_address), w3.to_checksum_address('0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c')],
                w3.to_checksum_address(WALLET_ADDRESS),
                int(time.time()) + 60
            ).build_transaction({
                'from': WALLET_ADDRESS, 'gas': 200000,
                'gasPrice': w3.to_wei(gas_fee * 2, 'gwei'), 'nonce': w3.eth.get_transaction_count(WALLET_ADDRESS)
            })
            signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
            tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
            bot.send_message(chat_id, f'⏳ Vente en cours de {amount} BNB de {contract_address}, TX: {tx_hash.hex()}')
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
            if receipt.status == 1:
                del portfolio[contract_address]
                bot.send_message(chat_id, f'✅ Vente effectuée : {amount} BNB de {contract_address}')
            else:
                bot.send_message(chat_id, f'⚠️ Échec vente {contract_address}, TX: {tx_hash.hex()}')
        except Exception as e:
            logger.error(f"Erreur vente BSC: {str(e)}")
            bot.send_message(chat_id, f'⚠️ Échec vente {contract_address}: {str(e)}')

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
            time.sleep(1)
    except Exception as e:
        logger.error(f"Erreur surveillance {contract_address}: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Erreur surveillance {contract_address}: {str(e)}')

def monitor_and_sell_dynamic(chat_id, contract_address, amount, chain):
    try:
        while contract_address in portfolio:
            current_mc = get_current_market_cap(contract_address)
            portfolio[contract_address]['current_market_cap'] = current_mc
            profit_pct = (current_mc - portfolio[contract_address]['market_cap_at_buy']) / portfolio[contract_address]['market_cap_at_buy'] * 100
            loss_pct = -profit_pct if profit_pct < 0 else 0
            tx_per_min = detected_tokens[contract_address]['tx_per_min']
            
            if profit_pct >= take_profit_steps[2] * 100 and tx_per_min > momentum_threshold:
                bot.send_message(chat_id, f"🚀 {contract_address} en fort momentum (tx/min: {tx_per_min}), attente pour maximiser...")
                time.sleep(10)
                continue
            
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
            time.sleep(1)
    except Exception as e:
        logger.error(f"Erreur surveillance dynamique {contract_address}: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur surveillance dynamique {contract_address}: {str(e)}")

def show_portfolio(chat_id):
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
                f"MC Actuel: ${current_mc:.2f}\nProfit: {profit:.2f}%\n"
                f"Take-Profit: x{take_profit_steps[0]}, x{take_profit_steps[1]}, x{take_profit_steps[2]}\n"
                f"Stop-Loss: -{stop_loss_threshold} %\n\n"
            )
        bot.send_message(chat_id, msg, reply_markup=markup if portfolio else None)
    except Exception as e:
        logger.error(f"Erreur portefeuille: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Erreur portefeuille: {str(e)}')

def get_solana_balance(wallet_address):
    try:
        response = session.post(SOLANA_RPC, json={
            "jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [str(solana_keypair.pubkey())]
        }, timeout=10)
        result = response.json().get('result', 0)
        return result.get('value', 0) / 10**9
    except Exception as e:
        logger.error(f"Erreur solde Solana: {str(e)}")
        return 0

def get_current_market_cap(contract_address):
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
            volume_24h = float(data.get('volume', {}).get('h24', 0))
            return volume_24h * supply
    except Exception as e:
        logger.error(f"Erreur market cap: {str(e)}")
        return detected_tokens[contract_address]['market_cap']

def refresh_token(chat_id, token):
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
            f"Profit: {profit:.2f}%\nTake-Profit: x{take_profit_steps[0]}, x{take_profit_steps[1]}, x{take_profit_steps[2]}\n"
            f"Stop-Loss: -{stop_loss_threshold} %"
        )
        bot.send_message(chat_id, f'🔍 Portefeuille rafraîchi pour {token}', reply_markup=markup)
    except Exception as e:
        logger.error(f"Erreur refresh: {str(e)}")
        bot.send_message(chat_id, f'⚠️ Erreur rafraîchissement {token}: {str(e)}')

def sell_token_immediate(chat_id, token):
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

async def monitor_twitter_free(chat_id):
    global last_twitter_call
    bot.send_message(chat_id, "📡 Surveillance Twitter réactivée (mode gratuit) : détection des mentions de CA par comptes influents...")
    logger.info("Surveillance Twitter réactivée en mode gratuit pour comptes influents.")
    while trade_active:
        try:
            current_time = time.time()
            if current_time - last_twitter_call < 15:
                await asyncio.sleep(15 - (current_time - last_twitter_call))
            url = "https://api.twitter.com/2/tweets/search/recent"
            params = {
                "query": "(0x OR So OR CA) -is:retweet has:mentions",
                "max_results": 10,
                "tweet.fields": "created_at,author_id",
                "user.fields": "public_metrics",
                "expansions": "author_id"
            }
            response = session.get(url, headers=TWITTER_HEADERS, params=params, timeout=10)
            if response.status_code == 429:
                logger.warning("Quota Twitter atteint, pause de 15min.")
                bot.send_message(chat_id, "⚠️ Quota Twitter atteint, pause de 15min.")
                await asyncio.sleep(900)
                continue
            response.raise_for_status()
            data = response.json()
            last_twitter_call = time.time()
            
            users = {user["id"]: user for user in data.get("includes", {}).get("users", [])}
            for tweet in data.get("data", []):
                author_id = tweet["author_id"]
                author = users.get(author_id, {})
                followers = author.get("public_metrics", {}).get("followers_count", 0)
                
                if followers > 10000:
                    text = tweet["text"]
                    potential_addresses = [word for word in text.split() if word.startswith("0x") or word.startswith("So") or (len(word) > 20 and "CA" in text.upper())]
                    for addr in potential_addresses:
                        if await check_twitter_token(chat_id, addr):
                            logger.info(f"Token détecté via Twitter par @{author.get('username', 'inconnu')} ({followers} followers) : {addr}")
                            bot.send_message(chat_id, f"📢 @{author.get('username', 'inconnu')} ({followers} followers) a mentionné {addr}")
            await asyncio.sleep(300)
        except Exception as e:
            logger.error(f"Erreur Twitter gratuit : {str(e)}")
            bot.send_message(chat_id, f"⚠️ Erreur Twitter : {str(e)}")
            await asyncio.sleep(900)

async def monitor_bsc_websocket(chat_id):
    bot.send_message(chat_id, "🌐 Surveillance BSC via WebSocket activée...")
    w3_ws = Web3(Web3.WebsocketProvider(BSC_WS_URI))
    if not w3_ws.is_connected():
        logger.error("Connexion WebSocket BSC échouée.")
        bot.send_message(chat_id, "⚠️ WebSocket BSC indisponible, retour au mode HTTP.")
        return
    factory = w3_ws.eth.contract(address=PANCAKE_FACTORY_ADDRESS, abi=PANCAKE_FACTORY_ABI)
    logger.info("WebSocket BSC connecté.")
    
    async def handle_event(event):
        token_address = event['args']['token0'] if event['args']['token0'] != "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c" else event['args']['token1']
        await check_bsc_token(chat_id, token_address, loose_mode_bsc)
    
    event_filter = factory.events.PairCreated.create_filter(fromBlock="latest")
    while trade_active:
        try:
            for event in event_filter.get_new_entries():
                await handle_event(event)
            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Erreur WebSocket BSC : {str(e)}")
            bot.send_message(chat_id, f"⚠️ Erreur WebSocket BSC : {str(e)}")
            await asyncio.sleep(60)

async def monitor_trending_free(chat_id):
    bot.send_message(chat_id, "📈 Surveillance des trending memecoins sur X activée...")
    keywords = ["memecoin", "pump", "moon", "0x", "So"]
    while trade_active:
        try:
            current_time = time.time()
            if current_time - last_twitter_call < 15:
                await asyncio.sleep(15 - (current_time - last_twitter_call))
            url = "https://api.twitter.com/2/tweets/search/recent"
            params = {
                "query": " ".join(keywords),
                "max_results": 10,
                "tweet.fields": "created_at"
            }
            response = session.get(url, headers=TWITTER_HEADERS, params=params, timeout=10)
            if response.status_code == 429:
                logger.warning("Quota Twitter atteint, pause de 15min.")
                bot.send_message(chat_id, "⚠️ Quota Twitter trending atteint, pause de 15min.")
                await asyncio.sleep(900)
                continue
            response.raise_for_status()
            data = response.json()
            last_twitter_call = time.time()
            for tweet in data.get("data", []):
                text = tweet["text"]
                potential_addresses = [word for word in text.split() if word.startswith("0x") or word.startswith("So")]
                for addr in potential_addresses:
                    if await check_twitter_token(chat_id, addr):
                        logger.info(f"Token trending détecté : {addr}")
            await asyncio.sleep(600)
        except Exception as e:
            logger.error(f"Erreur trending X : {str(e)}")
            bot.send_message(chat_id, f"⚠️ Erreur trending X : {str(e)}")
            await asyncio.sleep(900)

def set_webhook():
    logger.info("Configuration du webhook...")
    bot.remove_webhook()
    time.sleep(1)
    bot.set_webhook(url=WEBHOOK_URL)
    logger.info(f"Webhook configuré sur {WEBHOOK_URL}")

if __name__ == "__main__":
    logger.info("Démarrage de l'application...")
    initialize_bot()  # Initialisation synchrone avant tout
    set_webhook()     # Configuration du webhook avant le lancement de Flask
    logger.info(f"Démarrage de Flask sur 0.0.0.0:{PORT}...")
    app.run(host="0.0.0.0", port=PORT, debug=False)  # Lancement direct de Flask
