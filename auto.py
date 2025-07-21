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
from cachetools import TTLCache # Cache için eklendi

# Solana Kütüphaneleri
from solana.rpc.api import Client, RPCException
from solana.rpc.types import TxOpts
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solders.message import MessageV0
from solders.instruction import Instruction
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.rpc.responses import GetBalanceResp, GetBlockHeightResp
from solders.hash import Hash
from solders.signature import Signature # Sadece simülasyon veya hata ayıklama için kullanılabilir
from solders.system_program import transfer
from solders.transaction_status import TransactionConfirmationStatus
from solders.transaction import MessageHeader, CompiledInstruction, Message
from solders.address_lookup_table_account import AddressLookupTableAccount

# --- Ortam Değişkenleri ---
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
JUPITER_API_URL = os.environ.get("JUPITER_API_URL", "https://quote-api.jup.ag/v6")

SECRET_KEY = os.environ.get("SECRET_KEY", os.urandom(24).hex())
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "adminpassword") # Web paneli için şifre

app = Flask(__name__)
app.secret_key = SECRET_KEY

# --- Telethon Clients ---
bot_client = TelegramClient('auto_buy_bot_session', API_ID, API_HASH)

# --- Solana Client and Wallet Initialization ---
solana_client = None
payer_keypair = None

# List of RPC endpoints to try (updated with Helius RPC)
RPC_ENDPOINTS = [
    "https://mainnet.helius-rpc.com/?api-key=7930dbab-e806-4f3f-bf3b-716a14c6e3c3", # Helius Mainnet RPC
    "https://api.mainnet-beta.solana.com", # General RPC as fallback
    "https://solana-rpc.web3auth.io",
    "https://ssc-dao.genesysgo.net",
    "https://rpc.ankr.com/solana",
    "https://solana-mainnet.rpc.extrnode.com"
]

# Global variable to hold the active RPC URL
active_rpc_url = None

# Global lock and rate limit for Jupiter API calls
jupiter_api_lock = asyncio.Lock()
last_jupiter_call_time = 0
JUPITER_RATE_LIMIT_DELAY = 2.0 # Minimum delay between each Jupiter API call (increased)

# Semaphore for Jupiter API calls (to limit concurrent requests)
JUPITER_SEMAPHORE = asyncio.Semaphore(3) # Maximum 3 concurrent Jupiter API requests

# Cache for Jupiter quotes (with 5-minute TTL, store 100 tokens)
QUOTE_CACHE = TTLCache(maxsize=100, ttl=300)

async def get_healthy_client():
    """
    Attempts to connect to a healthy Solana RPC endpoint from a predefined list.
    Returns a Client object on success, None otherwise.
    """
    global active_rpc_url
    for url in RPC_ENDPOINTS:
        try:
            logger.info(f"RPC URL test ediliyor: {url}")
            client = Client(url)
            
            # Use get_block_height() for a health check
            block_height_resp = await asyncio.to_thread(client.get_block_height)
            
            # Check the GetBlockHeightResp object and its value
            if isinstance(block_height_resp, GetBlockHeightResp) and block_height_resp.value is not None and block_height_resp.value > 0:
                logger.info(f"Sağlıklı RPC'ye bağlandı: {url}. Blok yüksekliği: {block_height_resp.value}")
                active_rpc_url = url # Save the active URL
                return client
            else:
                logger.warning(f"RPC {url} sağlıksız görünüyor veya geçersiz blok yüksekliği döndürdü: {block_height_resp}")
        except Exception as e:
            logger.warning(f"RPC {url} bağlantısı başarısız: {str(e)}")
    logger.error("Tüm RPC uç noktaları başarısız oldu.")
    active_rpc_url = None # No healthy RPC found
    return None

async def get_balance_with_retry(pubkey: Pubkey, retries=3):
    """
    Retrieves Solana balance with a retry mechanism.
    Handles GetBalanceResp object.
    """
    for i in range(retries):
        try:
            # commitment="confirmed" added
            resp = await asyncio.to_thread(solana_client.get_balance, pubkey, commitment="confirmed")
            
            # Return the value directly from the GetBalanceResp object
            if isinstance(resp, GetBalanceResp):
                return resp.value
            else:
                logger.warning(f"get_balance için beklenmeyen yanıt türü: {type(resp)}. Tam yanıt: {resp}. Deneme {i+1}/{retries}")
        except Exception as e:
            logger.warning(f"Bakiye kontrol denemesi {i+1}/{retries} başarısız oldu: {e}")
            await asyncio.sleep(1) # Short delay before retrying
    return None

async def check_wallet_balance():
    """
    Checks the wallet's SOL balance and returns it in SOL.
    Returns None on error.
    """
    if not solana_client or not payer_keypair:
        logger.error("Solana istemcisi veya ödeme anahtarı başlatılmadı. Bakiye kontrol edilemiyor.")
        return None
    
    try:
        balance_lamports = await get_balance_with_retry(payer_keypair.pubkey())
        
        if balance_lamports is None:
            logger.error("Cüzdan bakiyesi alınamadı.")
            return None
            
        return balance_lamports / 10**9  # In SOL
    except Exception as e:
        logger.error(f"Bakiye kontrol hatası: {str(e)}", exc_info=True)
        return None

async def init_solana_client():
    """Initializes the Solana RPC client and wallet."""
    global solana_client, payer_keypair

    # Reset them at the start to ensure clean state for re-initialization
    solana_client = None
    payer_keypair = None

    try:
        client_temp = await get_healthy_client()
        if not client_temp:
            logger.critical("Sağlıklı RPC bulunamadı! Solana istemcisi başlatılamadı.")
            return # solana_client and payer_keypair remain None

        priv_key = await get_bot_setting("SOLANA_PRIVATE_KEY")
        if not priv_key:
            logger.error("Özel anahtar ayarlanmamış! Otomatik alım-satım işlevleri devre dışı bırakılacak.")
            return # payer_keypair remains None

        try:
            keypair_temp = Keypair.from_base58_string(priv_key)
            solana_client = client_temp # Only assign if keypair is successfully created
            payer_keypair = keypair_temp # Only assign if keypair is successfully created

            logger.info(f"Cüzdan başlatıldı: {payer_keypair.pubkey()}")
            logger.info(f"Aktif RPC URL'si: {active_rpc_url if active_rpc_url else 'Bilinmiyor'}")

            # Balance check (this can fail without invalidating the client/keypair)
            balance = await check_wallet_balance()
            logger.info(f"Başlangıç bakiyesi: {balance if balance is not None else 'Alınamadı'} SOL")

        except Exception as e:
            logger.error(f"Özel anahtardan ödeme anahtar çifti başlatılırken hata: {e}", exc_info=True)
            # If keypair creation fails, ensure they remain None
            solana_client = None
            payer_keypair = None
            return # Exit if keypair initialization fails

    except Exception as e:
        # This outer catch is for errors during get_healthy_client or get_bot_setting
        logger.error(f"Solana istemcisi başlatma hatası: {str(e)}", exc_info=True)
        # solana_client and payer_keypair are already None from the start or from inner except

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
        logger.error(f"Veritabanı bağlantısı başarısız oldu: {e}")
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
        logger.info("Veritabanı başlatıldı veya zaten mevcut.")
    except Exception as e:
        logger.error(f"Veritabanı başlatma sırasında hata: {e}")
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
        logger.error(f"Yöneticiler alınırken hata: {e}")
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
        logger.info(f"Yönetici {user_id} eklendi/güncellendi.")
    except Exception as e:
        logger.error(f"Yönetici {user_id} eklenirken hata: {e}")
    finally:
        if conn:
            conn.close()

def remove_admin_sync(user_id):
    admins = get_admins_sync()
    if admins.get(user_id, {}).get("is_default"):
        logger.warning(f"Varsayılan yönetici {user_id} kaldırılmaya çalışıldı.")
        return
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM admins WHERE user_id = %s", (user_id,))
        conn.commit()
        logger.info(f"Yönetici {user_id} kaldırıldı.")
    except Exception as e:
        logger.error(f"Yönetici {user_id} kaldırılırken hata: {e}")
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
        logger.error(f"Bot ayarı '{setting}' alınırken hata: {e}")
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
            # Mask private key for logging
            if setting == "SOLANA_PRIVATE_KEY" or setting == "JUPITER_API_KEY":
                logger.info(f"Bot ayarı '{setting}' ayarlandı. Değer güvenlik için maskelendi.")
            else:
                logger.info(f"Bot ayarı '{setting}' '{value}' olarak ayarlandı.")
    except Exception as e:
        logger.error(f"Bot ayarı '{setting}' ayarlanırken hata: {e}")
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
        logger.error(f"Sözleşme {contract_address} işlenmiş mi kontrol edilirken hata: {e}")
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
            logger.info(f"İşlenmiş sözleşme kaydedildi: {contract_address}.")
    except Exception as e:
        logger.error(f"İşlenmiş sözleşme {contract_address} kaydedilirken hata: {e}")
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
        logger.info(f"{token_name} ({contract_address}) için açık pozisyon eklendi/güncellendi.")
    except Exception as e:
        logger.error(f"{contract_address} için açık pozisyon eklenirken/güncellenirken hata: {e}")
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
        logger.error(f"Açık pozisyonlar alınırken hata: {e}")
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
            logger.info(f"{contract_address} için açık pozisyon kaldırıldı.")
        else:
            logger.warning(f"{contract_address} için kaldırılacak açık pozisyon bulunamadı.")
    except Exception as e:
        logger.error(f"{contract_address} için açık pozisyon kaldırılırken hata: {e}")
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
            logger.info(f"İşlem kaydedildi: {tx_type} {token_name} için ({tx_signature}).")
    except Exception as e:
        logger.error(f"{tx_signature} için işlem geçmişi kaydedilirken hata: {e}")
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
        logger.error(f"İşlem geçmişi alınırken hata: {e}")
        return []
    finally:
        if conn:
            conn.close()

