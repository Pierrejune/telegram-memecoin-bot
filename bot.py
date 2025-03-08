import os
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import logging
import requests
import time
import json
import base58
from flask import Flask, request, abort
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from solders.instruction import Instruction
import threading
import asyncio
from datetime import datetime
from waitress import serve
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import backoff
from queue import Queue

# Configuration logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Session HTTP avec retries
session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
retry_strategy = Retry(total=5, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET", "POST"])
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("https://", adapter)

# File d’attente pour Telegram
message_queue = Queue()
message_lock = threading.Lock()

# Variables globales
daily_trades = {'buys': [], 'sells': []}
rejected_tokens = {}
trade_active = False
bot_active = True
portfolio = {}
detected_tokens = {}
last_detection_time = {}
BLACKLISTED_TOKENS = {"So11111111111111111111111111111111111111112"}
pause_auto_sell = False
chat_id_global = None
stop_event = threading.Event()
active_threads = []

# Variables d’environnement
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
QUICKNODE_SOL_URL = os.getenv("QUICKNODE_SOL_URL")
PORT = int(os.getenv("PORT", 8080))

# Paramètres de trading
mise_depart_sol = 0.37
stop_loss_threshold = 15
trailing_stop_percentage = 5
take_profit_steps = [1.2, 2, 10, 100, 500]
max_positions = 5
profit_reinvestment_ratio = 0.9
slippage_max = 0.05
MIN_VOLUME_SOL = 100
MAX_VOLUME_SOL = 2000000
MIN_LIQUIDITY = 5000
MIN_MARKET_CAP_SOL = 1000
MAX_MARKET_CAP_SOL = 5000000
MIN_BUY_SELL_RATIO = 1.5
MAX_TOKEN_AGE_HOURS = 6
MIN_SOCIAL_MENTIONS = 5
MIN_SOL_AMOUNT = 0.1
MIN_ACCOUNTS_IN_TX = 3
DETECTION_COOLDOWN = 60

# Constantes Solana
RAYDIUM_PROGRAM_ID = Pubkey.from_string("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8")
PUMP_FUN_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfH43SboMiMEWCPkDPk")
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")

app = Flask(__name__)
bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=False)
solana_keypair = None

def send_message_worker():
    while bot_active:
        item = message_queue.get()
        chat_id, text = item[0], item[1]
        reply_markup = item[2] if len(item) > 2 else None
        try:
            with message_lock:
                if reply_markup:
                    bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode='Markdown')
                else:
                    bot.send_message(chat_id, text, parse_mode='Markdown')
            time.sleep(0.1)
        except Exception as e:
            logger.error(f"Erreur envoi message Telegram: {str(e)}")
        message_queue.task_done()

threading.Thread(target=send_message_worker, daemon=True).start()

def queue_message(chat_id, text, reply_markup=None):
    if bot_active:
        logger.info(f"Queueing message to {chat_id}: {text}")
        message_queue.put((chat_id, text, reply_markup))

def initialize_bot(chat_id):
    global solana_keypair
    try:
        solana_keypair = Keypair.from_base58_string(SOLANA_PRIVATE_KEY)
        queue_message(chat_id, "✅ Clé Solana initialisée")
        logger.info("Solana initialisé")
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur initialisation Solana: `{str(e)}`")
        logger.error(f"Erreur Solana: {str(e)}")
        solana_keypair = None

def set_webhook():
    try:
        bot.remove_webhook()
        time.sleep(1)
        success = bot.set_webhook(url=WEBHOOK_URL)
        if success:
            logger.info(f"Webhook configuré sur {WEBHOOK_URL}")
            return True
        else:
            raise Exception("Échec de la configuration du webhook")
    except Exception as e:
        logger.error(f"Erreur configuration webhook: {str(e)}")
        queue_message(chat_id_global, f"⚠️ Erreur webhook: `{str(e)}`")
        return False

def validate_address(token_address):
    try:
        Pubkey.from_string(token_address)
        return len(token_address) >= 32 and len(token_address) <= 44
    except ValueError:
        return False

