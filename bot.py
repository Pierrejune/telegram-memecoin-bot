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
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/91.0.4472.124", "Accept": "application/json"}
session = requests.Session()
session.headers.update(HEADERS)

# Variables d'environnement
TOKEN = os.getenv("TELEGRAM_TOKEN")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
SOLANA_WALLET_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY")
BSCSCAN_API_KEY = os.getenv("BSCSCAN_API_KEY")  # Déjà configuré
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")  # Optionnel
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
PORT = int(os.getenv("PORT", 8080))

# Validation avec tolérance pour BIRDEYE_API_KEY
logger.info("Validation des variables...")
required_vars = [TOKEN, WALLET_ADDRESS, PRIVATE_KEY, SOLANA_WALLET_PRIVATE_KEY, BSCSCAN_API_KEY]
if not all(required_vars):
    missing = [var_name for var_name, var in zip(["TELEGRAM_TOKEN", "WALLET_ADDRESS", "PRIVATE_KEY", "SOLANA_PRIVATE_KEY", "BSCSCAN_API_KEY"], required_vars) if not var]
    logger.error(f"Variables manquantes : {missing}")
    raise ValueError(f"Variables d'environnement manquantes : {missing}")
if not BIRDEYE_API_KEY:
    logger.warning("BIRDEYE_API_KEY manquant, holders Solana non vérifiés.")

# Initialisation
bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)
logger.info("Initialisation de Web3...")
w3 = Web3(Web3.HTTPProvider("https://bsc-dataseed.binance.org/"))
solana_keypair = Keypair.from_base58_string(SOLANA_WALLET_PRIVATE_KEY)
if not w3.is_connected():
    logger.error("Connexion BSC échouée.")
    w3 = None
else:
    logger.info("Connexion BSC réussie.")

PANCAKE_ROUTER_ADDRESS = "0x10ED43C718714eb63d5aA57B78B54704E256024E"
PANCAKE_FACTORY_ADDRESS = "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"
RAYDIUM_PROGRAM_ID = Pubkey.from_string("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8")

# Configuration de base
test_mode = True
mise_depart_bsc = 0.01
mise_depart_sol = 0.02
slippage = 5
gas_fee = 5
stop_loss_threshold = 30
take_profit_steps = [2, 3, 5]
portfolio = {}
cache = TTLCache(maxsize=100, ttl=300)

# Critères personnalisés (page 4)
MIN_VOLUME_SOL = 50000
MAX_VOLUME_SOL = 500000
MIN_VOLUME_BSC = 75000
MAX_VOLUME_BSC = 750000
MIN_LIQUIDITY = 100000
MIN_LIQUIDITY_PCT = 0.02
MIN_PRICE_CHANGE = 30
MAX_PRICE_CHANGE = 200
MIN_MARKET_CAP_SOL = 100000
MAX_MARKET_CAP_SOL = 500000
MIN_MARKET_CAP_BSC = 100000
MAX_MARKET_CAP_BSC = 200000
MAX_TAX = 5
MAX_HOLDER_PCT = 5

# Webhook Telegram
@app.route("/webhook", methods=["POST"])
def webhook():
    logger.info("Webhook reçu")
    if request.headers.get("content-type") == "application/json":
        update = telebot.types.Update.de_json(request.get_json())
        bot.process_new_updates([update])
        return "OK", 200
    logger.warning("Requête webhook invalide")
    abort(403)

# Commande /start
@bot.message_handler(commands=["start"])
def start_message(message):
    bot.send_message(message.chat.id, "🚀 Bienvenue sur ton bot de trading de memecoins !")
    show_main_menu(message.chat.id)

# Menu principal
def show_main_menu(chat_id):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("📊 Statut", callback_data="status"),
        InlineKeyboardButton("⚙️ Configure", callback_data="config"),
        InlineKeyboardButton("🚀 Lancer", callback_data="launch"),
        InlineKeyboardButton("🛑 Arrêter", callback_data="stop"),
        InlineKeyboardButton("💼 Portefeuille", callback_data="portfolio"),
        InlineKeyboardButton("🔧 Réglages", callback_data="settings")
    )
    bot.send_message(chat_id, "Que veux-tu faire ?", reply_markup=markup)

# Gestion des callbacks
@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    global test_mode, mise_depart_bsc, mise_depart_sol, trade_active, slippage, gas_fee
    chat_id = call.message.chat.id
    logger.info(f"Callback reçu: {call.data}")
    if call.data == "launch":
        if not globals().get("trade_active", False):
            globals()["trade_active"] = True
            bot.send_message(chat_id, "🚀 Trading lancé !")
            cycle_count = 0
            while globals().get("trade_active", False):
                cycle_count += 1
                bot.send_message(chat_id, f"ℹ️ Début du cycle de détection #{cycle_count}...")
                detect_new_tokens_bsc(chat_id)
                bot.send_message(chat_id, "✅ Détection BSC terminée, passage à Solana...")
                detect_new_tokens_solana(chat_id)
                bot.send_message(chat_id, "✅ Détection Solana terminée.")
                bot.send_message(chat_id, "⏳ Attente de 120 secondes avant le prochain cycle...")
                time.sleep(120)
        else:
            bot.send_message(chat_id, "⚠️ Trading déjà en cours.")
    elif call.data == "stop":
        globals()["trade_active"] = False
        bot.send_message(chat_id, "🛑 Trading arrêté.")