# --- Asynchronous Database Function Wrappers ---
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
    # If setting is not in DB and is set as environment variable, use it
    if val is None:
        if setting == "SOLANA_PRIVATE_KEY":
            return os.environ.get("SOLANA_PRIVATE_KEY")
        elif setting == "JUPITER_API_KEY":
            return os.environ.get("JUPITER_API_KEY")
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
    "slippage_tolerance": "5", # 5 = 0.5% (Jupiter uses BPS, 1 BPS = 0.01%)
    "auto_sell_enabled": "enabled",
    "profit_target_x": "5.0",
    "stop_loss_percent": "50.0",
    "SOLANA_PRIVATE_KEY": os.environ.get("SOLANA_PRIVATE_KEY", ""),
    "JUPITER_API_KEY": os.environ.get("JUPITER_API_KEY", ""), # New Jupiter API Key setting
    "DEFAULT_PRIORITY_FEE_LAMPORTS": "100000" # Default priority fee in micro-lamports (0.0001 SOL)
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
logger.info("🔥 Otomatik Alım-Satım Botu Loglama kurulumu tamamlandı. Bot başlatılıyor...")

# --- Telethon Helper Functions ---
async def retry_telethon_call(coro, max_retries=5, base_delay=1.0):
    """Retry mechanism for Telethon calls."""
    for i in range(max_retries):
        try:
            return await coro
        except Exception as e:
            logger.warning(f"Telethon çağrısı için yeniden deneme {i+1}/{max_retries} hatası nedeniyle: {e}")
            if i < max_retries - 1:
                delay = base_delay * (2 ** i) + random.uniform(0, 1)
                await asyncio.sleep(delay)
            else:
                logger.error(f"Telethon çağrısı için maksimum yeniden deneme sayısına ulaşıldı: {e}")
                raise
    raise RuntimeError("Yeniden deneme mantığı başarısız oldu veya max_retries 0 idi")