@app.route("/quicknode-webhook", methods=['POST'])
def quicknode_webhook():
    global chat_id_global, trade_active
    logger.info(f"Webhook QuickNode appelé, trade_active={trade_active}")
    if not trade_active:
        logger.info("Webhook reçu mais trading inactif")
        return 'Trading stopped', 503
    if request.method == "POST":
        content_type = request.headers.get("Content-Type", "")
        try:
            if "application/json" in content_type.lower():
                data = request.get_json()
            else:
                raw_data = request.get_data(as_text=True)
                data = json.loads(raw_data) if raw_data else []
            logger.info(f"Webhook QuickNode reçu: {json.dumps(data, indent=2)}")
        except (json.JSONDecodeError, ValueError) as e:
            logger.error(f"Erreur parsing webhook QuickNode: {str(e)}")
            return "Invalid JSON", 400

        try:
            for block in data:
                for tx in block.get('transactions', []):
                    token_address = tx.get('info', {}).get('tokenAddress')
                    operation = tx.get('operation')
                    sol_change = abs(tx.get('info', {}).get('changes', {}).get('sol', 0))
                    accounts = tx.get('info', {}).get('accounts', {})
                    num_accounts = len([k for k, v in accounts.items() if v])

                    if not token_address or token_address in BLACKLISTED_TOKENS or token_address in detected_tokens:
                        continue
                    if not validate_address(token_address):
                        logger.info(f"Token {token_address} ignoré : Adresse invalide")
                        continue
                    if operation not in ['buy', 'mint']:
                        logger.info(f"Token {token_address} ignoré : Opération {operation} non pertinente")
                        continue
                    if sol_change < MIN_SOL_AMOUNT:
                        logger.info(f"Token {token_address} ignoré : Montant SOL {sol_change} < {MIN_SOL_AMOUNT}")
                        continue
                    if num_accounts < MIN_ACCOUNTS_IN_TX:
                        logger.info(f"Token {token_address} ignoré : {num_accounts} comptes < {MIN_ACCOUNTS_IN_TX}")
                        continue
                    last_time = last_detection_time.get(token_address, 0)
                    if time.time() - last_time < DETECTION_COOLDOWN:
                        logger.info(f"Token {token_address} ignoré : Détection trop récente")
                        continue

                    token_data = get_token_data_quicknode(token_address)
                    if token_data and token_data.get('liquidity', 0) > MIN_LIQUIDITY:
                        if validate_token(chat_id_global, token_address, token_data):
                            queue_message(chat_id_global, f"🎯 Token détecté : `{token_address}` (Solana - QuickNode)")
                            logger.info(f"Token détecté : {token_address}")
                            detected_tokens[token_address] = True
                            last_detection_time[token_address] = time.time()
                            asyncio.run(buy_token_solana(chat_id_global, token_address, mise_depart_sol))
                        else:
                            logger.info(f"Token {token_address} rejeté par critères")
                    else:
                        logger.info(f"Token {token_address} ignoré : Liquidité insuffisante")
            return 'OK', 200
        except Exception as e:
            logger.error(f"Erreur traitement webhook QuickNode: {str(e)}")
            queue_message(chat_id_global, f"⚠️ Erreur webhook QuickNode: `{str(e)}`")
            return f"Erreur: {str(e)}", 500
    return abort(403)

@backoff.on_exception(backoff.expo, Exception, max_tries=5)
def get_token_data_quicknode(token_address):
    try:
        response = session.post(
            QUICKNODE_SOL_URL,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getAccountInfo",
                "params": [token_address, {"encoding": "jsonParsed"}]
            },
            timeout=10
        )
        response.raise_for_status()
        account_info = response.json().get('result', {}).get('value', {})

        supply_response = session.post(
            QUICKNODE_SOL_URL,
            json={"jsonrpc": "2.0", "id": 1, "method": "getTokenSupply", "params": [token_address]},
            timeout=10
        )
        supply_data = supply_response.json().get('result', {}).get('value', {})
        supply = float(supply_data.get('amount', '0')) / 10**9

        lamports = account_info.get('lamports', 0) if account_info else 0
        liquidity = lamports / 10**9 * 1000  # Approximation : 1 SOL ≈ 1000 $
        has_liquidity = liquidity > MIN_LIQUIDITY

        return {
            'volume_24h': 0,
            'liquidity': liquidity if has_liquidity else 0,
            'market_cap': 0,
            'price': 0,
            'buy_sell_ratio': 1,
            'pair_created_at': time.time(),
            'supply': supply,
            'has_liquidity': has_liquidity
        }
    except Exception as e:
        logger.error(f"Erreur QuickNode data pour {token_address}: {str(e)}")
        return None

