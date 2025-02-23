import os
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import logging
import requests
import time
import threading
from flask import Flask, request, abort
from cachetools import TTLCache
from web3 import Web3
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from solders.system_program import TransferParams, transfer
from solders.signature import Signature
from solders.instruction import Instruction
import json
import base58
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Configuration du logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Headers pour API
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/91.0.4472.124",
    "Accept": "application/json",
}

# Session persistante avec retries
session = requests.Session()
session.headers.update(HEADERS)
retry_strategy = Retry(total=5, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("https://", adapter)

# Chargement des variables depuis Cloud Run
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY")
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")
BSC_SCAN_API_KEY = os.getenv("BSC_SCAN_API_KEY")
TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN")
PORT = int(os.getenv("PORT", 8080))

# Headers pour Twitter API
TWITTER_HEADERS = {"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"}

# Validation des variables
logger.info("Validation des variables...")
missing_vars = []
required_vars = {
    "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
    "WALLET_ADDRESS": WALLET_ADDRESS,
    "PRIVATE_KEY": PRIVATE_KEY,
    "SOLANA_PRIVATE_KEY": SOLANA_PRIVATE_KEY,
    "WEBHOOK_URL": WEBHOOK_URL,
    "BIRDEYE_API_KEY": BIRDEYE_API_KEY,
    "BSC_SCAN_API_KEY": BSC_SCAN_API_KEY,
    "TWITTER_BEARER_TOKEN": TWITTER_BEARER_TOKEN
}
for var_name, var_value in required_vars.items():
    if not var_value:
        missing_vars.append(var_name)
if missing_vars:
    logger.error(f"Variables manquantes : {missing_vars}")
    raise ValueError(f"Variables manquantes : {missing_vars}")
else:
    logger.info("Toutes les variables sont présentes.")

# Initialisation
bot = telebot.TeleBot(TELEGRAM_TOKEN)
app = Flask(__name__)

# BSC (PancakeSwap)
w3 = Web3(Web3.HTTPProvider("https://bsc-dataseed.binance.org/"))
if not w3.is_connected():
    logger.error("Connexion BSC échouée.")
    raise ConnectionError("Connexion BSC échouée")
logger.info("Connexion BSC réussie.")
PANCAKE_ROUTER_ADDRESS = "0x10ED43C718714eb63d5aA57B78B54704E256024E"
PANCAKE_FACTORY_ADDRESS = "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"
PANCAKE_FACTORY_ABI = json.loads('''
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
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
solana_keypair = Keypair.from_base58_string(SOLANA_PRIVATE_KEY)
RAYDIUM_PROGRAM_ID = Pubkey.from_string("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSceAHj2")
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
    logger.info(f"Connexion Solana réussie, blockhash: {blockhash}")
except Exception as e:
    logger.error(f"Erreur initiale Solana RPC: {str(e)}")
    raise ConnectionError("Connexion Solana échouée")

# Configuration de base (50€ = 0.02 BNB / 0.37 SOL au 23/02/2025)
mise_depart_bsc = 0.02  # Approx. 50€ en BNB
mise_depart_sol = 0.37  # Approx. 50€ en SOL
slippage = 5  # Pourcentage
gas_fee = 5  # Gwei
stop_loss_threshold = 30  # Pourcentage
take_profit_steps = [2, 3, 5]  # Multiplicateurs
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
MAX_HOLDER_PCT = 5

# Vérification anti-rug pull BSC via Honeypot.is
def is_safe_token_bsc(token_address):
    try:
        response = session.get(f"https://api.honeypot.is/v2/IsHoneypot?address={token_address}")
        data = response.json()
        is_safe = (
            not data.get("isHoneypot", True) and
            data.get("buyTax", 0) <= MAX_TAX and
            data.get("sellTax", 0) <= MAX_TAX and
            data.get("maxHolders", 100) / data.get("totalSupply", 1) * 100 <= MAX_HOLDER_PCT
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
            headers={"X-API-KEY": BIRDEYE_API_KEY}
        )
        data = response.json().get('data', {})
        top_holders_pct = sum(h['percent'] for h in data.get('topHolders', [])) if data.get('topHolders') else 0
        is_safe = top_holders_pct <= MAX_HOLDER_PCT and data.get('liquidity', 0) >= MIN_LIQUIDITY
        return is_safe
    except Exception as e:
        logger.error(f"Erreur vérification Solana: {str(e)}")
        return False

# Surveillance Twitter/X pour Kanye West et autres personnalités
def monitor_twitter(chat_id):
    logger.info("Surveillance Twitter/X en cours...")
    try:
        # Tweets récents de Kanye West (@kanyewest)
        response = session.get(
            "https://api.twitter.com/2/tweets/search/recent?query=from:kanyewest memecoin OR token OR launch&max_results=10",
            headers=TWITTER_HEADERS
        )
        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", 60))
            logger.warning(f"Erreur 429 - Limite atteinte, attente de {retry_after} secondes")
            bot.send_message(chat_id, f"⚠ Limite Twitter atteinte, pause de {retry_after} secondes")
            time.sleep(retry_after)
            return
        response.raise_for_status()
        tweets = response.json().get('data', [])
        for tweet in tweets:
            text = tweet['text'].lower()
            if 'contract address' in text or "token" in text:
                words = text.split()
                for word in words:
                    if (len(word) == 42 and word.startswith("0x")) or len(word) == 44:
                        if word not in twitter_tokens:
                            twitter_tokens.append(word)
                            bot.send_message(chat_id, f"⚡ Nouveau token détecté via X (Kanye West): {word}")
                            check_twitter_token(chat_id, word)

        # Recherche générale
        response = session.get(
            "https://api.twitter.com/2/tweets/search/recent?query=memecoin OR token OR launch -from:kanyewest&max_results=10",
            headers=TWITTER_HEADERS
        )
        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", 60))
            logger.warning(f"Erreur 429 - Limite atteinte, attente de {retry_after} secondes")
            bot.send_message(chat_id, f"⚠ Limite Twitter atteinte, pause de {retry_after} secondes")
            time.sleep(retry_after)
            return
        response.raise_for_status()
        tweets = response.json().get('data', [])
        for tweet in tweets:
            text = tweet['text'].lower()
            if 'contract address' in text or 'token' in text:
                words = text.split()
                for word in words:
                    if (len(word) == 42 and word.startswith("0x")) or len(word) == 44:
                        if word not in twitter_tokens:
                            twitter_tokens.append(word)
                            bot.send_message(chat_id, f"⚡ Nouveau token détecté via X (communauté): {word}")
                            check_twitter_token(chat_id, word)
    except Exception as e:
        logger.error(f"Erreur surveillance Twitter: {str(e)}")
        bot.send_message(chat_id, f"⚠ Erreur surveillance Twitter: {str(e)}")

# Vérification des tokens détectés via Twitter
def check_twitter_token(chat_id, token_address):
    try:
        if token_address.startswith("0x"):  # BSC
            # Vérifier si c'est un contrat valide
            if w3.eth.get_code(w3.to_checksum_address(token_address)) == b'':
                bot.send_message(chat_id, f"⚠ Token X {token_address} rejeté - Pas un contrat valide")
                return
            
            # Obtenir les informations du token
            token_contract = w3.eth.contract(address=w3.to_checksum_address(token_address), abi=[{
                "name": "totalSupply",
                "outputs": [{"name": "", "type": "uint256"}],
                "payable": False,
                "stateMutability": "view",
                "type": "function"
            }])
            supply = token_contract.functions.totalSupply().call() / 10**18
            
            # Approximation via BSCScan (volume et liquidité)
            volume_response = session.get(
                f"https://api.bscscan.com/api?module=stats&action=tokenbalance&contractaddress={token_address}&address={PANCAKE_ROUTER_ADDRESS}&tag=latest&apikey={BSC_SCAN_API_KEY}"
            )
            volume_data = volume_response.json()
            if volume_data['status'] != '1':
                bot.send_message(chat_id, f"⚠ Token X {token_address} rejeté - Erreur API BSCScan: {volume_data['message']}")
                return
            volume = float(volume_data['result']) / 10**18
            liquidity = volume * 0.5  # Approximation
            market_cap = volume * supply
            
            if not (MIN_VOLUME_BSC <= volume <= MAX_VOLUME_BSC):
                bot.send_message(chat_id, f"⚠ Token X {token_address} rejeté - Volume hors limites: ${volume:.2f}")
            elif liquidity < MIN_LIQUIDITY:
                bot.send_message(chat_id, f"⚠ Token X {token_address} rejeté - Liquidité insuffisante: ${liquidity:.2f}")
            elif not (MIN_MARKET_CAP_BSC <= market_cap <= MAX_MARKET_CAP_BSC):
                bot.send_message(chat_id, f"⚠ Token X {token_address} rejeté - Market Cap hors limites: ${market_cap:.2f}")
            elif not is_safe_token_bsc(token_address):
                bot.send_message(chat_id, f"⚠ Token X {token_address} rejeté - Sécurité non validée (possible rug)")
            else:
                bot.send_message(chat_id, 
                    f"✅ Token X détecté : {token_address} (BSC) - Vol: ${volume:.2f}, Liq: ${liquidity:.2f}, MC: ${market_cap:.2f}"
                )
                detected_tokens[token_address] = {
                    'address': token_address,
                    'volume': volume,
                    'liquidity': liquidity,
                    'market_cap': market_cap,
                    'supply': supply
                }
                buy_token_bsc(chat_id, token_address, mise_depart_bsc)
        else:  # Solana
            response = session.get(
                f"https://public-api.birdeye.so/public/token_overview?address={token_address}",
                headers={"X-API-KEY": BIRDEYE_API_KEY}
            )
            data = response.json()['data']
            volume = float(data.get('v24hUSD', 0))
            liquidity = float(data.get('liquidity', 0))
            market_cap = float(data.get('mc', 0))
            supply = float(data.get('supply', 0))
            if not (MIN_VOLUME_SOL <= volume <= MAX_VOLUME_SOL):
                bot.send_message(chat_id, f"⚠ Token X {token_address} rejeté - Volume hors limites: ${volume:.2f}")
            elif liquidity < MIN_LIQUIDITY:
                bot.send_message(chat_id, f"⚠ Token X {token_address} rejeté - Liquidité insuffisante: ${liquidity:.2f}")
            elif not (MIN_MARKET_CAP_SOL <= market_cap <= MAX_MARKET_CAP_SOL):
                bot.send_message(chat_id, f"⚠ Token X {token_address} rejeté - Market Cap hors limites: ${market_cap:.2f}")
            elif not is_safe_token_solana(token_address):
                bot.send_message(chat_id, f"⚠ Token X {token_address} rejeté - Sécurité non validée (possible rug)")
            else:
                bot.send_message(chat_id, 
                    f"✅ Token X détecté : {token_address} (Solana) - Vol: ${volume:.2f}, Liq: ${liquidity:.2f}, MC: ${market_cap:.2f}"
                )
                detected_tokens[token_address] = {
                    'address': token_address,
                    'volume': volume,
                    'liquidity': liquidity,
                    'market_cap': market_cap,
                    'supply': supply
                }
                buy_token_solana(chat_id, token_address, mise_depart_sol)
    except Exception as e:
        logger.error(f"Erreur vérification token X {token_address} : {str(e)}")
        bot.send_message(chat_id, f"⚠ Erreur vérification token X {token_address} : {str(e)}")

# Détection des nouveaux tokens BSC
def detect_new_tokens_bsc(chat_id):
    try:
        factory = w3.eth.contract(address=PANCAKE_FACTORY_ADDRESS, abi=PANCAKE_FACTORY_ABI)
        latest_block = w3.eth.block_number
        events = factory.events.PairCreated.get_logs(fromBlock=latest_block-100, toBlock=latest_block)
        bot.send_message(chat_id, f"📡 {len(events)} nouvelles paires détectées sur BSC")
        for event in events:
            token0 = event['args']['token0']
            token1 = event['args']['token1']
            pair = event['args']['pair']
            if "0x0000" in token0.lower() or "0x0000" in token1.lower():
                bot.send_message(chat_id, f"⚠ Token ignoré - Adresse invalide ou suspecte: {token0} ou {token1}")
                continue
            
            # Vérifier le token principal (on suppose token1 comme cible)
            token_address = token1
            if w3.eth.get_code(w3.to_checksum_address(token_address)) == b'':
                bot.send_message(chat_id, f"⚠ Token {token_address} rejeté - Pas un contrat valide")
                continue
            
            token_contract = w3.eth.contract(address=w3.to_checksum_address(token_address), abi=[{
                "name": "totalSupply",
                "outputs": [{"name": "", "type": "uint256"}],
                "payable": False,
                "stateMutability": "view",
                "type": "function"
            }])
            supply = token_contract.functions.totalSupply().call() / 10**18
            
            volume_response = session.get(
                f"https://api.bscscan.com/api?module=stats&action=tokenbalance&contractaddress={token_address}&address={pair}&tag=latest&apikey={BSC_SCAN_API_KEY}"
            )
            volume_data = volume_response.json()
            if volume_data['status'] != '1':
                bot.send_message(chat_id, f"⚠ Token {token_address} rejeté - Erreur API BSCScan: {volume_data['message']}")
                continue
            volume = float(volume_data['result']) / 10**18
            liquidity = volume * 0.5  # Approximation
            market_cap = volume * supply
            
            if not (MIN_VOLUME_BSC <= volume <= MAX_VOLUME_BSC):
                bot.send_message(chat_id, f"⚠ Token {token_address} rejeté - Volume hors limites: ${volume:.2f}")
            elif liquidity < MIN_LIQUIDITY:
                bot.send_message(chat_id, f"⚠ Token {token_address} rejeté - Liquidité insuffisante: ${liquidity:.2f}")
            elif not (MIN_MARKET_CAP_BSC <= market_cap <= MAX_MARKET_CAP_BSC):
                bot.send_message(chat_id, f"⚠ Token {token_address} rejeté - Market Cap hors limites: ${market_cap:.2f}")
            elif not is_safe_token_bsc(token_address):
                bot.send_message(chat_id, f"⚠ Token {token_address} rejeté - Sécurité non validée (possible rug)")
            else:
                bot.send_message(chat_id, 
                    f"✅ Token détecté : {token_address} (BSC) - Vol: ${volume:.2f}, Liq: ${liquidity:.2f}, MC: ${market_cap:.2f}"
                )
                detected_tokens[token_address] = {
                    'address': token_address,
                    'volume': volume,
                    'liquidity': liquidity,
                    'market_cap': market_cap,
                    'supply': supply
                }
                buy_token_bsc(chat_id, token_address, mise_depart_bsc)
        bot.send_message(chat_id, "✅ Détection BSC terminée, passage à Solana...")
    except Exception as e:
        logger.error(f"Erreur détection BSC: {str(e)}")
        bot.send_message(chat_id, f"⚠ Erreur détection BSC: {str(e)}")

# Détection des nouveaux tokens Solana
def detect_new_tokens_solana(chat_id):
    try:
        response = session.get(
            "https://public-api.birdeye.so/defi/tokenlist?sort_by=mc&sort_type=desc&offset=0&limit=10",
            headers={"X-API-KEY": BIRDEYE_API_KEY}
        )
        response.raise_for_status()
        tokens = response.json()['data']['tokens']
        bot.send_message(chat_id, f"📡 {len(tokens)} nouveaux tokens détectés sur Solana")
        for token in tokens:
            token_address = token['address']
            volume = float(token.get('v24hUSD', 0))
            liquidity = float(token.get('liquidity', 0))
            market_cap = float(token.get('mc', 0))
            supply = float(token.get('supply', 0))
            
            if not (MIN_VOLUME_SOL <= volume <= MAX_VOLUME_SOL):
                bot.send_message(chat_id, f"⚠ Token {token_address} rejeté - Volume hors limites: ${volume:.2f}")
            elif liquidity < MIN_LIQUIDITY:
                bot.send_message(chat_id, f"⚠ Token {token_address} rejeté - Liquidité insuffisante: ${liquidity:.2f}")
            elif not (MIN_MARKET_CAP_SOL <= market_cap <= MAX_MARKET_CAP_SOL):
                bot.send_message(chat_id, f"⚠ Token {token_address} rejeté - Market Cap hors limites: ${market_cap:.2f}")
            elif not is_safe_token_solana(token_address):
                bot.send_message(chat_id, f"⚠ Token {token_address} rejeté - Sécurité non validée (possible rug)")
            else:
                bot.send_message(chat_id, 
                    f"✅ Token détecté : {token_address} (Solana) - Vol: ${volume:.2f}, Liq: ${liquidity:.2f}, MC: ${market_cap:.2f}"
                )
                detected_tokens[token_address] = {
                    'address': token_address,
                    'volume': volume,
                    'liquidity': liquidity,
                    'market_cap': market_cap,
                    'supply': supply
                }
                buy_token_solana(chat_id, token_address, mise_depart_sol)
        bot.send_message(chat_id, "✅ Détection Solana terminée.")
    except Exception as e:
        logger.error(f"Erreur détection Solana: {str(e)}")
        bot.send_message(chat_id, f"⚠ Erreur détection Solana: {str(e)}")

# Webhook Telegram
@app.route("/webhook", methods=['POST'])
def webhook():
    logger.info("Webhook reçu")
    try:
        if request.method == 'POST' and request.headers.get("content-type") == "application/json":
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
        bot.send_message(message.chat.id, "📡 Bot démarré ! Bienvenue sur ton bot de trading de memecoins.")
        show_main_menu(message.chat.id)
    except Exception as e:
        logger.error(f"Erreur dans start_message: {str(e)}")
        bot.send_message(message.chat.id, f"⚠ Erreur au démarrage: {str(e)}")

@bot.message_handler(commands=['menu'])
def menu_message(message):
    logger.info("Commande /menu reçue")
    try:
        bot.send_message(message.chat.id, "📡 Menu affiché !")
        show_main_menu(message.chat.id)
    except Exception as e:
        logger.error(f"Erreur dans menu_message: {str(e)}")
        bot.send_message(message.chat.id, f"⚠ Erreur affichage menu: {str(e)}")

# Menu principal avec nouveaux onglets
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
        bot.send_message(chat_id, "📡 Voici le menu principal :", reply_markup=markup)
    except Exception as e:
        logger.error(f"Erreur dans show_main_menu: {str(e)}")
        bot.send_message(chat_id, f"⚠ Erreur affichage menu: {str(e)}")

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
                f"- Gas Fee (BSC) : {gas_fee} Gwei"
            ))
        elif call.data == "config":
            show_config_menu(chat_id)
        elif call.data == "launch":
            if not trade_active:
                trade_active = True
                bot.send_message(chat_id, "🚀 Lancement du trading automatique !")
                threading.Thread(target=trading_cycle, args=(chat_id,), daemon=True).start()
            else:
                bot.send_message(chat_id, "⚠ Le trading est déjà actif !")
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
            bot.send_message(chat_id, f"📡 Mise BSC augmentée à {mise_depart_bsc} BNB")
        elif call.data == "increase_mise_sol":
            mise_depart_sol += 0.01
            bot.send_message(chat_id, f"📡 Mise Solana augmentée à {mise_depart_sol} SOL")
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
        bot.send_message(chat_id, f"⚠ Erreur générale: {str(e)}")

# Cycle de trading dans un thread séparé
def trading_cycle(chat_id):
    global trade_active
    cycle_count = 0
    while trade_active:
        cycle_count += 1
        bot.send_message(chat_id, f"ℹ️ Début du cycle de détection #{cycle_count}...")
        detect_new_tokens_bsc(chat_id)
        detect_new_tokens_solana(chat_id)
        if cycle_count % 5 == 0:  # Appel Twitter tous les 5 cycles (~5 min)
            monitor_twitter(chat_id)
        bot.send_message(chat_id, "⏳ Attente de 60 secondes avant le prochain cycle...")
        time.sleep(60)

# Menu de configuration
def show_config_menu(chat_id):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("📈 Augmenter mise BSC (+0.01 BNB)", callback_data="increase_mise_bsc"),
        InlineKeyboardButton("📈 Augmenter mise SOL (+0.01 SOL)", callback_data="increase_mise_sol")
    )
    try:
        bot.send_message(chat_id, "⚙️ Configuration :", reply_markup=markup)
    except Exception as e:
        logger.error(f"Erreur dans show_config_menu: {str(e)}")
        bot.send_message(chat_id, f"⚠ Erreur configuration: {str(e)}")

# Menu des réglages
def show_settings_menu(chat_id):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("📏 Ajuster Mise BSC", callback_data="adjust_mise_bsc"),
        InlineKeyboardButton("📏 Ajuster Mise Solana", callback_data="adjust_mise_sol"),
        InlineKeyboardButton("📏 Ajuster Slippage", callback_data="adjust_slippage"),
        InlineKeyboardButton("📏 Ajuster Gas Fee (BSC)", callback_data="adjust_gas")
    )
    try:
        bot.send_message(chat_id, "🔧 Réglages :", reply_markup=markup)
    except Exception as e:
        logger.error(f"Erreur dans show_settings_menu: {str(e)}")
        bot.send_message(chat_id, f"⚠ Erreur réglages: {str(e)}")

# Menu TP/SL
def show_tp_sl_menu(chat_id):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("📉 Ajuster Stop-Loss", callback_data="adjust_stop_loss"),
        InlineKeyboardButton("📈 Ajuster Take-Profit", callback_data="adjust_take_profit")
    )
    try:
        bot.send_message(chat_id, (
            f"📊 Paramètres TP/SL :\n"
            f"- Stop-Loss : -{stop_loss_threshold}%\n"
            f"- Take-Profit : x{take_profit_steps[0]}, x{take_profit_steps[1]}, x{take_profit_steps[2]}"
        ), reply_markup=markup)
    except Exception as e:
        logger.error(f"Erreur dans show_tp_sl_menu: {str(e)}")
        bot.send_message(chat_id, f"⚠ Erreur TP/SL: {str(e)}")

# Menu Seuils
def show_threshold_menu(chat_id):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("📉 Min Volume BSC", callback_data="adjust_min_volume_bsc"),
        InlineKeyboardButton("📈 Max Volume BSC", callback_data="adjust_max_volume_bsc"),
        InlineKeyboardButton("💧 Min Liquidité", callback_data="adjust_min_liquidity"),
        InlineKeyboardButton("📉 Min Market Cap BSC", callback_data="adjust_min_market_cap_bsc"),
        InlineKeyboardButton("📈 Max Market Cap BSC", callback_data="adjust_max_market_cap_bsc"),
        InlineKeyboardButton("📉 Min Volume Solana", callback_data="adjust_min_volume_sol"),
        InlineKeyboardButton("📈 Max Volume Solana", callback_data="adjust_max_volume_sol"),
        InlineKeyboardButton("📉 Min Market Cap Solana", callback_data="adjust_min_market_cap_sol"),
        InlineKeyboardButton("📈 Max Market Cap Solana", callback_data="adjust_max_market_cap_sol")
    )
    try:
        bot.send_message(chat_id, (
            f"📊 Seuils de détection :\n"
            f"- BSC Volume : {MIN_VOLUME_BSC}$ - {MAX_VOLUME_BSC}$\n"
            f"- BSC Market Cap : {MIN_MARKET_CAP_BSC}$ - {MAX_MARKET_CAP_BSC}$\n"
            f"- Min Liquidité : {MIN_LIQUIDITY}$\n"
            f"- Solana Volume : {MIN_VOLUME_SOL}$ - {MAX_VOLUME_SOL}$\n"
            f"- Solana Market Cap : {MIN_MARKET_CAP_SOL}$ - {MAX_MARKET_CAP_SOL}$"
        ), reply_markup=markup)
    except Exception as e:
        logger.error(f"Erreur dans show_threshold_menu: {str(e)}")
        bot.send_message(chat_id, f"⚠ Erreur seuils: {str(e)}")

# Ajustements avec notifications
def adjust_mise_bsc(message):
    global mise_depart_bsc
    chat_id = message.chat.id
    try:
        new_mise = float(message.text)
        if new_mise > 0:
            mise_depart_bsc = new_mise
            bot.send_message(chat_id, f"📡 Mise BSC mise à jour avec succès à {mise_depart_bsc} BNB")
        else:
            bot.send_message(chat_id, "⚠ La mise doit être positive !")
    except ValueError:
        bot.send_message(chat_id, "⚠ Erreur : Entrez un nombre valide (ex. : 0.02)")

def adjust_mise_sol(message):
    global mise_depart_sol
    chat_id = message.chat.id
    try:
        new_mise = float(message.text)
        if new_mise > 0:
            mise_depart_sol = new_mise
            bot.send_message(chat_id, f"📡 Mise Solana mise à jour avec succès à {mise_depart_sol} SOL")
        else:
            bot.send_message(chat_id, "⚠ La mise doit être positive !")
    except ValueError:
        bot.send_message(chat_id, "⚠ Erreur : Entrez un nombre valide (ex. : 0.37)")

def adjust_slippage(message):
    global slippage
    chat_id = message.chat.id
    try:
        new_slippage = float(message.text)
        if 0 <= new_slippage <= 100:
            slippage = new_slippage
            bot.send_message(chat_id, f"📡 Slippage mis à jour avec succès à {slippage}%")
        else:
            bot.send_message(chat_id, "⚠ Le slippage doit être entre 0 et 100% !")
    except ValueError:
        bot.send_message(chat_id, "⚠ Erreur : Entrez un pourcentage valide (ex. : 5)")

def adjust_gas_fee(message):
    global gas_fee
    chat_id = message.chat.id
    try:
        new_gas_fee = float(message.text)
        if new_gas_fee > 0:
            gas_fee = new_gas_fee
            bot.send_message(chat_id, f"📡 Frais de gas mis à jour avec succès à {gas_fee} Gwei")
        else:
            bot.send_message(chat_id, "⚠ Les frais de gas doivent être positifs !")
    except ValueError:
        bot.send_message(chat_id, "⚠ Erreur : Entrez un nombre valide (ex. : 5)")

def adjust_stop_loss(message):
    global stop_loss_threshold
    chat_id = message.chat.id
    try:
        new_sl = float(message.text)
        if new_sl > 0:
            stop_loss_threshold = new_sl
            bot.send_message(chat_id, f"📡 Stop-Loss mis à jour avec succès à {stop_loss_threshold}%")
        else:
            bot.send_message(chat_id, "⚠ Le Stop-Loss doit être positif !")
    except ValueError:
        bot.send_message(chat_id, "⚠ Erreur : Entrez un pourcentage valide (ex. : 30)")

def adjust_take_profit(message):
    global take_profit_steps
    chat_id = message.chat.id
    try:
        new_tp = [float(x) for x in message.text.split(",")]
        if len(new_tp) == 3 and all(x > 0 for x in new_tp):
            take_profit_steps = new_tp
            bot.send_message(chat_id, f"📡 Take-Profit mis à jour avec succès à x{take_profit_steps[0]}, x{take_profit_steps[1]}, x{take_profit_steps[2]}")
        else:
            bot.send_message(chat_id, "⚠ Entrez 3 valeurs positives séparées par des virgules (ex. : 2,3,5)")
    except ValueError:
        bot.send_message(chat_id, "⚠ Erreur : Entrez des nombres valides (ex. : 2,3,5)")

def adjust_min_volume_bsc(message):
    global MIN_VOLUME_BSC
    chat_id = message.chat.id
    try:
        new_value = float(message.text)
        if new_value >= 0:
            MIN_VOLUME_BSC = new_value
            bot.send_message(chat_id, f"📡 Min Volume BSC mis à jour avec succès à ${MIN_VOLUME_BSC}")
        else:
            bot.send_message(chat_id, "⚠ La valeur doit être positive !")
    except ValueError:
        bot.send_message(chat_id, "⚠ Erreur : Entrez un nombre valide (ex. : 75000)")

def adjust_max_volume_bsc(message):
    global MAX_VOLUME_BSC
    chat_id = message.chat.id
    try:
        new_value = float(message.text)
        if new_value >= MIN_VOLUME_BSC:
            MAX_VOLUME_BSC = new_value
            bot.send_message(chat_id, f"📡 Max Volume BSC mis à jour avec succès à ${MAX_VOLUME_BSC}")
        else:
            bot.send_message(chat_id, "⚠ La valeur doit être supérieure au minimum !")
    except ValueError:
        bot.send_message(chat_id, "⚠ Erreur : Entrez un nombre valide (ex. : 750000)")

def adjust_min_liquidity(message):
    global MIN_LIQUIDITY
    chat_id = message.chat.id
    try:
        new_value = float(message.text)
        if new_value >= 0:
            MIN_LIQUIDITY = new_value
            bot.send_message(chat_id, f"📡 Min Liquidité mis à jour avec succès à ${MIN_LIQUIDITY}")
        else:
            bot.send_message(chat_id, "⚠ La valeur doit être positive !")
    except ValueError:
        bot.send_message(chat_id, "⚠ Erreur : Entrez un nombre valide (ex. : 100000)")

def adjust_min_market_cap_bsc(message):
    global MIN_MARKET_CAP_BSC
    chat_id = message.chat.id
    try:
        new_value = float(message.text)
        if new_value >= 0:
            MIN_MARKET_CAP_BSC = new_value
            bot.send_message(chat_id, f"📡 Min Market Cap BSC mis à jour avec succès à ${MIN_MARKET_CAP_BSC}")
        else:
            bot.send_message(chat_id, "⚠ La valeur doit être positive !")
    except ValueError:
        bot.send_message(chat_id, "⚠ Erreur : Entrez un nombre valide (ex. : 200000)")

def adjust_max_market_cap_bsc(message):
    global MAX_MARKET_CAP_BSC
    chat_id = message.chat.id
    try:
        new_value = float(message.text)
        if new_value >= MIN_MARKET_CAP_BSC:
            MAX_MARKET_CAP_BSC = new_value
            bot.send_message(chat_id, f"📡 Max Market Cap BSC mis à jour avec succès à ${MAX_MARKET_CAP_BSC}")
        else:
            bot.send_message(chat_id, "⚠ La valeur doit être supérieure au minimum !")
    except ValueError:
        bot.send_message(chat_id, "⚠ Erreur : Entrez un nombre valide (ex. : 2000000)")

def adjust_min_volume_sol(message):
    global MIN_VOLUME_SOL
    chat_id = message.chat.id
    try:
        new_value = float(message.text)
        if new_value >= 0:
            MIN_VOLUME_SOL = new_value
            bot.send_message(chat_id, f"📡 Min Volume Solana mis à jour avec succès à ${MIN_VOLUME_SOL}")
        else:
            bot.send_message(chat_id, "⚠ La valeur doit être positive !")
    except ValueError:
        bot.send_message(chat_id, "⚠ Erreur : Entrez un nombre valide (ex. : 50000)")

def adjust_max_volume_sol(message):
    global MAX_VOLUME_SOL
    chat_id = message.chat.id
    try:
        new_value = float(message.text)
        if new_value >= MIN_VOLUME_SOL:
            MAX_VOLUME_SOL = new_value
            bot.send_message(chat_id, f"📡 Max Volume Solana mis à jour avec succès à ${MAX_VOLUME_SOL}")
        else:
            bot.send_message(chat_id, "⚠ La valeur doit être supérieure au minimum !")
    except ValueError:
        bot.send_message(chat_id, "⚠ Erreur : Entrez un nombre valide (ex. : 500000)")

def adjust_min_market_cap_sol(message):
    global MIN_MARKET_CAP_SOL
    chat_id = message.chat.id
    try:
        new_value = float(message.text)
        if new_value >= 0:
            MIN_MARKET_CAP_SOL = new_value
            bot.send_message(chat_id, f"📡 Min Market Cap Solana mis à jour avec succès à ${MIN_MARKET_CAP_SOL}")
        else:
            bot.send_message(chat_id, "⚠ La valeur doit être positive !")
    except ValueError:
        bot.send_message(chat_id, "⚠ Erreur : Entrez un nombre valide (ex. : 100000)")

def adjust_max_market_cap_sol(message):
    global MAX_MARKET_CAP_SOL
    chat_id = message.chat.id
    try:
        new_value = float(message.text)
        if new_value >= MIN_MARKET_CAP_SOL:
            MAX_MARKET_CAP_SOL = new_value
            bot.send_message(chat_id, f"📡 Max Market Cap Solana mis à jour avec succès à ${MAX_MARKET_CAP_SOL}")
        else:
            bot.send_message(chat_id, "⚠ La valeur doit être supérieure au minimum !")
    except ValueError:
        bot.send_message(chat_id, "⚠ Erreur : Entrez un nombre valide (ex. : 1000000)")

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
            int(time.time()) + 60 * 10
        ).build_transaction({
            'from': WALLET_ADDRESS,
            'value': amount_in,
            'gas': 250000,
            'gasPrice': w3.to_wei(gas_fee, 'gwei'),
            'nonce': w3.eth.get_transaction_count(WALLET_ADDRESS)
        })
        signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
        bot.send_message(chat_id, f"📡 Achat en cours de {amount} BNB de {contract_address}, TX: {tx_hash.hex()}")
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
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
            bot.send_message(chat_id, f"⚠ Échec achat {contract_address}, TX: {tx_hash.hex()}")
    except Exception as e:
        logger.error(f"Erreur achat BSC: {str(e)}")
        bot.send_message(chat_id, f"⚠ Échec achat {contract_address}:
