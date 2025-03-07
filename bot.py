import os
from telebot.async_telebot import AsyncTeleBot
import logging
import requests
import time
import json
import asyncio
import websockets
from datetime import datetime
import backoff
from web3 import Web3
from web3.middleware import geth_poa_middleware
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from solders.instruction import Instruction
import base58

# Configuration logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Variables globales
trade_active = False
portfolio = {}
detected_tokens = {}
daily_trades = {'buys': [], 'sells': []}
BLACKLISTED_TOKENS = {"So11111111111111111111111111111111111111112"}

# Variables d’environnement
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "default_token")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "0x0")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "dummy_key")
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY", "dummy_solana_key")
QUICKNODE_BSC_URL = "https://smart-necessary-ensemble.bsc.quiknode.pro/aeb370bcf4299bc365bbbd3d14d19a31f6e46f06/"
QUICKNODE_ETH_URL = "https://side-cold-diamond.quiknode.pro/698f06abfe4282fc22edbab42297cf468d78070f/"
QUICKNODE_SOL_URL = "https://little-maximum-glade.solana-mainnet.quiknode.pro/5da088be927d31731b0d7284c30a0640d8e4dd50/"
QUICKNODE_SOL_WS_URL = "wss://little-maximum-glade.solana-mainnet.quiknode.pro/5da088be927d31731b0d7284c30a0640d8e4dd50/"

# Initialisation
bot = AsyncTeleBot(TELEGRAM_TOKEN)
w3_bsc = Web3(Web3.HTTPProvider(QUICKNODE_BSC_URL))
w3_bsc.middleware_onion.inject(geth_poa_middleware, layer=0)
w3_eth = Web3(Web3.HTTPProvider(QUICKNODE_ETH_URL))
solana_keypair = Keypair.from_base58_string(SOLANA_PRIVATE_KEY)

# Paramètres de trading
mise_depart_bsc = 0.02
mise_depart_eth = 0.05
mise_depart_sol = 0.37
stop_loss_threshold = 15
trailing_stop_percentage = 5
take_profit_steps = [1.2, 2, 10, 100, 500]
profit_reinvestment_ratio = 0.9
slippage_max = 0.05
MAX_TOKEN_AGE_HOURS = 6
MIN_LIQUIDITY = 5000
MIN_VOLUME = 100
MAX_VOLUME = 2000000
MAX_POSITIONS = 5

# Constantes
PANCAKE_ROUTER_ADDRESS = "0x10ED43C718714eb63d5aA57B78B54704E256024E"
UNISWAP_ROUTER_ADDRESS = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"
RAYDIUM_PROGRAM_ID = Pubkey.from_string("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8")
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
PANCAKE_ROUTER_ABI = json.loads('[{"inputs": [{"internalType": "uint256", "name": "amountIn", "type": "uint256"}, {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"}, {"internalType": "address[]", "name": "path", "type": "address[]"}, {"internalType": "address", "name": "to", "type": "address"}, {"internalType": "uint256", "name": "deadline", "type": "uint256"}], "name": "swapExactETHForTokens", "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}], "stateMutability": "payable", "type": "function"}, {"inputs": [{"internalType": "uint256", "name": "amountOut", "type": "uint256"}, {"internalType": "uint256", "name": "amountInMax", "type": "uint256"}, {"internalType": "address[]", "name": "path", "type": "address[]"}, {"internalType": "address", "name": "to", "type": "address"}, {"internalType": "uint256", "name": "deadline", "type": "uint256"}], "name": "swapExactTokensForETH", "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}], "stateMutability": "nonpayable", "type": "function"}]')

def validate_address(token_address, chain):
    if chain in ['bsc', 'eth']:
        return bool(token_address.startswith("0x") and len(token_address) == 42)
    elif chain == 'solana':
        try:
            Pubkey.from_string(token_address)
            return len(token_address) >= 32 and len(token_address) <= 44
        except ValueError:
            return False
    return False

@backoff.on_exception(backoff.expo, Exception, max_tries=3)
def get_token_data(token_address, chain):
    try:
        response = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{token_address}", timeout=15)
        response.raise_for_status()
        pairs = response.json().get('pairs', [])
        if not pairs or pairs[0].get('chainId') != chain:
            return None
        data = pairs[0]
        age_hours = (time.time() - (data.get('pairCreatedAt', 0) / 1000)) / 3600
        if age_hours > MAX_TOKEN_AGE_HOURS:
            return None
        return {
            'volume_24h': float(data.get('volume', {}).get('h24', 0) or 0),
            'liquidity': float(data.get('liquidity', {}).get('usd', 0) or 0),
            'market_cap': float(data.get('marketCap', 0) or 0),
            'price': float(data.get('priceUsd', 0) or 0),
            'pair_created_at': data.get('pairCreatedAt', 0) / 1000
        }
    except Exception as e:
        logger.error(f"Erreur DexScreener {token_address}: {str(e)}")
        return None

