import os
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import logging
import requests
import time
import json
import base58
from flask import Flask, request, abort
from web3 import Web3
from web3.middleware import geth_poa_middleware
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from solders.instruction import Instruction
import threading
from datetime import datetime
import re
from waitress import serve
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
retry_strategy = Retry(total=5, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("https://", adapter)

# Variables globales
last_twitter_call = 0
twitter_last_reset = time.time()
twitter_requests_remaining = 450
daily_trades = {'buys': [], 'sells': []}
rejected_tokens = {}
trade_active = False
portfolio = {}
detected_tokens = {}

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "default_token")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "0x0")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "dummy_key")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://default.example.com/webhook")
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY", "dummy_solana_key")
TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN", "dummy_twitter_token")
QUICKNODE_URL = os.getenv("QUICKNODE_URL", "https://your-quicknode-endpoint")  # Ajoutez votre URL QuickNode ici
PORT = int(os.getenv("PORT", 8080))

BSC_NODES = [
    "https://bsc-dataseed1.binance.org/",
    "https://bsc-dataseed2.binance.org/",
    "https://bsc-dataseed3.binance.org/",
    "https://bsc-dataseed4.binance.org/",
    "https://bsc-dataseed1.ninicoin.io/",
    "https://bsc-dataseed1.defibit.io/"
]
SOLANA_NODES = ["https://api.mainnet-beta.solana.com", "https://solana-api.projectserum.com"]
ETH_NODES = [QUICKNODE_URL]  # Utilise QuickNode pour Ethereum
TWITTER_HEADERS = {"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"}

app = Flask(__name__)
bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=False)

w3_bsc = None
w3_eth = None
solana_keypair = None

mise_depart_bsc = 0.02
mise_depart_sol = 0.37
mise_depart_eth = 0.05  # Nouvelle mise pour Ethereum
gas_fee = 10
stop_loss_threshold = 15
trailing_stop_percentage = 5
take_profit_steps = [1.5, 2, 5]
max_positions = 5
profit_reinvestment_ratio = 0.7

MIN_VOLUME_SOL = 0
MAX_VOLUME_SOL = 2000000
MIN_VOLUME_BSC = 0
MAX_VOLUME_BSC = 2000000
MIN_VOLUME_ETH = 0
MAX_VOLUME_ETH = 2000000
MIN_LIQUIDITY = 100
MIN_MARKET_CAP_SOL = 500
MAX_MARKET_CAP_SOL = 3000000
MIN_MARKET_CAP_BSC = 500
MAX_MARKET_CAP_BSC = 3000000
MIN_MARKET_CAP_ETH = 500
MAX_MARKET_CAP_ETH = 3000000
MIN_BUY_SELL_RATIO = 0.5
MAX_TOKEN_AGE_HOURS = 12

ERC20_ABI = json.loads('[{"constant": true, "inputs": [], "name": "totalSupply", "outputs": [{"name": "", "type": "uint256"}], "payable": false, "stateMutability": "view", "type": "function"}]')
PANCAKE_ROUTER_ADDRESS = "0x10ED43C718714eb63d5aA57B78B54704E256024E"
PANCAKE_FACTORY_ADDRESS = "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"
UNISWAP_ROUTER_ADDRESS = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"  # Uniswap V2 Router
UNISWAP_FACTORY_ADDRESS = "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f"  # Uniswap V2 Factory
PANCAKE_ROUTER_ABI = json.loads('[{"inputs": [{"internalType": "uint256", "name": "amountIn", "type": "uint256"}, {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"}, {"internalType": "address[]", "name": "path", "type": "address[]"}, {"internalType": "address", "name": "to", "type": "address"}, {"internalType": "uint256", "name": "deadline", "type": "uint256"}], "name": "swapExactETHForTokens", "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}], "stateMutability": "payable", "type": "function"}, {"inputs": [{"internalType": "uint256", "name": "amountOut", "type": "uint256"}, {"internalType": "uint256", "name": "amountInMax", "type": "uint256"}, {"internalType": "address[]", "name": "path", "type": "address[]"}, {"internalType": "address", "name": "to", "type": "address"}, {"internalType": "uint256", "name": "deadline", "type": "uint256"}], "name": "swapExactTokensForETH", "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}], "stateMutability": "nonpayable", "type": "function"}]')
PANCAKE_FACTORY_ABI = json.loads('[{"anonymous":false,"inputs":[{"indexed":true,"internalType":"address","name":"token0","type":"address"},{"indexed":true,"internalType":"address","name":"token1","type":"address"},{"indexed":false,"internalType":"address","name":"pair","type":"address"},{"indexed":false,"internalType":"uint256","name":"","type":"uint256"}],"name":"PairCreated","type":"event"}]')
RAYDIUM_PROGRAM_ID = Pubkey.from_string("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8")
PUMP_FUN_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfH43SboMiMEWCPkDPk")
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")

def initialize_bot():
    global w3_bsc, w3_eth, solana_keypair
    # BSC
    for node in BSC_NODES:
        try:
            w3_bsc = Web3(Web3.HTTPProvider(node))
            w3_bsc.middleware_onion.inject(geth_poa_middleware, layer=0)
            if w3_bsc.is_connected():
                logger.info(f"Connexion BSC réussie via {node}")
                bot.send_message(chat_id_global, f"✅ Connexion BSC établie : {node}")
                break
        except Exception as e:
            logger.error(f"Erreur connexion BSC {node}: {str(e)}")
            bot.send_message(chat_id_global, f"⚠️ Erreur connexion BSC {node}: {str(e)}")
    if not w3_bsc or not w3_bsc.is_connected():
        w3_bsc = None
        bot.send_message(chat_id_global, "⚠️ Aucun nœud BSC fonctionnel")

    # Ethereum via QuickNode
    try:
        w3_eth = Web3(Web3.HTTPProvider(QUICKNODE_URL))
        if w3_eth.is_connected():
            logger.info(f"Connexion Ethereum réussie via QuickNode")
            bot.send_message(chat_id_global, "✅ Connexion Ethereum établie : QuickNode")
        else:
            raise Exception("Échec de connexion")
    except Exception as e:
        logger.error(f"Erreur connexion Ethereum: {str(e)}")
        bot.send_message(chat_id_global, f"⚠️ Erreur connexion Ethereum: {str(e)}")
        w3_eth = None

    # Solana
    try:
        solana_keypair = Keypair.from_bytes(base58.b58decode(SOLANA_PRIVATE_KEY))
        logger.info("Clé Solana initialisée")
        bot.send_message(chat_id_global, "✅ Clé Solana initialisée")
    except Exception as e:
        logger.error(f"Erreur initialisation Solana: {str(e)}")
        bot.send_message(chat_id_global, f"⚠️ Erreur initialisation Solana: {str(e)}")
        solana_keypair = None

