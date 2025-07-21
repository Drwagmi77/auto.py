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

# Solana Kütüphaneleri
from solana.rpc.api import Client, RPCException
from solana.rpc.types import TxOpts
from solders.keypair import Keypair
from solders.pubkey import Pubkey # Buradaki import yolu güncellendi (PublicKey yerine Pubkey)
from solders.transaction import VersionedTransaction
from solders.message import MessageV0
from solders.instruction import Instruction
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price

# --- Ortam Değişkenleri ---
# PostgreSQL Veritabanı Bilgileri
# Bu değerleri Render veya kullandığınız VPS'te ortam değişkeni olarak ayarlamanız GEREKLİDİR.
DB_NAME = os.environ.get("DB_NAME", "your_db_name") # Veritabanı adı
DB_USER = os.environ.get("DB_USER", "your_db_user") # Veritabanı kullanıcısı
DB_PASS = os.environ.get("DB_PASS") # Veritabanı şifresi
DB_HOST = os.environ.get("DB_HOST", "localhost") # Veritabanı hostu
DB_PORT = os.environ.get("DB_PORT", "5432") # Veritabanı portu

# Telegram API Bilgileri
# Bu bot için BotFather'dan aldığınız TOKEN
BOT_TOKEN = os.environ.get("BOT_TOKEN") 
# Kendi Telegram hesabınızın API ID ve HASH'i (my.telegram.org adresinden alın)
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")

# Sinyal botunuzun CA'ları paylaştığı kanalın ID'si (örn: -1001234567890)
# Bu bot sadece bu kanalı dinleyecektir.
SOURCE_CHANNEL_ID = int(os.environ.get("SOURCE_CHANNEL_ID")) 

# Solana Cüzdanı Bilgileri
# DİKKAT: BU ÇOK HASSAS BİR BİLGİDİR! ASLA KODUN İÇİNE YAZMAYIN!
# Render gibi platformlarda ortam değişkeni olarak ayarlayın.
SOLANA_PRIVATE_KEY = os.environ.get("SOLANA_PRIVATE_KEY") 
SOLANA_RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
JUPITER_API_URL = os.environ.get("JUPITER_API_URL", "https://quote-api.jup.ag/v6")

# Flask Uygulaması için Secret Key
SECRET_KEY = os.environ.get("SECRET_KEY", os.urandom(24).hex())


app = Flask(__name__)
app.secret_key = SECRET_KEY

# --- Telethon Client'ları ---
# user_client: Telegram kanalından mesajları okumak için kullanılır (kullanıcı oturumu)
# bot_client: Kendi botunuzun mesaj göndermesi ve admin paneli için kullanılır (bot oturumu)
bot_client = TelegramClient('auto_buy_bot_session', API_ID, API_HASH)
user_client = TelegramClient('auto_buy_user_session', API_ID, API_HASH)

# --- Solana Client ve Wallet Initialization ---
solana_client = None
payer_keypair = None

async def init_solana_client():
    """Solana RPC istemcisini ve cüzdanı başlatır."""
    global solana_client, payer_keypair
    try:
        solana_client = Client(SOLANA_RPC_URL)
        if SOLANA_PRIVATE_KEY:
            payer_keypair = Keypair.from_base58_string(SOLANA_PRIVATE_KEY)
            # DÜZELTME: Keypair objesinin public_key'i artık pubkey() metoduyla erişilir.
            logger.info(f"Solana client initialized. Wallet public key: {payer_keypair.pubkey()}")
            # Cüzdan bakiyesini kontrol et
            balance_response = await asyncio.to_thread(solana_client.get_balance, payer_keypair.pubkey())
            if balance_response and 'result' in balance_response and 'value' in balance_response['result']:
                balance_lamports = balance_response['result']['value']
                logger.info(f"Wallet balance: {balance_lamports / 10**9} SOL") # Lamports to SOL
            else:
                logger.warning("Could not retrieve wallet balance.")
        else:
            logger.error("SOLANA_PRIVATE_KEY not set. Auto-buying functionality will be disabled.")
            solana_client = None # Alım yapamayız
            payer_keypair = None # Alım yapamayız
    except Exception as e:
        logger.error(f"Error initializing Solana client or wallet: {e}")
        solana_client = None
        payer_keypair = None

# --- Veritabanı Bağlantı ve Yönetim Fonksiyonları (PostgreSQL) ---
def get_connection():
    """PostgreSQL veritabanı bağlantısı sağlar."""
    try:
        return psycopg2.connect(
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASS,
            host=DB_HOST,
            port=DB_PORT,
            sslmode="require" # Güvenli bağlantı için
        )
    except psycopg2.OperationalError as e:
        logger.error(f"Database connection failed: {e}")
        raise e

def init_db_sync():
    """Veritabanı tablolarını (yoksa) oluşturur."""
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
        conn.commit()
        logger.info("Database initialized or already exists.")
    except Exception as e:
        logger.error(f"Error during database initialization: {e}")
        raise
    finally:
        if conn:
            conn.close()

# Admin Yönetimi
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

# Bot Ayarları Yönetimi
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
            logger.info(f"Bot setting '{setting}' set to '{value}'.")
    except Exception as e:
        logger.error(f"Error setting bot setting '{setting}': {e}")
    finally:
        if conn:
            conn.close()

# İşlenmiş Kontratlar (Tekrar alımı engellemek için)
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

# Açık Pozisyonlar (Otomatik satım için)
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


# --- Async Veritabanı Fonksiyonları için Wrapper'lar ---
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


