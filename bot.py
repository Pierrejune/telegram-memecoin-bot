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
from solders.system_program import TransferParams, transfer
from solders.signature import Signature
from solders.instruction import Instruction
import json
import base58

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

# Chargement des variables depuis Cloud Run avec noms exacts
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY")
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")  # Corrigé pour correspondre à ton Cloud Run
BSC_SCAN_API_KEY = os.getenv("BSC_SCAN_API_KEY")
PORT = int(os.getenv("PORT", 8080))

# Headers spécifiques pour APIs
BIRDEYE_HEADERS = {"X-API-KEY": BIRDEYE_API_KEY}

# Validation des variables essentielles
logger.info("Validation des variables...")
missing_vars = []
required_vars = {
    "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
    "WALLET_ADDRESS": WALLET_ADDRESS,
    "PRIVATE_KEY": PRIVATE_KEY,
    "SOLANA_PRIVATE_KEY": SOLANA_PRIVATE_KEY,
    "WEBHOOK_URL": WEBHOOK_URL,
    "BIRDEYE_API_KEY": BIRDEYE_API_KEY,
    "BSC_SCAN_API_KEY": BSC_SCAN_API_KEY
}

for var_name, var_value in required_vars.items():
    if not var_value:
        missing_vars.append(var_name)

if missing_vars:
    logger.error(f"Variables d'environnement manquantes : {missing_vars}. Démarrage impossible.")
    raise ValueError(f"Variables d'environnement manquantes : {missing_vars}")
else:
    logger.info("Toutes les variables d'environnement sont présentes.")

# Initialisation
logger.info("Initialisation des composants...")
bot = telebot.TeleBot(TELEGRAM_TOKEN)
app = Flask(__name__)

# BSC (PancakeSwap)
logger.info("Connexion à BSC...")
w3 = Web3(Web3.HTTPProvider("https://bsc-dataseed.binance.org/"))
if not w3.is_connected():
    logger.error("Connexion BSC échouée.")
    raise ConnectionError("Connexion BSC échouée")
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
logger.info("Connexion à Solana...")
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
solana_keypair = Keypair.from_base58_string(SOLANA_PRIVATE_KEY)
RAYDIUM_PROGRAM_ID = Pubkey.from_string("675kPX9MHTjS2zt1qfr1NYHuzeLXiQM9H24wFSeeAHj2")
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")

# Vérification de Solana au démarrage
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

# Configuration de base
mise_depart_bsc = 0.01  # BNB
mise_depart_sol = 0.02  # SOL
slippage = 5  # Pourcentage
gas_fee = 5  # Gwei
stop_loss_threshold = 30  # Pourcentage
take_profit_steps = [2, 3, 5]  # Multiplicateurs
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
            headers=BIRDEYE_HEADERS
        )
        data = response.json()['data']
        top_holders_pct = sum(h['percent'] for h in data.get('topHolders', [])) if data.get('topHolders') else 0
        is_safe = top_holders_pct <= MAX_HOLDER_PCT and data.get('liquidity', 0) >= MIN_LIQUIDITY
        return is_safe
    except Exception as e:
        logger.error(f"Erreur vérification Solana: {str(e)}")
        return False

# Webhook Telegram
@app.route("/webhook", methods=['POST'])
def webhook():
    logger.info("Webhook reçu")
    try:
        if request.method == 'POST' and request.headers.get("content-type") == "application/json":
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
    global mise_depart_bsc, mise_depart_sol, trade_active, slippage, gas_fee
    chat_id = call.message.chat.id
    logger.info(f"Callback reçu: {call.data}")
    try:
        if call.data == "status":
            bot.send_message(chat_id, (
                f"Statut :\n"
                f"- Mise BSC: {mise_depart_bsc} BNB\n"
                f"- Mise Solana: {mise_depart_sol} SOL\n"
                f"- Slippage: {slippage} %\n"
                f"- Gas Fee: {gas_fee} Gwei\n"
                f"- Trading actif: {trade_active}"
            ))
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
                    detect_new_tokens_solana(chat_id)
                    bot.send_message(chat_id, "⏳ Attente de 60 secondes avant le prochain cycle...")
                    time.sleep(60)
            else:
                bot.send_message(chat_id, "⚠️ Trading déjà en cours.")
        elif call.data == "stop":
            trade_active = False
            bot.send_message(chat_id, "✅ Trading arrêté.")
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
        bot.send_message(chat_id, f"⚠️ Erreur générale: {str(e)}")