def validate_token(chat_id, token_address, data):
    try:
        volume_24h = data.get('volume_24h', 0)
        liquidity = data.get('liquidity', 0)
        market_cap = data.get('market_cap', 0)
        buy_sell_ratio = data.get('buy_sell_ratio', 1)
        age_hours = (time.time() - data.get('pair_created_at', time.time())) / 3600

        if len(portfolio) >= max_positions:
            queue_message(chat_id, f"⚠️ `{token_address}` rejeté : Limite de {max_positions} positions atteinte")
            return False
        if age_hours > MAX_TOKEN_AGE_HOURS:
            queue_message(chat_id, f"⚠️ `{token_address}` rejeté : Âge {age_hours:.2f}h > {MAX_TOKEN_AGE_HOURS}h")
            return False
        if liquidity <= MIN_LIQUIDITY:
            queue_message(chat_id, f"⚠️ `{token_address}` rejeté : Liquidité ${liquidity:.2f} <= ${MIN_LIQUIDITY}")
            return False
        if volume_24h < MIN_VOLUME_SOL or volume_24h > MAX_VOLUME_SOL:
            queue_message(chat_id, f"⚠️ `{token_address}` rejeté : Volume ${volume_24h:.2f} hors plage [{MIN_VOLUME_SOL}, {MAX_VOLUME_SOL}]")
            return False
        if market_cap < MIN_MARKET_CAP_SOL or market_cap > MAX_MARKET_CAP_SOL:
            queue_message(chat_id, f"⚠️ `{token_address}` rejeté : Market Cap ${market_cap:.2f} hors plage [{MIN_MARKET_CAP_SOL}, {MAX_MARKET_CAP_SOL}]")
            return False
        if buy_sell_ratio < MIN_BUY_SELL_RATIO:
            queue_message(chat_id, f"⚠️ `{token_address}` rejeté : Ratio A/V {buy_sell_ratio:.2f} < {MIN_BUY_SELL_RATIO}")
            return False
        return True
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur validation `{token_address}`: `{str(e)}`")
        return False

async def buy_token_solana(chat_id, contract_address, amount):
    try:
        if not solana_keypair:
            initialize_bot(chat_id)
            if not solana_keypair:
                raise Exception("Solana non initialisé")
        amount_in = int(amount * 10**9)
        response = session.post(QUICKNODE_SOL_URL, json={
            "jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash",
            "params": [{"commitment": "finalized"}]
        }, timeout=5)
        blockhash = response.json()['result']['value']['blockhash']
        tx = Transaction()
        tx.recent_blockhash = Pubkey.from_string(blockhash)
        instruction = Instruction(
            program_id=PUMP_FUN_PROGRAM_ID,
            accounts=[
                {"pubkey": solana_keypair.pubkey(), "is_signer": True, "is_writable": True},
                {"pubkey": Pubkey.from_string(contract_address), "is_signer": False, "is_writable": True},
                {"pubkey": TOKEN_PROGRAM_ID, "is_signer": False, "is_writable": False}
            ],
            data=bytes([2]) + amount_in.to_bytes(8, 'little')
        )
        tx.add(instruction)
        tx.sign([solana_keypair])
        tx_hash = session.post(QUICKNODE_SOL_URL, json={
            "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
            "params": [base58.b58encode(tx.serialize()).decode('utf-8')]
        }, timeout=5).json()['result']
        queue_message(chat_id, f"✅ Achat réussi : {amount} SOL de `{contract_address}` (TX: `{tx_hash}`)")
        token_data = get_token_data_quicknode(contract_address)
        entry_price = token_data.get('price', 0)
        portfolio[contract_address] = {
            'amount': amount, 'chain': 'solana', 'entry_price': entry_price,
            'price_history': [entry_price], 'highest_price': entry_price, 'profit': 0.0,
            'buy_time': time.time()
        }
        daily_trades['buys'].append({'token': contract_address, 'amount': amount, 'timestamp': datetime.now().strftime('%H:%M:%S')})
    except Exception as e:
        queue_message(chat_id, f"⚠️ Échec achat Solana `{contract_address}`: `{str(e)}`")
        logger.error(f"Échec achat Solana: {str(e)}")