# Vérification TokenSniffer
def is_valid_token_tokensniffer(contract_address):
    try:
        url = f"https://tokensniffer.com/token/{contract_address}"
        response = session.get(url, timeout=10)
        if response.status_code == 200:
            text = response.text.lower()
            if "rug pull" in text or "honeypot" in text or "owner renounced" not in text or "tax > 5%" in text:
                logger.info(f"Token {contract_address} suspect selon TokenSniffer")
                return False
            return True
        return False
    except Exception as e:
        logger.error(f"Erreur TokenSniffer: {str(e)}")
        return False

# Fetch données DexScreener pour BSC
def get_dexscreener_data_bsc(token_address):
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
        response = session.get(url, timeout=10)
        data = response.json()["pairs"][0]
        return {
            "volume": float(data["volume"]["h24"]),
            "liquidity": float(data["liquidity"]["usd"]),
            "market_cap": float(data["fdv"])
        }
    except Exception as e:
        logger.error(f"Erreur DexScreener BSC {token_address}: {str(e)}")
        return None

# Fetch holders BSC via BscScan
def get_top_holder_percentage_bsc(token_address):
    try:
        url = f"https://api.bscscan.com/api?module=token&action=tokenholderlist&contractaddress={token_address}&page=1&offset=10&apikey={BSCSCAN_API_KEY}"
        response = session.get(url, timeout=10)
        data = response.json()
        if data["status"] == "1":
            holders = sorted([{"address": h["TokenHolderAddress"], "value": float(h["TokenHolderQuantity"])} for h in data["result"]], key=lambda x: x["value"], reverse=True)
            top_holder_value = holders[0]["value"]
            total_supply_response = session.get(f"https://api.bscscan.com/api?module=stats&action=tokensupply&contractaddress={token_address}&apikey={BSCSCAN_API_KEY}")
            total_supply = float(total_supply_response.json()["result"]) / 10**18
            return (top_holder_value / total_supply) * 100
        return None
    except Exception as e:
        logger.error(f"Erreur BscScan holders {token_address}: {str(e)}")
        return None

# Fetch données Birdeye pour Solana
def get_birdeye_data_solana(token_address):
    if not BIRDEYE_API_KEY:
        logger.warning("Pas de BIRDEYE_API_KEY, données Solana limitées.")
        return None
    try:
        url = f"https://public-api.birdeye.so/defi/token_overview?address={token_address}"
        response = session.get(url, headers={"X-API-KEY": BIRDEYE_API_KEY}, timeout=10)
        data = response.json()["data"]
        return {
            "volume": float(data["volume"]),
            "liquidity": float(data["liquidity"]),
            "market_cap": float(data["mc"]),
            "top_holder_pct": float(data["holders"][0]["percent"]) if "holders" in data and data["holders"] else None
        }
    except Exception as e:
        logger.error(f"Erreur Birdeye Solana {token_address}: {str(e)}")
        return None

# Surveillance BSC
def detect_new_tokens_bsc(chat_id):
    bot.send_message(chat_id, "🔍 Recherche de nouveaux tokens sur BSC (PancakeSwap)...")
    try:
        factory_abi = json.loads('[{"anonymous": false,"inputs":[{"indexed": true,"internalType": "address","name": "token0","type": "address"},{"indexed": true,"internalType": "address","name": "token1","type": "address"},{"indexed": false,"internalType": "address","name": "pair","type": "address"},{"indexed": false,"internalType": "uint256","name": "type","type": "uint256"}],"name": "PairCreated","type": "event"}]')
        factory = w3.eth.contract(address=PANCAKE_FACTORY_ADDRESS, abi=factory_abi)
        latest_block = w3.eth.block_number
        event_filter = factory.events.PairCreated.create_filter(fromBlock=latest_block-100, toBlock=latest_block)
        events = event_filter.get_all_entries()
        bot.send_message(chat_id, f"📡 {len(events)} nouvelles paires trouvées sur BSC")
        for event in events:
            token_address = event.args.token0 if event.args.token1 == "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c" else event.args.token1
            data = get_dexscreener_data_bsc(token_address)
            if not data:
                continue
            top_holder_pct = get_top_holder_percentage_bsc(token_address)
            if (MIN_VOLUME_BSC <= data["volume"] <= MAX_VOLUME_BSC and 
                data["liquidity"] >= MIN_LIQUIDITY and 
                data["liquidity"] >= data["market_cap"] * MIN_LIQUIDITY_PCT and 
                MIN_MARKET_CAP_BSC <= data["market_cap"] <= MAX_MARKET_CAP_BSC and 
                is_valid_token_tokensniffer(token_address) and 
                (top_holder_pct is None or top_holder_pct <= MAX_HOLDER_PCT)):
                bot.send_message(chat_id, f"🚀 Token détecté : {token_address} (BSC) - Vol: ${data['volume']:.2f}, Liq: ${data['liquidity']:.2f}, MC: ${data['market_cap']:.2f}, Top Holder: {top_holder_pct if top_holder_pct else 'N/A'}%")
                buy_token_bsc(chat_id, token_address, mise_depart_bsc)
    except Exception as e:
        logger.error(f"Erreur détection BSC: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur lors de la détection BSC: {str(e)}")

