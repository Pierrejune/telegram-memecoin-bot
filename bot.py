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
import hashlib

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
BSC_SCAN_API_KEY = os.getenv("BSC_SCAN_API_KEY")
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
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
solana_keypair = Keypair.from_base58_string(SOLANA_WALLET_PRIVATE_KEY)
RAYDIUM_PROGRAM_ID = Pubkey.from_string("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSceAHj2")
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")

# Configuration de base
test_mode = True
mise_depart_bsc = 0.01  # BNB pour BSC
mise_depart_sol = 0.02  # SOL pour Solana
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

# APIs externes
BSC_SCAN_API_URL = "https://api.bscscan.com/api"
SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"

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

# Vérification BscScan
def is_valid_token_bscscan(contract_address):
    try:
        params = {'module': 'token', 'action': 'getTokenInfo', 'contractaddress': contract_address, 'apikey': BSC_SCAN_API_KEY}
        response = session.get(BSC_SCAN_API_URL, params=params, timeout=10)
        data = response.json()
        if data['status'] == '1' and float(data['result']['totalSupply']) >= 1000:
            return True
        return False
    except Exception as e:
        logger.error(f"Erreur BscScan: {str(e)}")
        return False

# Surveillance BscScan pour BSC
def detect_new_tokens_bsc(chat_id):
    global detected_tokens
    bot.send_message(chat_id, "🔍 Recherche de nouveaux tokens sur BscScan...")
    try:
        params = {
            'module': 'logs',
            'action': 'getLogs',
            'fromBlock': 'latest',
            'toBlock': 'latest',
            'topic0': '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef',  # Transfer event
            'apikey': BSC_SCAN_API_KEY
        }
        response = session.get(BSC_SCAN_API_URL, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data['status'] != '1':
            bot.send_message(chat_id, f"⚠️ Erreur BscScan: {data.get('message', 'Erreur inconnue')}")
            return
        logs = data['result'][:10]
        bot.send_message(chat_id, f"📡 {len(logs)} événements trouvés sur BscScan")
        for log in logs:
            ca = log['address']
            if ca in cache:
                continue
            liquidity = 150000
            volume = 100000
            market_cap = 500000
            price_change = 50

            if (MIN_VOLUME_BSC <= volume <= MAX_VOLUME_BSC and 
                liquidity >= MIN_LIQUIDITY and liquidity >= market_cap * MIN_LIQUIDITY_PCT and 
                MIN_PRICE_CHANGE <= price_change <= MAX_PRICE_CHANGE and 
                MIN_MARKET_CAP_BSC <= market_cap <= MAX_MARKET_CAP_BSC and 
                is_valid_token_tokensniffer(ca) and is_valid_token_bscscan(ca)):
                detected_tokens[ca] = {"status": "safe", "entry_price": None, "chain": "bsc", "market_cap": market_cap}
                bot.send_message(chat_id, f"🚀 Token détecté : {ca} (BSC) - Vol: ${volume}, Liq: ${liquidity}, MC: ${market_cap}")
                if trade_active and w3:
                    buy_token_bsc(chat_id, ca, mise_depart_bsc)
            else:
                bot.send_message(chat_id, f"❌ {ca} rejeté - Vol: ${volume}, Liq: ${liquidity}, MC: ${market_cap}, Change: {price_change}%")
            cache[ca] = True
    except requests.exceptions.RequestException as e:
        logger.error(f"Erreur BscScan HTTP: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur BscScan: {str(e)}")
    except Exception as e:
        logger.error(f"Erreur BscScan: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur BscScan inattendue: {str(e)}")

# Surveillance Solana pour Solana
def detect_new_tokens_solana(chat_id):
    global detected_tokens
    bot.send_message(chat_id, "🔍 Recherche de nouveaux tokens sur Solana...")
    try:
        response = session.post(SOLANA_RPC_URL, json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getProgramAccounts",
            "params": [
                str(TOKEN_PROGRAM_ID),
                {"encoding": "jsonParsed", "filters": [{"dataSize": 165}]}  # Taille des comptes token
            ]
        }, timeout=10)
        response.raise_for_status()
        data = response.json()
        accounts = data.get('result', [])[:10]
        bot.send_message(chat_id, f"📡 {len(accounts)} nouveaux tokens trouvés sur Solana")
        for account in accounts:
            ca = account['pubkey']
            if ca in cache:
                continue
            liquidity = 150000
            volume = 100000
            market_cap = 500000
            price_change = 50

            if (MIN_VOLUME_SOL <= volume <= MAX_VOLUME_SOL and 
                liquidity >= MIN_LIQUIDITY and liquidity >= market_cap * MIN_LIQUIDITY_PCT and 
                MIN_PRICE_CHANGE <= price_change <= MAX_PRICE_CHANGE and 
                MIN_MARKET_CAP_SOL <= market_cap <= MAX_MARKET_CAP_SOL):
                detected_tokens[ca] = {"status": "safe", "entry_price": None, "chain": "solana", "market_cap": market_cap}
                bot.send_message(chat_id, f"🚀 Token détecté : {ca} (Solana) - Vol: ${volume}, Liq: ${liquidity}, MC: ${market_cap}")
                if trade_active:
                    buy_token_solana(chat_id, ca, mise_depart_sol)
            else:
                bot.send_message(chat_id, f"❌ {ca} rejeté - Vol: ${volume}, Liq: ${liquidity}, MC: ${market_cap}, Change: {price_change}%")
            cache[ca] = True
    except requests.exceptions.RequestException as e:
        logger.error(f"Erreur Solana RPC HTTP: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur Solana RPC: {str(e)}")
    except Exception as e:
        logger.error(f"Erreur Solana RPC: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur Solana RPC inattendue: {str(e)}")

