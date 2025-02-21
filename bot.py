import os
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import logging
import requests
import time
from flask import Flask, request, abort
from web3 import Web3
from cachetools import TTLCache
import re

# Configuration du logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Chargement des variables d'environnement
TOKEN = os.getenv("TELEGRAM_TOKEN")
BSC_SCAN_API_KEY = os.getenv("BSC_SCAN_API_KEY")
TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

# Validation des variables critiques
if not TOKEN:
    logging.error("TELEGRAM_TOKEN manquant. Le bot ne peut pas démarrer.")
    raise ValueError("TELEGRAM_TOKEN manquant")
if not WEBHOOK_URL:
    logging.warning("WEBHOOK_URL manquant. Le webhook ne sera pas configuré.")

# Initialisation
bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

# Configuration Web3 (avec gestion d'erreur)
try:
    w3 = Web3(Web3.HTTPProvider("https://bsc-dataseed.binance.org/"))
    if not w3.is_connected():
        logging.warning("Connexion à BSC échouée. Les trades ne fonctionneront pas.")
except Exception as e:
    logging.error(f"Erreur initialisation Web3: {str(e)}")
    w3 = None

# Configuration de base
test_mode = True
mise_depart = 5  # En BNB
stop_loss_threshold = 10  # Stop loss dynamique (-10%)
take_profit_steps = [2, 3, 5]  # Vente progressive à +100%, +200%, +400%
gas_fee = 0.001  # Estimation des frais en BNB
detected_tokens = {}
trade_active = False
cache = TTLCache(maxsize=100, ttl=300)  # Cache de 5 minutes

# APIs externes
GMGN_API_URL = "https://api.gmgn.ai/new_tokens"
BSC_SCAN_API_URL = "https://api.bscscan.com/api"
TWITTER_TRACK_URL = "https://api.twitter.com/2/tweets/search/recent"
PANCAKE_POOL_ADDRESS = "0x10ED43C718714eb63d5aA57B78B54704E256024E"  # Exemple PancakeSwap

# Webhook Telegram
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        if request.headers.get("content-type") == "application/json":
            update = telebot.types.Update.de_json(request.get_json())
            bot.process_new_updates([update])
            return "OK", 200
        return abort(403)
    except Exception as e:
        logging.error(f"Erreur dans webhook: {str(e)}")
        return abort(500)

# Commande /start
@bot.message_handler(commands=["start"])
def start_message(message):
    try:
        bot.send_message(message.chat.id, "🤖 Bienvenue sur ton bot de trading de memecoins !")
        show_main_menu(message.chat.id)
    except Exception as e:
        logging.error(f"Erreur dans start_message: {str(e)}")

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
        logging.error(f"Erreur dans show_main_menu: {str(e)}")

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
        logging.error(f"Erreur dans callback_query: {str(e)}")
        bot.send_message(chat_id, "⚠️ Une erreur est survenue.")

# Menu de configuration
def show_config_menu(chat_id):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("💰 Augmenter mise (+1 BNB)", callback_data="increase_mise"),
        InlineKeyboardButton("🎯 Toggle Mode Test", callback_data="toggle_test")
    )
    try:
        bot.send_message(chat_id, "⚙️ Configuration :", reply_markup=markup)
    except Exception as e:
        logging.error(f"Erreur dans show_config_menu: {str(e)}")

@bot.callback_query_handler(func=lambda call: call.data in ["increase_mise", "toggle_test"])
def config_callback(call):
    global mise_depart, test_mode
    chat_id = call.message.chat.id
    try:
        if call.data == "increase_mise":
            mise_depart += 1
            bot.send_message(chat_id, f"💰 Mise augmentée à {mise_depart} BNB")
        elif call.data == "toggle_test":
            test_mode = not test_mode
            bot.send_message(chat_id, f"🎯 Mode Test {'activé' if test_mode else 'désactivé'}")
    except Exception as e:
        logging.error(f"Erreur dans config_callback: {str(e)}")

# Vérification TokenSniffer
def is_valid_token_tokensniffer(contract_address):
    try:
        url = f"https://tokensniffer.com/token/{contract_address}"
        response = requests.get(url, timeout=5)
        if response.status_code == 200 and "Rug Pull" not in response.text and "honeypot" not in response.text:
            return True
        return False
    except Exception as e:
        logging.error(f"Erreur TokenSniffer: {str(e)}")
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
        logging.error(f"Erreur BscScan: {str(e)}")
        return False

# Surveillance gmgn.ai
def detect_new_tokens(chat_id):
    global detected_tokens
    try:
        response = requests.get(GMGN_API_URL, timeout=5)
        tokens = response.json()
        for token in tokens:
            ca = token["contract_address"]
            if ca in cache:
                continue
            if is_valid_token_tokensniffer(ca) and is_valid_token_bscscan(ca):
                detected_tokens[ca] = {"status": "safe", "entry_price": None}
                bot.send_message(chat_id, f"✅ Nouveau token détecté : {token['name']} ({ca})")
    except Exception as e:
        logging.error(f"Erreur detect_new_tokens: {str(e)}")

# Surveillance Twitter
def monitor_twitter_for_tokens(chat_id):
    headers = {"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"}
    params = {"query": "contract address memecoin $ -is:retweet", "max_results": 10}
    try:
        response = requests.get(TWITTER_TRACK_URL, headers=headers, params=params, timeout=5)
        tweets = response.json()
        for tweet in tweets["data"]:
            ca_match = re.search(r"0x[a-fA-F0-9]{40}", tweet["text"])
            if ca_match:
                ca = ca_match.group(0)
                if ca not in detected_tokens and ca not in cache:
                    if is_valid_token_tokensniffer(ca) and is_valid_token_bscscan(ca):
                        detected_tokens[ca] = {"status": "safe", "entry_price": None}
                        bot.send_message(chat_id, f"🚀 Token détecté sur X : {ca}")
                    cache[ca] = True
    except Exception as e:
        logging.error(f"Erreur monitor_twitter_for_tokens: {str(e)}")

# Configuration du webhook
def set_webhook():
    try:
        if WEBHOOK_URL:
            bot.remove_webhook()
            bot.set_webhook(url=WEBHOOK_URL)
            logging.info("Webhook configuré avec succès !")
        else:
            logging.warning("WEBHOOK_URL non défini, webhook non configuré.")
    except Exception as e:
        logging.error(f"Erreur configuration webhook: {str(e)}")

# Démarrage
if __name__ == "__main__":
    logging.info("Bot starting...")
    try:
        set_webhook()
        port = int(os.getenv("PORT", 8080))
        logging.info(f"Starting Flask on port {port}...")
        app.run(host="0.0.0.0", port=port, debug=False)
    except Exception as e:
        logging.error(f"Erreur démarrage bot: {str(e)}")
        raise  # Relance l’erreur pour que Cloud Run signale le problème
