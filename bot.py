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
            bot.send_message(chat_id, f"📊 Statut :\