def set_webhook():
    try:
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(url=WEBHOOK_URL)
        logger.info(f"Webhook configuré sur {WEBHOOK_URL}")
        return True
    except Exception as e:
        logger.error(f"Erreur configuration webhook: {str(e)}")
        return False

def validate_address(token_address, chain):
    if chain in ['bsc', 'eth']:
        return bool(re.match(r'^0x[a-fA-F0-9]{40}$', token_address))
    elif chain == 'solana':
        try:
            Pubkey.from_string(token_address)
            return len(token_address) >= 32 and len(token_address) <= 44
        except ValueError:
            return False
    return False

def get_token_data(token_address, chain):
    for attempt in range(5):
        try:
            response = session.get(f"https://api.dexscreener.com/latest/dex/tokens/{token_address}", timeout=5)
            data = response.json()['pairs'][0] if response.json()['pairs'] else {}
            if not data or data.get('chainId') != chain:
                return {'volume_24h': 0, 'liquidity': 0, 'market_cap': 0, 'price': 0, 'buy_sell_ratio': 1, 'pair_created_at': time.time()}
            
            volume_24h = float(data.get('volume', {}).get('h24', 0))
            liquidity = float(data.get('liquidity', {}).get('usd', 0))
            market_cap = float(data.get('marketCap', 0))
            price = float(data.get('priceUsd', 0))
            buy_count_5m = float(data.get('txns', {}).get('m5', {}).get('buys', 0))
            sell_count_5m = float(data.get('txns', {}).get('m5', {}).get('sells', 0))
            buy_sell_ratio = buy_count_5m / max(sell_count_5m, 1)
            pair_created_at = data.get('pairCreatedAt', 0) / 1000 if data.get('pairCreatedAt') else time.time() - 3600
            
            return {
                'volume_24h': volume_24h, 'liquidity': liquidity, 'market_cap': market_cap,
                'price': price, 'buy_sell_ratio': buy_sell_ratio, 'pair_created_at': pair_created_at
            }
        except Exception as e:
            bot.send_message(chat_id_global, f"⚠️ Erreur DexScreener {token_address}: {str(e)}")
            if attempt < 4:
                time.sleep(2 ** attempt)  # Backoff exponentiel
            else:
                # Fallback : accepter volume 0 pour nouveaux tokens
                return {'volume_24h': 0, 'liquidity': 0, 'market_cap': 0, 'price': 0, 'buy_sell_ratio': 1, 'pair_created_at': time.time()}

def snipe_new_pairs_bsc(chat_id):
    global w3_bsc
    if w3_bsc is None or not w3_bsc.is_connected():
        initialize_bot()
    if not w3_bsc or not w3_bsc.is_connected():
        bot.send_message(chat_id, "⚠️ BSC non connecté, sniping désactivé")
        return
    last_block = w3_bsc.eth.block_number - 10
    node_index = 0
    while trade_active:
        try:
            current_block = w3_bsc.eth.block_number
            if current_block > last_block:
                logs = w3_bsc.eth.get_logs({
                    'fromBlock': last_block,
                    'toBlock': current_block,
                    'address': PANCAKE_FACTORY_ADDRESS
                })
                for log in logs:
                    if log['topics'][0].hex() == w3_bsc.sha3(text="PairCreated(address,address,address,uint256)").hex():
                        token0 = w3_bsc.to_checksum_address(log['data'][2:66][-40:])
                        token1 = w3_bsc.to_checksum_address(log['data'][66:130][-40:])
                        token_address = token0 if token0 != "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c" else token1
                        if token_address not in portfolio and (token_address not in rejected_tokens or (time.time() - rejected_tokens[token_address]) / 3600 > 12):
                            bot.send_message(chat_id, f'🎯 Snipe détecté : {token_address} (BSC)')
                            validate_and_trade(chat_id, token_address, 'bsc')
                last_block = current_block
            time.sleep(0.1)  # Polling rapide pour ne rien rater
        except Exception as e:
            bot.send_message(chat_id, f"⚠️ Erreur sniping BSC: {str(e)}")
            node_index = (node_index + 1) % len(BSC_NODES)
            w3_bsc = Web3(Web3.HTTPProvider(BSC_NODES[node_index]))
            time.sleep(5)

def snipe_new_pairs_eth(chat_id):
    global w3_eth
    if w3_eth is None or not w3_eth.is_connected():
        initialize_bot()
    if not w3_eth or not w3_eth.is_connected():
        bot.send_message(chat_id, "⚠️ Ethereum non connecté, sniping désactivé")
        return
    last_block = w3_eth.eth.block_number - 10
    while trade_active:
        try:
            current_block = w3_eth.eth.block_number
            if current_block > last_block:
                logs = w3_eth.eth.get_logs({
                    'fromBlock': last_block,
                    'toBlock': current_block,
                    'address': UNISWAP_FACTORY_ADDRESS
                })
                for log in logs:
                    if log['topics'][0].hex() == w3_eth.sha3(text="PairCreated(address,address,address,uint256)").hex():
                        token0 = w3_eth.to_checksum_address(log['data'][2:66][-40:])
                        token1 = w3_eth.to_checksum_address(log['data'][66:130][-40:])
                        token_address = token0 if token0 != "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2" else token1  # WETH
                        if token_address not in portfolio and (token_address not in rejected_tokens or (time.time() - rejected_tokens[token_address]) / 3600 > 12):
                            bot.send_message(chat_id, f'🎯 Snipe détecté : {token_address} (Ethereum)')
                            validate_and_trade(chat_id, token_address, 'eth')
                last_block = current_block
            time.sleep(0.1)
        except Exception as e:
            bot.send_message(chat_id, f"⚠️ Erreur sniping Ethereum: {str(e)}")
            time.sleep(5)