def get_dynamic_gas_price(chain):
    w3 = w3_bsc if chain == 'bsc' else w3_eth if chain == 'eth' else None
    return w3.eth.gas_price * 1.2 if w3 and w3.is_connected() else w3.to_wei(15, 'gwei')

async def snipe_new_pairs_bsc(chat_id):
    while trade_active:
        try:
            factory = w3_bsc.eth.contract(address="0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73", abi=[{"anonymous": False, "inputs": [{"indexed": True, "internalType": "address", "name": "token0", "type": "address"}, {"indexed": True, "internalType": "address", "name": "token1", "type": "address"}, {"indexed": False, "internalType": "address", "name": "pair", "type": "address"}, {"indexed": False, "internalType": "uint256", "name": "", "type": "uint256"}], "name": "PairCreated", "type": "event"}])
            latest_block = w3_bsc.eth.block_number
            events = factory.events.PairCreated.get_logs(fromBlock=max(latest_block - 200, 0), toBlock=latest_block)
            await bot.send_message(chat_id, f"🔄 Sniping BSC actif - {len(events)} paires détectées")
            for event in events:
                token_address = event['args']['token0'] if event['args']['token0'] != "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c" else event['args']['token1']
                await validate_and_trade(chat_id, token_address, 'bsc')
            await asyncio.sleep(1)
        except Exception as e:
            await bot.send_message(chat_id, f"⚠️ Erreur sniping BSC: {str(e)}")
            await asyncio.sleep(5)

async def snipe_new_pairs_eth(chat_id):
    while trade_active:
        try:
            factory = w3_eth.eth.contract(address="0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f", abi=[{"anonymous": False, "inputs": [{"indexed": True, "internalType": "address", "name": "token0", "type": "address"}, {"indexed": True, "internalType": "address", "name": "token1", "type": "address"}, {"indexed": False, "internalType": "address", "name": "pair", "type": "address"}, {"indexed": False, "internalType": "uint256", "name": "", "type": "uint256"}], "name": "PairCreated", "type": "event"}])
            latest_block = w3_eth.eth.block_number
            events = factory.events.PairCreated.get_logs(fromBlock=max(latest_block - 200, 0), toBlock=latest_block)
            await bot.send_message(chat_id, f"🔄 Sniping Ethereum actif - {len(events)} paires détectées")
            for event in events:
                token_address = event['args']['token0'] if event['args']['token0'] != "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2" else event['args']['token1']
                await validate_and_trade(chat_id, token_address, 'eth')
            await asyncio.sleep(1)
        except Exception as e:
            await bot.send_message(chat_id, f"⚠️ Erreur sniping Ethereum: {str(e)}")
            await asyncio.sleep(5)

async def snipe_solana_pools(chat_id):
    while trade_active:
        try:
            async with websockets.connect(QUICKNODE_SOL_WS_URL) as ws:
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "programSubscribe", "params": [str(RAYDIUM_PROGRAM_ID), {"encoding": "base64"}]}))
                await bot.send_message(chat_id, "🔄 Sniping Solana actif (Raydium)")
                while trade_active:
                    msg = json.loads(await ws.recv())
                    logger.info(f"Données Solana WebSocket: {msg}")
                    if 'params' in msg and 'result' in msg['params']:
                        data = msg['params']['result']['value']['account']['data'][0]
                        for token_address in data.split():
                            if validate_address(token_address, 'solana') and token_address not in BLACKLISTED_TOKENS:
                                await bot.send_message(chat_id, f"🎯 Snipe Solana détecté : {token_address}")
                                await validate_and_trade(chat_id, token_address, 'solana')
                    await asyncio.sleep(0.1)
        except Exception as e:
            await bot.send_message(chat_id, f"⚠️ Erreur sniping Solana: {str(e)}")
            logger.error(f"Erreur Solana: {str(e)}")
            await asyncio.sleep(5)

