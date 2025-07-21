import re
import asyncio
import logging
import os
import time
import json
import random
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from telethon import TelegramClient, events, Button
from telethon.tl.types import ChannelParticipantAdmin, ChannelParticipantCreator
from telethon.tl.functions.channels import GetParticipantRequest
from flask import Flask, jsonify, request, redirect, session, render_template_string
import threading
from datetime import datetime
import base64 # Base64 decoding

# Solana Libraries
from solana.rpc.api import Client, RPCException
from solana.rpc.types import TxOpts
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction 
from solders.message import MessageV0
from solders.instruction import Instruction
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.rpc.responses import GetBalanceResp, GetHealthResp # Import GetHealthResp as well

# --- Environment Variables ---
DB_NAME = os.environ.get("DB_NAME", "your_db_name")
DB_USER = os.environ.get("DB_USER", "your_db_user")
DB_PASS = os.environ.get("DB_PASS")
DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = os.environ.get("DB_PORT", "5432")

BOT_TOKEN = os.environ.get("BOT_TOKEN") 
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")

SOURCE_CHANNEL_ID = int(os.environ.get("SOURCE_CHANNEL_ID")) 

SOLANA_PRIVATE_KEY = os.environ.get("SOLANA_PRIVATE_KEY") 
SOLANA_RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
JUPITER_API_URL = os.environ.get("JUPITER_API_URL", "https://quote-api.jup.ag/v6")

SECRET_KEY = os.environ.get("SECRET_KEY", os.urandom(24).hex())

app = Flask(__name__)
app.secret_key = SECRET_KEY

# --- Telethon Clients ---
bot_client = TelegramClient('auto_buy_bot_session', API_ID, API_HASH)

# --- Solana Client and Wallet Initialization ---
solana_client = None
payer_keypair = None

# List of RPC endpoints to try
RPC_ENDPOINTS = [
    "https://api.mainnet-beta.solana.com",
    "https://solana-mainnet.rpc.extrnode.com",
    "https://rpc.ankr.com/solana",
    "https://fra59.nodes.rpcpool.com" # Triton One
]

async def get_healthy_client():
    """
    Attempts to connect to a healthy Solana RPC endpoint from a predefined list.
    Returns a Client object if successful, None otherwise.
    """
    for url in RPC_ENDPOINTS:
        try:
            logger.info(f"Testing RPC URL: {url}")
            client = Client(url)
            # get_health() might return a dict, string, or GetHealthResp, check its value
            health_response = await asyncio.to_thread(client.get_health)
            
            health_status = None
            if isinstance(health_response, dict) and 'result' in health_response and health_response['result'] == 'ok':
                health_status = 'healthy'
            elif isinstance(health_response, str) and health_response == 'ok': 
                health_status = 'healthy'
            elif isinstance(health_response, GetHealthResp) and health_response.value == 'ok': 
                health_status = 'healthy'

            if health_status == "healthy":
                logger.info(f"Connected to healthy RPC: {url}")
                return client
        except Exception as e:
            logger.warning(f"Failed to connect to RPC {url}: {e}")
    logger.error("No healthy RPC endpoint found after multiple attempts.")
    return None

async def get_balance_with_retry(pubkey: Pubkey, retries=3):
    """
    Retrieves Solana balance with a retry mechanism.
    Handles both GetBalanceResp objects and direct dict responses.
    """
    for i in range(retries):
        try:
            resp = await asyncio.to_thread(solana_client.get_balance, pubkey)
            
            # If it's the expected GetBalanceResp object
            if isinstance(resp, GetBalanceResp):
                return resp.value
            # If it's a dict, try to parse it as a raw JSON RPC response
            elif isinstance(resp, dict):
                # Ensure the 'result' key exists and is a dict, and 'value' key exists within it
                if 'result' in resp and isinstance(resp['result'], dict) and 'value' in resp['result']:
                    return resp['result']['value']
                elif 'error' in resp:
                    logger.warning(f"RPC Error in dict response for get_balance: {resp['error']}. Attempt {i+1}/{retries}")
                else:
                    logger.warning(f"Unexpected dict structure for get_balance: {resp}. Attempt {i+1}/{retries}")
            else:
                logger.warning(f"Unexpected response type for get_balance: {type(resp)}. Full response: {resp}. Attempt {i+1}/{retries}")
        except Exception as e:
            logger.warning(f"Balance check attempt {i+1}/{retries} failed: {e}")
            await asyncio.sleep(1) # Short delay before retrying
    return None

async def check_wallet_balance():
    """
    Checks the wallet's SOL balance and returns it in SOL.
    Returns None on error.
    """
    if not solana_client or not payer_keypair:
        logger.error("Solana client or payer keypair not initialized. Cannot check balance.")
        return None
    
    try:
        balance_lamports = await get_balance_with_retry(payer_keypair.pubkey())
        
        if balance_lamports is None:
            logger.error("Wallet balance could not be retrieved.")
            return None
            
        return balance_lamports / 10**9  # In SOL
    except Exception as e:
        logger.error(f"Balance check error: {str(e)}", exc_info=True)
        return None

async def init_solana_client():
    """Initializes the Solana RPC client and wallet."""
    global solana_client, payer_keypair
    try:
        # Try to get a healthy RPC client
        solana_client = await get_healthy_client()
        if not solana_client:
            logger.critical("Failed to initialize Solana client: No healthy RPC found. Auto-trading functions will be disabled.")
            solana_client = None
            payer_keypair = None # Ensure keypair is also None if RPC is not ready
            return

        current_private_key = await get_bot_setting("SOLANA_PRIVATE_KEY")
        if current_private_key:
            try:
                payer_keypair = Keypair.from_base58_string(current_private_key)
                logger.info(f"Solana client initialized. Wallet public key: {payer_keypair.pubkey()}")
                logger.info(f"Active RPC URL: {solana_client.endpoint_uri}") # Log the active RPC URL
                logger.info(f"Public Key: {payer_keypair.pubkey()}")
                
                balance = await check_wallet_balance()
                logger.info(f"Başlangıç bakiyesi: {balance if balance is not None else 'Alınamadı'} SOL")

            except Exception as e:
                logger.error(f"Error initializing payer keypair from private key: {e}", exc_info=True)
                payer_keypair = None # Invalidate keypair if it's bad
        else:
            logger.error("SOLANA_PRIVATE_KEY not set in bot settings. Auto-buying functionality will be disabled.")
            payer_keypair = None # Ensure keypair is None if not set
    except Exception as e:
        logger.error(f"Error during Solana client initialization process: {e}", exc_info=True)
        solana_client = None
        payer_keypair = None

# --- Database Connection and Management Functions (PostgreSQL) ---
def get_connection():
    """Provides a PostgreSQL database connection."""
    try:
        return psycopg2.connect(
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASS,
            host=DB_HOST,
            port=DB_PORT,
            sslmode="require"
        )
    except psycopg2.OperationalError as e:
        logger.error(f"Database connection failed: {e}")
        raise e

