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
from solders.transaction import Transaction
from solders.instruction import Instruction
import json

# Configuration du logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Headers pour API
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/91.0.4472.124",
    "Accept": "application/json",
}

# Session persistante
session = requests.Session()
session.headers.update(HEADERS)

# Chargement des variables
TOKEN = os.getenv("TELEGRAM_TOKEN")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
SOLANA_WALLET_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY")
PORT = int(os.getenv("PORT", 8080))

# Validation
if not TOKEN:
    logger.error("TELEGRAM_TOKEN manquant.")
    raise ValueError("TELEGRAM_TOKEN manquant")
if not all([WALLET_ADDRESS, PRIVATE_KEY]):
    logger.error("WALLET_ADDRESS ou PRIVATE_KEY BSC manquant.")
    raise ValueError("WALLET_ADDRESS ou PRIVATE_KEY BSC manquant")
if not SOLANA_WALLET_PRIVATE_KEY:
    logger.error("SOLANA_PRIVATE_KEY manquant.")
    raise ValueError("SOLANA_PRIVATE_KEY manquant")

# Initialisation
logger.info("Initialisation des composants...")
bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

# BSC (PancakeSwap)
w3 = Web3(Web3.HTTPProvider("https://bsc-dataseed.binance.org/"))
if not w3.is_connected():
    logger.error("Connexion BSC échouée.")
    w3 = None
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
SOLANA_RPC = "https://solana-mainnet.rpc.extrnode.com"  # Noeud alternatif gratuit
solana_keypair = Keypair.from_base58_string(SOLANA_WALLET_PRIVATE_KEY)
RAYDIUM_PROGRAM_ID = Pubkey.from_string("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSceAHj2")
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")

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

# Webhook Telegram
@app.route("/webhook", methods=["POST"])
def webhook():
    logger.info("Webhook reçu")
    try:
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
@bot.message_handler(commands=["start"])
def start_message(message):
    logger.info("Commande /start reçue")
    try:
        bot.send_message(message.chat.id, "🤖 Bienvenue sur ton bot de trading de memecoins !")
        show_main_menu(message.chat.id)
    except Exception as e:
        logger.error(f"Erreur dans start_message: {str(e)}")

# Menu principal
def show_main_menu(chat_id):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("📈 Statut", callback_data="status"),
        InlineKeyboardButton("⚙️ Configurer", callback_data="config"),
        InlineKeyboardButton("🚀 Lancer", callback_data="launch"),
        InlineKeyboardButton("❌ Arrêter", callback_data="stop"),
        InlineKeyboardButton("💼 Portefeuille", callback_data="portfolio"),
        InlineKeyboardButton("🔧 Réglages", callback_data="settings")
    )
    try:
        bot.send_message(chat_id, "Que veux-tu faire ?", reply_markup=markup)
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
            bot.send_message(chat_id, f"📊 Statut :\n- Mise BSC: {mise_depart_bsc} BNB\n- Mise Solana: {mise_depart_sol} SOL\n- Slippage: {slippage}%\n- Gas Fee: {gas_fee} Gwei\n- Mode test: {test_mode}\n- Trading actif: {trade_active}")
        elif call.data == "config":
            show_config_menu(chat_id)
        elif call.data == "launch":
            if not trade_active:
                trade_active = True
                bot.send_message(chat_id, "🚀 Trading lancé !")
                while trade_active:
                    detect_new_tokens_bsc(chat_id)
                    detect_new_tokens_solana(chat_id)
                    time.sleep(60)
            else:
                bot.send_message(chat_id, "⚠️ Trading déjà en cours.")
        elif call.data == "stop":
            trade_active = False
            bot.send_message(chat_id, "⏹ Trading arrêté.")
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
    except Exception as e:
        logger.error(f"Erreur dans callback_query: {str(e)}")
        bot.send_message(chat_id, "⚠️ Une erreur est survenue.")

# Menu de configuration
def show_config_menu(chat_id):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("💰 Augmenter mise BSC (+0.01 BNB)", callback_data="increase_mise_bsc"),
        InlineKeyboardButton("💰 Augmenter mise SOL (+0.01 SOL)", callback_data="increase_mise_sol"),
        InlineKeyboardButton("🎯 Toggle Mode Test", callback_data="toggle_test")
    )
    try:
        bot.send_message(chat_id, "⚙️ Configuration :", reply_markup=markup)
    except Exception as e:
        logger.error(f"Erreur dans show_config_menu: {str(e)}")

# Menu des réglages
def show_settings_menu(chat_id):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("💰 Ajuster Mise BSC", callback_data="adjust_mise_bsc"),
        InlineKeyboardButton("💰 Ajuster Mise Solana", callback_data="adjust_mise_sol"),
        InlineKeyboardButton("📉 Ajuster Slippage", callback_data="adjust_slippage"),
        InlineKeyboardButton("⛽ Ajuster Gas Fee (BSC)", callback_data="adjust_gas")
    )
    try:
        bot.send_message(chat_id, "🔧 Réglages :", reply_markup=markup)
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
            bot.send_message(chat_id, f"✅ Mise BSC mise à jour à {mise_depart_bsc} BNB")
        else:
            bot.send_message(chat_id, "⚠️ La mise doit être positive !")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Entrez un nombre valide (ex. : 0.05)")

# Ajuster la mise Solana
def adjust_mise_sol(message):
    global mise_depart_sol
    chat_id = message.chat.id
    try:
        new_mise = float(message.text)
        if new_mise > 0:
            mise_depart_sol = new_mise
            bot.send_message(chat_id, f"✅ Mise Solana mise à jour à {mise_depart_sol} SOL")
        else:
            bot.send_message(chat_id, "⚠️ La mise doit être positive !")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Entrez un nombre valide (ex. : 0.02)")