# Menu de configuration
def show_config_menu(chat_id):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("Augmenter mise BSC (+0.01 BNB)", callback_data="increase_mise_bsc"),
        InlineKeyboardButton("Augmenter mise SOL (+0.01 SOL)", callback_data="increase_mise_sol")
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

# Ajustements
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

def adjust_slippage(message):
    global slippage
    chat_id = message.chat.id
    try:
        new_slippage = float(message.text)
        if 0 <= new_slippage <= 100:
            slippage = new_slippage
            bot.send_message(chat_id, f"✅ Slippage mis à jour à {slippage} %")
        else:
            bot.send_message(chat_id, "⚠️ Le slippage doit être entre 0 et 100% !")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Entrez un pourcentage valide (ex. : 5)")

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

# Détection BSC avec BSCScan
def detect_new_tokens_bsc(chat_id):
    logger.info("Recherche de nouveaux tokens sur BSC (PancakeSwap via BSCScan)...")
    try:
        response = session.get(
            f"https://api.bscscan.com/api?module=logs&action=getLogs&fromBlock=latest&toBlock=latest&address={PANCAKE_FACTORY_ADDRESS}&topic0=0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9&apikey={BSC_SCAN_API_KEY}"
        )
        response.raise_for_status()
        events = response.json()['result']
        bot.send_message(chat_id, f"📡 {len(events)} nouvelles paires détectées sur BSC")
        for event in events[:10]:  # Limite à 10 pour éviter surcharge
            token0 = '0x' + event['topics'][1][-40:]
            token1 = '0x' + event['topics'][2][-40:]
            pair_address = '0x' + event['data'][-40:]
            token_address = token0 if token1 == "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c" else token1  # WBNB
            if token_address == "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c":
                continue  # Ignore si pas un token
            # Récupérer données via Web3
            pair_contract = w3.eth.contract(address=pair_address, abi=[{"constant": True, "inputs": [], "name": "getReserves", "outputs": [{"name": "", "type": "uint112"}, {"name": "", "type": "uint112"}, {"name": "", "type": "uint32"}], "payable": False, "stateMutability": "view", "type": "function"}])
            reserves = pair_contract.functions.getReserves().call()
            liquidity = reserves[0] / 10**18 if token0 == "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c" else reserves[1] / 10**18  # BNB
            token_contract = w3.eth.contract(address=token_address, abi=[{"constant": True, "inputs": [], "name": "totalSupply", "outputs": [{"name": "", "type": "uint256"}], "payable": False, "stateMutability": "view", "type": "function"}])
            supply = token_contract.functions.totalSupply().call() / 10**18
            price = liquidity / supply if supply > 0 else 0
            market_cap = price * supply
            volume_response = session.get(f"https://api.bscscan.com/api?module=account&action=tokenbalance&contractaddress={token_address}&address={pair_address}&tag=latest&apikey={BSC_SCAN_API_KEY}")
            volume = float(volume_response.json()['result']) / 10**18 * price  # Approximation
            if (MIN_VOLUME_BSC <= volume <= MAX_VOLUME_BSC and
                liquidity >= MIN_LIQUIDITY and
                MIN_MARKET_CAP_BSC <= market_cap <= MAX_MARKET_CAP_BSC and
                is_safe_token_bsc(token_address)):
                bot.send_message(chat_id, (
                    f"🚀 Token détecté : {token_address} (BSC) - "
                    f"Vol: ${volume:.2f}, Liq: ${liquidity:.2f}, MC: ${market_cap:.2f}"
                ))
                detected_tokens[token_address] = {
                    'address': token_address,
                    'volume': volume,
                    'liquidity': liquidity,
                    'market_cap': market_cap,
                    'supply': supply
                }
                buy_token_bsc(chat_id, token_address, mise_depart_bsc)
            else:
                bot.send_message(chat_id, f"⚠️ Token {token_address} rejeté (critères ou sécurité non respectés)")
    except Exception as e:
        logger.error(f"Erreur détection BSC: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur lors de la détection BSC: {str(e)}")
    bot.send_message(chat_id, "✅ Détection BSC terminée, passage à Solana...")

