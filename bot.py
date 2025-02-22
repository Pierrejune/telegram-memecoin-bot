import os
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import logging
import requests
import time
from flask import Flask, request, abort
from cachetools import TTLCache
import re
from web3 import Web3
from bs4 import BeautifulSoup

# Configuration du logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Headers pour simuler un navigateur
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/91.0.4472.124",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.5",
}

# Session persistante
logger.info("Création de la session requests...")
session = requests.Session()
session.headers.update(HEADERS)

# Chargement des variables
logger.info("Chargement des variables d'environnement...")
TOKEN = os.getenv("TELEGRAM_TOKEN")
BSC_SCAN_API_KEY = os.getenv("BSC_SCAN_API_KEY")
TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8080))

# Validation
logger.info("Validation des variables...")
if not TOKEN:
    logger.error("TELEGRAM_TOKEN manquant.")
    raise ValueError("TELEGRAM_TOKEN manquant")
if not all([WALLET_ADDRESS, PRIVATE_KEY]):
    logger.warning("WALLET_ADDRESS ou PRIVATE_KEY manquant.")

# Initialisation
logger.info("Initialisation des composants...")
bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)
logger.info("Flask initialisé.")
w3 = Web3(Web3.HTTPProvider("https://bsc-dataseed.binance.org/"))
if not w3.is_connected():
    logger.error("Connexion BSC échouée.")
    w3 = None
else:
    logger.info("Connexion BSC réussie.")

# Configuration de base
test_mode = True
mise_depart = 0.01
stop_loss_threshold = 30
take_profit_steps = [2, 3, 5]
gas_fee = 0.001
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
TWITTER_TRACK_URL = "https://api.twitter.com/2/tweets/search/recent"
DEXSCREENER_URL = "https://dexscreener.com/new-pairs?sort=created&order=desc"

# Webhook Telegram
@app.route("/webhook", methods=["POST"])
def webhook():
    logger.info("Webhook reçu")
    try:
        if request.headers.get("content-type") == "application/json":
            update = telebot.types.Update.de_json(request.get_json())
            bot.process_new_updates([update])
            logger.info("Webhook traité avec succès")
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
        InlineKeyboardButton("💼 Portefeuille", callback_data="portfolio")
    )
    try:
        bot.send_message(chat_id, "Que veux-tu faire ?", reply_markup=markup)
    except Exception as e:
        logger.error(f"Erreur dans show_main_menu: {str(e)}")

# Gestion des callbacks
@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    global test_mode, mise_depart, trade_active
    chat_id = call.message.chat.id
    logger.info(f"Callback reçu: {call.data}")
    try:
        if call.data == "status":
            bot.send_message(chat_id, f"📊 Statut :\n- Mise: {mise_depart} BNB\n- Mode test: {test_mode}\n- Trading actif: {trade_active}")
        elif call.data == "config":
            show_config_menu(chat_id)
        elif call.data == "launch":
            if not trade_active:
                trade_active = True
                bot.send_message(chat_id, "🚀 Trading lancé !")
                while trade_active:
                    detect_new_tokens(chat_id)
                    monitor_twitter_for_tokens(chat_id)
                    time.sleep(60)
            else:
                bot.send_message(chat_id, "⚠️ Trading déjà en cours.")
        elif call.data == "stop":
            trade_active = False
            bot.send_message(chat_id, "⏹ Trading arrêté.")
        elif call.data == "portfolio":
            show_portfolio(chat_id)
        elif call.data.startswith("refresh_"):
            token = call.data.split("_")[1]
            refresh_token(chat_id, token)
        elif call.data.startswith("sell_"):
            token = call.data.split("_")[1]
            sell_token_immediate(chat_id, token)
    except Exception as e:
        logger.error(f"Erreur dans callback_query: {str(e)}")
        bot.send_message(chat_id, "⚠️ Une erreur est survenue.")

# Menu de configuration
def show_config_menu(chat_id):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("💰 Augmenter mise (+0.01 BNB)", callback_data="increase_mise"),
        InlineKeyboardButton("🎯 Toggle Mode Test", callback_data="toggle_test")
    )
    try:
        bot.send_message(chat_id, "⚙️ Configuration :", reply_markup=markup)
    except Exception as e:
        logger.error(f"Erreur dans show_config_menu: {str(e)}")