async def validate_and_trade(chat Entonces_id, token_address, chain):
    if token_address in BLACKLISTED_TOKENS or token_address in portfolio or len(portfolio) >= MAX_POSITIONS:
        await bot.send_message(chat_id, f"⚠️ {token_address} rejeté : Blacklisté ou portefeuille plein")
        return
    data = get_token_data(token_address, chain)
    if not data:
        await bot.send_message(chat_id, f"⚠️ {token_address} rejeté : Pas de données")
        return

    volume_24h = data['volume_24h']
    liquidity = data['liquidity']
    market_cap = data['market_cap']
    price = data['price']
    age_hours = (time.time() - data['pair_created_at']) / 3600

    if age_hours > MAX_TOKEN_AGE_HOURS:
        await bot.send_message(chat_id, f"⚠️ {token_address} rejeté : Âge {age_hours:.2f}h > {MAX_TOKEN_AGE_HOURS}h")
        return
    if liquidity < MIN_LIQUIDITY:
        await bot.send_message(chat_id, f"⚠️ {token_address} rejeté : Liquidité ${liquidity:.2f} < ${MIN_LIQUIDITY}")
        return
    if volume_24h < MIN_VOLUME or volume_24h > MAX_VOLUME:
        await bot.send_message(chat_id, f"⚠️ {token_address} rejeté : Volume ${volume_24h:.2f} hors plage [{MIN_VOLUME}, {MAX_VOLUME}]")
        return

    bot.send_message(chat_id, f"✅ Token validé : {token_address} ({chain})")
    detected_tokens[token_address] = data
    amount = mise_depart_bsc if chain == 'bsc' else mise_depart_eth if chain == 'eth' else mise_depart_sol
    if chain == 'bsc':
        await buy_token_bsc(chat_id, token_address, amount)
    elif chain == 'eth':
        await buy_token_eth(chat_id, token_address, amount)
    else:
        await buy_token_solana(chat_id, token_address, amount)

async def buy_token_bsc(chat_id, contract_address, amount):
    try:
        router = w3_bsc.eth.contract(address=PANCAKE_ROUTER_ADDRESS, abi=PANCAKE_ROUTER_ABI)
        amount_in = w3_bsc.to_wei(amount, 'ether')
        amount_out_min = int(amount_in * (1 - slippage_max))
        tx = router.functions.swapExactETHForTokens(
            amount_out_min,
            [w3_bsc.to_checksum_address('0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c'), w3_bsc.to_checksum_address(contract_address)],
            w3_bsc.to_checksum_address(WALLET_ADDRESS),
            int(time.time()) + 30
        ).build_transaction({
            'from': WALLET_ADDRESS, 'value': amount_in, 'gas': 250000,
            'gasPrice': get_dynamic_gas_price('bsc'), 'nonce': w3_bsc.eth.get_transaction_count(WALLET_ADDRESS)
        })
        signed_tx = w3_bsc.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash = w3_bsc.eth.send_raw_transaction(signed_tx.rawTransaction)
        await bot.send_message(chat_id, f"⏳ Achat BSC {amount} BNB : {contract_address}, TX: {tx_hash.hex()}")
        receipt = w3_bsc.eth.wait_for_transaction_receipt(tx_hash, timeout=20)
        if receipt.status == 1:
            entry_price = detected_tokens[contract_address]['price']
            portfolio[contract_address] = {
                'amount': amount, 'chain': 'bsc', 'entry_price': entry_price,
                'highest_price': entry_price, 'price_history': [entry_price], 'profit': 0.0, 'buy_time': time.time()
            }
            await bot.send_message(chat_id, f"✅ Achat réussi : {amount} BNB de {contract_address}")
            daily_trades['buys'].append({'token': contract_address, 'chain': 'bsc', 'amount': amount, 'timestamp': datetime.now().strftime('%H:%M:%S')})
    except Exception as e:
        await bot.send_message(chat_id, f"⚠️ Erreur achat BSC: {str(e)}")