async def sell_token(chat_id, contract_address, amount, current_price):
    global mise_depart_sol
    try:
        if not solana_keypair:
            initialize_bot(chat_id)
            if not solana_keypair:
                raise Exception("Solana non initialisé")
        amount_out = int(amount * 10**9)
        response = session.post(QUICKNODE_SOL_URL, json={
            "jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash",
            "params": [{"commitment": "finalized"}]
        }, timeout=5)
        blockhash = response.json()['result']['value']['blockhash']
        tx = Transaction()
        tx.recent_blockhash = Pubkey.from_string(blockhash)
        instruction = Instruction(
            program_id=PUMP_FUN_PROGRAM_ID,
            accounts=[
                {"pubkey": solana_keypair.pubkey(), "is_signer": True, "is_writable": True},
                {"pubkey": Pubkey.from_string(contract_address), "is_signer": False, "is_writable": True},
                {"pubkey": TOKEN_PROGRAM_ID, "is_signer": False, "is_writable": False}
            ],
            data=bytes([3]) + amount_out.to_bytes(8, 'little')
        )
        tx.add(instruction)
        tx.sign([solana_keypair])
        tx_hash = session.post(QUICKNODE_SOL_URL, json={
            "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
            "params": [base58.b58encode(tx.serialize()).decode('utf-8')]
        }, timeout=5).json()['result']
        profit = (current_price - portfolio[contract_address]['entry_price']) * amount
        portfolio[contract_address]['profit'] += profit
        portfolio[contract_address]['amount'] -= amount
        reinvest_amount = profit * profit_reinvestment_ratio
        mise_depart_sol += reinvest_amount
        queue_message(chat_id, f"✅ Vente réussie : {amount} SOL, Profit: {profit:.4f} SOL (TX: `{tx_hash}`)")
        daily_trades['sells'].append({'token': contract_address, 'amount': amount, 'pnl': profit, 'timestamp': datetime.now().strftime('%H:%M:%S')})
        if portfolio[contract_address]['amount'] <= 0:
            del portfolio[contract_address]
    except Exception as e:
        queue_message(chat_id, f"⚠️ Échec vente Solana `{contract_address}`: `{str(e)}`")
        logger.error(f"Échec vente Solana: {str(e)}")

async def monitor_and_sell(chat_id):
    while not stop_event.is_set() and bot_active:
        try:
            if not portfolio:
                await asyncio.sleep(2)
                continue
            for contract_address, data in list(portfolio.items()):
                amount = data['amount']
                current_data = get_token_data_quicknode(contract_address)
                if not current_data:
                    continue
                current_price = current_data.get('price', 0)
                data['price_history'].append(current_price)
                if len(data['price_history']) > 10:
                    data['price_history'].pop(0)
                profit_pct = (current_price - data['entry_price']) / data['entry_price'] * 100 if data['entry_price'] > 0 else 0
                loss_pct = -profit_pct if profit_pct < 0 else 0
                data['highest_price'] = max(data['highest_price'], current_price)
                trailing_stop_price = data['highest_price'] * (1 - trailing_stop_percentage / 100)

                if not pause_auto_sell:
                    if profit_pct >= take_profit_steps[4] * 100:
                        await sell_token(chat_id, contract_address, amount * 0.5, current_price)
                    elif profit_pct >= take_profit_steps[3] * 100:
                        await sell_token(chat_id, contract_address, amount * 0.25, current_price)
                    elif profit_pct >= take_profit_steps[2] * 100:
                        await sell_token(chat_id, contract_address, amount * 0.2, current_price)
                    elif profit_pct >= take_profit_steps[1] * 100:
                        await sell_token(chat_id, contract_address, amount * 0.15, current_price)
                    elif profit_pct >= take_profit_steps[0] * 100:
                        await sell_token(chat_id, contract_address, amount * 0.1, current_price)
                    elif current_price <= trailing_stop_price or loss_pct >= stop_loss_threshold:
                        await sell_token(chat_id, contract_address, amount, current_price)
            await asyncio.sleep(0.5)
        except Exception as e:
            queue_message(chat_id, f"⚠️ Erreur surveillance: `{str(e)}`")
            logger.error(f"Erreur surveillance: {str(e)}")
            await asyncio.sleep(5)

async def show_portfolio(chat_id):
    try:
        sol_balance = await get_solana_balance(chat_id)
        msg = f"💰 *Portefeuille:*\nSOL : {sol_balance:.4f}\n\n"
        markup = InlineKeyboardMarkup()
        for ca, data in portfolio.items():
            current_price = get_token_data_quicknode(ca).get('price', 0)
            profit = (current_price - data['entry_price']) * data['amount']
            markup.add(
                InlineKeyboardButton(f"💸 Sell 25% {ca[:6]}", callback_data=f"sell_pct_{ca}_25"),
                InlineKeyboardButton(f"💸 Sell 50% {ca[:6]}", callback_data=f"sell_pct_{ca}_50"),
                InlineKeyboardButton(f"💸 Sell 100% {ca[:6]}", callback_data=f"sell_{ca}")
            )
            msg += (
                f"*Token:* `{ca}` (Solana)\n"
                f"Montant: {data['amount']:.4f} SOL\n"
                f"Prix d’entrée: ${data['entry_price']:.6f}\n"
                f"Prix actuel: ${current_price:.6f}\n"
                f"Profit: {profit:.4f} SOL\n\n"
            )
        queue_message(chat_id, msg, reply_markup=markup if portfolio else None)
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur portefeuille: `{str(e)}`")
        logger.error(f"Erreur portefeuille: {str(e)}")

