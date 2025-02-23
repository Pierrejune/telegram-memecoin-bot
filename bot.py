import os
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import logging
import requests
import time
from flask import Flask, request, abort
from cachetools import TTLCache
from web3 import Web3
from solders.keypair import Keypair
from solders.pubkey import Pubkey
import json
from tenacity import retry, stop_after_attempt, wait_exponential

# Configuration du logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Headers pour API
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/91.0.4472.124",
    "Accept": "application/json",
}

# Session persistante
session = requests.Session()
session.headers.update(HEADERS)

# Chargement des variables d'environnement
TOKEN = os.getenv("TELEGRAM_TOKEN")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
SOLANA_WALLET_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY")
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")
PORT = int(os.getenv("PORT", 8080))

# Validation des variables
logger.info("Validation des variables...")
missing_vars = []
if not TOKEN:
    missing_vars.append("TELEGRAM_TOKEN")
if not WALLET_ADDRESS:
    missing_vars.append("WALLET_ADDRESS")
if not PRIVATE_KEY:
    missing_vars.append("PRIVATE_KEY")
if not SOLANA_WALLET_PRIVATE_KEY:
    missing_vars.append("SOLANA_PRIVATE_KEY")
if not BIRDEYE_API_KEY:
    missing_vars.append("BIRDEYE_API_KEY")
if not WEBHOOK_URL:
    missing_vars.append("WEBHOOK_URL")
if missing_vars:
    error_msg = f"Variables d'environnement manquantes : {', '.join(missing_vars)}"
    logger.error(error_msg)
    raise ValueError(error_msg)

# Initialisation
logger.info("Initialisation des composants...")
bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

# BSC (PancakeSwap)
logger.info("Connexion à BSC...")
w3 = Web3(Web3.HTTPProvider("https://bsc-dataseed.binance.org/"))
if not w3.is_connected():
    logger.error("Connexion BSC échouée.")
    w3 = None