@bot.callback_query_handler(func=lambda call: call.data in ["increase_mise", "toggle_test"])
def config_callback(call):
    global mise_depart, test_mode
    chat_id = call.message.chat.id
    try:
        if call.data == "increase_mise":
            mise_depart += 0.01
            bot.send_message(chat_id, f"💰 Mise augmentée à {mise_depart} BNB")
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
            if "rug pull" in text or "honeypot" in text:
                return False
            if "owner renounced" not in text or "tax > 5%" in text:
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

# Surveillance DexScreener
def detect_new_tokens(chat_id):
    global detected_tokens
    bot.send_message(chat_id, "🔍 Recherche de nouveaux tokens sur DexScreener...")
    try:
        response = session.get(DEXSCREENER_URL, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        pairs = soup.select('div.pair-row')[:10]  # Ajuste selon la structure réelle
        bot.send_message(chat_id, f"📡 {len(pairs)} nouveaux tokens trouvés sur DexScreener")
        for pair in pairs:
            ca_elem = pair.select_one('span.contract-address')
            if not ca_elem:
                continue
            ca = ca_elem.text.strip()
            chain = "solana" if "solana" in pair.text.lower() else "bsc" if "bsc" in pair.text.lower() else None
            if ca in cache or not chain:
                continue
            
            # Métriques simulées (à ajuster avec API réelle)
            liquidity = float(pair.select_one('span.liquidity').text.replace('$', '').replace(',', '')) if pair.select_one('span.liquidity') else ueden0
            volume = float(pair.select_one('span.volume').text.replace('$', '').replace(',', '')) if pair.select_one('span.volume') else 0
            market_cap = float(pair.select_one('span.market-cap').text.replace('$', '').replace(',', '')) if pair.select_one('span.market-cap') else 0
            price_change = float(pair.select_one('span.price-change').text.replace('%', '')) if pair.select_one('span.price-change') else 0

            min_volume = MIN_VOLUME_SOL if chain == "solana" else MIN_VOLUME_BSC
            max_volume = MAX_VOLUME_SOL if chain == "solana" else MAX_VOLUME_BSC
            min_mc = MIN_MARKET_CAP_SOL if chain == "solana" else MIN_MARKET_CAP_BSC
            max_mc = MAX_MARKET_CAP_SOL if chain == "solana" else MAX_MARKET_CAP_BSC

            if (min_volume <= volume <= max_volume and 
                liquidity >= MIN_LIQUIDITY and liquidity >= market_cap * MIN_LIQUIDITY_PCT and 
                MIN_PRICE_CHANGE <= price_change <= MAX_PRICE_CHANGE and 
                min_mc <= market_cap <= max_mc and 
                is_valid_token_tokensniffer(ca) and is_valid_token_bscscan(ca)):
                detected_tokens[ca] = {"status": "safe", "entry_price": None, "chain": chain, "market_cap": market_cap}
                bot.send_message(chat_id, f"🚀 Token détecté : {ca} ({chain}) - Vol: ${volume}, Liq: ${liquidity}, MC: ${market_cap}")
                if trade_active and w3:
                    buy_token(chat_id, ca, mise_depart, chain)
            else:
                bot.send_message(chat_id, f"❌ {ca} rejeté - Vol: ${volume}, Liq: ${liquidity}, MC: ${market_cap}, Change: {price_change}%")
            cache[ca] = True
        if not pairs:
            bot.send_message(chat_id, "ℹ️ Aucun token trouvé sur DexScreener")
    except requests.exceptions.RequestException as e:
        logger.error(f"Erreur DexScreener HTTP: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur DexScreener: {str(e)}")
    except Exception as e:
        logger.error(f"Erreur DexScreener: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur DexScreener inattendue: {str(e)}")

# Surveillance Twitter
def monitor_twitter_for_tokens(chat_id):
    if not TWITTER_BEARER_TOKEN:
        bot.send_message(chat_id, "ℹ️ Twitter désactivé (TWITTER_BEARER_TOKEN manquant)")
        return
    headers = {"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"}
    params = {"query": "memecoin contract 0x -is:retweet", "max_results": 10}
    bot.send_message(chat_id, "🔍 Recherche de tokens sur Twitter...")
    try:
        response = session.get(TWITTER_TRACK_URL, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        tweets = response.json()
        if 'data' not in tweets:
            error_msg = tweets.get('error', 'Réponse invalide') if 'error' in tweets else 'Aucune donnée'
            bot.send_message(chat_id, f"⚠️ Réponse Twitter invalide: {error_msg}")
            return
        bot.send_message(chat_id, f"📡 {len(tweets['data'])} tweets trouvés sur Twitter")
        for tweet in tweets["data"]:
            ca_match = re.search(r"0x[a-fA-F0-9]{40}", tweet["text"])
            if ca_match:
                ca = ca_match.group(0)
                if ca not in detected_tokens and ca not in cache:
                    if is_valid_token_tokensniffer(ca) and is_valid_token_bscscan(ca):
                        detected_tokens[ca] = {"status": "safe", "entry_price": None, "chain": "bsc", "market_cap": 100000}
                        bot.send_message(chat_id, f"🚀 Token détecté sur X : {ca} (BSC)")
                        if trade_active and w3:
                            buy_token(chat_id, ca, mise_depart, "bsc")
                    else:
                        bot.send_message(chat_id, f"❌ {ca} rejeté par les filtres")
                cache[ca] = True
        if not tweets["data"]:
            bot.send_message(chat_id, "ℹ️ Aucun tweet trouvé sur Twitter")
    except requests.exceptions.RequestException as e:
        logger.error(f"Erreur Twitter HTTP: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur Twitter: {str(e)}")
    except Exception as e:
        logger.error(f"Erreur monitor_twitter_for_tokens: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur Twitter inattendue: {str(e)}")

# Achat de token
def buy_token(chat_id, contract_address, amount, chain):
    logger.info(f"Achat de {contract_address} sur {chain}")
    if test_mode:
        bot.send_message(chat_id, f"🧪 [Mode Test] Achat simulé de {amount} {chain.upper()} de {contract_address}")
        detected_tokens[contract_address]["entry_price"] = 0.01
        portfolio[contract_address] = {
            "amount": amount,
            "chain": chain,
            "entry_price": 0.01,
            "market_cap_at_buy": detected_tokens[contract_address]["market_cap"],
            "current_market_cap": detected_tokens[contract_address]["market_cap"]
        }
        monitor_and_sell(chat_id, contract_address, amount, chain)
        return
    try:
        router = w3.eth.contract(address=PANCAKE_ROUTER_ADDRESS, abi=PANCAKE_ROUTER_ABI)
        amount_in = w3.to_wei(amount, 'ether')
        tx = router.functions.swapExactETHForTokens(
            0,
            [w3.to_checksum_address("0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"), w3.to_checksum_address(contract_address)],
            w3.to_checksum_address(WALLET_ADDRESS),
            int(time.time()) + 60 * 10
        ).build_transaction({
            'from': WALLET_ADDRESS,
            'value': amount_in,
            'gas': 200000,
            'gasPrice': w3.to_wei('5', 'gwei'),
            'nonce': w3.eth.get_transaction_count(WALLET_ADDRESS)
        })
        signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
        bot.send_message(chat_id, f"🚀 Achat de {amount} {chain.upper()} de {contract_address}, TX: {tx_hash.hex()}")
        portfolio[contract_address] = {
            "amount": amount,
            "chain": chain,
            "entry_price": 0.01,  # À remplacer
            "market_cap_at_buy": detected_tokens[contract_address]["market_cap"],
            "current_market_cap": detected_tokens[contract_address]["market_cap"]
        }
        monitor_and_sell(chat_id, contract_address, amount, chain)
    except Exception as e:
        logger.error(f"Erreur achat token: {str(e)}")
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

# Vente de token
def sell_token(chat_id, contract_address, amount, chain, current_price):
    try:
        bot.send_message(chat_id, f"💸 Vente simulée de {amount} {chain.upper()} de {contract_address} à {current_price} (à implémenter)")
        if contract_address in portfolio:
            portfolio[contract_address]["amount"] -= amount
            if portfolio[contract_address]["amount"] <= 0:
                del portfolio[contract_address]
    except Exception as e:
        logger.error(f"Erreur vente token: {str(e)}")
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

# Solde Solana (simulation)
def get_solana_balance(wallet_address):
    try:
        return 0.5  # À implémenter
    except Exception as e:
        logger.error(f"Erreur solde Solana: {str(e)}")
        return 0

# Market cap en temps réel (simulation)
def get_current_market_cap(contract_address):
    try:
        return detected_tokens[contract_address]["market_cap"] * 1.5  # Simulation
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