# Achat de token sur BSC (PancakeSwap)
def buy_token_bsc(chat_id, contract_address, amount):
    logger.info(f"Achat de {contract_address} sur BSC")
    if test_mode:
        bot.send_message(chat_id, f"🧪 [Mode Test] Achat simulé de {amount} BNB de {contract_address}")
        detected_tokens[contract_address]["entry_price"] = 0.01
        portfolio[contract_address] = {
            "amount": amount,
            "chain": "bsc",
            "entry_price": 0.01,
            "market_cap_at_buy": detected_tokens[contract_address]["market_cap"],
            "current_market_cap": detected_tokens[contract_address]["market_cap"]
        }
        monitor_and_sell(chat_id, contract_address, amount, "bsc")
        return
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
        bot.send_message(chat_id, f"🚀 Achat de {amount} BNB de {contract_address}, TX: {tx_hash.hex()}")
        portfolio[contract_address] = {
            "amount": amount,
            "chain": "bsc",
            "entry_price": 0.01,
            "market_cap_at_buy": detected_tokens[contract_address]["market_cap"],
            "current_market_cap": detected_tokens[contract_address]["market_cap"]
        }
        monitor_and_sell(chat_id, contract_address, amount, "bsc")
    except Exception as e:
        logger.error(f"Erreur achat BSC: {str(e)}")
        bot.send_message(chat_id, f"❌ Échec achat {contract_address}: {str(e)}")

# Achat de token sur Solana (Raydium)
def buy_token_solana(chat_id, contract_address, amount):
    logger.info(f"Achat de {contract_address} sur Solana")
    if test_mode:
        bot.send_message(chat_id, f"🧪 [Mode Test] Achat simulé de {amount} SOL de {contract_address}")
        detected_tokens[contract_address]["entry_price"] = 0.01
        portfolio[contract_address] = {
            "amount": amount,
            "chain": "solana",
            "entry_price": 0.01,
            "market_cap_at_buy": detected_tokens[contract_address]["market_cap"],
            "current_market_cap": detected_tokens[contract_address]["market_cap"]
        }
        monitor_and_sell(chat_id, contract_address, amount, "solana")
        return
    try:
        amount_in = int(amount * 10**9)
        tx = Transaction()
        instruction = Instruction(
            program_id=RAYDIUM_PROGRAM_ID,
            accounts=[
                {"pubkey": Pubkey.from_string(contract_address), "is_signer": False, "is_writable": True},
                {"pubkey": solana_keypair.pubkey(), "is_signer": True, "is_writable": True},
            ],
            data=bytes([0])  # À remplacer par instruction réelle
        )
        tx.add(instruction)
        tx.recent_blockhash = Pubkey.from_string(hashlib.sha256(str(int(time.time())).encode()).hexdigest()[:32])
        tx.sign(solana_keypair)
        response = session.post(SOLANA_RPC_URL, json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [tx.serialize().hex()]
        })
        tx_hash = response.json().get('result')
        bot.send_message(chat_id, f"🚀 Achat de {amount} SOL de {contract_address}, TX: {tx_hash}")
        portfolio[contract_address] = {
            "amount": amount,
            "chain": "solana",
            "entry_price": 0.01,
            "market_cap_at_buy": detected_tokens[contract_address]["market_cap"],
            "current_market_cap": detected_tokens[contract_address]["market_cap"]
        }
        monitor_and_sell(chat_id, contract_address, amount, "solana")
    except Exception as e:
        logger.error(f"Erreur achat Solana: {str(e)}")
        bot.send_message(chat_id, f"❌ Échec achat {contract_address}: {str(e)}")