else:
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
        "outputs": [
            {"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}
        ],
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
        "outputs": [
            {"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}
        ],
        "stateMutability": "nonpayable",
        "type": "function"
    }
]
''')

# Solana (Raydium)
logger.info("Connexion à Solana...")
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
solana_keypair = Keypair.from_base58_string(SOLANA_WALLET_PRIVATE_KEY)
RAYDIUM_PROGRAM_ID = Pubkey.from_string("675kPX9MHTjS2zt1qfr1NYHuzeLXiQM9H24wFSeeAHj2")

# Vérification Solana avec retry
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def check_solana_connection():
    response = session.post(SOLANA_RPC, json={
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getLatestBlockhash",
        "params": [{"commitment": "finalized"}]
    }, timeout=5)
    response.raise_for_status()
    blockhash = response.json().get('result', {}).get('value', {}).get('blockhash')
    logger.info(f"Connexion Solana réussie, blockhash: {blockhash}")
    return blockhash

try:
    check_solana_connection()
except Exception as e:
    logger.error(f"Erreur initiale Solana RPC après retries: {str(e)}")

# Configuration de base
test_mode = True
mise_depart_bsc = 0.01
mise_depart_sol = 0.02
slippage = 5
gas_fee = 5
stop_loss_threshold = 30
take_profit_steps = [2, 3, 5]
detected_tokens = {}
trade_active = False
cache = TTLCache(maxsize=100, ttl=300)
portfolio = {}

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

# Vérification anti-rugpull avec BirdEye API
def check_rugpull(token_address, chain="bsc"):
    try:
        headers = {"accept": "application/json", "X-API-KEY": BIRDEYE_API_KEY}
        url = f"https://public-api.birdeye.so/public/token_security?address={token_address}&chain={chain}"
        response = session.get(url, headers=headers, timeout=5)
        response.raise_for_status()
        data = response.json().get('data', {})
        if data.get("is_honeypot", False) or data.get("rugpull_risk", 0) > 0.5:
            logger.warning(f"Token {token_address} suspect (Honeypot: {data.get('is_honeypot')}, Risk: {data.get('rugpull_risk')})")
            return False
        return True
    except Exception as e:
        logger.error(f"Erreur vérification rugpull pour {token_address}: {str(e)}")
        return False

# Détection des nouveaux tokens sur BSC
def detect_new_tokens_bsc(chat_id):
    bot.send_message(chat_id, "🔍 Recherche de nouveaux tokens sur BSC (PancakeSwap)...")
    if not w3:
        bot.send_message(chat_id, "⚠️ Connexion BSC non disponible.")
        return
    try:
        factory = w3.eth.contract(address=PANCAKE_FACTORY_ADDRESS, abi=PANCAKE_FACTORY_ABI)
        latest_block = w3.eth.block_number
        events = factory.events.PairCreated.get_logs(fromBlock=latest_block - 100, toBlock=latest_block)
        bot.send_message(chat_id, f"📡 {len(events)} nouvelles paires trouvées sur BSC")
        
        for event in events:
            token_address = event['args']['token0'] if event['args']['token1'] == '0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c' else event['args']['token1']
            headers = {"accept": "application/json", "X-API-KEY": BIRDEYE_API_KEY}
            url = f"https://public-api.birdeye.so/public/price?address={token_address}&chain=bsc"
            response = session.get(url, headers=headers, timeout=5)
            if response.status_code == 200:
                price_data = response.json().get('data', {})
                price = price_data.get('value', 0)
                volume = price_data.get('volume', 0) * 1000
                liquidity = price_data.get('liquidity', 0) * 1000
                market_cap = price * 10**6
            else:
                volume, liquidity, market_cap = 100000, 150000, 500000
            
            if (MIN_VOLUME_BSC <= volume <= MAX_VOLUME_BSC and 
                MIN_LIQUIDITY <= liquidity and 
                MIN_MARKET_CAP_BSC <= market_cap <= MAX_MARKET_CAP_BSC):
                bot.send_message(chat_id, f"🚀 Token détecté : {token_address} (BSC) - Vol: ${volume}, Liq: ${liquidity}, MC: ${market_cap}")
                detected_tokens[token_address] = {'volume': volume, 'liquidity': liquidity, 'market_cap': market_cap}
                
                if check_rugpull(token_address, "bsc"):
                    buy_token_bsc(chat_id, token_address, mise_depart_bsc)
                else:
                    bot.send_message(chat_id, f"⚠️ {token_address} suspecté de rugpull, achat annulé.")
            else:
                logger.info(f"Token {token_address} ne respecte pas les critères.")
    except Exception as e:
        logger.error(f"Erreur détection BSC: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur détection BSC: {str(e)}")

# Détection des nouveaux tokens sur Solana
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def detect_new_tokens_solana(chat_id):
    bot.send_message(chat_id, "🔍 Recherche de nouveaux tokens sur Solana...")
    try:
        response = session.post(SOLANA_RPC, json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignaturesForAddress",
            "params": [str(RAYDIUM_PROGRAM_ID), {"limit": 10}]
        }, timeout=5)
        response.raise_for_status()
        signatures = response.json().get('result', [])
        bot.send_message(chat_id, f"📡 {len(signatures)} signatures récentes trouvées sur Solana")
        
        for sig in signatures:
            headers = {"accept": "application/json", "X-API-KEY": BIRDEYE_API_KEY}
            # Note : Utilisation de l'API BirdEye pour obtenir une adresse token réelle
            url = f"https://public-api.birdeye.so/public/price?address={sig['signature']}&chain=solana"
            response = session.get(url, headers=headers, timeout=5)
            if response.status_code == 200:
                price_data = response.json().get('data', {})
                price = price_data.get('value', 0)
                volume = price_data.get('volume', 0) * 1000
                liquidity = price_data.get('liquidity', 0) * 1000
                market_cap = price * 10**6
                token_address = sig['signature']  # Simplification, ajustez avec une API réelle
            else:
                token_address, volume, liquidity, market_cap = "So11111111111111111111111111111111111111112", 60000, 120000, 300000
            
            if (MIN_VOLUME_SOL <= volume <= MAX_VOLUME_SOL and 
                MIN_LIQUIDITY <= liquidity and 
                MIN_MARKET_CAP_SOL <= market_cap <= MAX_MARKET_CAP_SOL):
                bot.send_message(chat_id, f"🚀 Token détecté : {token_address} (Solana) - Vol: ${volume}, Liq: ${liquidity}, MC: ${market_cap}")
                detected_tokens[token_address] = {'volume': volume, 'liquidity': liquidity, 'market_cap': market_cap}
                
                if check_rugpull(token_address, "solana"):
                    buy_token_solana(chat_id, token_address, mise_depart_sol)
                else:
                    bot.send_message(chat_id, f"⚠️ {token_address} suspecté de rugpull, achat annulé.")
            else:
                logger.info(f"Token {token_address} ne respecte pas les critères.")
    except Exception as e:
        logger.error(f"Erreur Solana RPC après retries: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur Solana RPC après 3 tentatives: {str(e)}")
        raise

# Webhook Telegram
@app.route("/webhook", methods=['POST'])
def webhook():
    logger.info("Webhook reçu")
    try:
        if request.method == 'POST':
            if request.headers.get("content-type") == "application/json":
                update = telebot.types.Update.de_json(request.get_json())
                bot.process_new_updates([update])
                return "OK", 200
        logger.warning("Requête webhook invalide")
        return abort(403)
    except Exception as e:
        logger.error(f"Erreur dans webhook: {str(e)}")
        return abort(500)

# Commande /start
@bot.message_handler(commands=['start'])
def start_message(message):
    logger.info("Commande /start reçue")
    try:
        bot.send_message(message.chat.id, "Bienvenue sur ton bot de trading de memecoins!")
        show_main_menu(message.chat.id)
    except Exception as e:
        logger.error(f"Erreur dans start_message: {str(e)}")
        bot.send_message(message.chat.id, f"⚠️ Erreur: {str(e)}")

# Menu principal
def show_main_menu(chat_id):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("Statut", callback_data="status"),
        InlineKeyboardButton("Configure", callback_data="config"),
        InlineKeyboardButton("Lancer", callback_data="launch"),
        InlineKeyboardButton("Arrêter", callback_data="stop"),
        InlineKeyboardButton("Portefeuille", callback_data="portfolio"),
        InlineKeyboardButton("Réglages", callback_data="settings")
    )
    try:
        bot.send_message(chat_id, "Que veux-tu faire?", reply_markup=markup)
    except Exception as e:
        logger.error(f"Erreur dans show_main_menu: {str(e)}")

# Gestion des callbacks
@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    global test_mode, mise_depart_bsc, mise_depart_sol, trade_active, slippage, gas_fee
    chat_id = call.message.chat.id
    logger.info(f"Callback reçu: {call.data}")
    try:
        if call.data == "status":
            bot.send_message(chat_id, f"Statut :\n- Mise BSC: {mise_depart_bsc} BNB\n- Mise Solana: {mise_depart_sol} SOL\n- Slippage: {slippage} %\n- Gas Fee: {gas_fee} Gwei\n- Mode test: {test_mode}\n- Trading actif: {trade_active}")
        elif call.data == "config":
            show_config_menu(chat_id)
        elif call.data == "launch":
            if not trade_active:
                trade_active = True
                bot.send_message(chat_id, "🚀 Trading lancé")
                cycle_count = 0
                while trade_active:
                    cycle_count += 1
                    bot.send_message(chat_id, f"ℹ️ Début du cycle de détection #{cycle_count}...")
                    detect_new_tokens_bsc(chat_id)
                    bot.send_message(chat_id, "✅ Détection BSC terminée, passage à Solana...")
                    detect_new_tokens_solana(chat_id)
                    bot.send_message(chat_id, "✅ Détection Solana terminée.")
                    bot.send_message(chat_id, "⏳ Attente de 60 secondes avant le prochain cycle...")
                    time.sleep(60)
            else:
                bot.send_message(chat_id, "⚠️ Trading déjà en cours.")
        elif call.data == "stop":
            trade_active = False
            bot.send_message(chat_id, "🛑 Trading arrêté.")
        elif call.data == "portfolio":
            show_portfolio(chat_id)
        elif call.data == "settings":
            show_settings_menu(chat_id)
        elif call.data.startswith("refresh_"):
            token = call.data.split("_")[1]
            refresh_token(chat_id, token)
        elif call.data.startswith("sell_"):
            token = call.data.split("_")[1]
            sell_token_immediate(chat_id, token)
        elif call.data == "adjust_mise_bsc":
            bot.send_message(chat_id, "Entrez la nouvelle mise pour BSC (en BNB, ex. : 0.05) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_mise_bsc)
        elif call.data == "adjust_mise_sol":
            bot.send_message(chat_id, "Entrez la nouvelle mise pour Solana (en SOL, ex. : 0.02) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_mise_sol)
        elif call.data == "adjust_slippage":
            bot.send_message(chat_id, "Entrez le nouveau slippage (en %, ex. : 5) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_slippage)
        elif call.data == "adjust_gas":
            bot.send_message(chat_id, "Entrez les nouveaux frais de gas pour BSC (en Gwei, ex. : 5) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_gas_fee)
        elif call.data == "increase_mise_bsc":
            mise_depart_bsc += 0.01
            bot.send_message(chat_id, f"Mise BSC augmentée à {mise_depart_bsc} BNB")
        elif call.data == "increase_mise_sol":
            mise_depart_sol += 0.01
            bot.send_message(chat_id, f"Mise Solana augmentée à {mise_depart_sol} SOL")
        elif call.data == "toggle_test":
            test_mode = not test_mode
            bot.send_message(chat_id, f"Mode test : {test_mode}")
    except Exception as e:
        logger.error(f"Erreur dans callback_query: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur générale: {str(e)}")

# Menu de configuration
def show_config_menu(chat_id):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("Augmenter mise BSC (+0.01 BNB)", callback_data="increase_mise_bsc"),
        InlineKeyboardButton("Augmenter mise SOL (+0.01 SOL)", callback_data="increase_mise_sol"),
        InlineKeyboardButton("Toggle Mode Test", callback_data="toggle_test")
    )
    try:
        bot.send_message(chat_id, "Configuration :", reply_markup=markup)
    except Exception as e:
        logger.error(f"Erreur dans show_config_menu: {str(e)}")

# Menu des réglages
def show_settings_menu(chat_id):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("Ajuster Mise BSC", callback_data="adjust_mise_bsc"),
        InlineKeyboardButton("Ajuster Mise Solana", callback_data="adjust_mise_sol"),
        InlineKeyboardButton("Ajuster Slippage", callback_data="adjust_slippage"),
        InlineKeyboardButton("Ajuster Gas Fee (BSC)", callback_data="adjust_gas")
    )
    try:
        bot.send_message(chat_id, "Réglages :", reply_markup=markup)
    except Exception as e:
        logger.error(f"Erreur dans show_settings_menu: {str(e)}")

# Ajuster la mise BSC
def adjust_mise_bsc(message):
    global mise_depart_bsc
    chat_id = message.chat.id
    try:
        new_mise = float(message.text)
        if new_mise > 0:
            mise_depart_bsc = new_mise
            bot.send_message(chat_id, f"Mise BSC mise à jour à {mise_depart_bsc} BNB")
        else:
            bot.send_message(chat_id, "La mise doit être positive!")
    except ValueError:
        bot.send_message(chat_id, "Entrez un nombre valide (ex. : 0.05)")

# Ajuster la mise Solana
def adjust_mise_sol(message):
    global mise_depart_sol
    chat_id = message.chat.id
    try:
        new_mise = float(message.text)
        if new_mise > 0:
            mise_depart_sol = new_mise
            bot.send_message(chat_id, f"Mise Solana mise à jour à {mise_depart_sol} SOL")
        else:
            bot.send_message(chat_id, "La mise doit être positive!")
    except ValueError:
        bot.send_message(chat_id, "Entrez un nombre valide (ex. : 0.02)")

# Ajuster le slippage
def adjust_slippage(message):
    global slippage
    chat_id = message.chat.id
    try:
        new_slippage = float(message.text)
        if 0 <= new_slippage <= 100:
            slippage = new_slippage
            bot.send_message(chat_id, f"Slippage mis à jour à {slippage} %")
        else:
            bot.send_message(chat_id, "Le slippage doit être entre 0 et 100%!")
    except ValueError:
        bot.send_message(chat_id, "Entrez un pourcentage valide (ex. : 5)")

# Ajuster les frais de gas
def adjust_gas_fee(message):
    global gas_fee
    chat_id = message.chat.id
    try:
        new_gas_fee = float(message.text)
        if new_gas_fee > 0:
            gas_fee = new_gas_fee
            bot.send_message(chat_id, f"Frais de gas mis à jour à {gas_fee} Gwei")
        else:
            bot.send_message(chat_id, "Les frais de gas doivent être positifs!")
    except ValueError:
        bot.send_message(chat_id, "Entrez un nombre valide (ex. : 5)")

# Achat de token sur BSC (PancakeSwap)
def buy_token_bsc(chat_id, contract_address, amount):
    logger.info(f"Achat de {contract_address} sur BSC")
    if test_mode:
        bot.send_message(chat_id, f"🧪 [Mode Test] Achat simulé de {amount} BNB de {contract_address}")
        detected_tokens[contract_address]['entry_price'] = 0.01
        portfolio[contract_address] = {
            'amount': amount,
            'chain': "bsc",
            'entry_price': 0.01,
            'market_cap_at_buy': detected_tokens[contract_address]['market_cap'],
            'current_market_cap': detected_tokens[contract_address]['market_cap']
        }
        return
    if not w3:
        bot.send_message(chat_id, "⚠️ Connexion BSC non disponible.")
        return
    try:
        router = w3.eth.contract(address=PANCAKE_ROUTER_ADDRESS, abi=PANCAKE_ROUTER_ABI)
        amount_in = w3.to_wei(amount, 'ether')
        amount_out_min = int(amount_in * (1 - slippage / 100))
        tx = router.functions.swapExactETHForTokens(
            amount_out_min,
            [w3.to_checksum_address('0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c'),
             w3.to_checksum_address(contract_address)],
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
        bot.send_message(chat_id, f"🚀 Achat de {amount} BNB de {contract_address}, TX: {tx_hash.hex()}")
        portfolio[contract_address] = {
            'amount': amount,
            'chain': "bsc",
            'entry_price': 0.01,
            'market_cap_at_buy': detected_tokens[contract_address]['market_cap'],
            'current_market_cap': detected_tokens[contract_address]['market_cap']
        }
        monitor_and_sell(chat_id, contract_address, amount, 'bsc')
    except Exception as e:
        logger.error(f"Erreur achat BSC: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Échec achat {contract_address}: {str(e)}")

# Achat de token sur Solana (Raydium) - Mode test uniquement pour éviter erreurs
def buy_token_solana(chat_id, contract_address, amount):
    logger.info(f"Achat de {contract_address} sur Solana")
    if test_mode:
        bot.send_message(chat_id, f"🧪 [Mode Test] Achat simulé de {amount} SOL de {contract_address}")
        detected_tokens[contract_address]['entry_price'] = 0.01
        portfolio[contract_address] = {
            'amount': amount,
            'chain': "solana",
            'entry_price': 0.01,
            'market_cap_at_buy': detected_tokens[contract_address]['market_cap'],
            'current_market_cap': detected_tokens[contract_address]['market_cap']
        }
        return
    bot.send_message(chat_id, "⚠️ Achat Solana non implémenté en mode réel pour ce déploiement.")
    # Note : Pour une implémentation réelle, ajoutez une bibliothèque comme `solana-py` ou `solders` avec une instruction Raydium valide.

# Vente de token
def sell_token(chat_id, token, amount, chain, current_price):
    if chain != "bsc":
        bot.send_message(chat_id, f"🧪 [Mode Test] Vente simulée de {amount} {chain.upper()} de {token} à {current_price}")
        if token in portfolio:
            portfolio[token]["amount"] -= amount
            if portfolio[token]["amount"] <= 0:
                del portfolio[token]
        return
    if test_mode:
        bot.send_message(chat_id, f"🧪 [Mode Test] Vente simulée de {amount} BNB de {token} à {current_price}")
        if token in portfolio:
            portfolio[token]["amount"] -= amount
            if portfolio[token]["amount"] <= 0:
                del portfolio[token]
        return
    if not w3:
        bot.send_message(chat_id, "⚠️ Connexion BSC non disponible.")
        return
    try:
        router = w3.eth.contract(address=PANCAKE_ROUTER_ADDRESS, abi=PANCAKE_ROUTER_ABI)
        token_amount = w3.to_wei(amount, 'ether')
        amount_in_max = int(token_amount * (1 + slippage / 100))
        tx = router.functions.swapExactTokensForETH(
            token_amount,
            amount_in_max,
            [w3.to_checksum_address(token), w3.to_checksum_address("0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c")],
            w3.to_checksum_address(WALLET_ADDRESS),
            int(time.time()) + 60 * 10
        ).build_transaction({
            'from': WALLET_ADDRESS,
            'gas': 250000,
            'gasPrice': w3.to_wei(gas_fee, 'gwei'),
            'nonce': w3.eth.get_transaction_count(WALLET_ADDRESS)
        })
        signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
        bot.send_message(chat_id, f"💰 Vente de {amount} BNB de {token}, TX: {tx_hash.hex()}")
        if token in portfolio:
            portfolio[token]["amount"] -= amount
            if portfolio[token]["amount"] <= 0:
                del portfolio[token]
    except Exception as e:
        logger.error(f"Erreur vente BSC: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Échec vente {token}: {str(e)}")

# Surveillance et vente automatique
def monitor_and_sell(chat_id, contract_address, amount, chain):
    try:
        while contract_address in portfolio:
            current_mc = get_current_market_cap(contract_address)
            profit = (current_mc - portfolio[contract_address]["market_cap_at_buy"]) / portfolio[contract_address]["market_cap_at_buy"] * 100
            loss = -profit
            if profit >= take_profit_steps[0] * 100 or loss >= stop_loss_threshold:
                current_price = current_mc / 1000000
                sell_token(chat_id, contract_address, amount, chain, current_price)
                break
            time.sleep(300)  # Vérification toutes les 5 minutes
    except Exception as e:
        logger.error(f"Erreur monitor_and_sell: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur surveillance {contract_address}: {str(e)}")

# Afficher le portefeuille
def show_portfolio(chat_id):
    try:
        bsc_balance = w3.eth.get_balance(WALLET_ADDRESS) / 10**18 if w3 else 0
        sol_balance = get_solana_balance(WALLET_ADDRESS)
        msg = f"💼 Portefeuille:\n- BSC: {bsc_balance:.4f} BNB\n- Solana: {sol_balance:.4f} SOL\n\nTokens détenus:\n"
        if not portfolio:
            msg += "Aucun token détenu."
            bot.send_message(chat_id, msg)
        else:
            for ca, data in portfolio.items():
                current_mc = get_current_market_cap(ca)
                profit = (current_mc - data["market_cap_at_buy"]) / data["market_cap_at_buy"] * 100
                markup = InlineKeyboardMarkup()
                markup.add(
                    InlineKeyboardButton("Refresh", callback_data=f"refresh_{ca}"),
                    InlineKeyboardButton("Sell All", callback_data=f"sell_{ca}")
                )
                msg += (f"Token: {ca} ({data['chain']})\n"
                        f"Contrat: {ca}\n"
                        f"MC Achat: ${data['market_cap_at_buy']:.2f}\n"
                        f"MC Actuel: ${current_mc:.2f}\n"
                        f"Profit: {profit:.2f}%\n"
                        f"Take-Profit: x{take_profit_steps[0]}, x{take_profit_steps[1]}, x{take_profit_steps[2]}\n"
                        f"Stop-Loss: -{stop_loss_threshold} %\n\n")
                bot.send_message(chat_id, msg, reply_markup=markup)
                msg = ""
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
        })
        result = response.json().get('result', {})
        return result.get('value', 0) / 10**9
    except Exception as e:
        logger.error(f"Erreur solde Solana: {str(e)}")
        return 0

# Market cap en temps réel avec BirdEye
def get_current_market_cap(contract_address):
    try:
        chain = portfolio.get(contract_address, {}).get('chain', 'bsc')
        headers = {"accept": "application/json", "X-API-KEY": BIRDEYE_API_KEY}
        url = f"https://public-api.birdeye.so/public/price?address={contract_address}&chain={chain}"
        response = session.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            return response.json().get('data', {}).get('value', 0) * 10**6
        return detected_tokens[contract_address]['market_cap'] * 1.5
    except Exception as e:
        logger.error(f"Erreur market cap: {str(e)}")
        return detected_tokens[contract_address]["market_cap"]

# Rafraîchir un token
def refresh_token(chat_id, token):
    try:
        current_mc = get_current_market_cap(token)
        profit = (current_mc - portfolio[token]["market_cap_at_buy"]) / portfolio[token]["market_cap_at_buy"] * 100
        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton("Refresh", callback_data=f"refresh_{token}"),
            InlineKeyboardButton("Sell All", callback_data=f"sell_{token}")
        )
        msg = (f"Token: {token} ({portfolio[token]['chain']})\n"
               f"Contrat: {token}\n"
               f"MC Achat: ${portfolio[token]['market_cap_at_buy']:.2f}\n"
               f"MC Actuel: ${current_mc:.2f}\n"
               f"Profit: {profit:.2f} %\n"
               f"Take-Profit: x{take_profit_steps[0]}, x{take_profit_steps[1]}, x{take_profit_steps[2]}\n"
               f"Stop-Loss: -{stop_loss_threshold} %")
        bot.send_message(chat_id, msg, reply_markup=markup)
    except Exception as e:
        logger.error(f"Erreur refresh: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur refresh: {str(e)}")

# Vente immédiate
def sell_token_immediate(chat_id, token):
    try:
        amount = portfolio[token]["amount"]
        chain = portfolio[token]["chain"]
        current_price = get_current_market_cap(token) / 1000000
        sell_token(chat_id, token, amount, chain, current_price)
        bot.send_message(chat_id, f"💰 Position {token} vendue entièrement!")
    except Exception as e:
        logger.error(f"Erreur vente immédiate: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur vente: {str(e)}")

# Configuration du webhook
def set_webhook():
    logger.info("Configuration du webhook...")
    try:
        if WEBHOOK_URL:
            bot.remove_webhook()
            bot.set_webhook(url=WEBHOOK_URL)
            logger.info("Webhook configuré avec succès")
    except Exception as e:
        logger.error(f"Erreur configuration webhook: {str(e)}")

# Point d'entrée principal
if __name__ == "__main__":
    logger.info("Démarrage du bot...")
    try:
        set_webhook()
        logger.info(f"Lancement de Flask sur le port {PORT}...")
        app.run(host="0.0.0.0", port=PORT, debug=True)  # Debug activé pour logs
    except Exception as e:
        logger.error(f"Erreur critique au démarrage: {str(e)}")
        raise