def extract_contract(text: str) -> str | None:
    """Extracts Solana contract address (Base58, 32-44 characters) from text."""
    m = re.findall(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b", text)
    return m[0] if m else None

def extract_token_name_from_message(text: str) -> str:
    """Extracts token name (in $TOKEN_NAME format) from message."""
    lines = text.strip().splitlines()
    if not lines:
        logger.debug("Token çıkarımı için boş mesaj alındı; 'unknown' döndürülüyor.")
        return "unknown"
    for line in lines:
        match = re.search(r"\$([A-Za-z0-9_]+)", line)
        if match:
            token = match.group(1)
            logger.debug(f"Çıkarılan token: '{token}' satırdan: '{line}'")
            return token
    logger.debug("Mesajda geçerli bir token ($WORD) bulunamadı; 'unknown' döndürülüyor.")
    return "unknown"

# --- Solana Auto Buy/Sell Functions ---
async def get_current_token_price_sol(token_mint_str: str, amount_token_to_check: float = 0.000000001, max_retries=7, initial_delay=3.0):
    """Estimates the current SOL price of a specific token, with retries."""
    global last_jupiter_call_time

    if not solana_client:
        logger.error("Solana istemcisi başlatılmadı. Token fiyatı alınamıyor.")
        return None

    async with JUPITER_SEMAPHORE: # Use Semaphore
        async with jupiter_api_lock:
            # Apply rate limit delay
            elapsed_time = time.time() - last_jupiter_call_time
            if elapsed_time < JUPITER_RATE_LIMIT_DELAY:
                await asyncio.sleep(JUPITER_RATE_LIMIT_DELAY - elapsed_time)

            headers = {}
            jupiter_api_key = await get_bot_setting("JUPITER_API_KEY")
            if jupiter_api_key:
                headers["Authorization"] = f"Bearer {jupiter_api_key}"

            for attempt in range(max_retries):
                try:
                    token_mint = Pubkey.from_string(token_mint_str)
                    input_mint = token_mint
                    output_mint = Pubkey.from_string("So11111111111111111111111111111111111111112")

                    # Get token decimals
                    token_info = await asyncio.to_thread(solana_client.get_token_supply, input_mint)
                    if not token_info or not hasattr(token_info, 'value') or not hasattr(token_info.value, 'decimals'):
                        logger.warning(f"{token_mint_str} için token arz bilgisi alınamadı. Ondalık basamaklar belirlenemiyor.")
                        return None
                    decimals = token_info.value.decimals
                    
                    amount_in_lamports = int(amount_token_to_check * (10**decimals))

                    quote_url = f"{JUPITER_API_URL}/quote?inputMint={input_mint}&outputMint={output_mint}&amount={amount_in_lamports}&slippageBps=0"
                    
                    response = requests.get(quote_url, headers=headers)
                    response.raise_for_status()
                    quote_data = response.json()

                    if not quote_data or "outAmount" not in quote_data or "inAmount" not in quote_data:
                        logger.warning(f"Fiyat kontrolü için geçersiz teklif verisi: {quote_data}. Deneme {attempt+1}/{max_retries}")
                        if attempt < max_retries - 1:
                            await asyncio.sleep(initial_delay * (2 ** attempt) + random.uniform(0, 1))
                        continue
                    
                    price_sol_per_token = (float(quote_data['outAmount']) / (10**9)) / (float(quote_data['inAmount']) / (10**decimals))
                    logger.debug(f"{token_mint_str} için mevcut fiyat: {price_sol_per_token} SOL/token")
                    last_jupiter_call_time = time.time() # Update successful call time
                    return price_sol_per_token

                except requests.exceptions.RequestException as e:
                    logger.warning(f"Jupiter'den token fiyatı alınırken hata (deneme {attempt+1}/{max_retries}): {e}")
                    if e.response is not None and e.response.status_code == 429:
                        # On 429 error, wait longer and notify admin
                        delay = initial_delay * (2 ** attempt) + random.uniform(0, 1)
                        logger.info(f"429 hatası, {delay:.2f} saniye beklenecek.")
                        await bot_client.send_message(
                            DEFAULT_ADMIN_ID,
                            "⚠️ **Jupiter API Rate Limit Aşıldı!**\n"
                            f"Son hata: `{e}`\n"
                            f"Bot {delay:.2f} saniye bekleyecek.",
                            parse_mode='md'
                        )
                        await asyncio.sleep(delay)
                    elif attempt < max_retries - 1:
                        # Default delay for other request errors
                        await asyncio.sleep(initial_delay * (2 ** attempt) + random.uniform(0, 1))
                    else:
                        logger.error(f"Jupiter'den token fiyatı alınırken maksimum yeniden deneme sayısına ulaşıldı: {e}")
                        return None
                except Exception as e:
                    logger.error(f"get_current_token_price_sol içinde beklenmeyen hata: {e}", exc_info=True)
                    return None
            last_jupiter_call_time = time.time() # Update time when retry loop finishes (even if failed)
    return None


async def get_swap_quote(input_mint: Pubkey, output_mint: Pubkey, amount_in_lamports: int, slippage_bps: int, max_retries=7, initial_delay=3.0):
    """Retrieves a swap quote from Jupiter Aggregator, with retries."""
    global last_jupiter_call_time
    if not solana_client or not payer_keypair:
        logger.error("Solana istemcisi veya ödeme anahtarı başlatılmadı. Teklif alınamıyor.")
        return None

    cache_key = f"quote_{input_mint}_{output_mint}_{amount_in_lamports}_{slippage_bps}"
    if cache_key in QUOTE_CACHE:
        logger.debug(f"Cache'ten teklif alındı: {cache_key}")
        return QUOTE_CACHE[cache_key]

    async with JUPITER_SEMAPHORE: # Use Semaphore
        async with jupiter_api_lock:
            # Apply rate limit delay
            elapsed_time = time.time() - last_jupiter_call_time
            if elapsed_time < JUPITER_RATE_LIMIT_DELAY:
                await asyncio.sleep(JUPITER_RATE_LIMIT_DELAY - elapsed_time)

            headers = {}
            jupiter_api_key = await get_bot_setting("JUPITER_API_KEY")
            if jupiter_api_key:
                headers["Authorization"] = f"Bearer {jupiter_api_key}"

            for attempt in range(max_retries):
                try:
                    quote_url = f"{JUPITER_API_URL}/quote?inputMint={input_mint}&outputMint={output_mint}&amount={amount_in_lamports}&slippageBps={slippage_bps}"
                    response = requests.get(quote_url, headers=headers)
                    response.raise_for_status()
                    quote_data = response.json()
                    
                    if not quote_data or "swapMode" not in quote_data:
                        logger.error(f"Geçersiz teklif verisi alındı: {quote_data}. Deneme {attempt+1}/{max_retries}")
                        if attempt < max_retries - 1:
                            await asyncio.sleep(initial_delay * (2 ** attempt) + random.uniform(0, 1))
                        continue

                    logger.info(f"Jupiter teklifi {input_mint}'ten {output_mint}'e alındı: {quote_data.get('outAmount')} {quote_data.get('outputToken', {}).get('symbol')}")
                    last_jupiter_call_time = time.time() # Update successful call time
                    QUOTE_CACHE[cache_key] = quote_data # Add to cache
                    return quote_data

                except requests.exceptions.RequestException as e:
                    logger.warning(f"Jupiter teklifi alınırken hata (deneme {attempt+1}/{max_retries}): {e}")
                    if e.response is not None and e.response.status_code == 429:
                        # On 429 error, wait longer and notify admin
                        delay = initial_delay * (2 ** attempt) + random.uniform(0, 1)
                        logger.info(f"429 hatası, {delay:.2f} saniye beklenecek.")
                        await bot_client.send_message(
                            DEFAULT_ADMIN_ID,
                            "⚠️ **Jupiter API Rate Limit Aşıldı!**\n"
                            f"Son hata: `{e}`\n"
                            f"Bot {delay:.2f} saniye bekleyecek.",
                            parse_mode='md'
                        )
                        await asyncio.sleep(delay)
                    elif attempt < max_retries - 1:
                        # Default delay for other request errors
                        await asyncio.sleep(initial_delay * (2 ** attempt) + random.uniform(0, 1))
                    else:
                        logger.error(f"Jupiter teklifi alınırken maksimum yeniden deneme sayısına ulaşıldı: {e}")
                        return None
                except Exception as e:
                    logger.error(f"get_swap_quote içinde beklenmeyen hata: {e}", exc_info=True)
                    return None
            last_jupiter_call_time = time.time() # Update time when retry loop finishes (even if failed)
    return None

async def get_swap_transaction(quote_response: dict, payer_pubkey: Pubkey, max_retries=7, initial_delay=3.0):
    """Retrieves the swap transaction from Jupiter Aggregator, with retries."""
    global last_jupiter_call_time
    if not solana_client or not payer_keypair:
        logger.error("Solana istemcisi veya ödeme anahtarı başlatılmadı. Takas işlemi alınamıyor.")
        return None

    async with JUPITER_SEMAPHORE: # Use Semaphore
        async with jupiter_api_lock:
            # Apply rate limit delay
            elapsed_time = time.time() - last_jupiter_call_time
            if elapsed_time < JUPITER_RATE_LIMIT_DELAY:
                await asyncio.sleep(JUPITER_RATE_LIMIT_DELAY - elapsed_time)

            headers = {"Content-Type": "application/json"}
            jupiter_api_key = await get_bot_setting("JUPITER_API_KEY")
            if jupiter_api_key:
                headers["Authorization"] = f"Bearer {jupiter_api_key}"

            for attempt in range(max_retries):
                try:
                    swap_url = f"{JUPITER_API_URL}/swap"
                    payload = {
                        "quoteResponse": quote_response,
                        "userPublicKey": str(payer_pubkey),
                        "wrapUnwrapSOL": True,
                        "dynamicComputeUnitLimit": True,
                        "prioritizationFeeLamports": "auto" # Auto prioritization fee
                    }
                    response = requests.post(swap_url, headers=headers, json=payload)
                    response.raise_for_status()
                    swap_data = response.json()

                    if not swap_data or "swapTransaction" not in swap_data:
                        logger.error(f"Geçersiz takas işlemi verisi alındı: {swap_data}. Deneme {attempt+1}/{max_retries}")
                        if attempt < max_retries - 1:
                            await asyncio.sleep(initial_delay * (2 ** attempt) + random.uniform(0, 1))
                        continue

                    logger.info("Jupiter'den takas işlemi başarıyla alındı.")
                    last_jupiter_call_time = time.time() # Update successful call time
                    return swap_data

                except requests.exceptions.RequestException as e:
                    logger.warning(f"Jupiter takas işlemi alınırken hata (deneme {attempt+1}/{max_retries}): {e}")
                    if e.response is not None and e.response.status_code == 429:
                        delay = initial_delay * (2 ** attempt) + random.uniform(0, 1)
                        logger.info(f"429 hatası, {delay:.2f} saniye beklenecek.")
                        await bot_client.send_message(
                            DEFAULT_ADMIN_ID,
                            "⚠️ **Jupiter API Rate Limit Aşıldı!**\n"
                            f"Son hata: `{e}`\n"
                            f"Bot {delay:.2f} saniye bekleyecek.",
                            parse_mode='md'
                        )
                        await asyncio.sleep(delay)
                    elif attempt < max_retries - 1:
                        await asyncio.sleep(initial_delay * (2 ** attempt) + random.uniform(0, 1))
                    else:
                        logger.error(f"Jupiter takas işlemi alınırken maksimum yeniden deneme sayısına ulaşıldı: {e}")
                        return None
                except Exception as e:
                    logger.error(f"get_swap_transaction içinde beklenmeyen hata: {e}", exc_info=True)
                    return None
            last_jupiter_call_time = time.time() # Update time when retry loop finishes (even if failed)
    return None

async def send_and_confirm_transaction(transaction_base64: str, priority_fee_lamports: int, max_retries=5, initial_delay=5.0):
    """
    Sends a serialized transaction to the Solana network and waits for confirmation.
    Includes adding compute budget instructions for priority fees.
    """
    if not solana_client or not payer_keypair:
        logger.error("Solana istemcisi veya ödeme anahtarı başlatılmadı. İşlem gönderilemiyor.")
        return None, "Solana istemcisi başlatılmadı."

    try:
        # Decode the base64 string to bytes
        raw_transaction_bytes = base64.b64decode(transaction_base64)
        
        # Deserialize the transaction. Jupiter usually returns a VersionedTransaction (V0)
        # We need to parse it and potentially add our own instructions.
        # Note: solders.transaction.VersionedTransaction.from_bytes() expects the full transaction,
        # including signatures. Jupiter's swapTransaction is often a partially signed transaction
        # or a message that needs to be wrapped.
        
        # More robust way to handle Jupiter's transaction:
        # It's often a base64 encoded `VersionedTransaction` which is already partially signed by Jupiter.
        # We need to deserialize it, add our compute budget instructions, and then sign it with our payer_keypair.
        
        # First, try to load as a VersionedTransaction
        try:
            transaction = VersionedTransaction.from_bytes(raw_transaction_bytes)
        except Exception as e:
            # If it fails, it might be just the message, or an older format.
            # For simplicity, we assume it's a V0 message that needs wrapping or a full V0 tx.
            # If Jupiter provides a direct message, you'd do:
            # message = MessageV0.from_bytes(raw_transaction_bytes)
            # transaction = VersionedTransaction(message, []) # Signatures will be added later
            logger.error(f"Jupiter'dan alınan işlemi ayrıştırırken hata: {e}. Ham işlem: {raw_transaction_bytes[:100]}...")
            return None, f"İşlem ayrıştırma hatası: {e}"

        # Add Compute Budget instructions for priority fees
        # These instructions must be added before signing.
        compute_unit_limit_instruction = set_compute_unit_limit(1_000_000) # Example limit, adjust as needed
        compute_unit_price_instruction = set_compute_unit_price(priority_fee_lamports)
        
        # Create a new message with added instructions.
        # This is a common pattern when modifying transactions from external sources.
        # You might need to re-compile the message if you add instructions.
        # For VersionedTransaction, you typically modify the instructions list directly if it's mutable,
        # or rebuild the message from scratch.
        
        # Simplistic approach: Assuming the transaction's message instructions are mutable or can be replaced.
        # A more correct way for VersionedTransaction might involve rebuilding MessageV0.
        # For now, we'll try to insert them directly.
        # Note: The exact placement of these instructions can matter for transaction efficiency.
        # Usually, they are placed at the beginning.
        
        # Rebuilding the message for VersionedTransaction (V0)
        # This is a more robust way to add instructions to a VersionedTransaction
        # Get existing instructions and accounts from the transaction's message
        current_instructions = list(transaction.message.instructions)
        current_static_accounts = list(transaction.message.static_account_keys)
        current_address_table_lookups = transaction.message.address_table_lookups

        # Prepend compute budget instructions
        new_instructions = [
            CompiledInstruction(
                program_id_index=current_static_accounts.index(Pubkey.from_string("ComputeBudget111111111111111111111111111111")),
                accounts=[],
                data=compute_unit_limit_instruction.data
            ),
            CompiledInstruction(
                program_id_index=current_static_accounts.index(Pubkey.from_string("ComputeBudget111111111111111111111111111111")),
                accounts=[],
                data=compute_unit_price_instruction.data
            )
        ] + current_instructions
        
        # Rebuild the message
        new_message = MessageV0(
            header=transaction.message.header,
            static_account_keys=current_static_accounts,
            recent_blockhash=transaction.message.recent_blockhash,
            compiled_instructions=new_instructions,
            address_table_lookups=current_address_table_lookups
        )
        
        # Create a new VersionedTransaction with the modified message
        transaction_to_send = VersionedTransaction(new_message, []) # Signatures will be added by sign()
        
        # Sign the transaction with our payer_keypair
        transaction_to_send.sign([payer_keypair])
        
        # Get the latest blockhash before sending for better reliability
        recent_blockhash_resp = await asyncio.to_thread(solana_client.get_latest_blockhash)
        if not recent_blockhash_resp or not recent_blockhash_resp.value:
            raise Exception("Son blok hash'i alınamadı.")
        
        transaction_to_send.message.recent_blockhash = Hash.from_string(recent_blockhash_resp.value.blockhash)

        signature = None
        for i in range(max_retries):
            try:
                # Send the raw, signed transaction
                tx_signature = await asyncio.to_thread(
                    solana_client.send_raw_transaction,
                    transaction_to_send.serialize(), # Serialize the VersionedTransaction
                    opts=TxOpts(skip_preflight=False) # skip_preflight=False for better error reporting
                )
                signature = tx_signature
                logger.info(f"İşlem gönderildi, imza: {signature}")

                # Wait for confirmation
                confirmation = await asyncio.to_thread(
                    solana_client.confirm_transaction,
                    signature,
                    commitment="confirmed" # Or "finalized" for higher certainty
                )
                
                # Check confirmation status
                if confirmation.value and confirmation.value[0].err:
                    error_detail = confirmation.value[0].err
                    logger.error(f"İşlem hatayla başarısız oldu: {error_detail}")
                    return None, f"İşlem başarısız oldu: {error_detail}"
                else:
                    logger.info(f"İşlem onaylandı: {signature}")
                    return str(signature), "success"
            except RPCException as e:
                logger.warning(f"İşlem gönderme/onaylama denemesi {i+1}/{max_retries} başarısız: {e}")
                if i < max_retries - 1:
                    await asyncio.sleep(initial_delay * (2 ** i) + random.uniform(0, 1))
                else:
                    logger.error(f"İşlem gönderme/onaylama için maksimum yeniden deneme sayısına ulaşıldı: {e}")
                    return None, f"İşlem onaylanamadı: {e}"
            except Exception as e:
                logger.error(f"İşlem gönderme/onaylama sırasında beklenmeyen hata: {e}", exc_info=True)
                return None, f"Bilinmeyen hata: {e}"

    except Exception as e:
        logger.error(f"İşlem işlenirken hata: {e}", exc_info=True)
        return None, f"İşlem işlenirken hata: {e}"

async def auto_buy_token(contract_address: str, token_name: str):
    """
    Performs automated token purchase for the specified contract address.
    """
    if not solana_client or not payer_keypair:
        logger.error("Solana istemcisi veya ödeme anahtarı başlatılmadı. Otomatik alım yapılamıyor.")
        await bot_client.send_message(
            DEFAULT_ADMIN_ID,
            "❌ **Otomatik Alım Başarısız!**\n"
            "Solana istemcisi veya cüzdan başlatılmamış. Lütfen özel anahtarınızı ayarlayın ve botu yeniden başlatın.",
            parse_mode='md'
        )
        return

    if await is_contract_processed(contract_address):
        logger.info(f"Sözleşme {contract_address} zaten işlenmiş, atlanıyor.")
        return

    bot_status = await get_bot_setting("bot_status")
    if bot_status != "running":
        logger.info(f"Bot duraklatıldı/durduruldu. Otomatik alım atlanıyor. Bot durumu: {bot_status}")
        return

    auto_buy_enabled = await get_bot_setting("auto_buy_enabled")
    if auto_buy_enabled != "enabled":
        logger.info("Otomatik alım devre dışı bırakıldı.")
        return

    buy_amount_sol_str = await get_bot_setting("buy_amount_sol")
    slippage_tolerance_str = await get_bot_setting("slippage_tolerance")
    profit_target_x_str = await get_bot_setting("profit_target_x")
    stop_loss_percent_str = await get_bot_setting("stop_loss_percent")
    priority_fee_lamports_str = await get_bot_setting("DEFAULT_PRIORITY_FEE_LAMPORTS")

    try:
        buy_amount_sol = float(buy_amount_sol_str)
        # Jupiter uses BPS (Basis Points), where 1 BPS = 0.01%. So 5% slippage is 500 BPS.
        slippage_bps = int(float(slippage_tolerance_str) * 100) 
        profit_target_x = float(profit_target_x_str)
        stop_loss_percent = float(stop_loss_percent_str)
        priority_fee_lamports = int(priority_fee_lamports_str)
    except ValueError:
        logger.error("Geçersiz bot ayarları (sayısal değerler). Otomatik alım iptal edildi.")
        await bot_client.send_message(
            DEFAULT_ADMIN_ID,
            "❌ **Otomatik Alım Başarısız!**\n"
            "Bot ayarları (alım miktarı, slippage, kar hedefi, stop loss, öncelik ücreti) geçersiz. Lütfen `/settings` ile kontrol edin.",
            parse_mode='md'
        )
        return

    logger.info(f"Yeni token {token_name} ({contract_address}) için otomatik alım başlatılıyor...")
    await bot_client.send_message(
        DEFAULT_ADMIN_ID,
        f"⚡️ **Yeni Token Sinyali Yakalandı!**\n"
        f"Token: **{token_name}**\n"
        f"Adres: `{contract_address}`\n"
        f"Alım işlemi başlatılıyor... ({buy_amount_sol} SOL)",
        parse_mode='md'
    )

    try:
        # Check wallet balance
        current_balance = await check_wallet_balance()
        if current_balance is None or current_balance < buy_amount_sol:
            raise Exception(f"Yetersiz SOL bakiyesi. Gerekli: {buy_amount_sol} SOL, Mevcut: {current_balance if current_balance is not None else 'N/A'} SOL.")

        # Get swap quote from SOL to token
        sol_mint = Pubkey.from_string("So11111111111111111111111111111111111111112")
        token_mint = Pubkey.from_string(contract_address)
        
        # Convert SOL amount to lamports
        amount_in_lamports = int(buy_amount_sol * (10**9))

        quote = await get_swap_quote(sol_mint, token_mint, amount_in_lamports, slippage_bps)

        if not quote:
            raise Exception("Jupiter'den takas teklifi alınamadı.")

        # Get swap transaction from Jupiter
        swap_transaction_data = await get_swap_transaction(quote, payer_keypair.pubkey())

        if not swap_transaction_data or "swapTransaction" not in swap_transaction_data:
            raise Exception("Jupiter'den takas işlemi alınamadı.")

        # Send and confirm the transaction
        tx_signature, tx_status_msg = await send_and_confirm_transaction(
            swap_transaction_data["swapTransaction"],
            priority_fee_lamports
        )

        if tx_status_msg != "success" or not tx_signature:
            raise Exception(f"İşlem gönderilemedi veya onaylanamadı: {tx_status_msg}")

        # Calculate received token amount and actual buy price
        token_info = await asyncio.to_thread(solana_client.get_token_supply, token_mint)
        token_decimals = token_info.value.decimals if token_info and token_info.value else 0
        
        received_token_amount = float(quote['outAmount']) / (10**token_decimals)
        
        # Actual buy price (SOL/token) = (SOL spent in lamports / 10^9) / (token received in lamports / 10^decimals)
        # Using quote's inAmount and outAmount for more accuracy
        actual_buy_price_sol_per_token = (float(quote['inAmount']) / (10**9)) / (float(quote['outAmount']) / (10**token_decimals))


        await add_open_position(
            contract_address,
            token_name,
            actual_buy_price_sol_per_token, # Buy price SOL/token
            received_token_amount, # Amount of token bought
            tx_signature,
            profit_target_x,
            stop_loss_percent
        )
        await record_processed_contract(contract_address)
        await add_transaction_history(
            tx_signature,
            "BUY",
            token_name,
            contract_address,
            buy_amount_sol, # SOL spent
            received_token_amount, # Token received
            actual_buy_price_sol_per_token, # Price SOL/token
            "SUCCESS"
        )

        await bot_client.send_message(
            DEFAULT_ADMIN_ID,
            f"✅ **Alım Başarılı!**\n"
            f"Token: **{token_name}**\n"
            f"Alınan Miktar: `{received_token_amount:.4f} {token_name}`\n"
            f"Harcanan SOL: `{buy_amount_sol:.4f}`\n"
            f"Alım Fiyatı: `{actual_buy_price_sol_per_token:.8f} SOL/{token_name}`\n"
            f"İşlem ID: [`{tx_signature}`](https://solscan.io/tx/{tx_signature})\n"
            f"Kar Hedefi: `{profit_target_x}x`, Stop Loss: `{stop_loss_percent}%`",
            parse_mode='md',
            link_preview=False
        )
        logger.info(f"Otomatik alım {token_name} için tamamlandı. İmza: {tx_signature}")

    except Exception as e:
        error_msg = f"Otomatik alım {token_name} ({contract_address}) için başarısız oldu: {e}"
        logger.error(error_msg, exc_info=True)
        await add_transaction_history(
            "N/A", # No signature for failed transactions
            "BUY",
            token_name,
            contract_address,
            buy_amount_sol,
            0,
            0,
            "FAILED",
            str(e)
        )
        await bot_client.send_message(
            DEFAULT_ADMIN_ID,
            f"❌ **Otomatik Alım Başarısız!**\n"
            f"Token: **{token_name}**\n"
            f"Adres: `{contract_address}`\n"
            f"Hata: `{e}`",
            parse_mode='md'
        )

async def auto_sell_token(contract_address: str, token_name: str, sell_reason: str, position_data: dict):
    """
    Performs automated token sale for the specified contract address.
    """
    if not solana_client or not payer_keypair:
        logger.error("Solana istemcisi veya ödeme anahtarı başlatılmadı. Otomatik satış yapılamıyor.")
        await bot_client.send_message(
            DEFAULT_ADMIN_ID,
            "❌ **Otomatik Satış Başarısız!**\n"
            "Solana istemcisi veya cüzdan başlatılmamış. Lütfen özel anahtarınızı ayarlayın ve botu yeniden başlatın.",
            parse_mode='md'
        )
        return

    auto_sell_enabled = await get_bot_setting("auto_sell_enabled")
    if auto_sell_enabled != "enabled":
        logger.info("Otomatik satış devre dışı bırakıldı.")
        return

    logger.info(f"Token {token_name} ({contract_address}) için otomatik satış başlatılıyor... Neden: {sell_reason}")
    await bot_client.send_message(
        DEFAULT_ADMIN_ID,
        f"🚨 **Otomatik Satış Tetiklendi!**\n"
        f"Token: **{token_name}**\n"
        f"Adres: `{contract_address}`\n"
        f"Neden: **{sell_reason}**\n"
        f"Satış işlemi başlatılıyor...",
        parse_mode='md'
    )

    priority_fee_lamports_str = await get_bot_setting("DEFAULT_PRIORITY_FEE_LAMPORTS")
    try:
        priority_fee_lamports = int(priority_fee_lamports_str)
    except ValueError:
        logger.error("Geçersiz öncelik ücreti ayarı. Otomatik satış iptal edildi.")
        await bot_client.send_message(
            DEFAULT_ADMIN_ID,
            "❌ **Otomatik Satış Başarısız!**\n"
            "Bot ayarlarındaki öncelik ücreti geçersiz. Lütfen `/settings` ile kontrol edin.",
            parse_mode='md'
        )
        return

    try:
        # Get swap quote from token to SOL
        token_mint = Pubkey.from_string(contract_address)
        sol_mint = Pubkey.from_string("So11111111111111111111111111111111111111112")
        
        # Amount of token to sell (from open position)
        amount_to_sell_token = position_data['buy_amount_token']

        # Get token decimals
        token_info = await asyncio.to_thread(solana_client.get_token_supply, token_mint)
        token_decimals = token_info.value.decimals if token_info and token_info.value else 0
        amount_in_lamports = int(amount_to_sell_token * (10**token_decimals))

        slippage_tolerance_str = await get_bot_setting("slippage_tolerance")
        slippage_bps = int(float(slippage_tolerance_str) * 100)

        quote = await get_swap_quote(token_mint, sol_mint, amount_in_lamports, slippage_bps)

        if not quote:
            raise Exception("Jupiter'den takas teklifi alınamadı.")

        # Get swap transaction from Jupiter
        swap_transaction_data = await get_swap_transaction(quote, payer_keypair.pubkey())

        if not swap_transaction_data or "swapTransaction" not in swap_transaction_data:
            raise Exception("Jupiter'den takas işlemi alınamadı.")

        # Send and confirm the transaction
        tx_signature, tx_status_msg = await send_and_confirm_transaction(
            swap_transaction_data["swapTransaction"],
            priority_fee_lamports
        )

        if tx_status_msg != "success" or not tx_signature:
            raise Exception(f"İşlem gönderilemedi veya onaylanamadı: {tx_status_msg}")

        # Calculate received SOL amount and actual sell price
        received_sol_amount = float(quote['outAmount']) / (10**9)
        
        actual_sell_price_sol_per_token = (float(quote['outAmount']) / (10**9)) / (float(quote['inAmount']) / (10**token_decimals))

        # Remove position from database
        await remove_open_position(contract_address)
        await add_transaction_history(
            tx_signature,
            "SELL",
            token_name,
            contract_address,
            received_sol_amount, # SOL received
            amount_to_sell_token, # Token sold
            actual_sell_price_sol_per_token, # Price SOL/token
            "SUCCESS"
        )

        profit_loss_sol = received_sol_amount - (position_data['buy_amount_token'] * position_data['buy_price_sol'])
        profit_loss_percent = (profit_loss_sol / (position_data['buy_amount_token'] * position_data['buy_price_sol'])) * 100 if (position_data['buy_amount_token'] * position_data['buy_price_sol']) != 0 else 0

        await bot_client.send_message(
            DEFAULT_ADMIN_ID,
            f"✅ **Satış Başarılı!**\n"
            f"Token: **{token_name}**\n"
            f"Satılan Miktar: `{amount_to_sell_token:.4f} {token_name}`\n"
            f"Alınan SOL: `{received_sol_amount:.4f}`\n"
            f"Satış Fiyatı: `{actual_sell_price_sol_per_token:.8f} SOL/{token_name}`\n"
            f"Kar/Zarar: `{profit_loss_sol:.4f} SOL ({profit_loss_percent:.2f}%)`\n"
            f"İşlem ID: [`{tx_signature}`](https://solscan.io/tx/{tx_signature})",
            parse_mode='md',
            link_preview=False
        )
        logger.info(f"Otomatik satış {token_name} için tamamlandı. İmza: {tx_signature}")

    except Exception as e:
        error_msg = f"Otomatik satış {token_name} ({contract_address}) için başarısız oldu: {e}"
        logger.error(error_msg, exc_info=True)
        await add_transaction_history(
            "N/A",
            "SELL",
            token_name,
            contract_address,
            0,
            amount_to_sell_token,
            0,
            "FAILED",
            str(e)
        )
        await bot_client.send_message(
            DEFAULT_ADMIN_ID,
            f"❌ **Otomatik Satış Başarısız!**\n"
            f"Token: **{token_name}**\n"
            f"Adres: `{contract_address}`\n"
            f"Hata: `{e}`",
            parse_mode='md'
        )

# --- Telegram Bot Commands ---
@bot_client.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    admins = await get_admins()
    if event.sender_id not in admins:
        await event.reply("Üzgünüm, bu botu kullanma yetkiniz yok.")
        return

    await event.reply(
        "👋 **Solana Memetoken Otomatik Alım-Satım Botuna Hoş Geldiniz!**\n\n"
        "Aşağıdaki komutları kullanabilirsiniz:\n"
        "• `/status` - Botun mevcut durumunu gösterir.\n"
        "• `/settings` - Bot ayarlarını görüntüle/değiştir.\n"
        "• `/positions` - Açık pozisyonlarınızı görüntüleyin.\n"
        "• `/history` - Son işlem geçmişinizi görüntüleyin.\n"
        "• `/buy <token_address> <sol_amount>` - Belirli bir token'ı manuel olarak satın alın.\n"
        "• `/sell <token_address>` - Belirli bir token'ı manuel olarak satın.\n"
        "• `/reinit_solana` - Solana istemcisini ve cüzdanı yeniden başlatır.\n"
        "• `/admin` - Yönetici ekle/kaldır (yalnızca varsayılan yönetici).\n\n"
        "Botunuzu yapılandırmak için `/settings` komutunu kullanmayı unutmayın.",
        parse_mode='md'
    )

@bot_client.on(events.NewMessage(pattern='/admin'))
async def admin_handler(event):
    if event.sender_id != DEFAULT_ADMIN_ID:
        await event.reply("Bu komutu kullanma yetkiniz yok. Yalnızca varsayılan yönetici kullanabilir.")
        return

    args = event.text.split()
    if len(args) < 2:
        admins = await get_admins()
        admin_list = "\n".join([f"- {a['first_name']} ({a['user_id']})" for a in admins.values()])
        await event.reply(
            "**Yönetici Yönetimi**\n\n"
            f"Mevcut Yöneticiler:\n{admin_list}\n\n"
            "Kullanım:\n"
            "`/admin add <user_id>`\n"
            "`/admin remove <user_id>`",
            parse_mode='md'
        )
        return

    action = args[1].lower()
    if action == "add" and len(args) == 3:
        try:
            user_id = int(args[2])
            # Try to get user's name
            try:
                user = await bot_client.get_entity(user_id)
                first_name = user.first_name if user.first_name else "Bilinmeyen"
                last_name = user.last_name if user.last_name else ""
            except Exception:
                first_name = "Bilinmeyen"
                last_name = ""
            
            await add_admin(user_id, first_name, last_name)
            await event.reply(f"Yönetici {user_id} ({first_name}) başarıyla eklendi.")
        except ValueError:
            await event.reply("Geçersiz kullanıcı ID'si.")
    elif action == "remove" and len(args) == 3:
        try:
            user_id = int(args[2])
            if user_id == DEFAULT_ADMIN_ID:
                await event.reply("Varsayılan yönetici kaldırılamaz.")
                return
            await remove_admin(user_id)
            await event.reply(f"Yönetici {user_id} başarıyla kaldırıldı.")
        except ValueError:
            await event.reply("Geçersiz kullanıcı ID'si.")
    else:
        await event.reply("Geçersiz komut. Kullanım:\n`/admin add <user_id>`\n`/admin remove <user_id>`")

@bot_client.on(events.NewMessage(pattern='/settings'))
async def settings_handler(event):
    admins = await get_admins()
    if event.sender_id not in admins:
        await event.reply("Üzgünüm, bu botu kullanma yetkiniz yok.")
        return

    args = event.text.split(maxsplit=2) # Split into at most 2 parts: /settings, key, value

    if len(args) == 1: # Show settings
        settings = {}
        for key in DEFAULT_BOT_SETTINGS.keys():
            value = await get_bot_setting(key)
            if key == "SOLANA_PRIVATE_KEY" and value:
                settings[key] = value[:5] + "..." + value[-5:] # Mask
            elif key == "JUPITER_API_KEY" and value:
                settings[key] = value[:5] + "..." + value[-5:] # Mask
            else:
                settings[key] = value if value is not None else "AYARLANMADI"

        settings_msg = "**Bot Ayarları:**\n\n"
        for key, value in settings.items():
            settings_msg += f"• `{key}`: `{value}`\n"
        settings_msg += "\nAyarları değiştirmek için:\n`/settings <anahtar> <değer>`"
        await event.reply(settings_msg, parse_mode='md')

    elif len(args) == 3: # Change setting
        setting_key = args[1].strip()
        setting_value = args[2].strip()

        if setting_key not in DEFAULT_BOT_SETTINGS:
            await event.reply(f"Bilinmeyen ayar anahtarı: `{setting_key}`")
            return

        if setting_key == "SOLANA_PRIVATE_KEY" and not re.match(r"^[1-9A-HJ-NP-Za-km-z]{87,88}$", setting_value):
            await event.reply("Geçersiz Solana özel anahtarı formatı. Base58 string olmalı.")
            return
        
        # Validation for numerical values
        if setting_key in ["buy_amount_sol", "slippage_tolerance", "profit_target_x", "stop_loss_percent", "DEFAULT_PRIORITY_FEE_LAMPORTS"]:
            try:
                float(setting_value) # Use float for all numerical settings
            except ValueError:
                await event.reply(f"`{setting_key}` için geçerli bir sayısal değer girin.")
                return

        await set_bot_setting(setting_key, setting_value)
        if setting_key == "SOLANA_PRIVATE_KEY" or setting_key == "JUPITER_API_KEY":
            await event.reply(f"Ayar `{setting_key}` başarıyla güncellendi (güvenlik için maskelendi).")
            # If private key changed, re-initialize Solana client
            if setting_key == "SOLANA_PRIVATE_KEY":
                await init_solana_client()
                await event.reply("Solana istemcisi özel anahtar değişikliği nedeniyle yeniden başlatıldı.")
        else:
            await event.reply(f"Ayar `{setting_key}` başarıyla `{setting_value}` olarak güncellendi.")
    else:
        await event.reply("Geçersiz kullanım. Kullanım:\n`/settings` (göster)\n`/settings <anahtar> <değer>` (değiştir)")

@bot_client.on(events.NewMessage(pattern='/status'))
async def status_handler(event):
    admins = await get_admins()
    if event.sender_id not in admins:
        await event.reply("Üzgünüm, bu botu kullanma yetkiniz yok.")
        return
    
    bot_status = await get_bot_setting("bot_status")
    auto_buy_enabled = await get_bot_setting("auto_buy_enabled")
    auto_sell_enabled = await get_bot_setting("auto_sell_enabled")
    
    balance = await check_wallet_balance()
    wallet_address = payer_keypair.pubkey() if payer_keypair else "Ayarlanmadı"
    
    status_msg = "**Bot Durumu:**\n\n"
    status_msg += f"• Bot Durumu: `{bot_status.upper()}`\n"
    status_msg += f"• Otomatik Alım: `{auto_buy_enabled.upper()}`\n"
    status_msg += f"• Otomatik Satış: `{auto_sell_enabled.upper()}`\n"
    status_msg += f"• Cüzdan Adresi: `{wallet_address}`\n"
    status_msg += f"• SOL Bakiyesi: `{balance:.4f} SOL`" if balance is not None else "• SOL Bakiyesi: `Alınamadı`\n"
    status_msg += f"• Aktif RPC: `{active_rpc_url if active_rpc_url else 'Bilinmiyor'}`\n"
    status_msg += f"• Varsayılan Öncelik Ücreti: `{await get_bot_setting('DEFAULT_PRIORITY_FEE_LAMPORTS')} Lamports`\n"
    
    await event.reply(status_msg, parse_mode='md')

@bot_client.on(events.NewMessage(pattern='/positions'))
async def positions_handler(event):
    admins = await get_admins()
    if event.sender_id not in admins:
        await event.reply("Üzgünüm, bu botu kullanma yetkiniz yok.")
        return

    positions = await get_open_positions()
    if not positions:
        await event.reply("Açık pozisyon bulunmamaktadır.")
        return

    positions_msg = "**Açık Pozisyonlar:**\n\n"
    for pos in positions:
        buy_time = datetime.fromtimestamp(pos['buy_timestamp']).strftime('%Y-%m-%d %H:%M:%S')
        positions_msg += (
            f"• Token: **{pos['token_name']}** (`{pos['contract_address'][:6]}...{pos['contract_address'][-4:]}`)\n"
            f"  Alım Fiyatı: `{pos['buy_price_sol']:.8f} SOL/token`\n"
            f"  Alınan Miktar: `{pos['buy_amount_token']:.4f} token`\n"
            f"  Alım TX: [`{pos['buy_tx_signature'][:6]}...{pos['buy_tx_signature'][-4:]}`](https://solscan.io/tx/{pos['buy_tx_signature']})\n"
            f"  Kar Hedefi: `{pos['target_profit_x']}x`, Stop Loss: `{pos['stop_loss_percent']}%`\n"
            f"  Alım Zamanı: `{buy_time}`\n\n"
        )
    await event.reply(positions_msg, parse_mode='md', link_preview=False)

@bot_client.on(events.NewMessage(pattern='/history'))
async def history_handler(event):
    admins = await get_admins()
    if event.sender_id not in admins:
        await event.reply("Üzgünüm, bu botu kullanma yetkiniz yok.")
        return

    history = await get_transaction_history()
    if not history:
        await event.reply("İşlem geçmişi bulunmamaktadır.")
        return

    history_msg = "**Son İşlem Geçmişi (Son 20 İşlem):**\n\n"
    for tx in history:
        tx_time = datetime.fromtimestamp(tx['timestamp']).strftime('%Y-%m-%d %H:%M:%S')
        tx_sig_short = tx['tx_signature'][:6] + "..." + tx['tx_signature'][-4:] if tx['tx_signature'] and tx['tx_signature'] != "N/A" else "N/A"
        contract_addr_short = tx['contract_address'][:6] + "..." + tx['contract_address'][-4:] if tx['contract_address'] else "N/A"
        
        history_msg += (
            f"• Tür: **{tx['type']}** | Durum: **{tx['status']}**\n"
            f"  Token: **{tx['token_name']}** (`{contract_addr_short}`)\n"
            f"  SOL Miktarı: `{tx['amount_sol']:.4f}` | Token Miktarı: `{tx['amount_token']:.4f}`\n"
            f"  Fiyat: `{tx['price_sol_per_token']:.8f}` SOL/token\n"
            f"  TX ID: [`{tx_sig_short}`](https://solscan.io/tx/{tx['tx_signature']})\n"
            f"  Zaman: `{tx_time}`\n"
        )
        if tx['error_message']:
            history_msg += f"  Hata: `{tx['error_message']}`\n"
        history_msg += "\n"
    await event.reply(history_msg, parse_mode='md', link_preview=False)

@bot_client.on(events.NewMessage(pattern='/buy'))
async def manual_buy_handler(event):
    admins = await get_admins()
    if event.sender_id not in admins:
        await event.reply("Üzgünüm, bu botu kullanma yetkiniz yok.")
        return

    args = event.text.split()
    if len(args) != 3:
        await event.reply("Kullanım: `/buy <token_address> <sol_amount>`")
        return

    token_address = args[1]
    sol_amount_str = args[2]

    try:
        sol_amount = float(sol_amount_str)
        if sol_amount <= 0:
            raise ValueError("SOL miktarı pozitif olmalı.")
    except ValueError:
        await event.reply("Geçersiz SOL miktarı.")
        return
    
    if not re.match(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$", token_address):
        await event.reply("Geçersiz token adresi formatı.")
        return

    # For manual buy, try to fetch token name if possible, otherwise use a placeholder
    token_name_from_api = "Manuel Token"
    try:
        token_mint_pubkey = Pubkey.from_string(token_address)
        token_meta = await asyncio.to_thread(solana_client.get_token_supply, token_mint_pubkey)
        if token_meta and token_meta.value and token_meta.value.ui_amount_string: # This field might contain symbol
             # This is a heuristic, actual token symbol might need more specific API call (e.g., from token list)
            token_name_from_api = token_meta.value.ui_amount_string.split(" ")[-1] if " " in token_meta.value.ui_amount_string else token_address[:4]
            # A better way would be to query a token list like Jupiter's or Solana's
            # For simplicity, we'll just use a placeholder or try to extract from UI amount string
    except Exception as e:
        logger.warning(f"Token {token_address} için isim alınamadı: {e}")

    await event.reply(f"Manuel alım başlatılıyor: {sol_amount} SOL karşılığında {token_address} tokenı...")
    await auto_buy_token(token_address, token_name_from_api)
    await event.reply("Manuel alım işlemi tamamlandı (yukarıdaki mesajları kontrol edin).")

@bot_client.on(events.NewMessage(pattern='/sell'))
async def manual_sell_handler(event):
    admins = await get_admins()
    if event.sender_id not in admins:
        await event.reply("Üzgünüm, bu botu kullanma yetkiniz yok.")
        return

    args = event.text.split()
    if len(args) != 2:
        await event.reply("Kullanım: `/sell <token_address>`")
        return

    token_address = args[1]
    
    if not re.match(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$", token_address):
        await event.reply("Geçersiz token adresi formatı.")
        return

    positions = await get_open_positions()
    position_to_sell = next((p for p in positions if p['contract_address'] == token_address), None)

    if not position_to_sell:
        await event.reply(f"Açık pozisyonlarda {token_address} tokenı bulunamadı.")
        return
    
    # Get token name from position data
    token_name = position_to_sell['token_name']

    await event.reply(f"Manuel satış başlatılıyor: {token_name} ({token_address})...")
    await auto_sell_token(token_address, token_name, "Manuel Satış", position_to_sell)
    await event.reply("Manuel satış işlemi tamamlandı (yukarıdaki mesajları kontrol edin).")


@bot_client.on(events.NewMessage(pattern='/reinit_solana'))
async def reinit_solana_handler(event):
    admins = await get_admins()
    if event.sender_id not in admins:
        await event.reply("Üzgünüm, bu botu kullanma yetkiniz yok.")
        return
    
    await event.reply("Solana istemcisi ve cüzdan yeniden başlatılıyor...")
    await init_solana_client()
    if solana_client and payer_keypair:
        balance = await check_wallet_balance()
        await event.reply(
            f"✅ Solana istemcisi başarıyla yeniden başlatıldı.\n"
            f"Cüzdan: `{payer_keypair.pubkey()}`\n"
            f"Bakiye: `{balance:.4f} SOL`" if balance is not None else "Bakiye: `Alınamadı`",
            parse_mode='md'
        )
    else:
        await event.reply("❌ Solana istemcisinin yeniden başlatılması başarısız oldu. Logları kontrol edin.")


# --- Telegram Channel Listener (for sniping signals) ---
@bot_client.on(events.NewMessage(chats=SOURCE_CHANNEL_ID))
async def channel_message_handler(event):
    logger.info(f"Kanal {SOURCE_CHANNEL_ID} adresinden yeni mesaj alındı.")
    logger.debug(f"Mesaj içeriği: {event.text}")

    contract_address = extract_contract(event.text)
    token_name = extract_token_name_from_message(event.text)

    if contract_address:
        logger.info(f"Mesajdan sözleşme adresi çıkarıldı: {contract_address}")
        if await is_contract_processed(contract_address):
            logger.info(f"Sözleşme {contract_address} zaten işlenmiş.")
            return

        bot_status = await get_bot_setting("bot_status")
        if bot_status == "running":
            # Start auto buy operation in background
            asyncio.create_task(auto_buy_token(contract_address, token_name))
        else:
            logger.info("Bot duraklatıldı, otomatik alım tetiklenmedi.")
    else:
        logger.debug("Mesajda sözleşme adresi bulunamadı.")

# --- Monitor Open Positions Task ---
async def monitor_open_positions():
    """
    Periodically checks open positions and performs sales based on profit/loss targets.
    """
    await bot_client.send_message(DEFAULT_ADMIN_ID, "📊 **Pozisyon İzleyici Başlatıldı.**")
    while True:
        try:
            auto_sell_enabled = await get_bot_setting("auto_sell_enabled")
            if auto_sell_enabled != "enabled":
                logger.info("Otomatik satış devre dışı, pozisyon izleme duraklatıldı.")
                await asyncio.sleep(60) # Wait longer
                continue

            positions = await get_open_positions()
            if not positions:
                logger.debug("İzlenecek açık pozisyon yok.")
                await asyncio.sleep(30) # No need to check too frequently
                continue

            for pos in positions:
                contract_address = pos['contract_address']
                token_name = pos['token_name']
                buy_price_sol = pos['buy_price_sol']
                target_profit_x = pos['target_profit_x']
                stop_loss_percent = pos['stop_loss_percent']

                current_price_sol_per_token = await get_current_token_price_sol(contract_address)

                if current_price_sol_per_token is None:
                    logger.warning(f"{token_name} ({contract_address}) için mevcut fiyat alınamadı. Atlanıyor.")
                    continue

                # Calculate Profit/Loss
                current_value_sol = pos['buy_amount_token'] * current_price_sol_per_token
                initial_cost_sol = pos['buy_amount_token'] * buy_price_sol
                
                profit_loss_sol = current_value_sol - initial_cost_sol
                profit_loss_percent = (profit_loss_sol / initial_cost_sol) * 100 if initial_cost_sol != 0 else 0

                logger.info(f"Pozisyon {token_name}: Mevcut Fiyat: {current_price_sol_per_token:.8f} SOL/token, Kar/Zarar: {profit_loss_percent:.2f}%")

                # Profit Target Check
                if current_price_sol_per_token >= (buy_price_sol * target_profit_x):
                    logger.info(f"Kar hedefi {target_profit_x}x ulaşıldı! {token_name} satılıyor.")
                    asyncio.create_task(auto_sell_token(contract_address, token_name, "Kar Hedefi Ulaşıldı", pos))
                # Stop Loss Check (as negative percentage)
                elif profit_loss_percent <= -abs(stop_loss_percent):
                    logger.info(f"Stop loss {stop_loss_percent}% tetiklendi! {token_name} satılıyor.")
                    asyncio.create_task(auto_sell_token(contract_address, token_name, "Stop Loss Tetiklendi", pos))
            
            await asyncio.sleep(30) # Check every 30 seconds
        except Exception as e:
            logger.error(f"Pozisyon izleme sırasında hata: {e}", exc_info=True)
            await bot_client.send_message(
                DEFAULT_ADMIN_ID,
                f"⚠️ **Pozisyon İzleyici Hatası!**\n"
                f"Hata: `{e}`\n"
                "İzleyici devam edecek.",
                parse_mode='md'
            )
            await asyncio.sleep(60) # Wait longer on error

# --- Flask Web Server ---
@app.route('/')
def index():
    if 'logged_in' not in session:
        return redirect(url_for('login'))
    
    return render_template_string("""
    <!DOCTYPE html>
    <html lang="tr">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Solana Bot Yönetim Paneli</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
        <style>
            body { font-family: 'Inter', sans-serif; }
            .card {
                background-color: #1f2937; /* Gray-800 */
                border-radius: 0.75rem; /* rounded-xl */
                box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.5), 0 10px 10px -5px rgba(0, 0, 0, 0.4); /* shadow-2xl */
                border: 1px solid #6d28d9; /* border-purple-700 */
            }
            .btn-primary {
                background-color: #8b5cf6; /* purple-500 */
                color: white;
                padding: 0.75rem 1.5rem;
                border-radius: 0.5rem;
                font-weight: 600;
                transition: background-color 0.3s ease;
            }
            .btn-primary:hover {
                background-color: #7c3aed; /* purple-600 */
            }
            .input-field {
                background-color: #111827; /* gray-900 */
                border: 1px solid #4b5563; /* gray-600 */
                color: white;
                padding: 0.75rem;
                border-radius: 0.375rem;
            }
        </style>
    </head>
    <body class="bg-gradient-to-br from-purple-900 to-indigo-900 text-white min-h-screen flex items-center justify-center p-6">
        <div class="container mx-auto max-w-4xl space-y-8">
            <h1 class="text-5xl font-extrabold text-center text-purple-300 mb-10">
                Solana Bot Yönetim Paneli
            </h1>

            <div class="grid md:grid-cols-2 gap-8">
                <!-- Bot Status -->
                <div class="card p-6">
                    <h2 class="text-2xl font-bold mb-4 text-purple-200">Bot Durumu</h2>
                    <div id="bot-status-content">Yükleniyor...</div>
                    <button onclick="fetchStatus()" class="btn-primary mt-4 w-full">Durumu Yenile</button>
                </div>

                <!-- Settings -->
                <div class="card p-6">
                    <h2 class="text-2xl font-bold mb-4 text-purple-200">Ayarlar</h2>
                    <div id="settings-content">Yükleniyor...</div>
                    <button onclick="fetchSettings()" class="btn-primary mt-4 w-full">Ayarları Yenile</button>
                </div>
            </div>

            <!-- Open Positions -->
            <div class="card p-6">
                <h2 class="text-2xl font-bold mb-4 text-purple-200">Açık Pozisyonlar</h2>
                <div id="positions-content">Yükleniyor...</div>
                <button onclick="fetchPositions()" class="btn-primary mt-4 w-full">Pozisyonları Yenile</button>
            </div>

            <!-- Transaction History -->
            <div class="card p-6">
                <h2 class="text-2xl font-bold mb-4 text-purple-200">İşlem Geçmişi</h2>
                <div id="history-content">Yükleniyor...</div>
                <button onclick="fetchHistory()" class="btn-primary mt-4 w-full">Geçmişi Yenile</button>
            </div>
            
            <div class="text-center mt-8">
                <button onclick="logout()" class="text-red-400 hover:text-red-300 font-semibold">Çıkış Yap</button>
            </div>
        </div>

        <script>
            async function fetchData(endpoint, targetElementId) {
                try {
                    const response = await fetch(endpoint);
                    if (!response.ok) {
                        throw new Error(`HTTP hata! Durum: ${response.status}`);
                    }
                    const data = await response.json();
                    const targetElement = document.getElementById(targetElementId);
                    if (targetElement) {
                        targetElement.innerHTML = formatData(data, targetElementId);
                    }
                } catch (error) {
                    console.error(`Veri çekme hatası (${endpoint}):`, error);
                    const targetElement = document.getElementById(targetElementId);
                    if (targetElement) {
                        targetElement.innerHTML = `<p class="text-red-400">Veri yüklenirken hata oluştu: ${error.message}</p>`;
                    }
                }
            }

            function formatData(data, type) {
                let html = '';
                if (type === 'bot-status-content') {
                    html += `<p><span class="font-semibold">Bot Durumu:</span> <span class="text-${data.bot_status === 'running' ? 'green' : 'red'}-400">${data.bot_status.toUpperCase()}</span></p>`;
                    html += `<p><span class="font-semibold">Otomatik Alım:</span> <span class="text-${data.auto_buy_enabled === 'enabled' ? 'green' : 'red'}-400">${data.auto_buy_enabled.toUpperCase()}</span></p>`;
                    html += `<p><span class="font-semibold">Otomatik Satış:</span> <span class="text-${data.auto_sell_enabled === 'enabled' ? 'green' : 'red'}-400">${data.auto_sell_enabled.toUpperCase()}</span></p>`;
                    html += `<p><span class="font-semibold">Cüzdan Adresi:</span> <code class="break-all">${data.wallet_address}</code></p>`;
                    html += `<p><span class="font-semibold">SOL Bakiyesi:</span> ${data.sol_balance !== null ? `${data.sol_balance.toFixed(4)} SOL` : 'Alınamadı'}</p>`;
                    html += `<p><span class="font-semibold">Aktif RPC:</span> <code class="break-all">${data.active_rpc || 'Bilinmiyor'}</code></p>`;
                    html += `<p><span class="font-semibold">Varsayılan Öncelik Ücreti:</span> <code>${data.DEFAULT_PRIORITY_FEE_LAMPORTS} Lamports</code></p>`;
                } else if (type === 'settings-content') {
                    for (const key in data) {
                        html += `<p><span class="font-semibold">${key}:</span> <code>${data[key]}</code></p>`;
                    }
                } else if (type === 'positions-content') {
                    if (data.length === 0) {
                        html = '<p>Açık pozisyon bulunmamaktadır.</p>';
                    } else {
                        data.forEach(pos => {
                            const buyTime = new Date(pos.buy_timestamp * 1000).toLocaleString();
                            html += `
                                <div class="mb-4 p-3 bg-gray-700 rounded-md">
                                    <p><span class="font-semibold">Token:</span> ${pos.token_name} (<code class="break-all">${pos.contract_address.substring(0,6)}...${pos.contract_address.slice(-4)}</code>)</p>
                                    <p><span class="font-semibold">Alım Fiyatı:</span> ${pos.buy_price_sol.toFixed(8)} SOL/token</p>
                                    <p><span class="font-semibold">Alınan Miktar:</span> ${pos.buy_amount_token.toFixed(4)} token</p>
                                    <p><span class="font-semibold">Kar Hedefi:</span> ${pos.target_profit_x}x, Stop Loss: ${pos.stop_loss_percent}%</p>
                                    <p><span class="font-semibold">Alım Zamanı:</span> ${buyTime}</p>
                                    <p><span class="font-semibold">Alım TX:</span> <a href="https://solscan.io/tx/${pos.buy_tx_signature}" target="_blank" class="text-blue-400 hover:underline">${pos.buy_tx_signature.substring(0,6)}...${pos.buy_tx_signature.slice(-4)}</a></p>
                                </div>
                            `;
                        });
                    }
                } else if (type === 'history-content') {
                    if (data.length === 0) {
                        html = '<p>İşlem geçmişi bulunmamaktadır.</p>';
                    } else {
                        data.forEach(tx => {
                            const txTime = new Date(tx.timestamp * 1000).toLocaleString();
                            html += `
                                <div class="mb-4 p-3 bg-gray-700 rounded-md">
                                    <p><span class="font-semibold">Tür:</span> ${tx.type} | <span class="font-semibold">Durum:</span> <span class="text-${tx.status === 'SUCCESS' ? 'green' : 'red'}-400">${tx.status}</span></p>
                                    <p><span class="font-semibold">Token:</span> ${tx.token_name} (<code class="break-all">${tx.contract_address.substring(0,6)}...${tx.contract_address.slice(-4)}</code>)</p>
                                    <p><span class="font-semibold">SOL Miktarı:</span> ${tx.amount_sol !== null ? tx.amount_sol.toFixed(4) : 'N/A'} | <span class="font-semibold">Token Miktarı:</span> ${tx.amount_token !== null ? tx.amount_token.toFixed(4) : 'N/A'}</p>
                                    <p><span class="font-semibold">Fiyat:</span> ${tx.price_sol_per_token !== null ? tx.price_sol_per_token.toFixed(8) : 'N/A'} SOL/token</p>
                                    <p><span class="font-semibold">Zaman:</span> ${txTime}</p>
                                    <p><span class="font-semibold">TX ID:</span> <a href="https://solscan.io/tx/${tx.tx_signature}" target="_blank" class="text-blue-400 hover:underline">${tx.tx_signature.substring(0,6)}...${tx.tx_signature.slice(-4)}</a></p>
                                    ${tx.error_message ? `<p class="text-red-400"><span class="font-semibold">Hata:</span> ${tx.error_message}</p>` : ''}
                                </div>
                            `;
                        });
                    }
                }
                return html;
            }

            async function fetchStatus() { await fetchData('/api/status', 'bot-status-content'); }
            async function fetchSettings() { await fetchData('/api/settings', 'settings-content'); }
            async function fetchPositions() { await fetchData('/api/positions', 'positions-content'); }
            async function fetchHistory() { await fetchData('/api/history', 'history-content'); }

            function logout() {
                fetch('/logout', { method: 'POST' })
                    .then(() => window.location.href = '/login');
            }

            // Fetch data when page loads
            document.addEventListener('DOMContentLoaded', () => {
                fetchStatus();
                fetchSettings();
                fetchPositions();
                fetchHistory();
            });
        </script>
    </body>
    </html>
    """)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form['password'] == DASHBOARD_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('index'))
        else:
            return render_template_string("""
                <!DOCTYPE html>
                <html lang="tr">
                <head>
                    <meta charset="UTF-8">
                    <meta name="viewport" content="width=device-width, initial-scale=1.0">
                    <title>Giriş Yap</title>
                    <script src="https://cdn.tailwindcss.com"></script>
                    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
                    <style>
                        body { font-family: 'Inter', sans-serif; }
                        .card {
                            background-color: #1f2937; /* Gray-800 */
                            border-radius: 0.75rem; /* rounded-xl */
                            box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.5), 0 10px 10px -5px rgba(0, 0, 0, 0.4); /* shadow-2xl */
                            border: 1px solid #6d28d9; /* border-purple-700 */
                        }
                        .btn-primary {
                            background-color: #8b5cf6; /* purple-500 */
                            color: white;
                            padding: 0.75rem 1.5rem;
                            border-radius: 0.5rem;
                            font-weight: 600;
                            transition: background-color 0.3s ease;
                        }
                        .btn-primary:hover {
                            background-color: #7c3aed; /* purple-600 */
                        }
                        .input-field {
                            background-color: #111827; /* gray-900 */
                            border: 1px solid #4b5563; /* gray-600 */
                            color: white;
                            padding: 0.75rem;
                            border-radius: 0.375rem;
                        }
                    </style>
                </head>
                <body class="bg-gradient-to-br from-purple-900 to-indigo-900 text-white min-h-screen flex items-center justify-center p-6">
                    <div class="card p-8 w-full max-w-md text-center">
                        <h2 class="text-3xl font-bold mb-6 text-purple-300">Yönetim Paneli Girişi</h2>
                        <p class="text-red-400 mb-4">Yanlış şifre!</p>
                        <form method="post">
                            <input type="password" name="password" placeholder="Şifre" class="input-field w-full mb-4" required>
                            <button type="submit" class="btn-primary w-full">Giriş Yap</button>
                        </form>
                    </div>
                </body>
                </html>
            """, error="Yanlış şifre!")
    return render_template_string("""
        <!DOCTYPE html>
        <html lang="tr">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Giriş Yap</title>
            <script src="https://cdn.tailwindcss.com"></script>
            <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
            <style>
                body { font-family: 'Inter', sans-serif; }
                .card {
                    background-color: #1f2937; /* Gray-800 */
                    border-radius: 0.75rem; /* rounded-xl */
                    box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.5), 0 10px 10px -5px rgba(0, 0, 0, 0.4); /* shadow-2xl */
                    border: 1px solid #6d28d9; /* border-purple-700 */
                }
                .btn-primary {
                    background-color: #8b5cf6; /* purple-500 */
                    color: white;
                    padding: 0.75rem 1.5rem;
                    border-radius: 0.5rem;
                    font-weight: 600;
                    transition: background-color 0.3s ease;
                }
                .btn-primary:hover {
                    background-color: #7c3aed; /* purple-600 */
                }
                .input-field {
                    background-color: #111827; /* gray-900 */
                    border: 1px solid #4b5563; /* gray-600 */
                    color: white;
                    padding: 0.75rem;
                    border-radius: 0.375rem;
                }
            </style>
        </head>
        <body class="bg-gradient-to-br from-purple-900 to-indigo-900 text-white min-h-screen flex items-center justify-center p-6">
            <div class="card p-8 w-full max-w-md text-center">
                <h2 class="text-3xl font-bold mb-6 text-purple-300">Yönetim Paneli Girişi</h2>
                <form method="post">
                    <input type="password" name="password" placeholder="Şifre" class="input-field w-full mb-4" required>
                    <button type="submit" class="btn-primary w-full">Giriş Yap</button>
                </form>
            </div>
        </body>
        </html>
    """)

@app.route('/logout', methods=['POST'])
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

@app.route('/api/status')
async def api_status():
    if 'logged_in' not in session:
        return jsonify({"error": "Yetkisiz erişim"}), 401
    
    bot_status = await get_bot_setting("bot_status")
    auto_buy_enabled = await get_bot_setting("auto_buy_enabled")
    auto_sell_enabled = await get_bot_setting("auto_sell_enabled")
    
    balance = await check_wallet_balance()
    wallet_address = str(payer_keypair.pubkey()) if payer_keypair else "Ayarlanmadı"
    priority_fee = await get_bot_setting("DEFAULT_PRIORITY_FEE_LAMPORTS")
    
    return jsonify({
        "bot_status": bot_status,
        "auto_buy_enabled": auto_buy_enabled,
        "auto_sell_enabled": auto_sell_enabled,
        "wallet_address": wallet_address,
        "sol_balance": balance,
        "active_rpc": active_rpc_url,
        "DEFAULT_PRIORITY_FEE_LAMPORTS": priority_fee
    })

@app.route('/api/settings')
async def api_settings():
    if 'logged_in' not in session:
        return jsonify({"error": "Yetkisiz erişim"}), 401
    
    settings = {}
    for key in DEFAULT_BOT_SETTINGS.keys():
        value = await get_bot_setting(key)
        if key == "SOLANA_PRIVATE_KEY" and value:
            settings[key] = value[:5] + "..." + value[-5:] # Mask
        elif key == "JUPITER_API_KEY" and value:
            settings[key] = value[:5] + "..." + value[-5:] # Mask
        else:
            settings[key] = value if value is not None else "AYARLANMADI"
    return jsonify(settings)

@app.route('/api/positions')
async def api_positions():
    if 'logged_in' not in session:
        return jsonify({"error": "Yetkisiz erişim"}), 401
    
    positions = await get_open_positions()
    return jsonify(positions)

@app.route('/api/history')
async def api_history():
    if 'logged_in' not in session:
        return jsonify({"error": "Yetkisiz erişim"}), 401
    
    history = await get_transaction_history()
    return jsonify(history)

# --- Bot and Flask Startup ---
async def main():
    # Initialize database
    await init_db()
    
    # Add default admin (if not exists)
    admins = await get_admins()
    if not admins:
        logger.info(f"Varsayılan yönetici {DEFAULT_ADMIN_ID} ekleniyor.")
        # Try to get user's name
        try:
            user = await bot_client.get_entity(DEFAULT_ADMIN_ID)
            first_name = user.first_name if user.first_name else "Varsayılan"
            last_name = user.last_name if user.last_name else "Yönetici"
        except Exception:
            first_name = "Varsayılan"
            last_name = "Yönetici"
        await add_admin(DEFAULT_ADMIN_ID, first_name, last_name, is_default=True)
        await bot_client.send_message(DEFAULT_ADMIN_ID, "Bot başlatıldı ve siz varsayılan yönetici olarak eklendiniz. Ayarlarınızı `/settings` ile yapılandırabilirsiniz.")
    
    # Save default settings to DB (if not exist)
    for key, default_value in DEFAULT_BOT_SETTINGS.items():
        current_value = await get_bot_setting(key)
        if current_value is None:
            await set_bot_setting(key, default_value)
            logger.info(f"Varsayılan ayar '{key}' '{default_value}' olarak kaydedildi.")

    # Initialize Solana client
    await init_solana_client()

    # Start Telegram bot
    logger.info("Telegram botu başlatılıyor...")
    await bot_client.start(bot_token=BOT_TOKEN)
    logger.info("Telegram botu başarıyla başlatıldı.")
    
    # Start background tasks
    asyncio.create_task(monitor_open_positions())
    logger.info("Pozisyon izleme görevi başlatıldı.")

    # Keep the bot running indefinitely
    await bot_client.run_until_disconnected()

def run_flask():
    # Run Flask application in a separate thread
    app.run(host='0.0.0.0', port=5000, debug=False)

if __name__ == '__main__':
    # Start Flask in a separate thread
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.start()

    # Start asyncio event loop and run the bot
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot manuel olarak durduruldu.")
    except Exception as e:
        logger.critical(f"Ana bot döngüsünde kritik hata: {e}", exc_info=True)