# --- Varsayılan Ayarlar ---
# Kendi Telegram User ID'nizi buraya yazın veya ortam değişkeni olarak ayarlayın
DEFAULT_ADMIN_ID = int(os.environ.get("DEFAULT_ADMIN_ID", "YOUR_TELEGRAM_USER_ID")) 
DEFAULT_BOT_SETTINGS = {
    "bot_status": "running",
    "auto_buy_enabled": "enabled",  # Otomatik alım varsayılan olarak AÇIK
    "buy_amount_sol": "0.05",        # Her alımda harcanacak SOL miktarı (0.05 SOL)
    "slippage_tolerance": "5",       # Kayma toleransı (%) (varsayılan 5%)
    "auto_sell_enabled": "enabled", # Otomatik satım varsayılan olarak AÇIK
    "profit_target_x": "5.0",        # Kar hedefi (5.0 = 5x)
    "stop_loss_percent": "50.0"      # Stop-loss yüzdesi (50.0 = %50 düşüş)
}

# --- Logging Kurulumu ---
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

# --- Telethon Yardımcı Fonksiyonlar ---
async def retry_telethon_call(coro, max_retries=5, base_delay=1.0):
    """Telethon çağrıları için tekrar deneme mekanizması."""
    for i in range(max_retries):
        try:
            return await coro
        except Exception as e:
            logger.warning(f"Retry attempt {i+1}/{max_retries} for Telethon call due to error: {e}")
            if i < max_retries - 1:
                delay = base_delay * (2 ** i) + random.uniform(0, 1)
            else:
                logger.error(f"Max retries reached for Telethon call: {e}")
                raise
    raise RuntimeError("Retry logic failed or max_retries was 0")

