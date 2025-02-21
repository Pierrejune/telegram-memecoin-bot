import os
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import logging
import requests
import time
from flask import Flask, request, abort
from web3 import Web3
from pancakeswap import PancakeSwap  # Hypothetical, replace with actual library or custom implementation
from cachetools import TTLCache
import re

# Configuration du logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Chargement des variables d'environnement
TOKEN = os.getenv("TELEGRAM_TOKEN")
BSC_SCAN_API_KEY = os.getenv("BSC_SCAN_API_KEY")
TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")  # À sécuriser davantage dans un vrai scénario
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

# Validation des variables
if not all([TOKEN, BSC_SCAN_API_KEY, TWITTER_BEARER_TOKEN, WALLET_ADDRESS, PRIVATE_KEY, WEBHOOK_URL]):
    raise ValueError("❌ ERREUR : Une ou plusieurs variables d'environnement sont absentes.")

# Initialisation
bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)
w3 = Web3(Web3.HTTPProvider("https://bsc-dataseed.binance.org/"))
pancake = PancakeSwap(w3, WALLET_ADDRESS, PRIVATE_KEY)  # Remplace par une implémentation réelle

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
PANCAKE_POOL_ADDRESS = "0x10ED43C718714eb63d5aA57B78B54704E256024E"  # Exemple d'adresse pool PancakeSwap

# Webhook Telegram
@app.route("/webhook", methods=["POST"])
def webhook():
    if request.headers.get("content-type") == "application/json":
        update = telebot.types.Update.de_json(request.get_json())
        bot.process_new_updates([update])
        return "OK", 200
    return abort(403)

# Commande /start
@bot.message_handler(commands=["start"])
def start_message(message):
    bot.send_message(message.chat.id, "🤖 Bienvenue sur ton bot de trading de memecoins !")
    show_main_menu(message.chat.id)

# Menu principal
def show_main_menu(chat_id):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("📈 Statut", callback_data="status"),
        InlineKeyboardButton("⚙️ Configurer", callback_data="config"),
        InlineKeyboardButton("🚀 Lancer", callback_data="launch"),
        InlineKeyboardButton("❌ Arrêter", callback_data="stop")
    )
    bot.send_message(chat_id, "Que veux-tu faire ?", reply_markup=markup)

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
    bot.send_message(chat_id, "⚙️ Configuration :", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data in ["increase_mise", "toggle_test"])
def config_callback(call):
    global mise_depart, test_mode
    chat_id = call.message.chat.id
    if call.data == "increase_mise":
        mise_depart += 1
        bot.send_message(chat_id, f"💰 Mise augmentée à {mise_depart} BNB")
    elif call.data == "toggle_test":
        test_mode = not test_mode
        bot.send_message(chat_id, f"🎯 Mode Test {'activé' if test_mode else 'désactivé'}")

# Vérification TokenSniffer
def is_valid_token_tokensniffer(contract_address):
    try:
        url = f"https://tokensniffer.com/token/{contract_address}"
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            if "Rug Pull" in response.text or "honeypot" in response.text:
                return False
            return True
        return False
    except Exception as e:
        logging.error(f"Erreur TokenSniffer pour {contract_address}: {str(e)}")
        return False

# Vérification BscScan + liquidité
def is_valid_token_bscscan(contract_address):
    try:
        params = {
            'module': 'token',
            'action': 'getTokenInfo',
            'contractaddress': contract_address,
            'apikey': BSC_SCAN_API_KEY
        }
        response = requests.get(BSC_SCAN_API_URL, params=params, timeout=5)
        data = response.json()
        if data['status'] == '1':
            total_supply = float(data['result']['totalSupply'])
            if total_supply < 1000:
                return False
            # Vérification liquidité (exemple simplifié)
            contract = w3.eth.contract(address=contract_address, abi=[])  # ABI à fournir
            liquidity = contract.functions.balanceOf(PANCAKE_POOL_ADDRESS).call()
            return liquidity > 1000  # Seuil minimal
        return False
    except Exception as e:
        logging.error(f"Erreur BscScan pour {contract_address}: {str(e)}")
        return False

# Surveillance gmgn.ai
def detect_new_tokens(chat_id):
    global detected_tokens
    try:
        response = requests.get(GMGN_API_URL, timeout=5)
        response.raise_for_status()
        tokens = response.json()
        for token in tokens:
            ca = token["contract_address"]
            if ca in cache:
                continue
            if is_valid_token_tokensniffer(ca) and is_valid_token_bscscan(ca):
                detected_tokens[ca] = {"status": "safe", "entry_price": None}
                bot.send_message(chat_id, f"✅ Nouveau token détecté : {token['name']} ({ca})")
                if trade_active and calculate_fees(mise_depart):
                    buy_token(chat_id, ca, mise_depart)
            else:
                bot.send_message(chat_id, f"❌ Token suspect détecté : {token['name']} ({ca})")
            cache[ca] = True
    except Exception as e:
        logging.error(f"Erreur detect_new_tokens: {str(e)}")