def snipe_solana_pools(chat_id):
    last_signature_raydium = None
    last_signature_pump = None
    node_index = 0
    while trade_active:
        node = SOLANA_NODES[node_index % len(SOLANA_NODES)]
        try:
            # Raydium
            response = session.post(node, json={
                "jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress",
                "params": [str(RAYDIUM_PROGRAM_ID), {"limit": 10}]
            }, timeout=1)
            signatures = response.json().get('result', [])
            for sig in signatures:
                if last_signature_raydium and sig['signature'] == last_signature_raydium:
                    break
                tx = session.post(node, json={
                    "jsonrpc": "2.0", "id": 1, "method": "getTransaction",
                    "params": [sig['signature'], {"encoding": "jsonParsed"}]
                }, timeout=1).json()
                if 'result' in tx and tx['result']:
                    for account in tx['result']['transaction']['message']['accountKeys']:
                        token_address = account['pubkey']
                        if validate_address(token_address, 'solana') and token_address not in portfolio:
                            bot.send_message(chat_id, f'🎯 Snipe détecté : {token_address} (Solana - Raydium)')
                            validate_and_trade(chat_id, token_address, 'solana')
            if signatures:
                last_signature_raydium = signatures[0]['signature']

            # Pump.fun
            response = session.post(node, json={
                "jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress",
                "params": [str(PUMP_FUN_PROGRAM_ID), {"limit": 10}]
            }, timeout=1)
            signatures = response.json().get('result', [])
            for sig in signatures:
                if last_signature_pump and sig['signature'] == last_signature_pump:
                    break
                tx = session.post(node, json={
                    "jsonrpc": "2.0", "id": 1, "method": "getTransaction",
                    "params": [sig['signature'], {"encoding": "jsonParsed"}]
                }, timeout=1).json()
                if 'result' in tx and tx['result']:
                    for account in tx['result']['transaction']['message']['accountKeys']:
                        token_address = account['pubkey']
                        if validate_address(token_address, 'solana') and token_address not in portfolio:
                            bot.send_message(chat_id, f'🎯 Snipe détecté : {token_address} (Solana - Pump.fun)')
                            validate_and_trade(chat_id, token_address, 'solana')
            if signatures:
                last_signature_pump = signatures[0]['signature']
            time.sleep(0.1)
        except Exception as e:
            bot.send_message(chat_id, f"⚠️ Erreur sniping Solana {node}: {str(e)}")
            node_index = (node_index + 1) % len(SOLANA_NODES)
            time.sleep(5)

def monitor_twitter(chat_id):
    global twitter_requests_remaining, twitter_last_reset, last_twitter_call
    while trade_active:
        try:
            current_time = time.time()
            if current_time - twitter_last_reset >= 900:
                twitter_requests_remaining = 450
                twitter_last_reset = current_time
                bot.send_message(chat_id, "ℹ️ Quota Twitter réinitialisé : 450 requêtes restantes")

            if twitter_requests_remaining <= 0:
                wait_time = 900 - (current_time - twitter_last_reset) + 1
                bot.send_message(chat_id, f"⏳ Twitter en attente : {int(wait_time)}s avant réinitialisation")
                time.sleep(wait_time)
                continue

            if current_time - last_twitter_call < 900:
                time.sleep(900 - (current_time - last_twitter_call))
                continue

            response = session.get(
                "https://api.twitter.com/2/tweets/search/recent?query=\"contract address\" OR CA OR launch OR pump -is:retweet&max_results=100&expansions=author_id&user.fields=public_metrics",
                headers=TWITTER_HEADERS, timeout=5
            )
            response.raise_for_status()
            twitter_requests_remaining -= 1
            last_twitter_call = current_time
            data = response.json()
            tweets = data.get('data', [])
            users = {u['id']: u for u in data.get('includes', {}).get('users', [])}

            for tweet in tweets:
                user = users.get(tweet['author_id'])
                followers = user.get('public_metrics', {}).get('followers_count', 0) if user else 0
                if followers >= 1000:
                    text = tweet['text'].lower()
                    words = text.split()
                    for word in words:
                        chain = 'bsc' if word.startswith("0x") and len(word) == 42 else 'solana' if len(word) in [32, 44] else 'eth'
                        if validate_address(word, chain) and word not in portfolio:
                            if word in rejected_tokens and (time.time() - rejected_tokens[word]) / 3600 < 12:
                                continue
                            bot.send_message(chat_id, f'🔍 Token détecté via Twitter (@{user["username"]}, {followers} abonnés): {word} ({chain})')
                            validate_and_trade(chat_id, word, chain)
        except Exception as e:
            bot.send_message(chat_id, f"⚠️ Erreur Twitter: {str(e)}")
            time.sleep(60)