def extract_contract(text: str) -> str | None:
    """Metinden Solana kontrat adresini (Base58, 32-44 karakter) çıkarır."""
    m = re.findall(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b", text)
    return m[0] if m else None

def extract_token_name_from_message(text: str) -> str:
    """Mesajdan token adını ($TOKEN_NAME formatında) çıkarır."""
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

# --- Solana Auto-Buy Fonksiyonları ---
async def get_current_token_price_sol(token_mint: Pubkey, amount_token_to_check: float = 0.000000001):
    """
    Belirli bir token'ın anlık SOL fiyatını tahmin eder.
    Küçük bir miktar token'ı SOL'a takas etme teklifi alarak fiyatı bulur.
    """
    if not solana_client:
        logger.error("Solana client not initialized. Cannot get token price.")
        return None

    try:
        # Token'dan SOL'a takas teklifi al
        input_mint = token_mint
        output_mint = Pubkey("So11111111111111111111111111111111111111112") # SOL mint address

        # Token'ın decimal sayısını al
        token_info = await asyncio.to_thread(solana_client.get_token_supply, token_mint)
        if not token_info or 'result' not in token_info or 'value' not in token_info['result']:
            logger.warning(f"Could not get token supply info for {token_mint}. Cannot determine decimals.")
            return None
        decimals = token_info['result']['value']['decimals']
        
        amount_in_lamports = int(amount_token_to_check * (10**decimals)) # Küçük bir miktar token

        quote_url = f"{JUPITER_API_URL}/quote?inputMint={input_mint}&outputMint={output_mint}&amount={amount_in_lamports}&slippageBps=0" # Slippage 0
        response = requests.get(quote_url)
        response.raise_for_status()
        quote_data = response.json()

        if not quote_data or "outAmount" not in quote_data or "inAmount" not in quote_data:
            logger.warning(f"Invalid quote data for price check: {quote_data}")
            return None
        
        # Fiyatı hesapla: (Çıkış SOL miktarı / Giriş Token miktarı)
        # SOL'un lamports'tan SOL'a, token'ın kendi decimal'ına göre dönüştürülmesi
        price_sol_per_token = (float(quote_data['outAmount']) / (10**9)) / (float(quote_data['inAmount']) / (10**decimals))
        logger.debug(f"Current price for {token_mint}: {price_sol_per_token} SOL/token")
        return price_sol_per_token

    except requests.exceptions.RequestException as e:
        logger.error(f"Error getting token price from Jupiter: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error in get_current_token_price_sol: {e}")
        return None

async def get_swap_quote(input_mint: Pubkey, output_mint: Pubkey, amount_in_lamports: int, slippage_bps: int):
    """Jupiter Aggregator'dan takas teklifi alır."""
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
    """Jupiter Aggregator'dan alınan teklifle takas işlemini gerçekleştirir."""
    if not solana_client or not payer_keypair:
        logger.error("Solana client or payer keypair not initialized. Cannot perform swap.")
        return False, "Solana client or wallet not ready."

    try:
        swap_url = f"{JUPITER_API_URL}/swap"
        swap_response = requests.post(swap_url, json={
            "quoteResponse": quote_data,
            "userPublicKey": str(payer_keypair.pubkey()), # Düzeltme: public_key() metodu kullanıldı
            "wrapUnwrapSOL": True, # Gerekirse SOL'u WEN/WSOL'a dönüştür
            "prioritizationFeeLamports": 100000 # Küçük bir öncelik ücreti ekle (isteğe bağlı)
        })
        swap_response.raise_for_status()
        swap_data = swap_response.json()

        if not swap_data or "swapTransaction" not in swap_data:
            logger.error(f"Invalid swap data received from Jupiter: {swap_data}")
            return False, "Invalid swap transaction data."

        serialized_tx = swap_data["swapTransaction"]
        transaction = await asyncio.to_thread(VersionedTransaction.from_bytes, bytes(serialized_tx, 'base64'))
        
        # İşlemi imzala
        transaction.sign([payer_keypair])

        # İşlemi gönder
        tx_signature = await asyncio.to_thread(solana_client.send_transaction, transaction, opts=TxOpts(skip_preflight=True))
        logger.info(f"Swap transaction sent: {tx_signature['result']}")

        # İşlemin onaylanmasını bekle
        confirmation = await asyncio.to_thread(solana_client.confirm_transaction, tx_signature['result'], commitment="confirmed")
        
        if confirmation['result']['value']['err']:
            logger.error(f"Transaction failed with error: {confirmation['result']['value']['err']}")
            return False, f"Transaction failed: {confirmation['result']['value']['err']}"
        else:
            logger.info(f"Transaction confirmed: {tx_signature['result']}")
            return True, tx_signature['result']

    except requests.exceptions.RequestException as e:
        logger.error(f"Error performing swap with Jupiter: {e}")
        return False, f"HTTP request error: {e}"
    except RPCException as e:
        logger.error(f"Solana RPC error during swap: {e}")
        return False, f"Solana RPC error: {e}"
    except Exception as e:
        logger.error(f"Unexpected error in perform_swap: {e}")
        return False, f"Unexpected error: {e}"

async def auto_buy_token(contract_address: str, token_name: str, buy_amount_sol: float, slippage_tolerance_percent: float):
    """Belirtilen kontrat adresindeki token'ı otomatik olarak satın alır."""
    if not solana_client or not payer_keypair:
        logger.error("Auto-buy skipped: Solana client or wallet not initialized.")
        return False, "Wallet not ready.", None, None

    # Kontratın daha önce işlenip işlenmediğini kontrol et (tekrar alımı engellemek için)
    if await is_contract_processed(contract_address):
        logger.info(f"Contract {contract_address} already processed for auto-buy. Skipping.")
        return False, "Contract already processed.", None, None

    input_mint = Pubkey("So11111111111111111111111111111111111111112") # SOL mint address
    output_mint = Pubkey(contract_address)
    amount_in_lamports = int(buy_amount_sol * 10**9) # SOL'u lamports'a çevir
    slippage_bps = int(slippage_tolerance_percent * 100) # Yüzdeyi basis points'e çevir (örn: %5 -> 500)

    logger.info(f"Attempting to auto-buy {contract_address} ({token_name}) with {buy_amount_sol} SOL and {slippage_tolerance_percent}% slippage.")

    quote_data = await get_swap_quote(input_mint, output_mint, amount_in_lamports, slippage_bps)
    if not quote_data:
        logger.error(f"Failed to get swap quote for {contract_address}.")
        return False, "Failed to get swap quote.", None, None

    success, tx_signature = await perform_swap(quote_data)
    if success:
        await record_processed_contract(contract_address) # Kontratı işlenmiş olarak kaydet

        # Satın alınan token miktarını ve gerçek alım fiyatını hesapla
        # quote_data'dan outputToken'ın decimal'ını almalıyız
        output_token_decimals = quote_data.get('outputToken', {}).get('decimals')
        if output_token_decimals is None:
            logger.warning(f"Could not determine decimals for {token_name}. Cannot calculate bought amount.")
            bought_amount_token = 0.0
            actual_buy_price_sol = 0.0
        else:
            bought_amount_token_lamports = int(quote_data['outAmount'])
            bought_amount_token = bought_amount_token_lamports / (10**output_token_decimals)
            actual_buy_price_sol = buy_amount_sol / bought_amount_token if bought_amount_token > 0 else 0.0

        logger.info(f"Successfully auto-bought token {contract_address}. Tx: {tx_signature}")
        return True, f"Successfully bought token {token_name}. Tx: {tx_signature}", actual_buy_price_sol, bought_amount_token
    else:
        logger.error(f"Failed to auto-buy token {contract_address}: {tx_signature}")
        return False, f"Failed to buy token {token_name}: {tx_signature}", None, None

async def auto_sell_token(contract_address: str, token_name: str, amount_to_sell_token: float, slippage_tolerance_percent: float):
    """Belirtilen token'ı otomatik olarak satar."""
    if not solana_client or not payer_keypair:
        logger.error("Auto-sell skipped: Solana client or wallet not initialized.")
        return False, "Wallet not ready."

    input_mint = Pubkey(contract_address)
    output_mint = Pubkey("So11111111111111111111111111111111111111112") # SOL mint address
    slippage_bps = int(slippage_tolerance_percent * 100)

    # Token'ın decimal sayısını al
    token_info = await asyncio.to_thread(solana_client.get_token_supply, input_mint)
    if not token_info or 'result' not in token_info or 'value' not in token_info['result']:
        logger.warning(f"Could not get token supply info for {token_name}. Cannot determine decimals for selling.")
        return False, "Could not get token decimals."
    decimals = token_info['result']['value']['decimals']
    
    amount_in_lamports = int(amount_to_sell_token * (10**decimals)) # Token'ı kendi decimal'ına göre lamports'a çevir

    logger.info(f"Attempting to auto-sell {amount_to_sell_token} {token_name} ({contract_address}) with {slippage_tolerance_percent}% slippage.")

    quote_data = await get_swap_quote(input_mint, output_mint, amount_in_lamports, slippage_bps)
    if not quote_data:
        logger.error(f"Failed to get swap quote for selling {token_name}.")
        return False, "Failed to get swap quote for selling."

    success, tx_signature = await perform_swap(quote_data)
    if success:
        logger.info(f"Successfully auto-sold token {token_name}. Tx: {tx_signature}")
        return True, f"Successfully sold token {token_name}. Tx: {tx_signature}"
    else:
        logger.error(f"Failed to auto-sell token {token_name}: {tx_signature}")
        return False, f"Failed to sell token {token_name}: {tx_signature}"

async def monitor_positions_task():
    """Açık pozisyonları izler ve kar/zarar hedeflerine göre otomatik satım yapar."""
    while True:
        await asyncio.sleep(30) # Her 30 saniyede bir kontrol et

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

            current_price_sol = await get_current_token_price_sol(Pubkey(contract_address)) # Düzeltme: Pubkey kullanıldı
            if current_price_sol is None:
                logger.warning(f"Could not get current price for {token_name}. Skipping monitoring for this position.")
                continue

            # Kar/Zarar hesaplaması
            current_value_sol = current_price_sol * buy_amount_token
            initial_value_sol = buy_price_sol * buy_amount_token # Bu aslında buy_amount_sol'a eşit olmalı

            # Kar hedefi kontrolü (örn: 5x = %400 kar)
            profit_threshold_price = buy_price_sol * target_profit_x
            
            # Stop-loss kontrolü (örn: %50 düşüş)
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

# --- Flask Web Sunucusu ---
pending_input = {} # Admin girdilerini bekleyen kullanıcıları takip etmek için

LOGIN_FORM = """<!doctype html>
<title>Login to Telegram</title>
<h2>Step 1: Enter your phone number</h2>
<form method="post">
  <input name="phone" placeholder="+1234567890" required>
  <button type="submit">Send Code</button>
</form>
"""

CODE_FORM = """<!doctype html>
<title>Enter the Code</title>
<h2>Step 2: Enter the code you received</h2>
<form method="post">
  <input name="code" placeholder="12345" required>
  <button type="submit">Verify</button>
</form>
"""

@app.route('/login', methods=['GET', 'POST'])
async def login():
    """Telegram kullanıcı oturumu için giriş sayfası."""
    if request.method == 'POST':
        phone = request.form.get('phone', '').strip()
        if not phone:
            return "<p>Phone number is required.</p>", 400
        session['phone'] = phone
        try:
            # user_client.connect() çağrısı, oturum dosyası yoksa veya geçersizse bağlantı kurmaya çalışır.
            # user_client.start() doğrudan input() çağırabilir, bu yüzden burada connect() kullanmak daha güvenli.
            await user_client.connect()
            await user_client.send_code_request(phone)
            logger.info(f"➡ Sent login code request to {phone}")
            return redirect('/submit-code')
        except Exception as e:
            logger.error(f"❌ Error sending login code to {phone}: {e}")
            return f"<p>Error sending code: {e}</p>", 500
    return render_template_string(LOGIN_FORM)

@app.route('/submit-code', methods=['GET', 'POST'])
async def submit_code():
    """Telegram giriş kodu doğrulama sayfası."""
    if 'phone' not in session:
        return redirect('/login')

    phone = session['phone']

    if request.method == 'POST':
        code = request.form.get('code', '').strip()
        if not code:
            return "<p>Code is required.</p>", 400
        try:
            # user_client zaten bağlı olmalı, sadece sign_in çağrısı yeterli
            await user_client.sign_in(phone, code)
            logger.info(f"✅ Logged in user-client for {phone}")
            session.pop('phone', None)
            return "<p>Login successful! You can close this tab.</p>"
        except Exception as e:
            logger.error(f"❌ Login failed for {phone}: {e}")
            return f"<p>Login failed: {e}</p>", 400

    return render_template_string(CODE_FORM)

@app.route('/')
def root():
    """Botun durumunu gösteren ana sayfa."""
    return jsonify(status="ok", message="Bot is running"), 200

@app.route('/health')
def health():
    """Botun sağlık kontrolü endpoint'i."""
    return jsonify(status="ok"), 200

# --- Telethon Admin Paneli İşleyicileri ---

@bot_client.on(events.CallbackQuery)
async def admin_callback_handler(event):
    """Admin panelindeki inline buton tıklamalarını işler."""
    uid = event.sender_id
    admins = await get_admins()
    if uid not in admins:
        logger.warning(f"Unauthorized callback query from user ID: {uid}")
        return await event.answer("❌ Yetkiniz yok.")

    data = event.data.decode()
    logger.info(f"Admin {uid} triggered callback: {data}")

    try:
        if data == 'admin_home':
            return await event.edit(await get_admin_dashboard(),
                                    buttons=await build_admin_keyboard(), link_preview=False)
        if data == 'admin_start':
            await set_bot_setting("bot_status", "running")
            await event.answer('▶ Bot başlatıldı.')
            return await event.edit(await get_admin_dashboard(),
                                    buttons=await build_admin_keyboard(), link_preview=False)
        if data == 'admin_pause':
            pending_input[uid] = {'action': 'pause'}
            kb = [[Button.inline("🔙 Geri", b"admin_home")]]
            return await event.edit("⏸ *Botu Duraklat*\n\nKaç dakika duraklatmalıyım?",
                                    buttons=kb, link_preview=False)
        if data == 'admin_stop':
            await set_bot_setting("bot_status", "stopped")
            await event.answer('🛑 Bot durduruldu.')
            return await event.edit("🛑 *Bot kapatıldı.*",
                                    buttons=[[Button.inline("🔄 Botu Başlat (çalışır duruma getir)", b"admin_start")],
                                             [Button.inline("🔙 Geri", b"admin_home")]],
                                    link_preview=False)
        
        # --- Otomatik Alım/Satım Ayarları ---
        if data == 'admin_auto_trade_settings':
            return await event.edit(await get_admin_dashboard(),
                                    buttons=await build_auto_trade_keyboard(), link_preview=False)
        
        # Otomatik Alım Ayarları
        if data == 'admin_enable_auto_buy':
            if not payer_keypair:
                await event.answer("❌ Solana özel anahtarı yapılandırılmamış. Otomatik alım etkinleştirilemez.", alert=True)
                return
            await set_bot_setting("auto_buy_enabled", "enabled")
            await event.answer('✅ Otomatik Alım Etkinleştirildi')
            return await event.edit(await get_admin_dashboard(),
                                    buttons=await build_auto_trade_keyboard(), link_preview=False)
        if data == 'admin_disable_auto_buy':
            await set_bot_setting("auto_buy_enabled", "disabled")
            await event.answer('❌ Otomatik Alım Devre Dışı Bırakıldı')
            return await event.edit(await get_admin_dashboard(),
                                    buttons=await build_auto_trade_keyboard(), link_preview=False)
        if data == 'admin_set_buy_amount':
            pending_input[uid] = {'action': 'set_buy_amount'}
            kb = [[Button.inline("🔙 Geri", b"admin_auto_trade_settings")]]
            current_amount = await get_bot_setting("buy_amount_sol")
            return await event.edit(f"💲 *Alım Miktarını Ayarla*\n\nMevcut miktar: `{current_amount} SOL`\n\nHer otomatik alım için harcanacak SOL miktarını girin (örn: `0.01`, `0.05`):",
                                    buttons=kb, link_preview=False)
        if data == 'admin_set_slippage':
            pending_input[uid] = {'action': 'set_slippage'}
            kb = [[Button.inline("🔙 Geri", b"admin_auto_trade_settings")]]
            current_slippage = await get_bot_setting("slippage_tolerance")
            return await event.edit(f"⚙️ *Kayma Toleransını Ayarla*\n\nMevcut kayma: `{current_slippage}%`\n\nKabul edilebilir maksimum fiyat kaymasını yüzde olarak girin (örn: `1`, `5`, `10`):",
                                    buttons=kb, link_preview=False)
        
        # Otomatik Satım Ayarları
        if data == 'admin_enable_auto_sell':
            await set_bot_setting("auto_sell_enabled", "enabled")
            await event.answer('✅ Otomatik Satım Etkinleştirildi')
            return await event.edit(await get_admin_dashboard(),
                                    buttons=await build_auto_trade_keyboard(), link_preview=False)
        if data == 'admin_disable_auto_sell':
            await set_bot_setting("auto_sell_enabled", "disabled")
            await event.answer('❌ Otomatik Satım Devre Dışı Bırakıldı')
            return await event.edit(await get_admin_dashboard(),
                                    buttons=await build_auto_trade_keyboard(), link_preview=False)
        if data == 'admin_set_profit_target':
            pending_input[uid] = {'action': 'set_profit_target'}
            kb = [[Button.inline("🔙 Geri", b"admin_auto_trade_settings")]]
            current_target = await get_bot_setting("profit_target_x")
            return await event.edit(f"📈 *Kar Hedefini Ayarla*\n\nMevcut hedef: `{current_target}x`\n\nToken'ın kaç katına çıktığında satılacağını girin (örn: `2.0` for 2x, `5.0` for 5x):",
                                    buttons=kb, link_preview=False)
        if data == 'admin_set_stop_loss':
            pending_input[uid] = {'action': 'set_stop_loss'}
            kb = [[Button.inline("🔙 Geri", b"admin_auto_trade_settings")]]
            current_stop_loss = await get_bot_setting("stop_loss_percent")
            return await event.edit(f"📉 *Stop-Loss Yüzdesini Ayarla*\n\nMevcut stop-loss: `{current_stop_loss}%`\n\nAlım fiyatından yüzde kaç düştüğünde satılacağını girin (örn: `10` for 10% drop, `50` for 50% drop):",
                                    buttons=kb, link_preview=False)
        
        # --- Admin Yönetimi ---
        if data == 'admin_admins':
            admins = await get_admins()
            kb = [
                [Button.inline("➕ Admin Ekle", b"admin_add_admin")],
            ]
            removable_admins = {aid: info for aid, info in admins.items() if aid != DEFAULT_ADMIN_ID and not info.get("is_default")}
            if removable_admins:
                kb.append([Button.inline("🗑 Admin Kaldır", b"admin_show_remove_admins")])
            kb.append([Button.inline("🔙 Geri", b"admin_home")])
            return await event.edit("👤 *Adminleri Yönet*", buttons=kb, link_preview=False)
        if data == 'admin_show_remove_admins':
            admins = await get_admins()
            kb = []
            for aid, info in admins.items():
                if aid != DEFAULT_ADMIN_ID and not info.get("is_default"):
                    kb.append([Button.inline(f"{info.get('first_name', 'N/A')} ({aid})", b"noop"),
                                 Button.inline("❌ Kaldır", f"remove_admin:{aid}".encode())])
            kb.append([Button.inline("🔙 Geri", b"admin_admins")])
            if not kb:
                return await event.edit("🗑 *Kaldırılabilir admin yok.*",
                                       buttons=[[Button.inline("🔙 Geri", b"admin_admins")]], link_preview=False)
            return await event.edit("🗑 *Kaldırılacak Admini Seç*", buttons=kb, link_preview=False)
        if data == 'admin_add_admin':
            pending_input[uid] = {'action': 'confirm_add_admin'}
            return await event.edit("➕ *Admin Ekle*\n\nEklenecek kullanıcı ID'sini gönderin:",
                                    buttons=[[Button.inline("🔙 Geri", b"admin_admins")]], link_preview=False)
        if data.startswith('remove_admin:'):
            aid = int(data.split(':')[1])
            await remove_admin(aid)
            await event.answer("✅ Admin kaldırıldı", alert=True)
            admins = await get_admins()
            kb = []
            for admin_id, info in admins.items():
                if admin_id != DEFAULT_ADMIN_ID and not info.get("is_default"):
                    kb.append([Button.inline(f"{info.get('first_name', 'N/A')} ({admin_id})", b"noop"),
                                 Button.inline("❌ Kaldır", f"remove_admin:{admin_id}".encode())])
            kb.append([Button.inline("🔙 Geri", b"admin_admins")])
            if not kb:
                return await event.edit("🗑 *Kaldırılabilir admin yok.*",
                                       buttons=[[Button.inline("🔙 Geri", b"admin_admins")]], link_preview=False)
            return await event.edit("🗑 *Kaldırılacak Admini Seç*", buttons=kb, link_preview=False)
        
        await event.answer("Bilinmeyen eylem.")

    except Exception as e:
        logger.error(f"Error in admin_callback_handler for user {uid}, data {data}: {e}")
        await event.answer("❌ Bir hata oluştu.")
        await event.edit(f"❌ Bir hata oluştu: {e}", buttons=[[Button.inline("🔙 Geri", b"admin_home")]], link_preview=False)

@bot_client.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    """Bot başlatıldığında veya /start komutu alındığında çalışır."""
    uid = event.sender_id
    admins = await get_admins()
    if uid not in admins:
        # Eğer admin yoksa, ilk kullanıcıyı varsayılan admin yap
        if not admins:
            await add_admin(uid, event.sender.first_name, event.sender.last_name, is_default=True)
            logger.info(f"Default admin set to: {uid}")
            await event.reply("🎉 Hoş geldiniz! Varsayılan admin olarak ayarlandınız. Botu yönetmek için `/admin` komutunu kullanın.")
        else:
            logger.warning(f"Unauthorized /start command from user ID: {uid}")
            return await event.reply("❌ Bu botu kullanmaya yetkiniz yok.")
    
    await event.reply(await get_admin_dashboard(), buttons=await build_admin_keyboard(), link_preview=False)

@bot_client.on(events.NewMessage(pattern='/admin'))
async def admin_handler(event):
    """/admin komutu alındığında admin panelini gösterir."""
    uid = event.sender_id
    admins = await get_admins()
    if uid not in admins:
        logger.warning(f"Unauthorized /admin command from user ID: {uid}")
        return await event.reply("❌ Admin paneline erişmeye yetkiniz yok.")
    
    await event.reply(await get_admin_dashboard(), buttons=await build_admin_keyboard(), link_preview=False)

async def get_admin_dashboard():
    """Admin paneli için gösterge tablosu metnini oluşturur."""
    bot_status = await get_bot_setting("bot_status")
    auto_buy_status = await get_bot_setting("auto_buy_enabled")
    buy_amount = await get_bot_setting("buy_amount_sol")
    slippage = await get_bot_setting("slippage_tolerance")
    auto_sell_status = await get_bot_setting("auto_sell_enabled")
    profit_target = await get_bot_setting("profit_target_x")
    stop_loss = await get_bot_setting("stop_loss_percent")

    dashboard_text = (
        "⚙️ *Admin Paneli*\n\n"
        f"🤖 Bot Durumu: *{bot_status.upper()}*\n"
        f"💰 Otomatik Alım: *{auto_buy_status.upper()}*\n"
        f"  - Alım Miktarı: `{buy_amount} SOL`\n"
        f"  - Kayma Toleransı: `{slippage}%`\n"
        f"📈 Otomatik Satım: *{auto_sell_status.upper()}*\n"
        f"  - Kar Hedefi: `{profit_target}x`\n"
        f"  - Stop-Loss: `{stop_loss}%`\n"
    )
    return dashboard_text

async def build_admin_keyboard():
    """Admin paneli ana klavyesini oluşturur."""
    bot_status = await get_bot_setting("bot_status")
    
    keyboard = [
        [Button.inline("👤 Adminler", b"admin_admins")],
        [Button.inline("📈 Otomatik Alım/Satım Ayarları", b"admin_auto_trade_settings")]
    ]
    
    if bot_status == "running":
        keyboard.append([Button.inline("⏸ Botu Duraklat", b"admin_pause"), Button.inline("🛑 Botu Durdur", b"admin_stop")])
    else:
        keyboard.append([Button.inline("▶ Botu Başlat", b"admin_start")])
    
    return keyboard

async def build_auto_trade_keyboard():
    """Otomatik alım/satım ayarları klavyesini oluşturur."""
    auto_buy_status = await get_bot_setting("auto_buy_enabled")
    auto_sell_status = await get_bot_setting("auto_sell_enabled")
    
    keyboard = []
    # Otomatik Alım Butonları
    if auto_buy_status == "enabled":
        keyboard.append([Button.inline("❌ Otomatik Alımı Devre Dışı Bırak", b"admin_disable_auto_buy")])
    else:
        keyboard.append([Button.inline("✅ Otomatik Alımı Etkinleştir", b"admin_enable_auto_buy")])
    
    keyboard.append([
        Button.inline("💲 Alım Miktarını Ayarla", b"admin_set_buy_amount"),
        Button.inline("⚙️ Kayma Toleransını Ayarla", b"admin_set_slippage")
    ])

    # Otomatik Satım Butonları
    if auto_sell_status == "enabled":
        keyboard.append([Button.inline("❌ Otomatik Satımı Devre Dışı Bırak", b"admin_disable_auto_sell")])
    else:
        keyboard.append([Button.inline("✅ Otomatik Satımı Etkinleştir", b"admin_enable_auto_sell")])
    
    keyboard.append([
        Button.inline("📈 Kar Hedefini Ayarla", b"admin_set_profit_target"),
        Button.inline("📉 Stop-Loss Ayarla", b"admin_set_stop_loss")
    ])

    keyboard.append([Button.inline("🔙 Geri", b"admin_home")])
    
    return keyboard

@bot_client.on(events.NewMessage)
async def handle_admin_input(event):
    """Adminlerden gelen metin girdilerini işler (ayarları değiştirmek için)."""
    uid = event.sender_id
    admins = await get_admins()
    if uid not in admins:
        return # Sadece adminler için

    if uid in pending_input:
        action_data = pending_input[uid]
        action = action_data['action']
        text_input = event.message.text

        if action == 'pause':
            try:
                minutes = int(text_input)
                await set_bot_setting("bot_status", f"paused:{time.time() + minutes*60}")
                await event.reply(f"✅ Bot {minutes} dakika duraklatıldı.")
                logger.info(f"Bot paused by admin {uid} for {minutes} minutes.")
            except ValueError:
                await event.reply("❌ Geçersiz giriş. Lütfen dakika için bir sayı girin.")
            finally:
                del pending_input[uid]
            return await event.reply(await get_admin_dashboard(),
                                     buttons=await build_admin_keyboard(), link_preview=False)
        
        elif action == 'confirm_add_admin':
            try:
                new_admin_id = int(text_input)
                # Yeni adminin bilgilerini almak için user_client kullan
                user_entity = await user_client.get_entity(new_admin_id)
                await add_admin(new_admin_id, user_entity.first_name, user_entity.last_name or "")
                await event.reply(f"✅ Admin {new_admin_id} eklendi.")
                logger.info(f"Admin {uid} added new admin {new_admin_id}.")
            except ValueError:
                await event.reply("❌ Geçersiz kullanıcı ID'si. Lütfen sayısal bir kullanıcı ID'si girin.")
            except Exception as e:
                await event.reply(f"❌ Admin eklenirken hata oluştu: {e}")
                logger.error(f"Error adding admin {text_input}: {e}")
            finally:
                del pending_input[uid]
            return await event.reply(await get_admin_dashboard(),
                                     buttons=await build_admin_keyboard(), link_preview=False)
        
        elif action == 'set_buy_amount':
            try:
                amount = float(text_input)
                if amount <= 0:
                    raise ValueError("Miktar pozitif olmalı.")
                await set_bot_setting("buy_amount_sol", str(amount))
                await event.reply(f"✅ Otomatik alım miktarı `{amount} SOL` olarak ayarlandı.")
                logger.info(f"Admin {uid} set auto-buy amount to {amount} SOL.")
            except ValueError:
                await event.reply("❌ Geçersiz miktar. Lütfen pozitif bir sayı girin (örn: `0.01`, `0.5`).")
            finally:
                del pending_input[uid]
            return await event.reply(await get_admin_dashboard(),
                                     buttons=await build_auto_trade_keyboard(), link_preview=False)
        
        elif action == 'set_slippage':
            try:
                slippage = float(text_input)
                if not (0 <= slippage <= 100): # Slippage %0 ile %100 arasında olmalı
                    raise ValueError("Kayma toleransı 0 ile 100 arasında olmalı.")
                await set_bot_setting("slippage_tolerance", str(slippage))
                await event.reply(f"✅ Kayma toleransı `{slippage}%` olarak ayarlandı.")
                logger.info(f"Admin {uid} set slippage tolerance to {slippage}%.")
            finally:
                del pending_input[uid]
            return await event.reply(await get_admin_dashboard(),
                                     buttons=await build_auto_trade_keyboard(), link_preview=False)
        
        elif action == 'set_profit_target':
            try:
                target_x = float(text_input)
                if target_x <= 1.0:
                    raise ValueError("Kar hedefi 1.0'dan büyük olmalı (örn: 2.0 for 2x).")
                await set_bot_setting("profit_target_x", str(target_x))
                await event.reply(f"✅ Kar hedefi `{target_x}x` olarak ayarlandı.")
                logger.info(f"Admin {uid} set profit target to {target_x}x.")
            finally:
                del pending_input[uid]
            return await event.reply(await get_admin_dashboard(),
                                     buttons=await build_auto_trade_keyboard(), link_preview=False)
        
        elif action == 'set_stop_loss':
            try:
                stop_loss = float(text_input)
                if not (0 <= stop_loss < 100): # Stop-loss %0 ile %100 arasında olmalı, %100 olmamalı
                    raise ValueError("Stop-loss 0 ile 100 arasında olmalı (100 hariç).")
                await set_bot_setting("stop_loss_percent", str(stop_loss))
                await event.reply(f"✅ Stop-loss `{stop_loss}%` olarak ayarlandı.")
                logger.info(f"Admin {uid} set stop-loss to {stop_loss}%.")
            finally:
                del pending_input[uid]
            return await event.reply(await get_admin_dashboard(),
                                     buttons=await build_auto_trade_keyboard(), link_preview=False)

# --- Telegram Mesaj İşleyici (Sinyal Kanalı) ---

@user_client.on(events.NewMessage(chats=SOURCE_CHANNEL_ID))
async def handle_incoming_signal(event):
    """Belirlenen kaynak kanaldan gelen yeni mesajları işler."""
    message_text = event.message.text
    if not message_text:
        logger.debug("Boş mesaj metni alındı. Atlanıyor.")
        return

    logger.info(f"Kaynak kanaldan mesaj alındı {event.chat_id}: {message_text[:100]}...")

    # Botun genel çalışma durumu kontrolü
    bot_status = await get_bot_setting("bot_status")
    if bot_status == "stopped":
        logger.info("Bot durduruldu. Mesaj işleme atlanıyor.")
        return
    if bot_status.startswith("paused"):
        pause_until_timestamp = float(bot_status.split(":")[1])
        if time.time() < pause_until_timestamp:
            logger.info("Bot duraklatıldı. Mesaj işleme atlanıyor.")
            return
        else:
            # Duraklatma süresi doldu, botu tekrar çalışır duruma getir
            await set_bot_setting("bot_status", "running")
            logger.info("Bot duraklatması sona erdi. İşlemler devam ediyor.")

    contract_address = extract_contract(message_text)
    token_name = extract_token_name_from_message(message_text) # Token adını da çıkar

    if contract_address:
        logger.info(f"Kontrat adresi bulundu: {contract_address}. Otomatik alım başlatılıyor.")
        
        # Otomatik Alım İşlemi
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
                logger.error("Otomatik alım/satım ayarları geçersiz. Lütfen kontrol edin.")
                await bot_client.send_message(DEFAULT_ADMIN_ID, "❌ Otomatik alım/satım ayarları geçersiz. Lütfen kontrol edin.")
                return

            # Otomatik alım işlemini başlat
            success, result_message, actual_buy_price_sol, bought_amount_token = await auto_buy_token(
                contract_address, token_name, buy_amount_sol, slippage_tolerance_percent
            )
            
            admin_message = f"💰 Otomatik Alım Durumu: {result_message}"
            await bot_client.send_message(DEFAULT_ADMIN_ID, admin_message)
            
            if success:
                # Başarılı alımdan sonra pozisyonu kaydet
                await add_open_position(
                    contract_address, 
                    token_name, 
                    actual_buy_price_sol, 
                    bought_amount_token, 
                    result_message.split("Tx: ")[1] if "Tx: " in result_message else "N/A", # İşlem imzasını al
                    profit_target_x, 
                    stop_loss_percent
                )
                logger.info(f"Token {token_name} için açık pozisyon kaydedildi.")
            else:
                logger.warning(f"Otomatik alım {contract_address} için başarısız oldu: {result_message}")
        else:
            logger.info(f"Otomatik alım devre dışı. {contract_address} için alım denemesi yapılmıyor.")
    else:
        logger.debug(f"Mesajda kontrat adresi bulunamadı {event.chat_id}.")

# --- Bot Başlatma ---

async def main():
    """Botu başlatır, veritabanını ve Solana istemcisini başlatır."""
    await init_db()
    await init_solana_client() # Solana client'ı başlat

    # Varsayılan admini ekle (sadece ilk çalıştırmada veya admin yoksa)
    admins = await get_admins()
    if not admins:
        await add_admin(DEFAULT_ADMIN_ID, "Default", "Admin", is_default=True)
        logger.info(f"Varsayılan admin {DEFAULT_ADMIN_ID} eklendi.")
    
    # Varsayılan bot ayarlarını yükle/ayarla
    for setting_key, default_value in DEFAULT_BOT_SETTINGS.items():
        current_value = await get_bot_setting(setting_key)
        if current_value is None:
            await set_bot_setting(setting_key, default_value)
            logger.info(f"Varsayılan ayar {setting_key} -> {default_value} olarak ayarlandı.")
        else:
            logger.info(f"Ayar {setting_key} zaten mevcut: {current_value}")

    logger.info("Telegram istemcileri bağlanıyor...")
    # user_client'ın oturum dosyasını yüklemeye çalışın, yoksa web arayüzünden giriş yapılması gerekecek.
    # Bu, EOFError'ı önlemek için önemlidir.
    try:
        await user_client.start() 
        logger.info("Kullanıcı istemcisi başarıyla bağlandı veya oturum yüklendi.")
    except Exception as e:
        logger.warning(f"Kullanıcı istemcisi başlatılırken hata oluştu (muhtemelen oturum dosyası yok): {e}")
        logger.warning("Lütfen botunuzun web arayüzüne giderek (Render URL'nizin sonuna /login ekleyerek) Telegram hesabınızla giriş yapın.")
        # Botun diğer kısımlarının çalışmaya devam etmesi için burada hata fırlatmayın,
        # ancak kullanıcıya web arayüzünden giriş yapması gerektiğini bildirin.

    await bot_client.start(bot_token=BOT_TOKEN) # Bot oturumu ile başla (mesaj göndermek için)
    logger.info("Telegram bot istemcisi bağlandı.")

    # Botun kendisiyle ilgili bilgileri logla
    me_bot = await bot_client.get_me()
    me_user = None
    try:
        me_user = await user_client.get_me()
    except Exception as e:
        logger.warning(f"Kullanıcı istemcisi bilgileri alınamadı (belki henüz giriş yapılmadı): {e}")

    logger.info(f"Otomatik Alım/Satım Botu: @{me_bot.username} ({me_bot.id})")
    if me_user:
        logger.info(f"Kullanıcı İstemcisi (kanalları okumak için): @{me_user.username} ({me_user.id})")
    else:
        logger.info("Kullanıcı İstemcisi henüz bağlı değil.")


    logger.info(f"Otomatik Alım/Satım Botu şu anda kanal ID'sini dinliyor: {SOURCE_CHANNEL_ID}")
    logger.info(f"Otomatik Alım Miktarı: {await get_bot_setting('buy_amount_sol')} SOL")
    logger.info(f"Kayma Toleransı: {await get_bot_setting('slippage_tolerance')}%")
    logger.info(f"Kar Hedefi: {await get_bot_setting('profit_target_x')}x")
    logger.info(f"Stop-Loss: {await get_bot_setting('stop_loss_percent')}%")

    # Pozisyon izleme görevini başlat
    asyncio.create_task(monitor_positions_task())
    logger.info("Pozisyon izleme görevi başlatıldı.")

    # Flask uygulamasını ayrı bir thread'de çalıştır
    def run_flask():
        app.run(host='0.0.0.0', port=os.environ.get('PORT', 5000))

    flask_thread = threading.Thread(target=run_flask)
    flask_thread.start()
    logger.info("Flask web sunucusu başlatıldı.")

    logger.info("Bot çalışıyor. Durdurmak için Ctrl+C tuşlarına basın.")
    # user_client'ın bağlantısının kesilmesini bekleyin, ancak eğer zaten bağlı değilse hata vermeyin.
    if user_client.is_connected():
        await user_client.run_until_disconnected()
    await bot_client.run_until_disconnected()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot kullanıcı tarafından durduruldu.")
    except Exception as e:
        logger.critical(f"Beklenmeyen bir hata oluştu: {e}", exc_info=True)
