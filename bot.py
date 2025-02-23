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
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Configuration du logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Headers pour API
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/91.0.4472.124",
    "Accept": "application/json",
}

# Session persistante avec retries
session = requests.Session()
session.headers.update(HEADERS)
retry_strategy = Retry(total=5, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("https://", adapter)

# Chargement des variables depuis Cloud Run
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY")
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")
BSC_SCAN_API_KEY = os.getenv("BSC_SCAN_API_KEY")
TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN")
PORT = int(os.getenv("PORT", 8080))

# Headers pour Twitter/X
TWITTER_HEADERS = {"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"}

# Validation des variables
logger.info("Validation des variables...")
missing_vars = []
required_vars = {
    "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
    "WALLET_ADDRESS": WALLET_ADDRESS,
    "PRIVATE_KEY": PRIVATE_KEY,
    "SOLANA_PRIVATE_KEY": SOLANA_PRIVATE_KEY,
    "WEBHOOK_URL": WEBHOOK_URL,
    "BIRDEYE_API_KEY": BIRDEYE_API_KEY,
    "BSC_SCAN_API_KEY": BSC_SCAN_API_KEY,
    "TWITTER_BEARER_TOKEN": TWITTER_BEARER_TOKEN
}
for var_name, var_value in required_vars.items():
    if not var_value:
        missing_vars.append(var_name)
if missing_vars:
    logger.error(f"Variables manquantes : {missing_vars}")
    raise ValueError(f"Variables manquantes : {missing_vars}")
else:
    logger.info("Toutes les variables sont présentes.")

# Initialisation
bot = telebot.TeleBot(TELEGRAM_TOKEN)
app = Flask(__name__)

# BSC (PancakeSwap)
w3 = Web3(Web3.HTTPProvider("https://bsc-dataseed.binance.org/"))
if not w3.is_connected():
    logger.error("Connexion BSC échouée.")
    raise ConnectionError("Connexion BSC échouée")
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
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
solana_keypair = Keypair.from_base58_string(SOLANA_PRIVATE_KEY)
RAYDIUM_PROGRAM_ID = Pubkey.from_string("675kPX9MHTjS2zt1qfr1NYHuzeLXiQM9H24wFSeeAHj2")
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")

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
mise_depart_bsc = 0.08  # ~50€ en BNB (600€/BNB)
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

# Surveillance Twitter/X
def monitor_twitter(chat_id):
    logger.info("Surveillance des tweets pour nouveaux tokens...")
    try:
        # Récupérer les tweets de Kanye West
        response = session.get(
            "https://api.twitter.com/2/users/169686021/tweets?max_results=10&tweet.fields=created_at,text",
            headers=TWITTER_HEADERS
        )
        response.raise_for_status()
        tweets = response.json().get('data', [])
        for tweet in tweets:
            text = tweet['text']
            # Recherche d'adresses de contrat (42 caractères hexadécimaux commençant par 0x ou Solana 44 caractères base58)
            potential_addresses = [
                word for word in text.split() 
                if (word.startswith('0x') and len(word) == 42) or (len(word) == 44 and all(c in '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz' for c in word))
            ]
            for address in potential_addresses:
                if len(address) == 42:  # BSC
                    check_token_bsc(chat_id, address)
                elif len(address) == 44:  # Solana
                    check_token_solana(chat_id, address)
    except Exception as e:
        logger.error(f"Erreur surveillance Twitter: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur surveillance Twitter: {str(e)}")

def check_token_bsc(chat_id, token_address):
    try:
        token_address = w3.to_checksum_address(token_address)
        pair_address = None  # Simplifié,假设 paire avec WBNB
        pair_contract = w3.eth.contract(address=token_address, abi=[{"constant": True, "inputs": [], "name": "getReserves", "outputs": [{"name": "", "type": "uint112"}, {"name": "", "type": "uint112"}, {"name": "", "type": "uint32"}], "payable": False, "stateMutability": "view", "type": "function"}])
        reserves = pair_contract.functions.getReserves().call()
        liquidity = reserves[1] / 10**18  # Approximation WBNB
        token_contract = w3.eth.contract(address=token_address, abi=[{"constant": True, "inputs": [], "name": "totalSupply", "outputs": [{"name": "", "type": "uint256"}], "payable": False, "stateMutability": "view", "type": "function"}])
        supply = token_contract.functions.totalSupply().call() / 10**18
        price = liquidity / supply if supply > 0 else 0
        market_cap = price * supply
        volume_response = session.get(f"https://api.bscscan.com/api?module=account&action=tokenbalance&contractaddress={token_address}&address={pair_address}&tag=latest&apikey={BSC_SCAN_API_KEY}")
        volume = float(volume_response.json()['result']) / 10**18 * price if volume_response.json()['result'] else 0
        if not (MIN_VOLUME_BSC <= volume <= MAX_VOLUME_BSC):
            bot.send_message(chat_id, f"⚠️ Token X {token_address} rejeté - Volume hors limites: ${volume:.2f}")
        elif liquidity < MIN_LIQUIDITY:
            bot.send_message(chat_id, f"⚠️ Token X {token_address} rejeté - Liquidité insuffisante: ${liquidity:.2f}")
        elif not (MIN_MARKET_CAP_BSC <= market_cap <= MAX_MARKET_CAP_BSC):
            bot.send_message(chat_id, f"⚠️ Token X {token_address} rejeté - Market Cap hors limites: ${market_cap:.2f}")
        elif not is_safe_token_bsc(token_address):
            bot.send_message(chat_id, f"⚠️ Token X {token_address} rejeté - Sécurité non validée (possible rug)")
        else:
            bot.send_message(chat_id, (
                f"🚀 Token X détecté : {token_address} (BSC) - "
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
    except Exception as e:
        logger.error(f"Erreur vérification token BSC X {token_address}: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur token X {token_address}: {str(e)}")

def check_token_solana(chat_id, token_address):
    try:
        response = session.get(
            f"https://public-api.birdeye.so/public/token_overview?address={token_address}",
            headers=BIRDEYE_HEADERS
        )
        data = response.json()['data']
        volume = float(data.get('v24hUSD', 0))
        liquidity = float(data.get('liquidity', 0))
        market_cap = float(data.get('mc', 0))
        supply = float(data.get('supply', 0))
        if (MIN_VOLUME_SOL <= volume <= MAX_VOLUME_SOL and
            liquidity >= MIN_LIQUIDITY and
            MIN_MARKET_CAP_SOL <= market_cap <= MAX_MARKET_CAP_SOL and
            is_safe_token_solana(token_address)):
            bot.send_message(chat_id, (
                f"🚀 Token X détecté : {token_address} (Solana) - "
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
            bot.send_message(chat_id, f"⚠️ Token X {token_address} rejeté (critères ou sécurité non respectés)")
    except Exception as e:
        logger.error(f"Erreur vérification token Solana X {token_address}: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur token X {token_address}: {str(e)}")

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

# Détection BSC avec BSCScan
def detect_new_tokens_bsc(chat_id):
    logger.info("Recherche de nouveaux tokens sur BSC (PancakeSwap via BSCScan)...")
    try:
        latest_block = w3.eth.block_number
        response = session.get(
            f"https://api.bscscan.com/api?module=logs&action=getLogs&fromBlock={latest_block - 100}&toBlock=latest&address={PANCAKE_FACTORY_ADDRESS}&topic0=0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9&apikey={BSC_SCAN_API_KEY}"
        )
        response.raise_for_status()
        events = response.json()['result']
        bot.send_message(chat_id, f"📡 {len(events)} nouvelles paires détectées sur BSC")
        if not events:
            bot.send_message(chat_id, "ℹ️ Aucune nouvelle paire trouvée dans les 100 derniers blocs BSC.")
        for event in events[:10]:
            try:
                token0 = w3.to_checksum_address('0x' + event['topics'][1][-40:])
                token1 = w3.to_checksum_address('0x' + event['topics'][2][-40:])
                pair_address = w3.to_checksum_address('0x' + event['data'][-40:])
                if token0 == token1 or "000000000000000000000000000" in pair_address.lower():
                    bot.send_message(chat_id, f"⚠️ Token ignoré - Adresse invalide ou suspecte: {pair_address}")
                    continue
                token_address = token0 if token1 == "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c" else token1
                if token_address == "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c":
                    continue
                time.sleep(1)
                pair_contract = w3.eth.contract(address=pair_address, abi=[{"constant": True, "inputs": [], "name": "getReserves", "outputs": [{"name": "", "type": "uint112"}, {"name": "", "type": "uint112"}, {"name": "", "type": "uint32"}], "payable": False, "stateMutability": "view", "type": "function"}])
                reserves = pair_contract.functions.getReserves().call()
                liquidity = reserves[0] / 10**18 if token0 == "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c" else reserves[1] / 10**18
                token_contract = w3.eth.contract(address=token_address, abi=[{"constant": True, "inputs": [], "name": "totalSupply", "outputs": [{"name": "", "type": "uint256"}], "payable": False, "stateMutability": "view", "type": "function"}])
                supply = token_contract.functions.totalSupply().call() / 10**18
                price = liquidity / supply if supply > 0 else 0
                market_cap = price * supply
                volume_response = session.get(f"https://api.bscscan.com/api?module=account&action=tokenbalance&contractaddress={token_address}&address={pair_address}&tag=latest&apikey={BSC_SCAN_API_KEY}")
                volume = float(volume_response.json()['result']) / 10**18 * price if volume_response.json()['result'] else 0
                if not (MIN_VOLUME_BSC <= volume <= MAX_VOLUME_BSC):
                    bot.send_message(chat_id, f"⚠️ Token {token_address} rejeté - Volume hors limites: ${volume:.2f}")
                elif liquidity < MIN_LIQUIDITY:
                    bot.send_message(chat_id, f"⚠️ Token {token_address} rejeté - Liquidité insuffisante: ${liquidity:.2f}")
                elif not (MIN_MARKET_CAP_BSC <= market_cap <= MAX_MARKET_CAP_BSC):
                    bot.send_message(chat_id, f"⚠️ Token {token_address} rejeté - Market Cap hors limites: ${market_cap:.2f}")
                elif not is_safe_token_bsc(token_address):
                    bot.send_message(chat_id, f"⚠️ Token {token_address} rejeté - Sécurité non validée (possible rug)")
                else:
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
            except Exception as e:
                logger.error(f"Erreur traitement token BSC {pair_address}: {str(e)}")
                bot.send_message(chat_id, f"⚠️ Token {pair_address} ignoré - Erreur synchro ou contrat: {str(e)}")
    except Exception as e:
        logger.error(f"Erreur détection BSC: {str(e)}")
        bot.send_message(chat_id, f"⚠️ Erreur lors de la détection BSC: {str(e)}")
    bot.send_message(chat_id, "✅ Détection BSC terminée, passage à Solana...")

# Détection Solana avec Birdeye
def detect_new_tokens_solana(chat_id):
    logger.info("Recherche de nouveaux tokens sur Solana...")
    attempts = 0
    max_attempts = 5
    while attempts < max_attempts:
        try:
            response = session.get(
                "https://public-api.birdeye.so/defi/tokenlist?sort_by=v24hUSD&sort_type=desc&offset=0&limit=10",
                headers=BIRDEYE_HEADERS,
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            if not data.get('success') or 'data' not in data or 'tokens' not in data['data']:
                raise ValueError("Réponse invalide de l'API Birdeye")
            tokens = data['data']['tokens']
            bot.send_message(chat_id, f"📡 {len(tokens)} nouveaux tokens trouvés sur Solana")
            for token in tokens:
                token_address = token['address']
                volume = float(token.get('v24hUSD', 0))
                liquidity = float(token.get('liquidity', 0))
                market_cap = float(token.get('mc', 0))
                supply = float(token.get('supply', 0))
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
        except requests.exceptions.RequestException as e:
            attempts += 1
            logger.error(f"Erreur réseau Solana (tentative {attempts}/{max_attempts}): {str(e)}")
            bot.send_message(chat_id, f"⚠️ Erreur réseau Solana, tentative {attempts}/{max_attempts}: {str(e)}")
            if attempts < max_attempts:
                time.sleep(5 * attempts)
        except ValueError as e:
            logger.error(f"Erreur données Birdeye: {str(e)}")
            bot.send_message(chat_id, f"⚠️ Erreur données Birdeye: {str(e)}")
            break
        except Exception as e:
            logger.error(f"Erreur inattendue Solana: {str(e)}")
            bot.send_message(chat_id, f"⚠️ Erreur Solana: {str(e)}")
            break
    else:
        logger.error("Échec détection Solana après toutes les tentatives.")
        bot.send_message(chat_id, "⚠️ Échec détection Solana après toutes les tentatives.")
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
            entry_price = amount / (int.from_bytes(receipt.logs[0].data, 'big') / 10**18)
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
        amount_in = int(amount * 10**9)
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
            data=bytes([2]) + amount_in.to_bytes(8, 'little')
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
                    InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh_{ca}"),
                    InlineKeyboardButton("💰 Sell All", callback_data=f"sell_{ca}")
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
            pair_address = detected_tokens[contract_address]['address']
            pair_contract = w3.eth.contract(address=pair_address, abi=[{"constant": True, "inputs": [], "name": "getReserves", "outputs": [{"name": "", "type": "uint112"}, {"name": "", "type": "uint112"}, {"name": "", "type": "uint32"}], "payable": False, "stateMutability": "view", "type": "function"}])
            reserves = pair_contract.functions.getReserves().call()
            price = (reserves[1] / 10**18) / (reserves[0] / 10**18) if reserves[0] != 0 else 0
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
            InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh_{token}"),
            InlineKeyboardButton("💰 Sell All", callback_data=f"sell_{token}")
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

# Gestion des onglets supplémentaires
def show_tp_sl_menu(chat_id):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("📈 Ajuster Take-Profit Étape 1", callback_data="adjust_tp1"),
        InlineKeyboardButton("📈 Ajuster Take-Profit Étape 2", callback_data="adjust_tp2"),
        InlineKeyboardButton("📈 Ajuster Take-Profit Étape 3", callback_data="adjust_tp3"),
        InlineKeyboardButton("📉 Ajuster Stop-Loss", callback_data="adjust_sl")
    )
    bot.send_message(chat_id, f"🎯 Stratégie TP/SL:\nTP: x{take_profit_steps[0]}, x{take_profit_steps[1]}, x{take_profit_steps[2]}\nSL: -{stop_loss_threshold}%", reply_markup=markup)

def show_thresholds_menu(chat_id):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("📏 Min Volume BSC", callback_data="adjust_min_vol_bsc"),
        InlineKeyboardButton("📏 Max Volume BSC", callback_data="adjust_max_vol_bsc"),
        InlineKeyboardButton("📏 Min Volume SOL", callback_data="adjust_min_vol_sol"),
        InlineKeyboardButton("📏 Max Volume SOL", callback_data="adjust_max_vol_sol"),
        InlineKeyboardButton("📏 Min Liquidité", callback_data="adjust_min_liq"),
        InlineKeyboardButton("📏 Min Market Cap BSC", callback_data="adjust_min_mc_bsc"),
        InlineKeyboardButton("📏 Max Market Cap BSC", callback_data="adjust_max_mc_bsc"),
        InlineKeyboardButton("📏 Min Market Cap SOL", callback_data="adjust_min_mc_sol"),
        InlineKeyboardButton("📏 Max Market Cap SOL", callback_data="adjust_max_mc_sol")
    )
    bot.send_message(chat_id, (
        f"📊 Seuils actuels:\n"
        f"BSC Volume: {MIN_VOLUME_BSC} - {MAX_VOLUME_BSC}\n"
        f"SOL Volume: {MIN_VOLUME_SOL} - {MAX_VOLUME_SOL}\n"
        f"Liquidité Min: {MIN_LIQUIDITY}\n"
        f"BSC Market Cap: {MIN_MARKET_CAP_BSC} - {MAX_MARKET_CAP_BSC}\n"
        f"SOL Market Cap: {MIN_MARKET_CAP_SOL} - {MAX_MARKET_CAP_SOL}"
    ), reply_markup=markup)

def adjust_tp_sl(message, step):
    global take_profit_steps, stop_loss_threshold
    chat_id = message.chat.id
    try:
        value = float(message.text)
        if step == "tp1" and value > 0:
            take_profit_steps[0] = value
            bot.send_message(chat_id, f"✅ TP Étape 1 mis à jour à x{value}")
        elif step == "tp2" and value > take_profit_steps[0]:
            take_profit_steps[1] = value
            bot.send_message(chat_id, f"✅ TP Étape 2 mis à jour à x{value}")
        elif step == "tp3" and value > take_profit_steps[1]:
            take_profit_steps[2] = value
            bot.send_message(chat_id, f"✅ TP Étape 3 mis à jour à x{value}")
        elif step == "sl" and 0 <= value <= 100:
            stop_loss_threshold = value
            bot.send_message(chat_id, f"✅ SL mis à jour à -{value}%")
        else:
            bot.send_message(chat_id, "⚠️ Valeur invalide ou hors limites !")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Entrez un nombre valide !")
    show_tp_sl_menu(chat_id)

def adjust_thresholds(message, param):
    global MIN_VOLUME_BSC, MAX_VOLUME_BSC, MIN_VOLUME_SOL, MAX_VOLUME_SOL, MIN_LIQUIDITY, MIN_MARKET_CAP_BSC, MAX_MARKET_CAP_BSC, MIN_MARKET_CAP_SOL, MAX_MARKET_CAP_SOL
    chat_id = message.chat.id
    try:
        value = float(message.text)
        if param == "min_vol_bsc" and 0 <= value < MAX_VOLUME_BSC:
            MIN_VOLUME_BSC = value
            bot.send_message(chat_id, f"✅ Min Volume BSC mis à jour à {value}")
        elif param == "max_vol_bsc" and value > MIN_VOLUME_BSC:
            MAX_VOLUME_BSC = value
            bot.send_message(chat_id, f"✅ Max Volume BSC mis à jour à {value}")
        elif param == "min_vol_sol" and 0 <= value < MAX_VOLUME_SOL:
            MIN_VOLUME_SOL = value
            bot.send_message(chat_id, f"✅ Min Volume SOL mis à jour à {value}")
        elif param == "max_vol_sol" and value > MIN_VOLUME_SOL:
            MAX_VOLUME_SOL = value
            bot.send_message(chat_id, f"✅ Max Volume SOL mis à jour à {value}")
        elif param == "min_liq" and value >= 0:
            MIN_LIQUIDITY = value
            bot.send_message(chat_id, f"✅ Min Liquidité mis à jour à {value}")
        elif param == "min_mc_bsc" and 0 <= value < MAX_MARKET_CAP_BSC:
            MIN_MARKET_CAP_BSC = value
            bot.send_message(chat_id, f"✅ Min Market Cap BSC mis à jour à {value}")
        elif param == "max_mc_bsc" and value > MIN_MARKET_CAP_BSC:
            MAX_MARKET_CAP_BSC = value
            bot.send_message(chat_id, f"✅ Max Market Cap BSC mis à jour à {value}")
        elif param == "min_mc_sol" and 0 <= value < MAX_MARKET_CAP_SOL:
            MIN_MARKET_CAP_SOL = value
            bot.send_message(chat_id, f"✅ Min Market Cap SOL mis à jour à {value}")
        elif param == "max_mc_sol" and value > MIN_MARKET_CAP_SOL:
            MAX_MARKET_CAP_SOL = value
            bot.send_message(chat_id, f"✅ Max Market Cap SOL mis à jour à {value}")
        else:
            bot.send_message(chat_id, "⚠️ Valeur invalide ou hors limites !")
    except ValueError:
        bot.send_message(chat_id, "⚠️ Entrez un nombre valide !")
    show_thresholds_menu(chat_id)

# Gestion des callbacks pour TP/SL et seuils
@bot.callback_query_handler(func=lambda call: call.data.startswith("adjust_"))
def handle_adjustments(call):
    chat_id = call.message.chat.id
    action = call.data
    if action == "adjust_tp1":
        bot.send_message(chat_id, "Entrez la nouvelle valeur pour TP Étape 1 (ex. 2 pour x2) :")
        bot.register_next_step_handler_by_chat_id(chat_id, lambda msg: adjust_tp_sl(msg, "tp1"))
    elif action == "adjust_tp2":
        bot.send_message(chat_id, "Entrez la nouvelle valeur pour TP Étape 2 (ex. 3 pour x3) :")
        bot.register_next_step_handler_by_chat_id(chat_id, lambda msg: adjust_tp_sl(msg, "tp2"))
    elif action == "adjust_tp3":
        bot.send_message(chat_id, "Entrez la nouvelle valeur pour TP Étape 3 (ex. 5 pour x5) :")
        bot.register_next_step_handler_by_chat_id(chat_id, lambda msg: adjust_tp_sl(msg, "tp3"))
    elif action == "adjust_sl":
        bot.send_message(chat_id, "Entrez la nouvelle valeur pour SL (ex. 30 pour -30%) :")
        bot.register_next_step_handler_by_chat_id(chat_id, lambda msg: adjust_tp_sl(msg, "sl"))
    elif action.startswith("adjust_"):
        param = action.replace("adjust_", "")
        bot.send_message(chat_id, f"Entrez la nouvelle valeur pour {param.replace('_', ' ').title()} :")
        bot.register_next_step_handler_by_chat_id(chat_id, lambda msg: adjust_thresholds(msg, param))

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