def validate_and_trade(chat_id, token_address, chain):
    try:
        data = get_token_data(token_address, chain)
        volume_24h = data['volume_24h']
        liquidity = data['liquidity']
        market_cap = data['market_cap']
        buy_sell_ratio = data['buy_sell_ratio']
        price = data['price']
        pair_created_at = data['pair_created_at']

        age_hours = (time.time() - pair_created_at) / 3600
        min_volume = MIN_VOLUME_BSC if chain == 'bsc' else MIN_VOLUME_ETH if chain == 'eth' else MIN_VOLUME_SOL
        max_volume = MAX_VOLUME_BSC if chain == 'bsc' else MAX_VOLUME_ETH if chain == 'eth' else MAX_VOLUME_SOL
        min_market_cap = MIN_MARKET_CAP_BSC if chain == 'bsc' else MIN_MARKET_CAP_ETH if chain == 'eth' else MIN_MARKET_CAP_SOL
        max_market_cap = MAX_MARKET_CAP_BSC if chain == 'bsc' else MAX_MARKET_CAP_ETH if chain == 'eth' else MAX_MARKET_CAP_SOL

        if len(portfolio) >= max_positions:
            bot.send_message(chat_id, f'⚠️ {token_address} rejeté : Limite de {max_positions} positions atteinte')
            return
        if age_hours > MAX_TOKEN_AGE_HOURS or age_hours < 0:
            bot.send_message(chat_id, f'⚠️ {token_address} rejeté : Âge {age_hours:.2f}h > {MAX_TOKEN_AGE_HOURS}h')
            rejected_tokens[token_address] = time.time()
            return
        if liquidity < MIN_LIQUIDITY and liquidity > 0:
            bot.send_message(chat_id, f'⚠️ {token_address} rejeté : Liquidité ${liquidity:.2f} < ${MIN_LIQUIDITY}')
            rejected_tokens[token_address] = time.time()
            return
        if volume_24h > max_volume:
            bot.send_message(chat_id, f'⚠️ {token_address} rejeté : Volume ${volume_24h:.2f} > ${max_volume}')
            rejected_tokens[token_address] = time.time()
            return
        if market_cap > max_market_cap:
            bot.send_message(chat_id, f'⚠️ {token_address} rejeté : Market Cap ${market_cap:.2f} > ${max_market_cap}')
            rejected_tokens[token_address] = time.time()
            return
        if buy_sell_ratio < MIN_BUY_SELL_RATIO and age_hours > 0.5:  # Tolérance pour nouveaux tokens
            bot.send_message(chat_id, f'⚠️ {token_address} rejeté : Ratio A/V {buy_sell_ratio:.2f} < {MIN_BUY_SELL_RATIO}')
            rejected_tokens[token_address] = time.time()
            return

        bot.send_message(chat_id, f'✅ Token validé : {token_address} ({chain}) - Vol: ${volume_24h:.2f}, Liq: ${liquidity:.2f}, MC: ${market_cap:.2f}')
        detected_tokens[token_address] = {
            'address': token_address, 'volume': volume_24h, 'liquidity': liquidity,
            'market_cap': market_cap, 'supply': market_cap / price if price > 0 else 0, 'price': price,
            'buy_sell_ratio': buy_sell_ratio
        }
        if chain == 'bsc':
            buy_token_bsc(chat_id, token_address, mise_depart_bsc)
        elif chain == 'eth':
            buy_token_eth(chat_id, token_address, mise_depart_eth)
        else:
            buy_token_solana(chat_id, token_address, mise_depart_sol)
    except Exception as e:
        bot.send_message(chat_id, f"⚠️ Erreur validation {token_address}: {str(e)}")

def buy_token_bsc(chat_id, contract_address, amount):
    try:
        if w3_bsc is None or not w3_bsc.is_connected():
            initialize_bot()
            if not w3_bsc.is_connected():
                raise Exception("BSC non connecté")
        router = w3_bsc.eth.contract(address=PANCAKE_ROUTER_ADDRESS, abi=PANCAKE_ROUTER_ABI)
        amount_in = w3_bsc.to_wei(amount, 'ether')
        amount_out_min = int(amount_in * 0.95)
        tx = router.functions.swapExactETHForTokens(
            amount_out_min,
            [w3_bsc.to_checksum_address('0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c'), w3_bsc.to_checksum_address(contract_address)],
            w3_bsc.to_checksum_address(WALLET_ADDRESS),
            int(time.time()) + 60
        ).build_transaction({
            'from': WALLET_ADDRESS, 'value': amount_in, 'gas': 200000,
            'gasPrice': w3_bsc.to_wei(gas_fee, 'gwei'), 'nonce': w3_bsc.eth.get_transaction_count(WALLET_ADDRESS)
        })
        signed_tx = w3_bsc.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash = w3_bsc.eth.send_raw_transaction(signed_tx.rawTransaction)
        bot.send_message(chat_id, f'⏳ Achat BSC {amount} BNB de {contract_address}, TX: {tx_hash.hex()}')
        receipt = w3_bsc.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
        if receipt.status == 1:
            entry_price = detected_tokens[contract_address]['price']
            portfolio[contract_address] = {
                'amount': amount, 'chain': 'bsc', 'entry_price': entry_price,
                'market_cap_at_buy': detected_tokens[contract_address]['market_cap'],
                'current_market_cap': detected_tokens[contract_address]['market_cap'],
                'price_history': [entry_price], 'highest_price': entry_price, 'profit': 0.0,
                'buy_time': time.time()
            }
            bot.send_message(chat_id, f'✅ Achat réussi : {amount} BNB de {contract_address}')
            daily_trades['buys'].append({'token': contract_address, 'chain': 'bsc', 'amount': amount, 'timestamp': datetime.now().strftime('%H:%M:%S')})
    except Exception as e:
        bot.send_message(chat_id, f"⚠️ Échec achat BSC {contract_address}: {str(e)}")

def buy_token_eth(chat_id, contract_address, amount):
    try:
        if w3_eth is None or not w3_eth.is_connected():
            initialize_bot()
            if not w3_eth.is_connected():
                raise Exception("Ethereum non connecté")
        router = w3_eth.eth.contract(address=UNISWAP_ROUTER_ADDRESS, abi=PANCAKE_ROUTER_ABI)  # Même ABI que PancakeSwap
        amount_in = w3_eth.to_wei(amount, 'ether')
        amount_out_min = int(amount_in * 0.95)
        tx = router.functions.swapExactETHForTokens(
            amount_out_min,
            [w3_eth.to_checksum_address('0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2'), w3_eth.to_checksum_address(contract_address)],  # WETH
            w3_eth.to_checksum_address(WALLET_ADDRESS),
            int(time.time()) + 60
        ).build_transaction({
            'from': WALLET_ADDRESS, 'value': amount_in, 'gas': 200000,
            'gasPrice': w3_eth.to_wei(gas_fee, 'gwei'), 'nonce': w3_eth.eth.get_transaction_count(WALLET_ADDRESS)
        })
        signed_tx = w3_eth.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash = w3_eth.eth.send_raw_transaction(signed_tx.rawTransaction)
        bot.send_message(chat_id, f'⏳ Achat Ethereum {amount} ETH de {contract_address}, TX: {tx_hash.hex()}')
        receipt = w3_eth.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
        if receipt.status == 1:
            entry_price = detected_tokens[contract_address]['price']
            portfolio[contract_address] = {
                'amount': amount, 'chain': 'eth', 'entry_price': entry_price,
                'market_cap_at_buy': detected_tokens[contract_address]['market_cap'],
                'current_market_cap': detected_tokens[contract_address]['market_cap'],
                'price_history': [entry_price], 'highest_price': entry_price, 'profit': 0.0,
                'buy_time': time.time()
            }
            bot.send_message(chat_id, f'✅ Achat réussi : {amount} ETH de {contract_address}')
            daily_trades['buys'].append({'token': contract_address, 'chain': 'eth', 'amount': amount, 'timestamp': datetime.now().strftime('%H:%M:%S')})
    except Exception as e:
        bot.send_message(chat_id, f"⚠️ Échec achat Ethereum {contract_address}: {str(e)}")