# Surveillance globale Twitter
def monitor_twitter_for_tokens(chat_id):
    headers = {"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"}
    params = {
        "query": "contract address memecoin $ -is:retweet",  # Recherche globale, exclut retweets
        "max_results": 10
    }
    try:
        response = requests.get(TWITTER_TRACK_URL, headers=headers, params=params, timeout=5)
        response.raise_for_status()
        tweets = response.json()
        for tweet in tweets["data"]:
            ca_match = re.search(r"0x[a-fA-F0-9]{40}", tweet["text"])
            if ca_match:
                ca = ca_match.group(0)
                if ca not in detected_tokens and ca not in cache:
                    if is_valid_token_tokensniffer(ca) and is_valid_token_bscscan(ca):
                        detected_tokens[ca] = {"status": "safe", "entry_price": None}
                        bot.send_message(chat_id, f"🚀 Token détecté sur X : {ca}")
                        if trade_active and calculate_fees(mise_depart):
                            buy_token(chat_id, ca, mise_depart)
                    cache[ca] = True
    except Exception as e:
        logging.error(f"Erreur monitor_twitter_for_tokens: {str(e)}")

# Gestion des frais
def calculate_fees(amount):
    return amount - gas_fee > 0  # Vérifie si le trade reste rentable

# Achat de token
def buy_token(chat_id, contract_address, amount):
    try:
        if test_mode:
            bot.send_message(chat_id, f"🧪 [Mode Test] Achat simulé de {amount} BNB de {contract_address}")
            detected_tokens[contract_address]["entry_price"] = 0.01  # Prix fictif
            return
        tx = pancake.buy_token(contract_address, amount * 10**18)  # Conversion en wei
        current_price = get_current_price(contract_address)
        detected_tokens[contract_address]["entry_price"] = current_price
        bot.send_message(chat_id, f"🚀 Achat de {amount} BNB de {contract_address} à {current_price} BNB")
        monitor_trade(chat_id, contract_address)
    except Exception as e:
        logging.error(f"Erreur achat {contract_address}: {str(e)}")
        bot.send_message(chat_id, f"❌ Échec achat {contract_address}")

# Prix actuel (simplifié)
def get_current_price(contract_address):
    # À remplacer par une API réelle (ex. PancakeSwap ou Dexscreener)
    return 0.01 if test_mode else float(pancake.get_price(contract_address)) / 10**18

# Gestion des trades
def monitor_trade(chat_id, token):
    entry_price = detected_tokens[token]["entry_price"]
    while trade_active and token in detected_tokens:
        current_price = get_current_price(token)
        profit = (current_price - entry_price) / entry_price * 100
        if profit >= take_profit_steps[0] * 100:
            sell_token(chat_id, token, 0.5, current_price)  # Vente 50%
        elif profit >= take_profit_steps[1] * 100:
            sell_token(chat_id, token, 0.25, current_price)  # Vente 25%
        elif profit >= take_profit_steps[2] * 100:
            sell_token(chat_id, token, 1.0, current_price)  # Vente totale
        elif profit <= -stop_loss_threshold:
            sell_token(chat_id, token, 1.0, current_price)  # Stop loss
        time.sleep(10)  # Vérification toutes les 10 secondes

# Vente de token
def sell_token(chat_id, token, fraction, current_price):
    try:
        if test_mode:
            bot.send_message(chat_id, f"🧪 [Mode Test] Vente simulée de {fraction*100}% de {token} à {current_price} BNB")
            if fraction == 1.0:
                del detected_tokens[token]
            return
        amount = mise_depart * fraction
        tx = pancake.sell_token(token, amount * 10**18)
        profit = (current_price - detected_tokens[token]["entry_price"]) * amount
        bot.send_message(chat_id, f"✅ Vente de {fraction*100}% de {token} à {current_price} BNB, profit: {profit:.4f} BNB")
        if fraction == 1.0:
            del detected_tokens[token]
    except Exception as e:
        logging.error(f"Erreur vente {token}: {str(e)}")
        bot.send_message(chat_id, f"❌ Échec vente {token}")

# Configuration du webhook
def set_webhook():
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL)
    logging.info("✅ Webhook configuré avec succès !")

if __name__ == "__main__":
    set_webhook()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