# Surveillance Solana
def detect_new_tokens_solana(chat_id):
    bot.send_message(chat_id, "🔍 Recherche de nouveaux tokens sur Solana...")
    retries = 3
    for attempt in range(retries):
        try:
            response = session.post(SOLANA_RPC, json={
                "jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress",
                "params": [str(RAYDIUM_PROGRAM_ID), {"limit": 10}]
            }, timeout=10)
            if response.status_code == 429:
                raise Exception("429 Too Many Requests")
            signatures = response.json().get("result", [])
            bot.send_message(chat_id, f"📡 {len(signatures)} signatures récentes trouvées sur Solana")
            for sig in signatures:
                tx_response = session.post(SOLANA_RPC, json={
                    "jsonrpc": "2.0", "id": 1, "method": "getTransaction",
                    "params": [sig["signature"], {"encoding": "jsonParsed"}]
                })
                tx_data = tx_response.json()["result"]
                token_address = tx_data["meta"]["postTokenBalances"][0]["mint"] if tx_data["meta"]["postTokenBalances"] else None
                if not token_address:
                    continue
                data = get_birdeye_data_solana(token_address)
                if not data:
                    continue
                top_holder_pct = data["top_holder_pct"]
                if (MIN_VOLUME_SOL <= data["volume"] <= MAX_VOLUME_SOL and 
                    data["liquidity"] >= MIN_LIQUIDITY and 
                    data["liquidity"] >= data["market_cap"] * MIN_LIQUIDITY_PCT and 
                    MIN_MARKET_CAP_SOL <= data["market_cap"] <= MAX_MARKET_CAP_SOL and 
                    (top_holder_pct is None or top_holder_pct <= MAX_HOLDER_PCT)):
                    bot.send_message(chat_id, f"🚀 Token détecté : {token_address} (Solana) - Vol: ${data['volume']:.2f}, Liq: ${data['liquidity']:.2f}, MC: ${data['market_cap']:.2f}, Top Holder: {top_holder_pct if top_holder_pct else 'N/A'}%")
                    buy_token_solana(chat_id, token_address, mise_depart_sol)
            break
        except Exception as e:
            logger.error(f"Erreur Solana RPC (tentative {attempt+1}/{retries}): {str(e)}")
            if attempt == retries - 1:
                bot.send_message(chat_id, f"⚠️ Erreur Solana RPC après {retries} tentatives: {str(e)}")
            time.sleep(5)

# Achat BSC
def buy_token_bsc(chat_id, contract_address, amount):
    if test_mode:
        bot.send_message(chat_id, f"🧪 [Mode Test] Achat simulé de {amount} BNB de {contract_address}")
        portfolio[contract_address] = {"amount": amount, "chain": "bsc", "entry_price": 0.01, "market_cap_at_buy": 150000}
        monitor_and_sell(chat_id, contract_address, amount, "bsc")
    else:
        # À implémenter
        pass

# Achat Solana
def buy_token_solana(chat_id, contract_address, amount):
    if test_mode:
        bot.send_message(chat_id, f"🧪 [Mode Test] Achat simulé de {amount} SOL de {contract_address}")
        portfolio[contract_address] = {"amount": amount, "chain": "solana", "entry_price": 0.01, "market_cap_at_buy": 300000}
        monitor_and_sell(chat_id, contract_address, amount, "solana")
    else:
        # À implémenter
        pass

# Surveillance et vente
def monitor_and_sell(chat_id, contract_address, amount, chain):
    entry_price = portfolio[contract_address]["entry_price"]
    market_cap_at_buy = portfolio[contract_address]["market_cap_at_buy"]
    position_open = True
    sold_half = False
    iteration = 0
    max_iterations = 5
    while position_open and globals().get("trade_active", False) and iteration < max_iterations:
        current_price = entry_price * 1.5  # À remplacer par API
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
        iteration += 1
        time.sleep(10)

# Vente
def sell_token(chat_id, contract_address, amount, chain, current_price):
    if test_mode:
        bot.send_message(chat_id, f"🧪 [Mode Test] Vente simulée de {amount} {chain.upper()} de {contract_address} à {current_price}")
        portfolio[contract_address]["amount"] -= amount
        if portfolio[contract_address]["amount"] <= 0:
            del portfolio[contract_address]
    else:
        # À implémenter
        pass

# Configuration webhook
def set_webhook():
    if WEBHOOK_URL:
        bot.remove_webhook()
        bot.set_webhook(url=WEBHOOK_URL)
        logger.info("Webhook configuré")

# Point d'entrée
if __name__ == "__main__":
    logger.info("Démarrage du bot...")
    set_webhook()
    logger.info(f"Lancement de Flask sur 0.0.0.0:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