# Détection Solana avec Birdeye
def detect_new_tokens_solana(chat_id):
    logger.info("Recherche de nouveaux tokens sur Solana...")
    attempts = 0
    max_attempts = 3
    while attempts < max_attempts:
        try:
            response = session.get(
                "https://public-api.birdeye.so/public/tokenlist?sort_by=volume&sort_type=desc&limit=10",
                headers=BIRDEYE_HEADERS
            )
            response.raise_for_status()
            tokens = response.json()['data']['tokens']
            bot.send_message(chat_id, f"📡 {len(tokens)} nouveaux tokens trouvés sur Solana")
            for token in tokens:
                token_address = token['address']
                volume = float(token['volume'])
                liquidity = float(token['liquidity'])
                market_cap = float(token['marketCap'])
                supply = float(token['supply'])
                if (MIN_VOLUME_SOL <= volume <= MAX_VOLUME_SOL and
                    liquidity >= MIN_LIQUIDITY and
                    MIN_MARKET_CAP_SOL <= market_cap <= MAX_MARKET_CAP_SOL and
                    is_safe_token_solana(token_address)):
                    bot.send_message(chat_id, (
                        f"🚀 Token détecté : {token_address} (Solana) - "
                        f"Vol: ${volume:.2f}, Liq: ${liquidity:.2f}, MC: ${market_cap:.2f}"
                    ))
                    detected_tokens[token_address] = {
                        'address': token_address,
                        'volume': volume,
                        'liquidity': liquidity,
                        'market_cap': market_cap,
                        'supply': supply
                    }
                    buy_token_solana(chat_id, token_address, mise_depart_sol)
                else:
                    bot.send_message(chat_id, f"⚠️ Token {token_address} rejeté (critères ou sécurité non respectés)")
            break
        except requests.exceptions.HTTPError as e:
            attempts += 1
            if e.response.status_code == 429:
                bot.send_message(chat_id, f"⚠️ Trop de requêtes, attente {5 * attempts}s ({attempts}/{max_attempts})")
                time.sleep(5 * attempts)
            else:
                bot.send_message(chat_id, f"⚠️ Erreur Birdeye: {str(e)}")
                break
        except Exception as e:
            bot.send_message(chat_id, f"⚠️ Erreur Solana: {str(e)}")
            break
    else:
        bot.send_message(chat_id, f"⚠️ Erreur après {max_attempts} tentatives: 429 Too Many Requests")
    bot.send_message(chat_id, "✅ Détection Solana terminée.")

# Achat BSC
def buy_token_bsc(chat_id, contract_address, amount):
    logger.info(f"Achat de {contract_address} sur BSC")
    try:
        router = w3.eth.contract(address=PANCAKE_ROUTER_ADDRESS, abi=PANCAKE_ROUTER_ABI)
        amount_in = w3.to_wei(amount, 'ether')
        amount_out_min = int(amount_in * (1 - slippage / 100))
        tx = router.functions.swapExactETHForTokens(
            amount_out_min,
            [w3.to_checksum_address('0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c'), w3.to_checksum_address(contract_address)],
            w3.to_checksum_address(WALLET_ADDRESS),
            int(time.time()) + 60 * 10
        ).build_transaction({
            'from': WALLET_ADDRESS, 'value': amount_in, 'gas': 250000,
            'gasPrice': w3.to_wei(gas_fee, 'gwei'), 'nonce': w3.eth.get_transaction_count(WALLET_ADDRESS)
        })
        signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
        bot.send_message(chat_id, f"🚀 Achat de {amount} BNB de {contract_address}, TX: {tx_hash.hex()}")
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
        if receipt.status == 1:
            entry_price = amount / (int.from_bytes(receipt.logs[0].data, 'big') / 10**18)  # Simplifié
            portfolio[contract_address] = {
                'amount': amount, 'chain': 'bsc', 'entry_price': entry_price,
                'market_cap_at_buy': detected_tokens[contract_address]['market_cap'],
                'current_market_cap': detected_tokens[contract_address]['market_cap']
            }
            monitor_and_sell(chat_id, contract_address, amount, 'bsc')
        else:
            bot.send_message(chat_id, f"⚠️ Échec transaction: {tx_hash.hex()}")
    except Exception as e:
        logger.error(f"Erreur achat BSC: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Échec achat {contract_address}: {str(e)}")