def buy_token_solana(chat_id, contract_address, amount):
    try:
        if solana_keypair is None:
            initialize_bot()
            if not solana_keypair:
                raise Exception("Solana non initialisé")
        node = SOLANA_NODES[0]
        amount_in = int(amount * 10**9)
        response = session.post(node, json={
            "jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash", "params": [{"commitment": "finalized"}]
        }, timeout=1)
        blockhash = response.json()['result']['value']['blockhash']
        tx = Transaction.from_recent_blockhash(Pubkey.from_string(blockhash))
        instruction = Instruction(
            program_id=RAYDIUM_PROGRAM_ID,
            accounts=[
                {"pubkey": solana_keypair.pubkey(), "is_signer": True, "is_writable": True},
                {"pubkey": Pubkey.from_string(contract_address), "is_signer": False, "is_writable": True},
                {"pubkey": TOKEN_PROGRAM_ID, "is_signer": False, "is_writable": False}
            ],
            data=bytes([2]) + amount_in.to_bytes(8, 'little')
        )
        tx.add(instruction)
        tx.sign([solana_keypair])
        tx_hash = session.post(node, json={
            "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
            "params": [base58.b58encode(tx.serialize()).decode('utf-8')]
        }, timeout=1).json()['result']
        bot.send_message(chat_id, f'⏳ Achat Solana {amount} SOL de {contract_address}, TX: {tx_hash}')
        entry_price = detected_tokens[contract_address]['price']
        portfolio[contract_address] = {
            'amount': amount, 'chain': 'solana', 'entry_price': entry_price,
            'market_cap_at_buy': detected_tokens[contract_address]['market_cap'],
            'current_market_cap': detected_tokens[contract_address]['market_cap'],
            'price_history': [entry_price], 'highest_price': entry_price, 'profit': 0.0,
            'buy_time': time.time()
        }
        bot.send_message(chat_id, f'✅ Achat réussi : {amount} SOL de {contract_address}')
        daily_trades['buys'].append({'token': contract_address, 'chain': 'solana', 'amount': amount, 'timestamp': datetime.now().strftime('%H:%M:%S')})
    except Exception as e:
        bot.send_message(chat_id, f"⚠️ Échec achat Solana {contract_address}: {str(e)}")

def sell_token(chat_id, contract_address, amount, chain, current_price):
    global mise_depart_bsc, mise_depart_sol, mise_depart_eth
    if chain == "solana":
        try:
            if solana_keypair is None:
                initialize_bot()
                if not solana_keypair:
                    raise Exception("Solana non initialisé")
            node = SOLANA_NODES[0]
            amount_out = int(amount * 10**9)
            response = session.post(node, json={
                "jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash", "params": [{"commitment": "finalized"}]
            }, timeout=1)
            blockhash = response.json()['result']['value']['blockhash']
            tx = Transaction.from_recent_blockhash(Pubkey.from_string(blockhash))
            instruction = Instruction(
                program_id=RAYDIUM_PROGRAM_ID,
                accounts=[
                    {"pubkey": solana_keypair.pubkey(), "is_signer": True, "is_writable": True},
                    {"pubkey": Pubkey.from_string(contract_address), "is_signer": False, "is_writable": True},
                    {"pubkey": TOKEN_PROGRAM_ID, "is_signer": False, "is_writable": False}
                ],
                data=bytes([3]) + amount_out.to_bytes(8, 'little')
            )
            tx.add(instruction)
            tx.sign([solana_keypair])
            tx_hash = session.post(node, json={
                "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                "params": [base58.b58encode(tx.serialize()).decode('utf-8')]
            }, timeout=1).json()['result']
            bot.send_message(chat_id, f'⏳ Vente Solana {amount} SOL de {contract_address}, TX: {tx_hash}')
            profit = (current_price - portfolio[contract_address]['entry_price']) * amount
            portfolio[contract_address]['profit'] += profit
            portfolio[contract_address]['amount'] -= amount
            if portfolio[contract_address]['amount'] <= 0:
                del portfolio[contract_address]
            reinvest_amount = profit * profit_reinvestment_ratio
            mise_depart_sol += reinvest_amount
            bot.send_message(chat_id, f'✅ Vente Solana réussie : {amount} SOL, Profit: {profit:.4f} SOL, Réinvesti: {reinvest_amount:.4f} SOL')
            daily_trades['sells'].append({'token': contract_address, 'chain': 'solana', 'amount': amount, 'pnl': profit, 'timestamp': datetime.now().strftime('%H:%M:%S')})
        except Exception as e:
            bot.send_message(chat_id, f"⚠️ Échec vente Solana {contract_address}: {str(e)}")
    elif chain == "eth":
        try:
            if w3_eth is None or not w3_eth.is_connected():
                initialize_bot()
                if not w3_eth.is_connected():
                    raise Exception("Ethereum non connecté")
            token_amount = w3_eth.to_wei(amount, 'ether')
            amount_in_max = int(token_amount * 1.05)
            router = w3_eth.eth.contract(address=UNISWAP_ROUTER_ADDRESS, abi=PANCAKE_ROUTER_ABI)
            tx = router.functions.swapExactTokensForETH(
                token_amount,
                amount_in_max,
                [w3_eth.to_checksum_address(contract_address), w3_eth.to_checksum_address('0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2')],
                w3_eth.to_checksum_address(WALLET_ADDRESS),
                int(time.time()) + 60
            ).build_transaction({
                'from': WALLET_ADDRESS, 'gas': 200000,
                'gasPrice': w3_eth.to_wei(gas_fee, 'gwei'), 'nonce': w3_eth.eth.get_transaction_count(WALLET_ADDRESS)
            })
            signed_tx = w3_eth.eth.account.sign_transaction(tx, PRIVATE_KEY)
            tx_hash = w3_eth.eth.send_raw_transaction(signed_tx.rawTransaction)
            bot.send_message(chat_id, f'⏳ Vente Ethereum {amount} ETH de {contract_address}, TX: {tx_hash.hex()}')
            receipt = w3_eth.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
            if receipt.status == 1:
                profit = (current_price - portfolio[contract_address]['entry_price']) * amount
                portfolio[contract_address]['profit'] += profit
                portfolio[contract_address]['amount'] -= amount
                if portfolio[contract_address]['amount'] <= 0:
                    del portfolio[contract_address]
                reinvest_amount = profit * profit_reinvestment_ratio
                mise_depart_eth += reinvest_amount
                bot.send_message(chat_id, f'✅ Vente Ethereum réussie : {amount} ETH, Profit: {profit:.4f} ETH, Réinvesti: {reinvest_amount:.4f} ETH')
                daily_trades['sells'].append({'token': contract_address, 'chain': 'eth', 'amount': amount, 'pnl': profit, 'timestamp': datetime.now().strftime('%H:%M:%S')})
        except Exception as e:
            bot.send_message(chat_id, f"⚠️ Échec vente Ethereum {contract_address}: {str(e)}")
    else:
        try:
            if w3_bsc is None or not w3_bsc.is_connected():
                initialize_bot()
                if not w3_bsc.is_connected():
                    raise Exception("BSC non connecté")
            token_amount = w3_bsc.to_wei(amount, 'ether')
            amount_in_max = int(token_amount * 1.05)
            router = w3_bsc.eth.contract(address=PANCAKE_ROUTER_ADDRESS, abi=PANCAKE_ROUTER_ABI)
            tx = router.functions.swapExactTokensForETH(
                token_amount,
                amount_in_max,
                [w3_bsc.to_checksum_address(contract_address), w3_bsc.to_checksum_address('0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c')],
                w3_bsc.to_checksum_address(WALLET_ADDRESS),
                int(time.time()) + 60
            ).build_transaction({
                'from': WALLET_ADDRESS, 'gas': 200000,
                'gasPrice': w3_bsc.to_wei(gas_fee, 'gwei'), 'nonce': w3_bsc.eth.get_transaction_count(WALLET_ADDRESS)
            })
            signed_tx = w3_bsc.eth.account.sign_transaction(tx, PRIVATE_KEY)
            tx_hash = w3_bsc.eth.send_raw_transaction(signed_tx.rawTransaction)
            bot.send_message(chat_id, f'⏳ Vente BSC {amount} BNB de {contract_address}, TX: {tx_hash.hex()}')
            receipt = w3_bsc.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
            if receipt.status == 1:
                profit = (current_price - portfolio[contract_address]['entry_price']) * amount
                portfolio[contract_address]['profit'] += profit
                portfolio[contract_address]['amount'] -= amount
                if portfolio[contract_address]['amount'] <= 0:
                    del portfolio[contract_address]
                reinvest_amount = profit * profit_reinvestment_ratio
                mise_depart_bsc += reinvest_amount
                bot.send_message(chat_id, f'✅ Vente BSC réussie : {amount} BNB, Profit: {profit:.4f} BNB, Réinvesti: {reinvest_amount:.4f} BNB')
                daily_trades['sells'].append({'token': contract_address, 'chain': 'bsc', 'amount': amount, 'pnl': profit, 'timestamp': datetime.now().strftime('%H:%M:%S')})
        except Exception as e:
            bot.send_message(chat_id, f"⚠️ Échec vente BSC {contract_address}: {str(e)}")