async def buy_token_eth(chat_id, contract_address, amount):
    try:
        router = w3_eth.eth.contract(address=UNISWAP_ROUTER_ADDRESS, abi=PANCAKE_ROUTER_ABI)
        amount_in = w3_eth.to_wei(amount, 'ether')
        amount_out_min = int(amount_in * (1 - slippage_max))
        tx = router.functions.swapExactETHForTokens(
            amount_out_min,
            [w3_eth.to_checksum_address('0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2'), w3_eth.to_checksum_address(contract_address)],
            w3_eth.to_checksum_address(WALLET_ADDRESS),
            int(time.time()) + 30
        ).build_transaction({
            'from': WALLET_ADDRESS, 'value': amount_in, 'gas': 250000,
            'gasPrice': get_dynamic_gas_price('eth'), 'nonce': w3_eth.eth.get_transaction_count(WALLET_ADDRESS)
        })
        signed_tx = w3_eth.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash = w3_eth.eth.send_raw_transaction(signed_tx.rawTransaction)
        await bot.send_message(chat_id, f"⏳ Achat Ethereum {amount} ETH : {contract_address}, TX: {tx_hash.hex()}")
        receipt = w3_eth.eth.wait_for_transaction_receipt(tx_hash, timeout=20)
        if receipt.status == 1:
            entry_price = detected_tokens[contract_address]['price']
            portfolio[contract_address] = {
                'amount': amount, 'chain': 'eth', 'entry_price': entry_price,
                'highest_price': entry_price, 'price_history': [entry_price], 'profit': 0.0, 'buy_time': time.time()
            }
            await bot.send_message(chat_id, f"✅ Achat réussi : {amount} ETH de {contract_address}")
            daily_trades['buys'].append({'token': contract_address, 'chain': 'eth', 'amount': amount, 'timestamp': datetime.now().strftime('%H:%M:%S')})
    except Exception as e:
        await bot.send_message(chat_id, f"⚠️ Erreur achat Ethereum: {str(e)}")

async def buy_token_solana(chat_id, contract_address, amount):
    try:
        amount_in = int(amount * 10**9)
        response = requests.post(QUICKNODE_SOL_URL, json={
            "jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash", "params": [{"commitment": "finalized"}]
        }, timeout=5)
        blockhash = response.json()['result']['value']['blockhash']
        tx = Transaction()
        tx.recent_blockhash = Pubkey.from_string(blockhash)
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
        tx_hash = requests.post(QUICKNODE_SOL_URL, json={
            "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
            "params": [base58.b58encode(tx.serialize()).decode('utf-8')]
        }, timeout=5).json()['result']
        await bot.send_message(chat_id, f"⏳ Achat Solana {amount} SOL : {contract_address}, TX: {tx_hash}")
        entry_price = detected_tokens[contract_address]['price']
        portfolio[contract_address] = {
            'amount': amount, 'chain': 'solana', 'entry_price': entry_price,
            'highest_price': entry_price, 'price_history': [entry_price], 'profit': 0.0, 'buy_time': time.time()
        }
        await bot.send_message(chat_id, f"✅ Achat réussi : {amount} SOL de {contract_address}")
        daily_trades['buys'].append({'token': contract_address, 'chain': 'solana', 'amount': amount, 'timestamp': datetime.now().strftime('%H:%M:%S')})
    except Exception as e:
        await bot.send_message(chat_id, f"⚠️ Erreur achat Solana: {str(e)}")