async def get_solana_balance(chat_id):
    try:
        if not solana_keypair:
            initialize_bot(chat_id)
        if not solana_keypair:
            return 0
        response = session.post(QUICKNODE_SOL_URL, json={
            "jsonrpc": "2.0", "id": 1, "method": "getBalance",
            "params": [str(solana_keypair.pubkey())]
        }, timeout=5)
        return response.json().get('result', {}).get('value', 0) / 10**9
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur solde Solana: `{str(e)}`")
        return 0

async def show_daily_summary(chat_id):
    try:
        msg = f"📅 *Récapitulatif du jour ({datetime.now().strftime('%Y-%m-%d')})*:\n\n"
        msg += "📈 *Achats* :\n"
        total_buys = 0
        for trade in daily_trades['buys']:
            total_buys += trade['amount']
            msg += f"- `{trade['token']}` : {trade['amount']} SOL à {trade['timestamp']}\n"
        msg += f"Total investi : {total_buys:.4f} SOL\n\n"
        msg += "📉 *Ventes* :\n"
        total_profit = 0
        for trade in daily_trades['sells']:
            total_profit += trade['pnl']
            msg += f"- `{trade['token']}` : {trade['amount']} SOL à {trade['timestamp']}, PNL: {trade['pnl']:.4f} SOL\n"
        msg += f"Profit net : {total_profit:.4f} SOL\n"
        queue_message(chat_id, msg)
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur récapitulatif: `{str(e)}`")
        logger.error(f"Erreur récapitulatif: {str(e)}")

async def adjust_mise_sol(message):
    global mise_depart_sol
    chat_id = message.chat.id
    try:
        new_mise = float(message.text)
        if new_mise > 0:
            mise_depart_sol = new_mise
            queue_message(chat_id, f"✅ Mise Solana mise à jour à {mise_depart_sol} SOL")
        else:
            queue_message(chat_id, "⚠️ La mise doit être positive!")
    except ValueError:
        queue_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 0.37)")

async def adjust_stop_loss(message):
    global stop_loss_threshold
    chat_id = message.chat.id
    try:
        new_sl = float(message.text)
        if new_sl > 0:
            stop_loss_threshold = new_sl
            queue_message(chat_id, f"✅ Stop-Loss mis à jour à {stop_loss_threshold} %")
        else:
            queue_message(chat_id, "⚠️ Le Stop-Loss doit être positif!")
    except ValueError:
        queue_message(chat_id, "⚠️ Erreur : Entrez un pourcentage valide (ex. : 15)")

async def adjust_take_profit(message):
    global take_profit_steps
    chat_id = message.chat.id
    try:
        new_tp = [float(x) for x in message.text.split(",")]
        if len(new_tp) == 5 and all(x > 0 for x in new_tp):
            take_profit_steps = new_tp
            queue_message(chat_id, f"✅ Take-Profit mis à jour à x{take_profit_steps[0]}, x{take_profit_steps[1]}, x{take_profit_steps[2]}, x{take_profit_steps[3]}, x{take_profit_steps[4]}")
        else:
            queue_message(chat_id, "⚠️ Entrez 5 valeurs positives séparées par des virgules (ex. : 1.2,2,10,100,500)")
    except ValueError:
        queue_message(chat_id, "⚠️ Erreur : Entrez des nombres valides (ex. : 1.2,2,10,100,500)")

async def adjust_reinvestment_ratio(message):
    global profit_reinvestment_ratio
    chat_id = message.chat.id
    try:
        new_ratio = float(message.text)
        if 0 <= new_ratio <= 1:
            profit_reinvestment_ratio = new_ratio
            queue_message(chat_id, f"✅ Ratio de réinvestissement mis à jour à {profit_reinvestment_ratio * 100}%")
        else:
            queue_message(chat_id, "⚠️ Le ratio doit être entre 0 et 1 (ex. : 0.9)")
    except ValueError:
        queue_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 0.9)")

