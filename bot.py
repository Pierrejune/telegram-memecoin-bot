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

# Configuration du logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Chargement des variables d'environnement
TOKEN = os.getenv("TELEGRAM_TOKEN")
BSC_SCAN_API_KEY = os.getenv("BSC_SCAN_API_KEY")
TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8080))

# Validation des variables critiques
if not TOKEN:
    logger.error("TELEGRAM_TOKEN manquant.")
    raise ValueError("TELEGRAM_TOKEN manquant")
if not all([WALLET_ADDRESS, PRIVATE_KEY]):
    logger.warning("WALLET_ADDRESS ou PRIVATE_KEY manquant. Trading désactivé.")
if not WEBHOOK_URL:
    logger.warning("WEBHOOK_URL manquant.")

# Initialisation
bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)
w3 = Web3(Web3.HTTPProvider("https://bsc-dataseed.binance.org/"))
if not w3.is_connected():
    logger.error("Connexion BSC échouée. Trading désactivé.")
    w3 = None

# Configuration PancakeSwap (ABI à compléter)
PANCAKE_ROUTER_ADDRESS = "0x10ED43C718714eb63d5aA57B78B54704E256024E"
PANCAKE_ROUTER_ABI = []  # Remplace par l’ABI réel de PancakeSwap Router V2

# Configuration de base
test_mode = True
mise_depart = 0.01
stop_loss_threshold = 10
take_profit_steps = [2, 3, 5]
gas_fee = 0.001
detected_tokens = {}
trade_active = False
cache = TTLCache(maxsize=100, ttl=300)

# APIs externes
BSC_SCAN_API_URL = "https://api.bscscan.com/api"
TWITTER_TRACK_URL = "https://api.twitter.com/2/tweets/search/recent"

# Webhook Telegram
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        logger.info("Webhook reçu")
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
        InlineKeyboardButton("❌ Arrêter", callback_data="stop")
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
    try:
        if call.data == "status":
            bot.send_message(chat_id, f"📊 Statut :\n- Mise: {mise_depart} BNB\n- Mode test: {test_mode}\n- Trading actif: {trade_active}")
        elif call.data == "config":
            show_config_menu(chat_id)
        elif call.data == "launch":
            if not trade_active:
                trade_active = True
                bot.send_message(chat_id, "🚀 Trading lancé !")
                detect_new_tokens(chat_id)
                monitor_twitter_for_tokens(chat_id)
            else:
                bot.send_message(chat_id, "⚠️ Trading déjà en cours.")
        elif call.data == "stop":
            trade_active = False
            bot.send_message(chat_id, "⏹ Trading arrêté.")
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
        response = requests.get(url, timeout=5)
        if response.status_code == 200 and "Rug Pull" not in response.text and "honeypot" not in response.text:
            return True
        return False
    except Exception as e:
        logger.error(f"Erreur TokenSniffer: {str(e)}")
        return False

# Vérification BscScan
def is_valid_token_bscscan(contract_address):
    try:
        params = {'module': 'token', 'action': 'getTokenInfo', 'contractaddress': contract_address, 'apikey': BSC_SCAN_API_KEY}
        response = requests.get(BSC_SCAN_API_URL, params=params, timeout=5)
        data = response.json()
        if data['status'] == '1' and float(data['result']['totalSupply']) >= 1000:
            return True
        return False
    except Exception as e:
        logger.error(f"Erreur BscScan: {str(e)}")
        return False

# Surveillance gmgn.ai (désactivée temporairement)
def detect_new_tokens(chat_id):
    global detected_tokens
    bot.send_message(chat_id, "ℹ️ Recherche gmgn.ai désactivée pour l’instant (API indisponible)")

# Surveillance Twitter
def monitor_twitter_for_tokens(chat_id):
    headers = {"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"}
    params = {"query": "memecoin contract 0x -is:retweet", "max_results": 10}
    bot.send_message(chat_id, "🔍 Recherche de tokens sur Twitter...")
    try:
        response = requests.get(TWITTER_TRACK_URL, headers=headers, params=params, timeout=5)
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
                        detected_tokens[ca] = {"status": "safe", "entry_price": None}
                        bot.send_message(chat_id, f"🚀 Token détecté sur X : {ca}")
                        if trade_active and w3:
                            buy_token(chat_id, ca, mise_depart)
                    else:
                        bot.send_message(chat_id, f"❌ {ca} rejeté par les filtres de sécurité")
                else:
                    bot.send_message(chat_id, f"ℹ️ {ca} déjà vu ou en cache")
        if not tweets["data"]:
            bot.send_message(chat_id, "ℹ️ Aucun tweet trouvé sur Twitter")
    except requests.exceptions.RequestException as e:
        logger.error(f"Erreur Twitter HTTP: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur Twitter: {str(e)}")
    except Exception as e:
        logger.error(f"Erreur monitor_twitter_for_tokens: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur Twitter inattendue: {str(e)}")

# Achat de token sur PancakeSwap
def buy_token(chat_id, contract_address, amount):
    if not w3 or test_mode:
        bot.send_message(chat_id, f"🧪 [Mode Test] Achat simulé de {amount} BNB de {contract_address}")
        detected_tokens[contract_address]["entry_price"] = 0.01
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
        bot.send_message(chat_id, f"🚀 Achat de {amount} BNB de {contract_address}, TX: {tx_hash.hex()}")
    except Exception as e:
        logger.error(f"Erreur achat token: {str(e)}")
        bot.send_message(chat_id, f"❌ Échec achat {contract_address}: {str(e)}")

# Configuration du webhook
def set_webhook():
    try:
        if WEBHOOK_URL:
            bot.remove_webhook()
            bot.set_webhook(url=WEBHOOK_URL)
            logger.info("Webhook configuré avec succès")
    except Exception as e:
        logger.error(f"Erreur configuration webhook: {str(e)}")

# Point d’entrée principal
if __name__ == "__main__":
    logger.info("Initialisation du bot...")
    try:
        logger.info("Configuration du webhook Telegram...")
        set_webhook()
        logger.info(f"Démarrage de Flask sur le port {PORT}...")
        app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
    except Exception as e:
        logger.error(f"Erreur critique au démarrage: {str(e)}")
        raise