# Achat Solana avec Raydium (simplifié)
def buy_token_solana(chat_id, contract_address, amount):
    logger.info(f"Achat de {contract_address} sur Solana")
    try:
        amount_in = int(amount * 10**9)  # Lamports (SOL)
        response = session.post(SOLANA_RPC, json={
            "jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash",
            "params": [{"commitment": "finalized"}]
        })
        blockhash = response.json()['result']['value']['blockhash']
        tx = Transaction()
        instruction = Instruction(
            program_id=RAYDIUM_PROGRAM_ID,
            accounts=[
                {"pubkey": solana_keypair.pubkey(), "is_signer": True, "is_writable": True},
                {"pubkey": Pubkey.from_string(contract_address), "is_signer": False, "is_writable": True},
                {"pubkey": TOKEN_PROGRAM_ID, "is_signer": False, "is_writable": False},
            ],
            data=bytes([2]) + amount_in.to_bytes(8, 'little')  # Instruction basique
        )
        tx.add(instruction)
        tx.recent_blockhash = Pubkey.from_string(blockhash)
        tx.sign(solana_keypair)
        tx_hash = session.post(SOLANA_RPC, json={
            "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
            "params": [base58.b58encode(tx.serialize()).decode('utf-8')]
        }).json()['result']
        bot.send_message(chat_id, f"🚀 Achat de {amount} SOL de {contract_address}, TX: {tx_hash}")
        time.sleep(5)
        entry_price = amount / (detected_tokens[contract_address]['supply'] * get_current_market_cap(contract_address) / detected_tokens[contract_address]['supply'])
        portfolio[contract_address] = {
            'amount': amount, 'chain': 'solana', 'entry_price': entry_price,
            'market_cap_at_buy': detected_tokens[contract_address]['market_cap'],
            'current_market_cap': detected_tokens[contract_address]['market_cap']
        }
        monitor_and_sell(chat_id, contract_address, amount, 'solana')
    except Exception as e:
        logger.error(f"Erreur achat Solana: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Échec achat {contract_address}: {str(e)}")

# Vente
def sell_token(chat_id, contract_address, amount, chain, current_price):
    if chain == "solana":
        try:
            amount_out = int(amount * 10**9)
            response = session.post(SOLANA_RPC, json={
                "jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash",
                "params": [{"commitment": "finalized"}]
            })
            blockhash = response.json()['result']['value']['blockhash']
            tx = Transaction()
            instruction = Instruction(
                program_id=RAYDIUM_PROGRAM_ID,
                accounts=[
                    {"pubkey": solana_keypair.pubkey(), "is_signer": True, "is_writable": True},
                    {"pubkey": Pubkey.from_string(contract_address), "is_signer": False, "is_writable": True},
                    {"pubkey": TOKEN_PROGRAM_ID, "is_signer": False, "is_writable": False},
                ],
                data=bytes([3]) + amount_out.to_bytes(8, 'little')
            )
            tx.add(instruction)
            tx.recent_blockhash = Pubkey.from_string(blockhash)
            tx.sign(solana_keypair)
            tx_hash = session.post(SOLANA_RPC, json={
                "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                "params": [base58.b58encode(tx.serialize()).decode('utf-8')]
            }).json()['result']
            bot.send_message(chat_id, f"✅ Vente de {amount} SOL de {contract_address}, TX: {tx_hash}")
            if contract_address in portfolio:
                portfolio[contract_address]["amount"] -= amount
                if portfolio[contract_address]["amount"] <= 0:
                    del portfolio[contract_address]
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
                int(time.time()) + 60 * 10
            ).build_transaction({
                'from': WALLET_ADDRESS, 'gas': 250000,
                'gasPrice': w3.to_wei(gas_fee, 'gwei'), 'nonce': w3.eth.get_transaction_count(WALLET_ADDRESS)
            })
            signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
            tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
            bot.send_message(chat_id, f"✅ Vente de {amount} BNB de {contract_address}, TX: {tx_hash.hex()}")
            if contract_address in portfolio:
                portfolio[contract_address]["amount"] -= amount
                if portfolio[contract_address]["amount"] <= 0:
                    del portfolio[contract_address]
        except Exception as e:
            logger.error(f"Erreur vente BSC: {str(e)}")
            bot.send_message(chat_id, f"⚠️ Échec vente {contract_address}: {str(e)}")

# Surveillance et vente automatique
def monitor_and_sell(chat_id, contract_address, amount, chain):
    logger.info(f"Surveillance de {contract_address} ({chain})")
    while contract_address in portfolio:
        try:
            current_mc = get_current_market_cap(contract_address)
            portfolio[contract_address]['current_market_cap'] = current_mc
            profit_pct = (current_mc - portfolio[contract_address]['market_cap_at_buy']) / portfolio[contract_address]['market_cap_at_buy'] * 100
            loss_pct = -profit_pct if profit_pct < 0 else 0
            
            if profit_pct >= take_profit_steps[0] * 100:
                sell_amount = amount / 3
                sell_token(chat_id, contract_address, sell_amount, chain, current_mc / detected_tokens[contract_address]['supply'])
                if profit_pct >= take_profit_steps[1] * 100:
                    sell_token(chat_id, contract_address, sell_amount, chain, current_mc / detected_tokens[contract_address]['supply'])
                if profit_pct >= take_profit_steps[2] * 100:
                    sell_token(chat_id, contract_address, amount - 2 * sell_amount, chain, current_mc / detected_tokens[contract_address]['supply'])
                    break
            elif loss_pct >= stop_loss_threshold:
                sell_token(chat_id, contract_address, amount, chain, current_mc / detected_tokens[contract_address]['supply'])
                bot.send_message(chat_id, f"🛑 Stop-Loss déclenché pour {contract_address}")
                break
            time.sleep(60)
        except Exception as e:
            logger.error(f"Erreur surveillance {contract_address}: {str(e)}")
            bot.send_message(chat_id, f"⚠️ Erreur surveillance {contract_address}: {str(e)}")