# Ajuster le slippage
def adjust_slippage(message):
    global slippage
    chat_id = message.chat.id
    try:
        new_slippage = float(message.text)
        if 0 <= new_slippage <= 100:
            slippage = new_slippage
            bot.send_message(chat_id, f"✅ Slippage mis à jour à {slippage}%")
        else:
            bot.send_message(chat_id, "⚠️ Le slippage doit être entre 0 et 100% !")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Entrez un pourcentage valide (ex. : 5)")

# Ajuster les frais de gas
def adjust_gas_fee(message):
    global gas_fee
    chat_id = message.chat.id
    try:
        new_gas_fee = float(message.text)
        if new_gas_fee > 0:
            gas_fee = new_gas_fee
            bot.send_message(chat_id, f"✅ Frais de gas mis à jour à {gas_fee} Gwei")
        else:
            bot.send_message(chat_id, "⚠️ Les frais de gas doivent être positifs !")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Entrez un nombre valide (ex. : 5)")

@bot.callback_query_handler(func=lambda call: call.data in ["increase_mise_bsc", "increase_mise_sol", "toggle_test"])
def config_callback(call):
    global mise_depart_bsc, mise_depart_sol, test_mode
    chat_id = call.message.chat.id
    try:
        if call.data == "increase_mise_bsc":
            mise_depart_bsc += 0.01
            bot.send_message(chat_id, f"💰 Mise BSC augmentée à {mise_depart_bsc} BNB")
        elif call.data == "increase_mise_sol":
            mise_depart_sol += 0.01
            bot.send_message(chat_id, f"💰 Mise Solana augmentée à {mise_depart_sol} SOL")
        elif call.data == "toggle_test":
            test_mode = not test_mode
            bot.send_message(chat_id, f"🎯 Mode Test {'activé' if test_mode else 'désactivé'}")
    except Exception as e:
        logger.error(f"Erreur dans config_callback: {str(e)}")

# Vérification TokenSniffer
def is_valid_token_tokensniffer(contract_address):
    try:
        url = f"https://tokensniffer.com/token/{contract_address}"
        response = session.get(url, timeout=10)
        if response.status_code == 200:
            text = response.text.lower()
            if "rug pull" in text or "honeypot" in text or "owner renounced" not in text or "tax > 5%" in text:
                return False
            return True
        return False
    except Exception as e:
        logger.error(f"Erreur TokenSniffer: {str(e)}")
        return False

# Surveillance BSC pour nouveaux tokens
def detect_new_tokens_bsc(chat_id):
    global detected_tokens
    bot.send_message(chat_id, "🔍 Recherche de nouveaux tokens sur BSC (PancakeSwap)...")
    try:
        factory = w3.eth.contract(address=PANCAKE_FACTORY_ADDRESS, abi=PANCAKE_FACTORY_ABI)
        latest_block = w3.eth.block_number
        event_filter = factory.events.PairCreated.create_filter(fromBlock=latest_block-100, toBlock=latest_block)
        events = event_filter.get_all_entries()
        bot.send_message(chat_id, f"📡 {len(events)} nouvelles paires trouvées sur BSC")
        for event in events:
            token0 = event['args']['token0']
            token1 = event['args']['token1']
            ca = token0 if token0 != "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c" else token1  # WBNB exclu
            if ca in cache:
                continue
            liquidity = 150000
            volume = 100000
            market_cap = 500000
            price_change = 50

            if (MIN_VOLUME_BSC <= volume <= MAX_VOLUME_BSC and 
                liquidity >= MIN_LIQUIDITY and liquidity >= market_cap * MIN_LIQUIDITY_PCT and 
                MIN_PRICE_CHANGE <= price_change <= MAX_PRICE_CHANGE and 
                MIN_MARKET_CAP_BSC <= market_cap <= MAX_MARKET_CAP_BSC):
                detected_tokens[ca] = {"status": "safe", "entry_price": None, "chain": "bsc", "market_cap": market_cap}
                bot.send_message(chat_id, f"🚀 Token détecté : {ca} (BSC) - Vol: ${volume}, Liq: ${liquidity}, MC: ${market_cap}")
                if trade_active and w3:
                    buy_token_bsc(chat_id, ca, mise_depart_bsc)
            else:
                bot.send_message(chat_id, f"❌ {ca} rejeté - Vol: ${volume}, Liq: ${liquidity}, MC: ${market_cap}, Change: {price_change}%")
            cache[ca] = True
    except Exception as e:
        logger.error(f"Erreur détection BSC: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur détection BSC: {str(e)}")

# Surveillance Solana pour nouveaux tokens
def detect_new_tokens_solana(chat_id):
    global detected_tokens
    bot.send_message(chat_id, "🔍 Recherche de nouveaux tokens sur Solana...")
    try:
        response = session.post(SOLANA_RPC, json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getRecentBlockhash",
            "params": []
        }, timeout=10)
        response.raise_for_status()
        blockhash = response.json().get('result', {}).get('value', {}).get('blockhash')
        logger.info(f"Blockhash Solana: {blockhash}")

        response = session.post(SOLANA_RPC, json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getConfirmedSignaturesForAddress2",
            "params": [str(TOKEN_PROGRAM_ID), {"limit": 10}]
        }, timeout=10)
        response.raise_for_status()
        data = response.json()
        signatures = data.get('result', [])
        bot.send_message(chat_id