async def adjust_detection_criteria(message):
    global MIN_VOLUME_SOL, MAX_VOLUME_SOL, MIN_LIQUIDITY, MIN_MARKET_CAP_SOL, MAX_MARKET_CAP_SOL, MIN_BUY_SELL_RATIO, MAX_TOKEN_AGE_HOURS
    chat_id = message.chat.id
    try:
        criteria = message.text.split(",")
        if len(criteria) != 7:
            queue_message(chat_id, "⚠️ Entrez 7 valeurs séparées par des virgules (ex. : 100,2000000,5000,1000,5000000,1.5,6)")
            return
        min_vol, max_vol, min_liq, min_mc, max_mc, min_ratio, max_age = map(float, criteria)
        if min_vol < 0 or max_vol < min_vol or min_liq < 0 or min_mc < 0 or max_mc < min_mc or min_ratio < 0 or max_age < 0:
            queue_message(chat_id, "⚠️ Valeurs invalides ! Assurez-vous que toutes sont positives et cohérentes.")
            return
        MIN_VOLUME_SOL = min_vol
        MAX_VOLUME_SOL = max_vol
        MIN_LIQUIDITY = min_liq
        MIN_MARKET_CAP_SOL = min_mc
        MAX_MARKET_CAP_SOL = max_mc
        MIN_BUY_SELL_RATIO = min_ratio
        MAX_TOKEN_AGE_HOURS = max_age
        queue_message(chat_id, (
            f"✅ Critères de détection mis à jour :\n"
            f"Volume : [{MIN_VOLUME_SOL}, {MAX_VOLUME_SOL}] SOL\n"
            f"Liquidité min : {MIN_LIQUIDITY} $\n"
            f"Market Cap : [{MIN_MARKET_CAP_SOL}, {MAX_MARKET_CAP_SOL}] $\n"
            f"Ratio A/V min : {MIN_BUY_SELL_RATIO}\n"
            f"Âge max : {MAX_TOKEN_AGE_HOURS} h"
        ))
    except ValueError:
        queue_message(chat_id, "⚠️ Erreur : Entrez des nombres valides (ex. : 100,2000000,5000,1000,5000000,1.5,6)")

def run_task_in_thread(task, *args):
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(task(*args))
        loop.close()
    except Exception as e:
        logger.error(f"Erreur dans thread {task.__name__}: {str(e)}")
        queue_message(args[0], f"⚠️ Erreur thread `{task.__name__}`: `{str(e)}`")

def initialize_and_run_threads(chat_id):
    global trade_active, chat_id_global, active_threads, bot_active
    chat_id_global = chat_id
    if not bot_active:
        queue_message(chat_id, "⚠️ Bot arrêté, relancez avec /start")
        return
    try:
        initialize_bot(chat_id)
        if solana_keypair:
            trade_active = True
            stop_event.clear()
            queue_message(chat_id, "▶️ Trading Solana lancé avec succès!")
            logger.info(f"Trading démarré pour chat_id {chat_id}, trade_active={trade_active}")
            tasks = [monitor_and_sell]
            active_threads = []
            for task in tasks:
                thread = threading.Thread(target=run_task_in_thread, args=(task, chat_id), daemon=True)
                thread.start()
                active_threads.append(thread)
                logger.info(f"Tâche {task.__name__} lancée")
        else:
            queue_message(chat_id, "⚠️ Échec initialisation : Solana non connecté")
            logger.error("Échec initialisation: Solana manquant")
            trade_active = False
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur initialisation: `{str(e)}`")
        logger.error(f"Erreur initialisation: {str(e)}")
        trade_active = False

@app.route("/webhook", methods=['POST'])
def webhook():
    if not bot_active:
        logger.info("Webhook Telegram reçu mais bot inactif")
        return 'Bot stopped', 503
    if request.method == "POST" and request.headers.get("content-type") == "application/json":
        update_json = request.get_json()
        logger.info(f"Webhook Telegram reçu: {json.dumps(update_json)}")
        try:
            update = telebot.types.Update.de_json(update_json)
            bot.process_new_updates([update])
            return 'OK', 200
        except Exception as e:
            logger.error(f"Erreur traitement webhook Telegram: {str(e)}")
            queue_message(chat_id_global, f"⚠️ Erreur webhook Telegram: `{str(e)}`")
            return f"Erreur: {str(e)}", 500
    logger.error("Requête webhook Telegram invalide")
    return abort(403)