# Afficher le portefeuille
def show_portfolio(chat_id):
    try:
        bsc_balance = w3.eth.get_balance(WALLET_ADDRESS) / 10**18 if w3 else 0
        sol_balance = get_solana_balance(WALLET_ADDRESS)
        msg = f"Portefeuille:\n- BSC: {bsc_balance:.4f} BNB\n- Solana: {sol_balance:.4f} SOL\n\nTokens détenus:\n"
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
                msg += (
                    f"Token: {ca} ({data['chain']})\n"
                    f"Contrat: {ca}\n"
                    f"MC Achat: ${data['market_cap_at_buy']:.2f}\n"
                    f"MC Actuel: ${current_mc:.2f}\n"
                    f"Profit: {profit:.2f}%\n"
                    f"Take-Profit: x{take_profit_steps[0]}, x{take_profit_steps[1]}, x{take_profit_steps[2]}\n"
                    f"Stop-Loss: -{stop_loss_threshold}%\n\n"
                )
            bot.send_message(chat_id, msg, reply_markup=markup)
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

# Market cap en temps réel avec BSCScan et Birdeye
def get_current_market_cap(contract_address):
    try:
        if contract_address in portfolio and portfolio[contract_address]['chain'] == 'solana':
            response = session.get(
                f"https://public-api.birdeye.so/public/price?address={contract_address}",
                headers=BIRDEYE_HEADERS
            )
            price = response.json()['data']['value']
            supply = detected_tokens[contract_address]['supply']
            return price * supply
        else:  # BSC
            token_contract = w3.eth.contract(address=contract_address, abi=[{"constant": True, "inputs": [], "name": "totalSupply", "outputs": [{"name": "", "type": "uint256"}], "payable": False, "stateMutability": "view", "type": "function"}])
            supply = token_contract.functions.totalSupply().call() / 10**18
            pair_address = detected_tokens[contract_address]['address']  # Simplifié, à ajuster avec pair réel
            pair_contract = w3.eth.contract(address=pair_address, abi=[{"constant": True, "inputs": [], "name": "getReserves", "outputs": [{"name": "", "type": "uint112"}, {"name": "", "type": "uint112"}, {"name": "", "type": "uint32"}], "payable": False, "stateMutability": "view", "type": "function"}])
            reserves = pair_contract.functions.getReserves().call()
            price = (reserves[1] / 10**18) / (reserves[0] / 10**18) if reserves[0] != 0 else 0  # WBNB/Token
            return price * supply
    except Exception as e:
        logger.error(f"Erreur market cap: {str(e)}")
        return detected_tokens[contract_address]['market_cap']

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
        msg = (
            f"Token: {token} ({portfolio[token]['chain']})\n"
            f"Contrat: {token}\n"
            f"MC Achat: ${portfolio[token]['market_cap_at_buy']:.2f}\n"
            f"MC Actuel: ${current_mc:.2f}\n"
            f"Profit: {profit:.2f}%\n"
            f"Take-Profit: x{take_profit_steps[0]}, x{take_profit_steps[1]}, x{take_profit_steps[2]}\n"
            f"Stop-Loss: -{stop_loss_threshold}%"
        )
        bot.send_message(chat_id, msg, reply_markup=markup)
    except Exception as e:
        logger.error(f"Erreur refresh: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur refresh: {str(e)}")

# Vente immédiate
def sell_token_immediate(chat_id, token):
    try:
        amount = portfolio[token]["amount"]
        chain = portfolio[token]["chain"]
        current_price = get_current_market_cap(token) / detected_tokens[token]['supply']
        sell_token(chat_id, token, amount, chain, current_price)
        bot.send_message(chat_id, f"✅ Position {token} vendue entièrement!")
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
        app.run(host="0.0.0.0", port=PORT, debug=False)
    except Exception as e:
        logger.error(f"Erreur critique au démarrage: {str(e)}")
        raise