def init_db_sync():
    """Creates database tables (if they don't exist)."""
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS admins (
                    user_id BIGINT PRIMARY KEY,
                    first_name TEXT NOT NULL,
                    last_name TEXT,
                    lang TEXT,
                    is_default BOOLEAN DEFAULT FALSE
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_settings (
                    setting_key TEXT PRIMARY KEY,
                    setting_value TEXT
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS processed_contracts (
                    contract_address TEXT PRIMARY KEY,
                    timestamp REAL NOT NULL
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS open_positions (
                    contract_address TEXT PRIMARY KEY,
                    token_name TEXT NOT NULL,
                    buy_price_sol REAL NOT NULL,
                    buy_amount_token REAL NOT NULL,
                    buy_tx_signature TEXT NOT NULL,
                    target_profit_x REAL NOT NULL,
                    stop_loss_percent REAL NOT NULL,
                    buy_timestamp REAL NOT NULL
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS transaction_history (
                    id SERIAL PRIMARY KEY,
                    tx_signature TEXT NOT NULL,
                    type TEXT NOT NULL,
                    token_name TEXT NOT NULL,
                    contract_address TEXT NOT NULL,
                    amount_sol REAL,
                    amount_token REAL,
                    price_sol_per_token REAL,
                    timestamp REAL NOT NULL,
                    status TEXT NOT NULL,
                    error_message TEXT
                );
            """)
        conn.commit()
        logger.info("Database initialized or already exists.")
    except Exception as e:
        logger.error(f"Error during database initialization: {e}")
        raise
    finally:
        if conn:
            conn.close()

def get_admins_sync():
    conn = None
    try:
        conn = get_connection()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM admins")
            rows = cur.fetchall()
            return {r["user_id"]: r for r in rows}
    except Exception as e:
        logger.error(f"Error getting admins: {e}")
        return {}
    finally:
        if conn:
            conn.close()

def add_admin_sync(user_id, first_name, last_name="", lang="en", is_default=False):
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO admins (user_id, first_name, last_name, lang, is_default)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE
                  SET first_name=%s, last_name=%s, lang=%s, is_default=%s;
            """, (user_id, first_name, last_name, lang, is_default,
                  first_name, last_name, lang, is_default))
        conn.commit()
        logger.info(f"Admin {user_id} added/updated.")
    except Exception as e:
        logger.error(f"Error adding admin {user_id}: {e}")
    finally:
        if conn:
            conn.close()

def remove_admin_sync(user_id):
    admins = get_admins_sync()
    if admins.get(user_id, {}).get("is_default"):
        logger.warning(f"Attempted to remove default admin {user_id}.")
        return
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM admins WHERE user_id = %s", (user_id,))
        conn.commit()
        logger.info(f"Admin {user_id} removed.")
    except Exception as e:
        logger.error(f"Error removing admin {user_id}: {e}")
    finally:
        if conn:
            conn.close()

def get_bot_setting_sync(setting):
    conn = None
    try:
        conn = get_connection()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT setting_value FROM bot_settings WHERE setting_key = %s", (setting,))
            row = cur.fetchone()
            return row["setting_value"] if row else None
    except Exception as e:
        logger.error(f"Error getting bot setting '{setting}': {e}")
        return None
    finally:
        if conn:
            conn.close()

def set_bot_setting_sync(setting, value):
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO bot_settings (setting_key, setting_value)
                VALUES (%s, %s)
                ON CONFLICT (setting_key) DO UPDATE SET setting_value = %s
            """, (setting, value, value))
        conn.commit()
        if cur.rowcount > 0:
            # Mask private key if it's the SOLANA_PRIVATE_KEY setting
            if setting == "SOLANA_PRIVATE_KEY":
                logger.info(f"Bot setting '{setting}' set. Value masked for security.")
            else:
                logger.info(f"Bot setting '{setting}' set to '{value}'.")
    except Exception as e:
        logger.error(f"Error setting bot setting '{setting}': {e}")
    finally:
        if conn:
            conn.close()

def is_contract_processed_sync(contract_address):
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM processed_contracts WHERE contract_address = %s",
                        (contract_address,))
            return cur.fetchone() is not None
    except Exception as e:
        logger.error(f"Error checking if contract {contract_address} processed: {e}")
        return False
    finally:
        if conn:
            conn.close()

def record_processed_contract_sync(contract_address):
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO processed_contracts (contract_address, timestamp)
                VALUES (%s, %s) ON CONFLICT DO NOTHING
            """, (contract_address, time.time()))
        conn.commit()
        if cur.rowcount > 0:
            logger.info(f"Recorded processed contract: {contract_address}.")
    except Exception as e:
        logger.error(f"Error recording processed contract {contract_address}: {e}")
    finally:
        if conn:
            conn.close()

def add_open_position_sync(contract_address, token_name, buy_price_sol, buy_amount_token, buy_tx_signature, target_profit_x, stop_loss_percent):
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO open_positions (contract_address, token_name, buy_price_sol, buy_amount_token, buy_tx_signature, target_profit_x, stop_loss_percent, buy_timestamp)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (contract_address) DO UPDATE
                SET token_name = EXCLUDED.token_name,
                    buy_price_sol = EXCLUDED.buy_price_sol,
                    buy_amount_token = EXCLUDED.buy_amount_token,
                    buy_tx_signature = EXCLUDED.buy_tx_signature,
                    target_profit_x = EXCLUDED.target_profit_x,
                    stop_loss_percent = EXCLUDED.stop_loss_percent,
                    buy_timestamp = EXCLUDED.buy_timestamp;
            """, (contract_address, token_name, buy_price_sol, buy_amount_token, buy_tx_signature, target_profit_x, stop_loss_percent, time.time()))
        conn.commit()
        logger.info(f"Open position added/updated for {token_name} ({contract_address}).")
    except Exception as e:
        logger.error(f"Error adding/updating open position for {contract_address}: {e}")
    finally:
        if conn:
            conn.close()

def get_open_positions_sync():
    conn = None
    try:
        conn = get_connection()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM open_positions")
            return cur.fetchall()
    except Exception as e:
        logger.error(f"Error getting open positions: {e}")
        return []
    finally:
        if conn:
            conn.close()

def remove_open_position_sync(contract_address):
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM open_positions WHERE contract_address = %s", (contract_address,))
        conn.commit()
        if cur.rowcount > 0:
            logger.info(f"Open position for {contract_address} removed.")
        else:
            logger.warning(f"No open position found for {contract_address} to remove.")
    except Exception as e:
        logger.error(f"Error removing open position for {contract_address}: {e}")
    finally:
        if conn:
            conn.close()

def add_transaction_history_sync(tx_signature, tx_type, token_name, contract_address, amount_sol, amount_token, price_sol_per_token, status, error_message=None):
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO transaction_history (tx_signature, type, token_name, contract_address, amount_sol, amount_token, price_sol_per_token, timestamp, status, error_message)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (tx_signature, tx_type, token_name, contract_address, amount_sol, amount_token, price_sol_per_token, time.time(), status, error_message))
        conn.commit()
        if cur.rowcount > 0:
            logger.info(f"Transaction recorded: {tx_type} for {token_name} ({tx_signature}).")
    except Exception as e:
        logger.error(f"Error recording transaction history for {tx_signature}: {e}")
    finally:
        if conn:
            conn.close()

def get_transaction_history_sync():
    conn = None
    try:
        conn = get_connection()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM transaction_history ORDER BY timestamp DESC LIMIT 20;")
            return cur.fetchall()
    except Exception as e:
        logger.error(f"Error getting transaction history: {e}")
        return []
    finally:
        if conn:
            conn.close()

# --- Async Database Functions Wrappers ---
async def init_db():
    await asyncio.to_thread(init_db_sync)

async def get_admins():
    return await asyncio.to_thread(get_admins_sync)

async def add_admin(user_id, first_name, last_name="", lang="en", is_default=False):
    await asyncio.to_thread(add_admin_sync, user_id, first_name, last_name, lang, is_default)

async def remove_admin(user_id):
    await asyncio.to_thread(remove_admin_sync, user_id)

async def get_bot_setting(setting):
    val = await asyncio.to_thread(get_bot_setting_sync, setting)
    if setting == "SOLANA_PRIVATE_KEY" and val is None:
        return os.environ.get("SOLANA_PRIVATE_KEY")
    return val if val is not None else DEFAULT_BOT_SETTINGS.get(setting)

async def set_bot_setting(setting, value):
    await asyncio.to_thread(set_bot_setting_sync, setting, value)

async def is_contract_processed(contract_address):
    return await asyncio.to_thread(is_contract_processed_sync, contract_address)

async def record_processed_contract(contract_address):
    await asyncio.to_thread(record_processed_contract_sync, contract_address)

async def add_open_position(contract_address, token_name, buy_price_sol, buy_amount_token, buy_tx_signature, target_profit_x, stop_loss_percent):
    await asyncio.to_thread(add_open_position_sync, contract_address, token_name, buy_price_sol, buy_amount_token, buy_tx_signature, target_profit_x, stop_loss_percent)

async def get_open_positions():
    return await asyncio.to_thread(get_open_positions_sync)

async def remove_open_position(contract_address):
    await asyncio.to_thread(remove_open_position_sync, contract_address)

async def add_transaction_history(*args):
    await asyncio.to_thread(add_transaction_history_sync, *args)

async def get_transaction_history():
    return await asyncio.to_thread(get_transaction_history_sync)

# --- Default Settings ---
DEFAULT_ADMIN_ID = int(os.environ.get("DEFAULT_ADMIN_ID", "YOUR_TELEGRAM_USER_ID")) 
DEFAULT_BOT_SETTINGS = {
    "bot_status": "running",
    "auto_buy_enabled": "enabled",
    "buy_amount_sol": "0.05",
    "slippage_tolerance": "5",
    "auto_sell_enabled": "enabled",
    "profit_target_x": "5.0",
    "stop_loss_percent": "50.0",
    "SOLANA_PRIVATE_KEY": os.environ.get("SOLANA_PRIVATE_KEY", "")
}

# --- Logging Setup ---
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("logs/auto_buy_sell_bot.log", mode='a'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)
logger.info("🔥 Auto-Buy/Sell Bot Logging setup complete. Bot is starting...")

# --- Telethon Helper Functions ---
async def retry_telethon_call(coro, max_retries=5, base_delay=1.0):
    """Retry mechanism for Telethon calls."""
    for i in range(max_retries):
        try:
            return await coro
        except Exception as e:
            logger.warning(f"Retry attempt {i+1}/{max_retries} for Telethon call due to error: {e}")
            if i < max_retries - 1:
                delay = base_delay * (2 ** i) + random.uniform(0, 1)
                await asyncio.sleep(delay)
            else:
                logger.error(f"Max retries reached for Telethon call: {e}")
                raise
    raise RuntimeError("Retry logic failed or max_retries was 0")

def extract_contract(text: str) -> str | None:
    """Extracts Solana contract address (Base58, 32-44 characters) from text."""
    m = re.findall(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b", text)
    return m[0] if m else None

def extract_token_name_from_message(text: str) -> str:
    """Extracts token name (in $TOKEN_NAME format) from the message."""
    lines = text.strip().splitlines()
    if not lines:
        logger.debug("Empty message received for token extraction; returning 'unknown'.")
        return "unknown"
    for line in lines:
        match = re.search(r"\$([A-Za-z0-9_]+)", line)
        if match:
            token = match.group(1)
            logger.debug(f"Token extracted: '{token}' from line: '{line}'")
            return token
    logger.debug("No valid token ($WORD) found in the message; returning 'unknown'.")
    return "unknown"

# --- Solana Auto-Buy Functions ---
async def get_current_token_price_sol(token_mint_str: str, amount_token_to_check: float = 0.000000001):
    """Estimates the current SOL price of a specific token."""
    if not solana_client:
        logger.error("Solana client not initialized. Cannot get token price.")
        return None

    try:
        token_mint = Pubkey.from_string(token_mint_str)
        input_mint = token_mint
        output_mint = Pubkey.from_string("So11111111111111111111111111111111111111112")

        token_info = await asyncio.to_thread(solana_client.get_token_supply, input_mint) 
        if not token_info or not hasattr(token_info, 'value') or not hasattr(token_info.value, 'decimals'):
            logger.warning(f"Could not get token supply info for {token_mint_str}. Cannot determine decimals.")
            return None
        decimals = token_info.value.decimals
        
        amount_in_lamports = int(amount_token_to_check * (10**decimals))

        quote_url = f"{JUPITER_API_URL}/quote?inputMint={input_mint}&outputMint={output_mint}&amount={amount_in_lamports}&slippageBps=0"
        response = requests.get(quote_url)
        response.raise_for_status()
        quote_data = response.json()

        if not quote_data or "outAmount" not in quote_data or "inAmount" not in quote_data:
            logger.warning(f"Invalid quote data for price check: {quote_data}")
            return None
        
        price_sol_per_token = (float(quote_data['outAmount']) / (10**9)) / (float(quote_data['inAmount']) / (10**decimals))
        logger.debug(f"Current price for {token_mint_str}: {price_sol_per_token} SOL/token")
        return price_sol_per_token

    except requests.exceptions.RequestException as e:
        logger.error(f"Error getting token price from Jupiter: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error in get_current_token_price_sol: {e}")
        return None

async def get_swap_quote(input_mint: Pubkey, output_mint: Pubkey, amount_in_lamports: int, slippage_bps: int):
    """Gets a swap quote from Jupiter Aggregator."""
    if not solana_client or not payer_keypair:
        logger.error("Solana client or payer keypair not initialized. Cannot get quote.")
        return None

    try:
        quote_url = f"{JUPITER_API_URL}/quote?inputMint={input_mint}&outputMint={output_mint}&amount={amount_in_lamports}&slippageBps={slippage_bps}"
        response = requests.get(quote_url)
        response.raise_for_status()
        quote_data = response.json()
        
        if not quote_data or "swapMode" not in quote_data:
            logger.error(f"Invalid quote data received: {quote_data}")
            return None

        logger.info(f"Jupiter quote received for {input_mint} to {output_mint}: {quote_data.get('outAmount')} {quote_data.get('outputToken', {}).get('symbol')}")
        return quote_data

    except requests.exceptions.RequestException as e:
        logger.error(f"Error getting Jupiter quote: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error in get_swap_quote: {e}")
        return None

async def perform_swap(quote_data: dict):
    """Performs a swap transaction with the quote received from Jupiter Aggregator."""
    if not solana_client or not payer_keypair:
        logger.error("Solana client or payer keypair not initialized. Cannot perform swap.")
        return False, "Solana client or wallet not ready.", None

    try:
        swap_url = f"{JUPITER_API_URL}/swap"
        swap_response = requests.post(swap_url, json={
            "quoteResponse": quote_data,
            "userPublicKey": str(payer_keypair.pubkey()),
            "wrapUnwrapSOL": True,
            "prioritizationFeeLamports": 100000
        })
        swap_response.raise_for_status() # Raises HTTPError for bad responses (e.g., 4xx, 5xx)
        swap_data = swap_response.json()

        if not swap_data or "swapTransaction" not in swap_data:
            logger.error(f"Invalid swap data received from Jupiter: {swap_data}")
            return False, "Invalid swap transaction data.", None

        # CRITICAL FIX: Ensure swapTransaction is a string before decoding
        swap_transaction_str = swap_data.get("swapTransaction")
        if not isinstance(swap_transaction_str, str):
            error_msg = f"Jupiter API returned 'swapTransaction' not as a string. Type: {type(swap_transaction_str)}, Value: {swap_transaction_str}"
            logger.error(error_msg)
            return False, error_msg, None

        # Base64 decode transaction
        tx_bytes = base64.b64decode(swap_transaction_str)
        
        # New method: Send the raw transaction directly
        tx_signature = await asyncio.to_thread(
            solana_client.send_raw_transaction,
            tx_bytes,
            opts=TxOpts(skip_preflight=True)
        )
        
        logger.info(f"Swap transaction sent: {tx_signature}")

        # Wait for confirmation
        confirmation = await asyncio.to_thread(
            solana_client.confirm_transaction,
            tx_signature,
            commitment="confirmed"
        )
        
        # Check confirmation
        if confirmation.value and confirmation.value[0].err:
            logger.error(f"Transaction failed with error: {confirmation.value[0].err}")
            return False, f"Transaction failed: {confirmation.value[0].err}", None
        else:
            logger.info(f"Transaction confirmed: {tx_signature}")
            return True, tx_signature, quote_data

    except requests.exceptions.RequestException as e:
        logger.error(f"Error performing swap with Jupiter: {e}")
        return False, f"HTTP request error: {e}", None
    except RPCException as e:
        logger.error(f"Solana RPC error during swap: {e}")
        return False, f"Solana RPC error: {e}", None
    except Exception as e:
        logger.error(f"Unexpected error in perform_swap: {str(e)}", exc_info=True)
        return False, f"Unexpected error: {str(e)}", None

async def auto_buy_token(contract_address: str, token_name: str, buy_amount_sol: float, slippage_tolerance_percent: float):
    """Automatically buys token at the specified contract address."""
    if not solana_client or not payer_keypair:
        logger.error("Auto-buy skipped: Solana client or wallet not initialized.")
        return False, "Wallet not ready.", None, None

    if await is_contract_processed(contract_address):
        logger.info(f"Contract {contract_address} already processed for auto-buy. Skipping.")
        return False, "Contract already processed.", None, None

    # Check wallet balance
    current_balance = await check_wallet_balance()
    if current_balance is None:
        await add_transaction_history(
            "N/A", 'buy', token_name, contract_address,
            buy_amount_sol, 0.0, 0.0, 'failed', "Wallet balance could not be retrieved."
        )
        return False, "Wallet balance could not be retrieved", None, None
        
    if current_balance < buy_amount_sol:
        error_msg = f"Insufficient SOL balance. Required: {buy_amount_sol} SOL, Available: {current_balance:.4f} SOL."
        logger.error(error_msg)
        await add_transaction_history(
            "N/A", 'buy', token_name, contract_address,
            buy_amount_sol, 0.0, 0.0, 'failed', error_msg
        )
        return False, error_msg, None, None

    input_mint = Pubkey.from_string("So11111111111111111111111111111111111111112")
    output_mint = Pubkey.from_string(contract_address)
    amount_in_lamports = int(buy_amount_sol * 10**9)
    slippage_bps = int(slippage_tolerance_percent * 100)

    logger.info(f"Attempting to auto-buy {contract_address} ({token_name}) with {buy_amount_sol} SOL and {slippage_tolerance_percent}% slippage.")

    quote_data = await get_swap_quote(input_mint, output_mint, amount_in_lamports, slippage_bps)
    if not quote_data:
        await add_transaction_history(
            "N/A", 'buy', token_name, contract_address,
            buy_amount_sol, 0.0, 0.0, 'failed', "Failed to get swap quote."
        )
        logger.error(f"Failed to get swap quote for {contract_address}.")
        return False, "Failed to get swap quote.", None, None

    # Retry mechanism for perform_swap
    max_swap_retries = 3
    swap_success = False
    tx_signature = None
    final_quote_data = None
    swap_error_message = ""

    for attempt in range(max_swap_retries):
        logger.info(f"Attempting swap for {token_name} (Attempt {attempt+1}/{max_swap_retries})")
        success, msg, data = await perform_swap(quote_data)
        if success:
            swap_success = True
            tx_signature = msg
            final_quote_data = data
            swap_error_message = "" # Clear any previous error message
            break
        else:
            swap_error_message = msg # This will be the error message
            logger.warning(f"Swap attempt {attempt+1}/{max_swap_retries} failed for {token_name}: {msg}")
            if attempt < max_swap_retries - 1:
                await asyncio.sleep(2 * (attempt + 1)) # Exponential backoff
    
    if not swap_success:
        await add_transaction_history(
            "N/A", 'buy', token_name, contract_address,
            buy_amount_sol, 0.0, 0.0, 'failed', swap_error_message
        )
        logger.error(f"Failed to auto-buy token {contract_address} after {max_swap_retries} attempts: {swap_error_message}")
        return False, f"Failed to buy token {token_name}: {swap_error_message}", None, None
    
    # If swap was successful
    await record_processed_contract(contract_address)

    output_token_decimals = final_quote_data.get('outputToken', {}).get('decimals')
    if output_token_decimals is None:
        logger.warning(f"Could not determine decimals for {token_name}. Cannot calculate bought amount.")
        bought_amount_token = 0.0
        actual_buy_price_sol = 0.0
    else:
        bought_amount_token_lamports = int(final_quote_data['outAmount'])
        bought_amount_token = bought_amount_token_lamports / (10**output_token_decimals)
        actual_buy_price_sol = buy_amount_sol / bought_amount_token if bought_amount_token > 0 else 0.0

    await add_transaction_history(
        tx_signature, 'buy', token_name, contract_address,
        buy_amount_sol, bought_amount_token, actual_buy_price_sol, 'success'
    )
    logger.info(f"Successfully auto-bought token {contract_address}. Tx: {tx_signature}")
    return True, f"Successfully bought token {token_name}. Tx: {tx_signature}", actual_buy_price_sol, bought_amount_token

async def auto_sell_token(contract_address: str, token_name: str, amount_to_sell_token: float, slippage_tolerance_percent: float):
    """Automatically sells the specified token."""
    if not solana_client or not payer_keypair:
        logger.error("Auto-sell skipped: Solana client or wallet not initialized.")
        return False, "Wallet not ready."

    input_mint = Pubkey.from_string(contract_address)
    output_mint = Pubkey.from_string("So11111111111111111111111111111111111111112")
    slippage_bps = int(slippage_tolerance_percent * 100)

    token_info = await asyncio.to_thread(solana_client.get_token_supply, input_mint)
    if not token_info or not hasattr(token_info, 'value') or not hasattr(token_info.value, 'decimals'):
        await add_transaction_history(
            "N/A", 'sell', token_name, contract_address,
            0.0, amount_to_sell_token, 0.0, 'failed', "Could not get token decimals for selling."
        )
        logger.warning(f"Could not get token supply info for {token_name}. Cannot determine decimals for selling.")
        return False, "Could not get token decimals."
    decimals = token_info.value.decimals
    
    amount_in_lamports = int(amount_to_sell_token * (10**decimals))

    logger.info(f"Attempting to auto-sell {amount_to_sell_token} {token_name} ({contract_address}) with {slippage_tolerance_percent}% slippage.")

    quote_data = await get_swap_quote(input_mint, output_mint, amount_in_lamports, slippage_bps)
    if not quote_data:
        await add_transaction_history(
            "N/A", 'sell', token_name, contract_address,
            0.0, amount_to_sell_token, 0.0, 'failed', "Failed to get swap quote for selling."
        )
        logger.error(f"Failed to get swap quote for selling {token_name}.")
        return False, "Failed to get swap quote for selling."

    # Retry mechanism for perform_swap during sell
    max_swap_retries = 3
    swap_success = False
    tx_signature = None
    final_quote_data = None
    swap_error_message = ""

    for attempt in range(max_swap_retries):
        logger.info(f"Attempting sell swap for {token_name} (Attempt {attempt+1}/{max_swap_retries})")
        success, msg, data = await perform_swap(quote_data)
        if success:
            swap_success = True
            tx_signature = msg
            final_quote_data = data
            swap_error_message = ""
            break
        else:
            swap_error_message = msg
            logger.warning(f"Sell swap attempt {attempt+1}/{max_swap_retries} failed for {token_name}: {msg}")
            if attempt < max_swap_retries - 1:
                await asyncio.sleep(2 * (attempt + 1)) # Exponential backoff
    
    if not swap_success:
        await add_transaction_history(
            "N/A", 'sell', token_name, contract_address,
            0.0, amount_to_sell_token, 0.0, 'failed', swap_error_message
        )
        logger.error(f"Failed to auto-sell token {token_name} after {max_swap_retries} attempts: {swap_error_message}")
        return False, f"Failed to sell token {token_name}: {swap_error_message}"

    # If sell swap was successful
    received_sol_lamports = int(final_quote_data['outAmount'])
    received_sol = received_sol_lamports / (10**9)
    sell_price_sol_per_token = received_sol / amount_to_sell_token if amount_to_sell_token > 0 else 0.0

    await add_transaction_history(
        tx_signature, 'sell', token_name, contract_address,
        received_sol, amount_to_sell_token, sell_price_sol_per_token, 'success'
    )
    logger.info(f"Successfully auto-sold token {token_name}. Tx: {tx_signature}")
    return True, f"Successfully sold token {token_name}. Tx: {tx_signature}"

async def monitor_positions_task():
    """Monitors open positions and performs auto-sells based on profit/loss targets."""
    while True:
        await asyncio.sleep(30)

        auto_sell_enabled = await get_bot_setting("auto_sell_enabled")
        if auto_sell_enabled != "enabled":
            logger.debug("Auto-sell is disabled. Skipping position monitoring.")
            continue

        positions = await get_open_positions()
        if not positions:
            logger.debug("No open positions to monitor.")
            continue

        slippage_tolerance_str = await get_bot_setting("slippage_tolerance")
        try:
            slippage_tolerance_percent = float(slippage_tolerance_str)
        except ValueError:
            logger.error("Invalid slippage tolerance setting for auto-sell. Using default 5%.")
            slippage_tolerance_percent = 5.0

        for pos in positions:
            contract_address = pos['contract_address']
            token_name = pos['token_name']
            buy_price_sol = pos['buy_price_sol']
            buy_amount_token = pos['buy_amount_token']
            target_profit_x = pos['target_profit_x']
            stop_loss_percent = pos['stop_loss_percent']

            current_price_sol = await get_current_token_price_sol(contract_address)
            if current_price_sol is None:
                logger.warning(f"Could not get current price for {token_name}. Skipping monitoring for this position.")
                continue

            profit_threshold_price = buy_price_sol * target_profit_x
            stop_loss_threshold_price = buy_price_sol * (1 - (stop_loss_percent / 100))

            logger.info(f"Monitoring {token_name} ({contract_address}): Buy Price: {buy_price_sol:.8f} SOL/token, Current Price: {current_price_sol:.8f} SOL/token")
            logger.info(f"  Target Profit Price: {profit_threshold_price:.8f} SOL/token (x{target_profit_x}), Stop Loss Price: {stop_loss_threshold_price:.8f} SOL/token ({-stop_loss_percent}%)")

            should_sell = False
            sell_reason = ""

            if current_price_sol >= profit_threshold_price:
                should_sell = True
                sell_reason = f"Profit target ({target_profit_x}x) reached for {token_name}."
            elif current_price_sol <= stop_loss_threshold_price:
                should_sell = True
                sell_reason = f"Stop-loss ({stop_loss_percent}%) triggered for {token_name}."

            if should_sell:
                logger.info(f"Initiating auto-sell for {token_name}: {sell_reason}")
                success, message = await auto_sell_token(contract_address, token_name, buy_amount_token, slippage_tolerance_percent)
                if success:
                    await remove_open_position(contract_address)
                    await bot_client.send_message(
                        DEFAULT_ADMIN_ID,
                        f"✅ Otomatik satım başarılı!\nToken: `{token_name}`\nSebep: `{sell_reason}`\nİşlem: `{message}`",
                        parse_mode='md'
                    )
                    logger.info(f"Auto-sell successful for {token_name}. Position removed.")
                else:
                    await bot_client.send_message(
                        DEFAULT_ADMIN_ID,
                        f"❌ Otomatik satım başarısız!\nToken: `{token_name}`\nSebep: `{sell_reason}`\nHata: `{message}`",
                        parse_mode='md'
                    )
                    logger.error(f"Auto-sell failed for {token_name}: {message}")
            else:
                logger.debug(f"No sell condition met for {token_name}.")

# --- Flask Web Server ---
pending_input = {}

@app.route('/')
def root():
    """Main page showing bot status."""
    return jsonify(status="ok", message="Bot is running"), 200

@app.route('/health')
def health():
    """Bot health check endpoint."""
    return jsonify(status="ok"), 200

# --- Telethon Admin Panel Handlers ---

@bot_client.on(events.CallbackQuery)
async def admin_callback_handler(event):
    """Handles inline button clicks in the admin panel."""
    uid = event.sender_id
    admins = await get_admins()
    if uid not in admins:
        logger.warning(f"Unauthorized callback query from user ID: {uid}")
        return await event.answer("❌ You are not authorized.")

    data = event.data.decode()
    logger.info(f"Admin {uid} triggered callback: {data}")

    try:
        if data == 'admin_home':
            return await event.edit(await get_admin_dashboard(),
                                    buttons=await build_admin_keyboard(), link_preview=False)
        if data == 'admin_start':
            await set_bot_setting("bot_status", "running")
            await event.answer('▶ Bot started.')
            return await event.edit(await get_admin_dashboard(),
                                    buttons=await build_admin_keyboard(), link_preview=False)
        if data == 'admin_pause':
            pending_input[uid] = {'action': 'pause'}
            kb = [[Button.inline("🔙 Back", b"admin_home")]]
            return await event.edit("⏸ *Pause Bot*\n\nHow many minutes should I pause?",
                                    buttons=kb, link_preview=False)
        if data == 'admin_stop':
            await set_bot_setting("bot_status", "stopped")
            await event.answer('🛑 Bot stopped.')
            return await event.edit("🛑 *Bot is shut down.*",
                                    buttons=[[Button.inline("🔄 Start Bot (bring to running state)", b"admin_start")],
                                             [Button.inline("🔙 Back", b"admin_home")]],
                                    link_preview=False)
        
        if data == 'admin_auto_trade_settings':
            return await event.edit(await get_admin_dashboard(),
                                    buttons=await build_auto_trade_keyboard(), link_preview=False)
        
        if data == 'admin_enable_auto_buy':
            if not payer_keypair:
                await event.answer("❌ Solana private key not configured. Auto-buy cannot be enabled.", alert=True)
                return
            await set_bot_setting("auto_buy_enabled", "enabled")
            await event.answer('✅ Auto-Buy Enabled')
            return await event.edit(await get_admin_dashboard(),
                                    buttons=await build_auto_trade_keyboard(), link_preview=False)
        if data == 'admin_disable_auto_buy':
            await set_bot_setting("auto_buy_enabled", "disabled")
            await event.answer('❌ Auto-Buy Disabled')
            return await event.edit(await get_admin_dashboard(),
                                    buttons=await build_auto_trade_keyboard(), link_preview=False)
        if data == 'admin_set_buy_amount':
            pending_input[uid] = {'action': 'set_buy_amount'}
            kb = [[Button.inline("🔙 Back", b"admin_auto_trade_settings")]]
            current_amount = await get_bot_setting("buy_amount_sol")
            return await event.edit(f"💲 *Set Buy Amount*\n\nCurrent amount: `{current_amount} SOL`\n\nEnter the amount of SOL to spend for each auto-buy (e.g., `0.01`, `0.05`):",
                                    buttons=kb, link_preview=False)
        if data == 'admin_set_slippage':
            pending_input[uid] = {'action': 'set_slippage'}
            kb = [[Button.inline("🔙 Back", b"admin_auto_trade_settings")]]
            current_slippage = await get_bot_setting("slippage_tolerance")
            return await event.edit(f"⚙️ *Set Slippage Tolerance*\n\nCurrent slippage: `{current_slippage}%`\n\nEnter the maximum acceptable price slippage as a percentage (e.g., `1`, `5`, `10`):",
                                    buttons=kb, link_preview=False)
        
        if data == 'admin_enable_auto_sell':
            await set_bot_setting("auto_sell_enabled", "enabled")
            await event.answer('✅ Auto-Sell Enabled')
            return await event.edit(await get_admin_dashboard(),
                                    buttons=await build_auto_trade_keyboard(), link_preview=False)
        if data == 'admin_disable_auto_sell':
            await set_bot_setting("auto_sell_enabled", "disabled")
            await event.answer('❌ Auto-Sell Disabled')
            return await event.edit(await get_admin_dashboard(),
                                    buttons=await build_auto_trade_keyboard(), link_preview=False)
        if data == 'admin_set_profit_target':
            pending_input[uid] = {'action': 'set_profit_target'}
            kb = [[Button.inline("🔙 Back", b"admin_auto_trade_settings")]]
            current_target = await get_bot_setting("profit_target_x")
            return await event.edit(f"📈 *Set Profit Target*\n\nCurrent target: `{current_target}x`\n\nEnter how many times the token price should increase before selling (e.g., `2.0` for 2x, `5.0` for 5x):",
                                    buttons=kb, link_preview=False)
        if data == 'admin_set_stop_loss':
            pending_input[uid] = {'action': 'set_stop_loss'}
            kb = [[Button.inline("🔙 Back", b"admin_auto_trade_settings")]]
            current_stop_loss = await get_bot_setting("stop_loss_percent")
            return await event.edit(f"📉 *Set Stop-Loss Percentage*\n\nCurrent stop-loss: `{current_stop_loss}%`\n\nEnter the percentage drop from the buy price at which to sell (e.g., `10` for 10% drop, `50` for 50% drop):",
                                    buttons=kb, link_preview=False)
        
        if data == 'admin_admins':
            admins = await get_admins()
            kb = [
                [Button.inline("➕ Add Admin", b"admin_add_admin")],
            ]
            removable_admins = {aid: info for aid, info in admins.items() if aid != DEFAULT_ADMIN_ID and not info.get("is_default")}
            if removable_admins:
                kb.append([Button.inline("🗑 Remove Admin", b"admin_show_remove_admins")])
            kb.append([Button.inline("🔙 Back", b"admin_home")])
            return await event.edit("👤 *Manage Admins*", buttons=kb, link_preview=False)
        if data == 'admin_show_remove_admins':
            admins = await get_admins()
            kb = []
            for aid, info in admins.items():
                if aid != DEFAULT_ADMIN_ID and not info.get("is_default"):
                    kb.append([Button.inline(f"{info.get('first_name', 'N/A')} ({aid})", b"noop"),
                                 Button.inline("❌ Remove", f"remove_admin:{aid}".encode())])
            kb.append([Button.inline("🔙 Back", b"admin_admins")])
            if not kb:
                return await event.edit("🗑 *No removable admins found.*",
                                       buttons=[[Button.inline("🔙 Back", b"admin_admins")]], link_preview=False)
            return await event.edit("🗑 *Select Admin to Remove*", buttons=kb, link_preview=False)
        if data == 'admin_add_admin':
            pending_input[uid] = {'action': 'confirm_add_admin'}
            return await event.edit("➕ *Add Admin*\n\nSend the user ID to add:",
                                    buttons=[[Button.inline("🔙 Back", b"admin_admins")]], link_preview=False)
        if data.startswith('remove_admin:'):
            aid = int(data.split(':')[1])
            await remove_admin(aid)
            await event.answer("✅ Admin removed", alert=True)
            admins = await get_admins()
            kb = []
            for admin_id, info in admins.items():
                if admin_id != DEFAULT_ADMIN_ID and not info.get("is_default"):
                    kb.append([Button.inline(f"{info.get('first_name', 'N/A')} ({admin_id})", b"noop"),
                                 Button.inline("❌ Remove", f"remove_admin:{admin_id}".encode())])
            kb.append([Button.inline("🔙 Back", b"admin_admins")])
            if not kb:
                return await event.edit("🗑 *No removable admins found.*",
                                       buttons=[[Button.inline("🔙 Back", b"admin_admins")]], link_preview=False)
            return await event.edit("🗑 *Select Admin to Remove*", buttons=kb, link_preview=False)
        
        if data == 'admin_wallet_settings':
            return await event.edit(await get_wallet_settings_dashboard(),
                                    buttons=await build_wallet_settings_keyboard(), link_preview=False)
        if data == 'admin_set_wallet_private_key':
            pending_input[uid] = {'action': 'set_wallet_private_key'}
            kb = [[Button.inline("🔙 Back", b"admin_wallet_settings")]]
            return await event.edit(
                "⚠️ *CAUTION: VERY SENSITIVE INFORMATION!* ⚠️\n\n"
                "Please enter your Solana private key (in Base58 format). "
                "This key grants the bot access to your wallet. "
                "Your funds may be at risk with incorrect or malicious use.\n\n"
                "Paste your new private key here:",
                buttons=kb, parse_mode='md', link_preview=False
            )
        if data == 'admin_transaction_history':
            history = await get_transaction_history()
            if not history:
                history_text = "📜 *Transaction History*\n\nNo transactions found yet."
            else:
                history_text = "📜 *Last 20 Transactions*\n\n"
                for tx in history:
                    status_emoji = "✅" if tx['status'] == 'success' else "❌"
                    tx_type_emoji = "⬆️" if tx['type'] == 'buy' else "⬇️"
                    tx_time = datetime.fromtimestamp(tx['timestamp']).strftime('%Y-%m-%d %H:%M:%S')
                    tx_sig_short = tx['tx_signature'][:6] + "..." + tx['tx_signature'][-4:] if tx['tx_signature'] and tx['tx_signature'] != "N/A" else "N/A"
                    contract_addr_short = tx['contract_address'][:6] + "..." + tx['contract_address'][-4:] if tx['contract_address'] else "N/A"

                    history_text += (
                        f"{status_emoji} {tx_type_emoji} `{tx_time}`\n"
                        f"  Token: *{tx['token_name']}*\n"
                        f"  Contract: `{contract_addr_short}`\n"
                        f"  Amount: `{tx['amount_token']:.4f}` Token / `{tx['amount_sol']:.4f}` SOL\n"
                        f"  Price: `{tx['price_sol_per_token']:.8f}` SOL/Token\n"
                        f"  TX: `{tx_sig_short}`\n"
                    )
                    if tx['error_message']:
                        history_text += f"  Error: `{tx['error_message']}`\n"
                    history_text += "\n"
            
            kb = [[Button.inline("🔙 Back", b"admin_home")]]
            return await event.edit(history_text, buttons=kb, parse_mode='md', link_preview=False)

        await event.answer("Unknown action.")

    except Exception as e:
        logger.error(f"Error in admin_callback_handler for user {uid}, data {data}: {e}")
        await event.answer("❌ An error occurred.")
        await event.edit(f"❌ An error occurred: {e}", buttons=[[Button.inline("🔙 Back", b"admin_home")]], parse_mode='md', link_preview=False)

@bot_client.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    """Runs when the bot is started or /start command is received."""
    uid = event.sender_id
    admins = await get_admins()
    if uid not in admins:
        if not admins:
            await add_admin(uid, event.sender.first_name, event.sender.last_name, is_default=True)
            logger.info(f"Default admin set to: {uid}")
            await event.reply("🎉 Welcome! You have been set as the default admin. Use `/admin` to manage the bot.")
        else:
            logger.warning(f"Unauthorized /start command from user ID: {uid}")
            return await event.reply("❌ You are not authorized to use this bot.")
    
    await event.reply(await get_admin_dashboard(), buttons=await build_admin_keyboard(), parse_mode='md', link_preview=False)

@bot_client.on(events.NewMessage(pattern='/admin'))
async def admin_handler(event):
    """Shows the admin panel when /admin command is received."""
    uid = event.sender_id
    admins = await get_admins()
    if uid not in admins:
        logger.warning(f"Unauthorized /admin command from user ID: {uid}")
        return await event.reply("❌ You are not authorized to access the admin panel.")
    
    await event.reply(await get_admin_dashboard(), buttons=await build_admin_keyboard(), parse_mode='md', link_preview=False)

async def get_admin_dashboard():
    """Generates the dashboard text for the admin panel."""
    bot_status = await get_bot_setting("bot_status")
    auto_buy_status = await get_bot_setting("auto_buy_enabled")
    buy_amount = await get_bot_setting("buy_amount_sol")
    slippage = await get_bot_setting("slippage_tolerance")
    auto_sell_status = await get_bot_setting("auto_sell_enabled")
    profit_target = await get_bot_setting("profit_target_x")
    stop_loss = await get_bot_setting("stop_loss_percent")

    dashboard_text = (
        "⚙️ *Admin Panel*\n\n"
        f"🤖 Bot Status: *{bot_status.upper()}*\n"
        f"💰 Auto-Buy: *{auto_buy_status.upper()}*\n"
        f"  - Buy Amount: `{buy_amount} SOL`\n"
        f"  - Slippage Tolerance: `{slippage}%`\n"
        f"📈 Auto-Sell: *{auto_sell_status.upper()}*\n"
        f"  - Profit Target: `{profit_target}x`\n"
        f"  - Stop-Loss: `{stop_loss}%`\n"
    )
    return dashboard_text

async def get_wallet_settings_dashboard():
    """Generates the dashboard text for wallet settings panel."""
    wallet_pubkey = "N/A"
    wallet_balance = "N/A"
    if payer_keypair:
        wallet_pubkey = str(payer_keypair.pubkey())
        balance = await check_wallet_balance() # Use the new check_wallet_balance function
        if balance is not None:
            wallet_balance = f"{balance:.4f} SOL"
        else:
            wallet_balance = "Balance could not be retrieved (Error)"

    dashboard_text = (
        "💳 *Wallet Settings*\n\n"
        f"Active Wallet Public Key: `{wallet_pubkey}`\n"
        f"Balance: `{wallet_balance}`\n\n"
        "⚠️ *Be very careful when entering your private key! This key grants the bot full access to your wallet. "
        "Your funds may be at risk with incorrect or malicious use.*"
    )
    return dashboard_text

async def build_admin_keyboard():
    """Builds the main keyboard for the admin panel."""
    bot_status = await get_bot_setting("bot_status")
    
    keyboard = [
        [Button.inline("👤 Admins", b"admin_admins"), Button.inline("💳 Wallet Settings", b"admin_wallet_settings")],
        [Button.inline("📈 Auto-Buy/Sell Settings", b"admin_auto_trade_settings")],
        [Button.inline("📜 Transaction History", b"admin_transaction_history")]
    ]
    
    if bot_status == "running":
        keyboard.append([Button.inline("⏸ Pause Bot", b"admin_pause"), Button.inline("🛑 Stop Bot", b"admin_stop")])
    else:
        keyboard.append([Button.inline("▶ Start Bot", b"admin_start")])
    
    return keyboard

async def build_auto_trade_keyboard():
    """Builds the auto-buy/sell settings keyboard."""
    auto_buy_status = await get_bot_setting("auto_buy_enabled")
    auto_sell_status = await get_bot_setting("auto_sell_enabled")
    
    keyboard = []
    if auto_buy_status == "enabled":
        keyboard.append([Button.inline("❌ Disable Auto-Buy", b"admin_disable_auto_buy")])
    else:
        keyboard.append([Button.inline("✅ Enable Auto-Buy", b"admin_enable_auto_buy")])
    
    keyboard.append([
        Button.inline("💲 Set Buy Amount", b"admin_set_buy_amount"),
        Button.inline("⚙️ Set Slippage Tolerance", b"admin_set_slippage")
    ])

    if auto_sell_status == "enabled":
        keyboard.append([Button.inline("❌ Disable Auto-Sell", b"admin_disable_auto_sell")])
    else:
        keyboard.append([Button.inline("✅ Enable Auto-Sell", b"admin_enable_auto_sell")])
    
    keyboard.append([
        Button.inline("📈 Set Profit Target", b"admin_set_profit_target"),
        Button.inline("📉 Set Stop-Loss", b"admin_set_stop_loss")
    ])

    keyboard.append([Button.inline("🔙 Back", b"admin_home")])
    
    return keyboard

async def build_wallet_settings_keyboard():
    """Builds the wallet settings keyboard."""
    keyboard = [
        [Button.inline("🔑 Set New Private Key", b"admin_set_wallet_private_key")],
        [Button.inline("🔙 Back", b"admin_home")]
    ]
    return keyboard

@bot_client.on(events.NewMessage)
async def handle_admin_input(event):
    """Handles text inputs from admins (for changing settings)."""
    uid = event.sender_id
    admins = await get_admins()
    if uid not in admins:
        return

    text_input = event.message.text
    if text_input and text_input.startswith('/'):
        if uid in pending_input:
            del pending_input[uid]
        return

    if uid in pending_input:
        action_data = pending_input[uid]
        action = action_data['action']
        
        if action == 'pause':
            try:
                minutes = int(text_input)
                await set_bot_setting("bot_status", f"paused:{time.time() + minutes*60}")
                await event.reply(f"✅ Bot paused for {minutes} minutes.")
                logger.info(f"Bot paused by admin {uid} for {minutes} minutes.")
            except ValueError:
                await event.reply("❌ Invalid input. Please enter a number for minutes.")
            finally:
                del pending_input[uid]
            return await event.reply(await get_admin_dashboard(),
                                     buttons=await build_admin_keyboard(), parse_mode='md', link_preview=False)
        
        elif action == 'confirm_add_admin':
            try:
                new_admin_id = int(text_input)
                await add_admin(new_admin_id, f"User_{new_admin_id}", "")
                await event.reply(f"✅ Admin {new_admin_id} added.")
                logger.info(f"Admin {uid} added new admin {new_admin_id}.")
            except ValueError:
                await event.reply("❌ Invalid user ID. Please enter a numerical user ID.")
            except Exception as e:
                await event.reply(f"❌ Error adding admin: {e}")
                logger.error(f"Error adding admin {text_input}: {e}")
            finally:
                del pending_input[uid]
            return await event.reply(await get_admin_dashboard(),
                                     buttons=await build_admin_keyboard(), parse_mode='md', link_preview=False)
        
        elif action == 'set_buy_amount':
            try:
                amount = float(text_input)
                if amount <= 0:
                    raise ValueError("Amount must be positive.")
                await set_bot_setting("buy_amount_sol", str(amount))
                await event.reply(f"✅ Auto-buy amount set to `{amount} SOL`.")
                logger.info(f"Admin {uid} set auto-buy amount to {amount} SOL.")
            except ValueError:
                await event.reply("❌ Invalid amount. Please enter a positive number (e.g., `0.01`, `0.5`).")
            finally:
                del pending_input[uid]
            return await event.reply(await get_admin_dashboard(),
                                     buttons=await build_auto_trade_keyboard(), parse_mode='md', link_preview=False)
        
        elif action == 'set_slippage':
            try:
                slippage = float(text_input)
                if not (0 <= slippage <= 100):
                    raise ValueError("Slippage tolerance must be between 0 and 100.")
                await set_bot_setting("slippage_tolerance", str(slippage))
                await event.reply(f"✅ Slippage tolerance set to `{slippage}%`.")
                logger.info(f"Admin {uid} set slippage tolerance to {slippage}%.")
            finally:
                del pending_input[uid]
            return await event.reply(await get_admin_dashboard(),
                                     buttons=await build_auto_trade_keyboard(), parse_mode='md', link_preview=False)
        
        elif action == 'set_profit_target':
            try:
                target_x = float(text_input)
                if target_x <= 1.0:
                    raise ValueError("Profit target must be greater than 1.0 (e.g., 2.0 for 2x).")
                await set_bot_setting("profit_target_x", str(target_x))
                await event.reply(f"✅ Profit target set to `{target_x}x`.")
                logger.info(f"Admin {uid} set profit target to {target_x}x.")
            finally:
                del pending_input[uid]
            return await event.reply(await get_admin_dashboard(),
                                     buttons=await build_auto_trade_keyboard(), parse_mode='md', link_preview=False)
        
        elif action == 'set_stop_loss':
            try:
                stop_loss = float(text_input)
                if not (0 <= stop_loss < 100):
                    raise ValueError("Stop-loss must be between 0 and 100 (exclusive of 100).")
                await set_bot_setting("stop_loss_percent", str(stop_loss))
                await event.reply(f"✅ Stop-loss set to `{stop_loss}%`.")
                logger.info(f"Admin {uid} set stop-loss to {stop_loss}%.")
            finally:
                del pending_input[uid]
            return await event.reply(await get_admin_dashboard(),
                                     buttons=await build_auto_trade_keyboard(), parse_mode='md', link_preview=False)
        
        elif action == 'set_wallet_private_key':
            try:
                new_private_key = text_input.strip()
                if not new_private_key:
                    raise ValueError("Private key cannot be empty.")
                
                await set_bot_setting("SOLANA_PRIVATE_KEY", new_private_key)
                
                # Re-initialize Solana client and keypair with the new key
                await init_solana_client()

                test_keypair = None
                try:
                    test_keypair = Keypair.from_base58_string(new_private_key)
                except Exception as e:
                    await event.reply(f"❌ Entered private key is in invalid format: {e}")
                    logger.error(f"Invalid private key format entered by admin {uid}: {e}")
                    return await event.reply(await get_wallet_settings_dashboard(),
                                             buttons=await build_wallet_settings_keyboard(), parse_mode='md', link_preview=False)

                if payer_keypair and str(payer_keypair.pubkey()) == str(test_keypair.pubkey()):
                     await event.reply(f"✅ New private key successfully set. New Public Key: `{payer_keypair.pubkey()}`")
                     logger.info(f"Admin {uid} set new Solana private key.")
                else:
                    await event.reply("❌ A problem occurred while setting the private key or the key is invalid.")
                    logger.error(f"Failed to set new private key for admin {uid}.")

            except ValueError as ve:
                await event.reply(f"❌ Invalid private key format: {ve}")
            except Exception as e:
                await event.reply(f"❌ Error setting private key: {e}")
                logger.error(f"Error setting new private key for admin {uid}: {e}")
            finally:
                del pending_input[uid]
            return await event.reply(await get_wallet_settings_dashboard(),
                                     buttons=await build_wallet_settings_keyboard(), parse_mode='md', link_preview=False)

# --- Telegram Message Handler (Signal Channel) ---
@bot_client.on(events.NewMessage(chats=SOURCE_CHANNEL_ID))
async def handle_incoming_signal(event):
    """Handles new messages from the designated source channel."""
    message_text = event.message.text
    if not message_text:
        logger.debug("Empty message text received. Skipping.")
        return

    logger.info(f"Message received from source channel {event.chat_id}: {message_text[:100]}...")

    bot_status = await get_bot_setting("bot_status")
    if bot_status == "stopped":
        logger.info("Bot is stopped. Skipping message processing.")
        return
    if bot_status.startswith("paused"):
        pause_until_timestamp = float(bot_status.split(":")[1])
        if time.time() < pause_until_timestamp:
            logger.info("Bot is paused. Skipping message processing.")
            return
        else:
            await set_bot_setting("bot_status", "running")
            logger.info("Bot pause ended. Operations resuming.")

    contract_address = extract_contract(message_text)
    token_name = extract_token_name_from_message(message_text)

    if contract_address:
        logger.info(f"Contract address found: {contract_address}. Initiating auto-buy.")
        
        auto_buy_enabled = await get_bot_setting("auto_buy_enabled")
        if auto_buy_enabled == "enabled":
            buy_amount_sol_str = await get_bot_setting("buy_amount_sol")
            slippage_tolerance_str = await get_bot_setting("slippage_tolerance")
            profit_target_x_str = await get_bot_setting("profit_target_x")
            stop_loss_percent_str = await get_bot_setting("stop_loss_percent")
            
            try:
                buy_amount_sol = float(buy_amount_sol_str)
                slippage_tolerance_percent = float(slippage_tolerance_str)
                profit_target_x = float(profit_target_x_str)
                stop_loss_percent = float(stop_loss_percent_str)
            except ValueError:
                logger.error("Auto-buy/sell settings are invalid. Please check.")
                await bot_client.send_message(DEFAULT_ADMIN_ID, "❌ Auto-buy/sell settings are invalid. Please check.", parse_mode='md')
                return

            success, result_message, actual_buy_price_sol, bought_amount_token = await auto_buy_token(
                contract_address, token_name, buy_amount_sol, slippage_tolerance_percent
            )
            
            admin_message = f"💰 Auto-Buy Status: {result_message}"
            await bot_client.send_message(DEFAULT_ADMIN_ID, admin_message, parse_mode='md')
            
            if success:
                await add_open_position(
                    contract_address, 
                    token_name, 
                    actual_buy_price_sol, 
                    bought_amount_token, 
                    result_message.split("Tx: ")[1] if "Tx: " in result_message else "N/A",
                    profit_target_x, 
                    stop_loss_percent
                )
                logger.info(f"Open position recorded for token {token_name}.")
            else:
                logger.warning(f"Auto-buy failed for {contract_address}: {result_message}")
        else:
            logger.info(f"Auto-buy is disabled. Not attempting to buy for {contract_address}.")
    else:
        logger.debug(f"No contract address found in message from {event.chat_id}.")

# --- Bot Startup ---

async def main():
    """Starts the bot, initializes the database and Solana client."""
    await init_db()
    
    admins = await get_admins()
    if not admins:
        await add_admin(DEFAULT_ADMIN_ID, "Default", "Admin", is_default=True)
        logger.info(f"Default admin {DEFAULT_ADMIN_ID} added.")
    
    for setting_key, default_value in DEFAULT_BOT_SETTINGS.items():
        current_value = await get_bot_setting(setting_key)
        if current_value is None:
            await set_bot_setting(setting_key, default_value)
            logger.info(f"Default setting {setting_key} -> {default_value} set.")
        else:
            logger.info(f"Setting {setting_key} already exists: {current_value}")

    await init_solana_client()

    logger.info("Connecting Telegram client...")
    await bot_client.start(bot_token=BOT_TOKEN)
    logger.info("Telegram client connected.")

    me_bot = await bot_client.get_me()
    logger.info(f"Auto-Buy/Sell Bot: @{me_bot.username} ({me_bot.id})")
    logger.info(f"Auto-Buy/Sell Bot is currently listening to channel ID: {SOURCE_CHANNEL_ID}")
    logger.info(f"Auto-Buy Amount: {await get_bot_setting('buy_amount_sol')} SOL")
    logger.info(f"Slippage Tolerance: {await get_bot_setting('slippage_tolerance')}%")
    logger.info(f"Profit Target: {await get_bot_setting('profit_target_x')}x")
    logger.info(f"Stop-Loss: {await get_bot_setting('stop_loss_percent')}%")

    # Mask private key in logs after init
    private_key_setting = await get_bot_setting('SOLANA_PRIVATE_KEY')
    if private_key_setting:
        logger.info(f"SOLANA_PRIVATE_KEY (masked): {'*' * (len(private_key_setting) - 4)}{private_key_setting[-4:]}") # Log only last 4 chars
    else:
        logger.info("SOLANA_PRIVATE_KEY: Not set")


    asyncio.create_task(monitor_positions_task())
    logger.info("Position monitoring task started.")

    def run_flask():
        app.run(host='0.0.0.0', port=os.environ.get('PORT', 5000))

    flask_thread = threading.Thread(target=run_flask)
    flask_thread.start()
    logger.info("Flask web server started.")

    logger.info("Bot is running. Press Ctrl+C to stop.")
    await bot_client.run_until_disconnected()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    except Exception as e:
        logger.critical(f"An unexpected error occurred: {e}", exc_info=True)