@bot.message_handler(commands=['start', 'Start', 'START'])
def start_message(message):
    global trade_active, bot_active
    chat_id = message.chat.id
    logger.info(f"Commande /start reçue de {chat_id}")
    if not bot_active:
        bot_active = True
        queue_message(chat_id, "✅ Bot redémarré!")
    if not trade_active:
        queue_message(chat_id, "✅ Bot démarré!")
        initialize_and_run_threads(chat_id)
    else:
        queue_message(chat_id, "ℹ️ Trading déjà actif!")

@bot.message_handler(commands=['menu', 'Menu', 'MENU'])
def menu_message(message):
    chat_id = message.chat.id
    logger.info(f"Commande /menu reçue de {chat_id}")
    queue_message(chat_id, "ℹ️ Affichage du menu...")
    show_main_menu(chat_id)

@bot.message_handler(commands=['stop', 'Stop', 'STOP'])
def stop_message(message):
    global trade_active, active_threads, bot_active
    chat_id = message.chat.id
    logger.info(f"Commande /stop reçue de {chat_id}")
    if trade_active or bot_active:
        trade_active = False
        bot_active = False
        stop_event.set()
        for thread in active_threads:
            if thread.is_alive():
                logger.info(f"Attente arrêt du thread {thread.name}")
                thread.join(timeout=2)
        active_threads = []
        while not message_queue.empty():
            message_queue.get()
        queue_message(chat_id, "⏹️ Trading et bot arrêtés.")
        logger.info("Trading et bot arrêtés, threads réinitialisés")
    else:
        queue_message(chat_id, "ℹ️ Trading déjà arrêté!")

@bot.message_handler(commands=['pause', 'Pause', 'PAUSE'])
def pause_auto_sell_handler(message):
    global pause_auto_sell
    chat_id = message.chat.id
    logger.info(f"Commande /pause reçue de {chat_id}")
    pause_auto_sell = True
    queue_message(chat_id, "⏸️ Ventes automatiques désactivées.")

@bot.message_handler(commands=['resume', 'Resume', 'RESUME'])
def resume_auto_sell_handler(message):
    global pause_auto_sell
    chat_id = message.chat.id
    logger.info(f"Commande /resume reçue de {chat_id}")
    pause_auto_sell = False
    queue_message(chat_id, "▶️ Ventes automatiques réactivées.")

def show_main_menu(chat_id):
    try:
        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton("ℹ️ Statut", callback_data="status"),
            InlineKeyboardButton("▶️ Lancer", callback_data="launch"),
            InlineKeyboardButton("⏹️ Arrêter", callback_data="stop")
        )
        markup.add(
            InlineKeyboardButton("💰 Portefeuille", callback_data="portfolio"),
            InlineKeyboardButton("📅 Récapitulatif", callback_data="daily_summary")
        )
        markup.add(
            InlineKeyboardButton("🔧 Ajuster Mise SOL", callback_data="adjust_mise_sol"),
            InlineKeyboardButton("📉 Ajuster Stop-Loss", callback_data="adjust_stop_loss")
        )
        markup.add(
            InlineKeyboardButton("📈 Ajuster Take-Profit", callback_data="adjust_take_profit"),
            InlineKeyboardButton("🔄 Ajuster Réinvestissement", callback_data="adjust_reinvestment")
        )
        markup.add(
            InlineKeyboardButton("🔍 Ajuster Critères Détection", callback_data="adjust_detection_criteria")
        )
        queue_message(chat_id, "*Menu principal:*", reply_markup=markup)
        logger.info(f"Menu affiché pour {chat_id}")
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur affichage menu: `{str(e)}`")
        logger.error(f"Erreur affichage menu: {str(e)}")