# Surveillance et vente
def monitor_and_sell(chat_id, contract_address, amount, chain):
    entry_price = portfolio[contract_address]["entry_price"]
    market_cap = portfolio[contract_address]["market_cap_at_buy"]
    position_open = True
    sold_half = False
    while position_open and trade_active:
        try:
            current_price = entry_price * (1 + (market_cap / 1000000))
            profit_pct = ((current_price - entry_price) / entry_price) * 100
            if profit_pct >= take_profit_steps[0] * 100 and not sold_half:
                sell_token(chat_id, contract_address, amount / 2, chain, current_price)
                sold_half = True
            elif profit_pct >= take_profit_steps[1] * 100:
                sell_token(chat_id, contract_address, amount / 4, chain, current_price)
            elif profit_pct >= take_profit_steps[2] * 100:
                sell_token(chat_id, contract_address, amount / 4, chain, current_price)
                position_open = False
            elif profit_pct <= -stop_loss_threshold:
                sell_token(chat_id, contract_address, amount, chain, current_price)
                position_open = False
            time.sleep(10)
        except Exception as e:
            logger.error(f"Erreur surveillance: {str(e)}")
            bot.send_message(chat_id, f"⚠️ Erreur surveillance {contract_address}: {str(e)}")
            break

# Vente de token (BSC)
def sell_token(chat_id, contract_address, amount, chain, current_price):
    if chain != "bsc":
        bot.send_message(chat_id, f"🧪 [Mode Test] Vente simulée de {amount} {chain.upper()} de {contract_address} à {current_price}")
        if contract_address in portfolio:
            portfolio[contract_address]["amount"] -= amount
            if portfolio[contract_address]["amount"] <= 0:
                del portfolio[contract_address]
        return
    if test_mode:
        bot.send_message(chat_id, f"🧪 [Mode Test] Vente simulée de {amount} BNB de {contract_address} à {current_price}")
        if contract_address in portfolio:
            portfolio[contract_address]["amount"] -= amount
            if portfolio[contract_address]["amount"] <= 0:
                del portfolio[contract_address]
        return
    try:
        router = w3.eth.contract(address=PANCAKE_ROUTER_ADDRESS, abi=PANCAKE_ROUTER_ABI)
        token_amount = w3.to_wei(amount, 'ether')
        amount_in_max = int(token_amount * (1 + slippage / 100))
        tx = router.functions.swapExactTokensForETH(
            token_amount,
            amount_in_max,
            [w3.to_checksum_address(contract_address), w3.to_checksum_address("0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c")],
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
        bot.send_message(chat_id, f"💸 Vente de {amount} BNB de {contract_address}, TX: {tx_hash.hex()}")
        if contract_address in portfolio:
            portfolio[contract_address]["amount"] -= amount
            if portfolio[contract_address]["amount"] <= 0:
                del portfolio[contract_address]
    except Exception as e:
        logger.error(f"Erreur vente BSC: {str(e)}")
        bot.send_message(chat_id, f"❌ Échec vente {contract_address}: {str(e)}")

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
                        f"Stop-Loss: -{stop_loss_threshold}%\n\n")
                bot.send_message(chat_id, msg, reply_markup=markup)
                msg = ""
    except Exception as e:
        logger.error(f"Erreur portefeuille: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur portefeuille: {str(e)}")

# Solde Solana réel
def get_solana_balance(wallet_address):
    try:
        response = session.post(SOLANA_RPC_URL, json={
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

# Market cap en temps réel simulé
def get_current_market_cap(contract_address):
    try:
        return detected_tokens[contract_address]["market_cap"] * 1.5
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
               f"Profit: {profit:.2f}%\n"
               f"Take-Profit: x{take_profit_steps[0]}, x{take_profit_steps[1]}, x{take_profit_steps[2]}\n"
               f"Stop-Loss: -{stop_loss_threshold}%")
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
        bot.send_message(chat_id, f"✅ Position {token} vendue entièrement !")
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

# Point d’entrée principal
if __name__ == "__main__":
    logger.info("Démarrage du bot...")
    try:
        set_webhook()
        logger.info(f"Lancement de Flask sur le port {PORT}...")
        app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
    except Exception as e:
        logger.error(f"Erreur critique au démarrage: {str(e)}")
        raise