async def sell_token(chat_id, contract_address, amount, chain, current_price):
    global mise_depart_bsc, mise_depart_eth, mise_depart_sol
    try:
        profit_pct = (current_price - portfolio[contract_address]['entry_price']) / portfolio[contract_address]['entry_price'] * 100
        if chain == 'bsc':
            router = w3_bsc.eth.contract(address=PANCAKE_ROUTER_ADDRESS, abi=PANCAKE_ROUTER_ABI)
            token_amount = w3_bsc.to_wei(amount, 'ether')
            amount_in_max = int(token_amount * (1 + slippage_max))
            tx = router.functions.swapExactTokensForETH(
                token_amount, amount_in_max,
                [w3_bsc.to_checksum_address(contract_address), w3_bsc.to_checksum_address('0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c')],
                w3_bsc.to_checksum_address(WALLET_ADDRESS),
                int(time.time()) + 30
            ).build_transaction({
                'from': WALLET_ADDRESS, 'gas': 250000,
                'gasPrice': get_dynamic_gas_price('bsc'), 'nonce': w3_bsc.eth.get_transaction_count(WALLET_ADDRESS)
            })
            signed_tx = w3_bsc.eth.account.sign_transaction(tx, PRIVATE_KEY)
            tx_hash = w3_bsc.eth.send_raw_transaction(signed_tx.rawTransaction)
            receipt = w3_bsc.eth.wait_for_transaction_receipt(tx_hash, timeout=20)
            if receipt.status == 1:
                profit = (current_price - portfolio[contract_address]['entry_price']) * amount
                portfolio[contract_address]['profit'] += profit
                portfolio[contract_address]['amount'] -= amount
                reinvest_amount = profit * profit_reinvestment_ratio
                mise_depart_bsc += reinvest_amount
                await bot.send_message(chat_id, f"✅ Vente BSC réussie : {amount} BNB, Profit: {profit:.4f} BNB")
                if portfolio[contract_address]['amount'] <= 0:
                    del portfolio[contract_address]
                daily_trades['sells'].append({'token': contract_address, 'chain': 'bsc', 'amount': amount, 'pnl': profit, 'timestamp': datetime.now().strftime('%H:%M:%S')})
        elif chain == 'eth':
            router = w3_eth.eth.contract(address=UNISWAP_ROUTER_ADDRESS, abi=PANCAKE_ROUTER_ABI)
            token_amount = w3_eth.to_wei(amount, 'ether')
            amount_in_max = int(token_amount * (1 + slippage_max))
            tx = router.functions.swapExactTokensForETH(
                token_amount, amount_in_max,
                [w3_eth.to_checksum_address(contract_address), w3_eth.to_checksum_address('0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2')],
                w3_eth.to_checksum_address(WALLET_ADDRESS),
                int(time.time()) + 30
            ).build_transaction({
                'from': WALLET_ADDRESS, 'gas': 250000,
                'gasPrice': get_dynamic_gas_price('eth'), 'nonce': w3_eth.eth.get_transaction_count(WALLET_ADDRESS)
            })
            signed_tx = w3_eth.eth.account.sign_transaction(tx, PRIVATE_KEY)
            tx_hash = w3_eth.eth.send_raw_transaction(signed_tx.rawTransaction)
            receipt = w3_eth.eth.wait_for_transaction_receipt(tx_hash, timeout=20)
            if receipt.status == 1:
                profit = (current_price - portfolio[contract_address]['entry_price']) * amount
                portfolio[contract_address]['profit'] += profit
                portfolio[contract_address]['amount'] -= amount
                reinvest_amount = profit * profit_reinvestment_ratio
                mise_depart_eth += reinvest_amount
                await bot.send_message(chat_id, f"✅ Vente ETH réussie : {amount} ETH, Profit: {profit:.4f} ETH")
                if portfolio[contract_address]['amount'] <= 0:
                    del portfolio[contract_address]
                daily_trades['sells'].append({'token': contract_address, 'chain': 'eth', 'amount': amount, 'pnl': profit, 'timestamp': datetime.now().strftime('%H:%M:%S')})
        else:
            amount_out = int(amount * 10**9)
            response = requests.post(QUICKNODE_SOL_URL, json={
                "jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash", "params": [{"commitment": "finalized"}]
            }, timeout=5)
            blockhash = response.json()['result']['value']['blockhash']
            tx = Transaction()
            tx.recent_blockhash = Pubkey.from_string(blockhash)
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
            tx_hash = requests.post(QUICKNODE_SOL_URL, json={
                "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                "params": [base58.b58encode(tx.serialize()).decode('utf-8')]
            }, timeout=5).json()['result']
            profit = (current_price - portfolio[contract_address]['entry_price']) * amount
            portfolio[contract_address]['profit'] += profit
            portfolio[contract_address]['amount'] -= amount
            reinvest_amount = profit * profit_reinvestment_ratio
            mise_depart_sol += reinvest_amount
            await bot.send_message(chat_id, f"✅ Vente Solana réussie : {amount} SOL, Profit: {profit:.4f} SOL")
            if portfolio[contract_address]['amount'] <= 0:
                del portfolio[contract_address]
            daily_trades['sells'].append({'token': contract_address, 'chain': 'solana', 'amount': amount, 'pnl': profit, 'timestamp': datetime.now().strftime('%H:%M:%S')})
    except Exception as e:
        await bot.send_message(chat_id, f"⚠️ Erreur vente {contract_address}: {str(e)}")