@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    global trade_active, active_threads, bot_active
    chat_id = call.message.chat.id
    logger.info(f"Callback reçu: {call.data} de {chat_id}")
    try:
        if not bot_active:
            queue_message(chat_id, "⚠️ Bot arrêté, relancez avec /start")
            return
        if call.data == "status":
            sol_balance = asyncio.run(get_solana_balance(chat_id))
            queue_message(chat_id, (
                f"ℹ️ *Statut actuel* :\n"
                f"Trading actif: {'Oui' if trade_active else 'Non'}\n"
                f"SOL disponible: {sol_balance:.4f}\n"
                f"Positions: {len(portfolio)}/{max_positions}\n"
                f"Mise Solana: {mise_depart_sol} SOL\n"
                f"Stop-Loss: {stop_loss_threshold}%\n"
                f"Take-Profit: x{take_profit_steps[0]}, x{take_profit_steps[1]}, x{take_profit_steps[2]}, x{take_profit_steps[3]}, x{take_profit_steps[4]}\n"
                f"Critères détection: Volume [{MIN_VOLUME_SOL}, {MAX_VOLUME_SOL}] SOL, Liquidité min {MIN_LIQUIDITY} $, Market Cap [{MIN_MARKET_CAP_SOL}, {MAX_MARKET_CAP_SOL}] $, Ratio A/V min {MIN_BUY_SELL_RATIO}, Âge max {MAX_TOKEN_AGE_HOURS} h"
            ))
        elif call.data == "launch":
            if not trade_active:
                initialize_and_run_threads(chat_id)
            else:
                queue_message(chat_id, "ℹ️ Trading déjà actif!")
        elif call.data == "stop":
            if trade_active or bot_active:
                trade_active = False
                bot_active = False
                stop_event.set()
                for thread in active_threads:
                    if thread.is_alive():
                        logger.info(f"Attente arrêt du thread {thread.name}")
                        thread.join(timeout=2)
                active_threads = []
                while not message_queue.empty():
                    message_queue.get()
                queue_message(chat_id, "⏹️ Trading et bot arrêtés.")
                logger.info("Trading et bot arrêtés via callback")
            else:
                queue_message(chat_id, "ℹ️ Trading déjà arrêté!")
        elif call.data == "portfolio":
            asyncio.run(show_portfolio(chat_id))
        elif call.data == "daily_summary":
            asyncio.run(show_daily_summary(chat_id))
        elif call.data == "adjust_mise_sol":
            queue_message(chat_id, "Entrez la nouvelle mise Solana (ex. : 0.37) :")
            bot.register_next_step_handler(call.message, adjust_mise_sol)
        elif call.data == "adjust_stop_loss":
            queue_message(chat_id, "Entrez le nouveau Stop-Loss (ex. : 15) :")
            bot.register_next_step_handler(call.message, adjust_stop_loss)
        elif call.data == "adjust_take_profit":
            queue_message(chat_id, "Entrez les nouveaux Take-Profit (ex. : 1.2,2,10,100,500) :")
            bot.register_next_step_handler(call.message, adjust_take_profit)
        elif call.data == "adjust_reinvestment":
            queue_message(chat_id, "Entrez le nouveau ratio de réinvestissement (ex. : 0.9) :")
            bot.register_next_step_handler(call.message, adjust_reinvestment_ratio)
        elif call.data == "adjust_detection_criteria":
            queue_message(chat_id, "Entrez les nouveaux critères (min_vol, max_vol, min_liq, min_mc, max_mc, min_ratio, max_age) ex. : 100,2000000,5000,1000,5000000,1.5,6 :")
            bot.register_next_step_handler(call.message, adjust_detection_criteria)
        elif call.data.startswith("sell_pct_"):
            parts = call.data.split("_")
            contract_address, pct = parts[2], int(parts[3]) / 100
            if contract_address in portfolio:
                current_data = get_token_data_quicknode(contract_address)
                if current_data:
                    amount = portfolio[contract_address]['amount'] * pct
                    asyncio.run(sell_token(chat_id, contract_address, amount, current_data['price']))
        elif call.data.startswith("sell_"):
            contract_address = call.data.split("_")[1]
            if contract_address in portfolio:
                current_data = get_token_data_quicknode(contract_address)
                if current_data:
                    amount = portfolio[contract_address]['amount']
                    asyncio.run(sell_token(chat_id, contract_address, amount, current_data['price']))
    except Exception as e:
        queue_message(chat_id, f"⚠️ Erreur callback: `{str(e)}`")
        logger.error(f"Erreur callback: {str(e)}")

if __name__ == "__main__":
    if not all([TELEGRAM_TOKEN, WALLET_ADDRESS, SOLANA_PRIVATE_KEY, WEBHOOK_URL, QUICKNODE_SOL_URL]):
        logger.error("Variables d’environnement manquantes")
        exit(1)
    if set_webhook():
        logger.info("Webhook Telegram configuré, démarrage du serveur...")
        serve(app, host="0.0.0.0", port=PORT, threads=10)
    else:
        logger.error("Échec du webhook Telegram, passage en mode polling")
        bot.polling(none_stop=True)