def sell_token_percentage(chat_id, token, percentage):
    try:
        if token not in portfolio:
            bot.send_message(chat_id, f'⚠️ Vente impossible : {token} n\'est pas dans le portefeuille')
            return
        total_amount = portfolio[token]['amount']
        amount_to_sell = total_amount * (percentage / 100)
        chain = portfolio[token]['chain']
        market_cap = get_token_data(token, chain)['market_cap']
        supply = detected_tokens.get(token, {}).get('supply', 0)
        current_price = market_cap / supply if supply > 0 else 0
        sell_token(chat_id, token, amount_to_sell, chain, current_price)
    except Exception as e:
        bot.send_message(chat_id, f"⚠️ Erreur vente partielle {token}: {str(e)}")

def monitor_and_sell(chat_id):
    while trade_active:
        try:
            if not portfolio:
                time.sleep(1)
                continue
            for contract_address, data in list(portfolio.items()):
                chain = data['chain']
                amount = data['amount']
                current_mc = get_token_data(contract_address, chain)['market_cap']
                current_price = current_mc / data['supply'] if data['supply'] > 0 else 0
                data['price_history'].append(current_price)
                if len(data['price_history']) > 10:
                    data['price_history'].pop(0)
                portfolio[contract_address]['current_market_cap'] = current_mc
                                profit_pct = (current_price - data['entry_price']) / data['entry_price'] * 100 if data['entry_price'] > 0 else 0
                loss_pct = -profit_pct if profit_pct < 0 else 0
                data['highest_price'] = max(data['highest_price'], current_price)
                trailing_stop_price = data['highest_price'] * (1 - trailing_stop_percentage / 100)

                if profit_pct >= take_profit_steps[2] * 100:
                    sell_token(chat_id, contract_address, amount, chain, current_price)
                elif profit_pct >= take_profit_steps[1] * 100:
                    sell_token(chat_id, contract_address, amount / 2, chain, current_price)
                elif profit_pct >= take_profit_steps[0] * 100:
                    sell_token(chat_id, contract_address, amount / 3, chain, current_price)
                elif current_price <= trailing_stop_price or loss_pct >= stop_loss_threshold:
                    sell_token(chat_id, contract_address, amount, chain, current_price)
            time.sleep(1)
        except Exception as e:
            bot.send_message(chat_id, f"⚠️ Erreur surveillance: {str(e)}")
            time.sleep(5)