async def monitor_and_sell(chat_id):
    while trade_active:
        try:
            if not portfolio:
                await asyncio.sleep(2)
                continue
            for contract_address, data in list(portfolio.items()):
                chain = data['chain']
                amount = data['amount']
                current_data = get_token_data(contract_address, chain)
                if not current_data:
                    continue
                current_price = current_data['price']
                data['price_history'].append(current_price)
                if len(data['price_history']) > 10:
                    data['price_history'].pop(0)
                data['highest_price'] = max(data['highest_price'], current_price)
                profit_pct = (current_price - data['entry_price']) / data['entry_price'] * 100 if data['entry_price'] > 0 else 0
                loss_pct = -profit_pct if profit_pct < 0 else 0
                trailing_stop_price = data['highest_price'] * (1 - trailing_stop_percentage / 100)

                if profit_pct >= take_profit_steps[4] * 100:
                    await sell_token(chat_id, contract_address, amount * 0.5, chain, current_price)
                elif profit_pct >= take_profit_steps[3] * 100:
                    await sell_token(chat_id, contract_address, amount * 0.25, chain, current_price)
                elif profit_pct >= take_profit_steps[2] * 100:
                    await sell_token(chat_id, contract_address, amount * 0.2, chain, current_price)
                elif profit_pct >= take_profit_steps[1] * 100:
                    await sell_token(chat_id, contract_address, amount * 0.15, chain, current_price)
                elif profit_pct >= take_profit_steps[0] * 100:
                    await sell_token(chat_id, contract_address, amount * 0.1, chain, current_price)
                elif current_price <= trailing_stop_price or loss_pct >= stop_loss_threshold:
                    await sell_token(chat_id, contract_address, amount, chain, current_price)
            await asyncio.sleep(1)
        except Exception as e:
            await bot.send_message(chat_id, f"⚠️ Erreur surveillance: {str(e)}")
            await asyncio.sleep(5)

async def show_portfolio(chat_id):
    msg = "💰 Portefeuille:\n"
    for ca, data in portfolio.items():
        chain = data['chain']
        current_price = get_token_data(ca, chain)['price'] if get_token_data(ca, chain) else data['entry_price']
        profit = (current_price - data['entry_price']) * data['amount']
        msg += f"{ca} ({chain}): {data['amount']} @ {current_price:.6f}, Profit: {profit:.4f} {chain.upper()}\n"
    await bot.send_message(chat_id, msg if portfolio else "💰 Portefeuille vide")

async def show_daily_summary(chat_id):
    msg = f"📅 Récapitulatif ({datetime.now().strftime('%Y-%m-%d')}):\n\n📈 Achats:\n"
    for trade in daily_trades['buys']:
        msg += f"- {trade['token']} ({trade['chain']}): {trade['amount']} à {trade['timestamp']}\n"
    msg += "\n📉 Ventes:\n"
    for trade in daily_trades['sells']:
        msg += f"- {trade['token']} ({trade['chain']}): {trade['amount']}, PNL: {trade['pnl']:.4f} à {trade['timestamp']}\n"
    await bot.send_message(chat_id, msg if daily_trades['buys'] or daily_trades['sells'] else "📅 Aucun trade aujourd'hui")

@bot.message_handler(commands=['start'])
async def start_message(message):
    await bot.send_message(message.chat.id, "✅ Bot démarré!")
    await bot.send_message(message.chat.id, "Utilisez /menu pour voir les options.")

@bot.message_handler(commands=['menu'])
async def menu_message(message):
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("▶️ Lancer", callback_data="launch"))
    markup.add(InlineKeyboardButton("⏹️ Arrêter", callback_data="stop"))
    markup.add(InlineKeyboardButton("💰 Portefeuille", callback_data="portfolio"))
    markup.add(InlineKeyboardButton("📅 Récapitulatif", callback_data="summary"))
    await bot.send_message(message.chat.id, "Menu:", reply_markup=markup)

@bot.message_handler(commands=['stop'])
async def stop_message(message):
    global trade_active
    trade_active = False
    await bot.send_message(message.chat.id, "⏹️ Trading arrêté!")
    logger.info("Bot arrêté via /stop")

@bot.callback_query_handler(func=lambda call: True)
async def callback_query(call):
    global trade_active
    chat_id = call.message.chat.id
    if call.data == "launch" and not trade_active:
        trade_active = True
        await bot.send_message(chat_id, "▶️ Trading lancé!")
        await asyncio.gather(
            snipe_new_pairs_bsc(chat_id),
            snipe_new_pairs_eth(chat_id),
            snipe_solana_pools(chat_id),
            monitor_and_sell(chat_id)
        )
    elif call.data == "stop":
        trade_active = False
        await bot.send_message(chat_id, "⏹️ Trading arrêté!")
    elif call.data == "portfolio":
        await show_portfolio(chat_id)
    elif call.data == "summary":
        await show_daily_summary(chat_id)

async def main():
    await bot.polling(none_stop=True)

if __name__ == "__main__":
    asyncio.run(main())