def show_portfolio(chat_id):
    try:
        if w3_bsc is None or w3_eth is None or solana_keypair is None:
            initialize_bot()
        bnb_balance = w3_bsc.eth.get_balance(WALLET_ADDRESS) / 10**18 if w3_bsc and w3_bsc.is_connected() else 0
        eth_balance = w3_eth.eth.get_balance(WALLET_ADDRESS) / 10**18 if w3_eth and w3_eth.is_connected() else 0
        sol_balance = get_solana_balance(chat_id)
        msg = f'💰 Portefeuille:\nBNB : {bnb_balance:.4f}\nETH : {eth_balance:.4f}\nSOL : {sol_balance:.4f}\n\n'
        markup = InlineKeyboardMarkup()
        for ca, data in portfolio.items():
            chain = data['chain']
            current_mc = get_token_data(ca, chain)['market_cap']
            profit = (current_mc - data['market_cap_at_buy']) / data['market_cap_at_buy'] * 100 if data['market_cap_at_buy'] > 0 else 0
            markup.add(
                InlineKeyboardButton(f"💸 Sell 25% {ca[:6]}", callback_data=f"sell_pct_{ca}_25"),
                InlineKeyboardButton(f"💸 Sell 50% {ca[:6]}", callback_data=f"sell_pct_{ca}_50"),
                InlineKeyboardButton(f"💸 Sell 100% {ca[:6]}", callback_data=f"sell_{ca}")
            )
            msg += (
                f"Token: {ca} ({chain})\nMC Achat: ${data['market_cap_at_buy']:.2f}\n"
                f"MC Actuel: ${current_mc:.2f}\nProfit: {profit:.2f}%\nProfit cumulé: {data['profit']:.4f} {chain.upper()}\n\n"
            )
        bot.send_message(chat_id, msg, reply_markup=markup if portfolio else None)
    except Exception as e:
        bot.send_message(chat_id, f'⚠️ Erreur portefeuille: {str(e)}')

def get_solana_balance(chat_id):
    try:
        if solana_keypair is None:
            initialize_bot()
        if not solana_keypair:
            return 0
        response = session.post(SOLANA_NODES[0], json={
            "jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [str(solana_keypair.pubkey())]
        }, timeout=1)
        result = response.json().get('result', {})
        return result.get('value', 0) / 10**9
    except Exception as e:
        bot.send_message(chat_id, f"⚠️ Erreur solde Solana: {str(e)}")
        return 0

def sell_token_immediate(chat_id, token):
    try:
        if token not in portfolio:
            bot.send_message(chat_id, f'⚠️ Vente impossible : {token} n\'est pas dans le portefeuille')
            return
        amount = portfolio[token]["amount"]
        chain = portfolio[token]['chain']
        current_price = get_token_data(token, chain)['market_cap'] / detected_tokens.get(token, {}).get('supply', 1)
        sell_token(chat_id, token, amount, chain, current_price)
    except Exception as e:
        bot.send_message(chat_id, f'⚠️ Erreur vente immédiate {token}: {str(e)}')

def show_daily_summary(chat_id):
    try:
        msg = f"📅 Récapitulatif du jour ({datetime.now().strftime('%Y-%m-%d')}):\n\n"
        msg += "📈 Achats :\n"
        total_buys_bnb = 0
        total_buys_eth = 0
        total_buys_sol = 0
        for trade in daily_trades['buys']:
            if trade['chain'] == 'bsc':
                total_buys_bnb += trade['amount']
            elif trade['chain'] == 'eth':
                total_buys_eth += trade['amount']
            else:
                total_buys_sol += trade['amount']
            msg += f"- {trade['token']} ({trade['chain']}) : {trade['amount']} {trade['chain'].upper()} à {trade['timestamp']}\n"
        msg += f"Total investi : {total_buys_bnb:.4f} BNB / {total_buys_eth:.4f} ETH / {total_buys_sol:.4f} SOL\n\n"

        msg += "📉 Ventes :\n"
        total_profit_bnb = 0
        total_profit_eth = 0
        total_profit_sol = 0
        for trade in daily_trades['sells']:
            if trade['chain'] == 'bsc':
                total_profit_bnb += trade['pnl']
            elif trade['chain'] == 'eth':
                total_profit_eth += trade['pnl']
            else:
                total_profit_sol += trade['pnl']
            msg += f"- {trade['token']} ({trade['chain']}) : {trade['amount']} {trade['chain'].upper()} à {trade['timestamp']}, PNL: {trade['pnl']:.4f} {trade['chain'].upper()}\n"
        msg += f"Profit net : {total_profit_bnb:.4f} BNB / {total_profit_eth:.4f} ETH / {total_profit_sol:.4f} SOL\n"
        
        bot.send_message(chat_id, msg)
    except Exception as e:
        bot.send_message(chat_id, f'⚠️ Erreur récapitulatif: {str(e)}')

def adjust_mise_bsc(message):
    global mise_depart_bsc
    chat_id = message.chat.id
    try:
        new_mise = float(message.text)
        if new_mise > 0:
            mise_depart_bsc = new_mise
            bot.send_message(chat_id, f'✅ Mise BSC mise à jour à {mise_depart_bsc} BNB')
        else:
            bot.send_message(chat_id, "⚠️ La mise doit être positive!")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 0.02)")
    show_main_menu(chat_id)

def adjust_mise_eth(message):
    global mise_depart_eth
    chat_id = message.chat.id
    try:
        new_mise = float(message.text)
        if new_mise > 0:
            mise_depart_eth = new_mise
            bot.send_message(chat_id, f'✅ Mise Ethereum mise à jour à {mise_depart_eth} ETH')
        else:
            bot.send_message(chat_id, "⚠️ La mise doit être positive!")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 0.05)")
    show_main_menu(chat_id)

def adjust_mise_sol(message):
    global mise_depart_sol
    chat_id = message.chat.id
    try:
        new_mise = float(message.text)
        if new_mise > 0:
            mise_depart_sol = new_mise
            bot.send_message(chat_id, f'✅ Mise Solana mise à jour à {mise_depart_sol} SOL')
        else:
            bot.send_message(chat_id, "⚠️ La mise doit être positive!")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un nombre valide (ex. : 0.37)")
    show_main_menu(chat_id)

def adjust_stop_loss(message):
    global stop_loss_threshold
    chat_id = message.chat.id
    try:
        new_sl = float(message.text)
        if new_sl > 0:
            stop_loss_threshold = new_sl
            bot.send_message(chat_id, f'✅ Stop-Loss mis à jour à {stop_loss_threshold} %')
        else:
            bot.send_message(chat_id, "⚠️ Le Stop-Loss doit être positif!")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Erreur : Entrez un pourcentage valide (ex. : 15)")
    show_main_menu(chat_id)

def adjust_take_profit(message):
    global take_profit_steps
    chat_id = message.chat.id
    try:
        new_tp = [float(x) for x in message.text.split(",")]
        if len(new_tp) == 3 and all(x > 0 for x in new_tp):
            take_profit_steps = new_tp
            bot.send_message(chat_id, f'✅ Take-Profit mis à jour à x{take_profit_steps[0]}, x{take_profit_steps[1]}, x{take_profit_steps[2]}')
        else:
            bot.send_message(chat_id, "⚠️ Entrez 3 valeurs positives séparées par des virgules (ex. : 1.5,2,5)")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Erreur : Entrez des nombres valides (ex. : 1.5,2,5)")
    show_main_menu(chat_id)

chat_id_global = None

def initialize_and_run_threads(chat_id):
    global trade_active, chat_id_global
    chat_id_global = chat_id
    try:
        initialize_bot()
        trade_active = True
        bot.send_message(chat_id, "▶️ Trading lancé avec succès!")
        threading.Thread(target=snipe_new_pairs_bsc, args=(chat_id,), daemon=True).start()
        threading.Thread(target=snipe_new_pairs_eth, args=(chat_id,), daemon=True).start()
        threading.Thread(target=snipe_solana_pools, args=(chat_id,), daemon=True).start()
        threading.Thread(target=monitor_twitter, args=(chat_id,), daemon=True).start()
        threading.Thread(target=monitor_and_sell, args=(chat_id,), daemon=True).start()
    except Exception as e:
        bot.send_message(chat_id, f"⚠️ Erreur initialisation: {str(e)}")
        trade_active = False

@app.route("/webhook", methods=['POST'])
def webhook():
    global trade_active
    if request.method == "POST" and request.headers.get("content-type") == "application/json":
        update = telebot.types.Update.de_json(request.get_json())
        bot.process_new_updates([update])
        if not trade_active and update.message:
            initialize_and_run_threads(update.message.chat.id)
        return 'OK', 200
    return abort(403)

@bot.message_handler(commands=['start'])
def start_message(message):
    bot.send_message(message.chat.id, "✅ Bot démarré!")
    show_main_menu(message.chat.id)

def show_main_menu(chat_id):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("ℹ️ Statut", callback_data="status"),
        InlineKeyboardButton("▶️ Lancer", callback_data="launch"),
        InlineKeyboardButton("⏹️ Arrêter", callback_data="stop"),
        InlineKeyboardButton("💰 Portefeuille", callback_data="portfolio"),
        InlineKeyboardButton("📅 Récapitulatif", callback_data="daily_summary")
    )
    markup.add(
        InlineKeyboardButton("🔧 Ajuster Mise BSC", callback_data="adjust_mise_bsc"),
        InlineKeyboardButton("🔧 Ajuster Mise ETH", callback_data="adjust_mise_eth"),
        InlineKeyboardButton("🔧 Ajuster Mise SOL", callback_data="adjust_mise_sol")
    )
    markup.add(
        InlineKeyboardButton("📉 Ajuster Stop-Loss", callback_data="adjust_stop_loss"),
        InlineKeyboardButton("📈 Ajuster Take-Profit", callback_data="adjust_take_profit")
    )
    bot.send_message(chat_id, "Menu principal:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    global trade_active
    chat_id = call.message.chat.id
    try:
        if call.data == "status":
            bot.send_message(chat_id, (
                f"ℹ️ Statut actuel :\nTrading actif: {'Oui' if trade_active else 'Non'}\n"
                f"Mise BSC: {mise_depart_bsc} BNB\nMise Ethereum: {mise_depart_eth} ETH\nMise Solana: {mise_depart_sol} SOL\n"
                f"Positions: {len(portfolio)}/{max_positions}\n"
                f"Stop-Loss: {stop_loss_threshold}%\nTake-Profit: x{take_profit_steps[0]}, x{take_profit_steps[1]}, x{take_profit_steps[2]}"
            ))
        elif call.data == "launch":
            if not trade_active:
                initialize_and_run_threads(chat_id)
            else:
                bot.send_message(chat_id, "⚠️ Trading déjà en cours.")
        elif call.data == "stop":
            trade_active = False
            bot.send_message(chat_id, "⏹️ Trading arrêté.")
        elif call.data == "portfolio":
            show_portfolio(chat_id)
        elif call.data == "daily_summary":
            show_daily_summary(chat_id)
        elif call.data == "adjust_mise_bsc":
            bot.send_message(chat_id, "Entrez la nouvelle mise BSC (en BNB, ex. : 0.02) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_mise_bsc)
        elif call.data == "adjust_mise_eth":
            bot.send_message(chat_id, "Entrez la nouvelle mise Ethereum (en ETH, ex. : 0.05) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_mise_eth)
        elif call.data == "adjust_mise_sol":
            bot.send_message(chat_id, "Entrez la nouvelle mise Solana (en SOL, ex. : 0.37) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_mise_sol)
        elif call.data == "adjust_stop_loss":
            bot.send_message(chat_id, "Entrez le nouveau seuil de Stop-Loss (en %, ex. : 15) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_stop_loss)
        elif call.data == "adjust_take_profit":
            bot.send_message(chat_id, "Entrez les nouveaux seuils de Take-Profit (3 valeurs séparées par des virgules, ex. : 1.5,2,5) :")
            bot.register_next_step_handler_by_chat_id(chat_id, adjust_take_profit)
        elif call.data.startswith("sell_"):
            token = call.data.split("_")[1]
            sell_token_immediate(chat_id, token)
        elif call.data.startswith("sell_pct_"):
            _, token, pct = call.data.split("_")
            sell_token_percentage(chat_id, token, float(pct))
    except Exception as e:
        bot.send_message(chat_id, f"⚠️ Erreur générale: {str(e)}")

if __name__ == "__main__":
    if set_webhook():
        logger.info("Webhook configuré, démarrage du serveur...")
        serve(app, host="0.0.0.0", port=PORT, threads=8)
    else:
        logger.error("Échec du webhook, passage en mode polling")
        bot.polling(none_stop=True)
