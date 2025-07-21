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
import base64
from cachetools import TTLCache

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
from solders.signature import Signature

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

app = Flask(__name__)
app.secret_key = SECRET_KEY

# --- Telethon İstemcileri ---
bot_client = TelegramClient('auto_buy_bot_session', API_ID, API_HASH)

# --- Solana İstemci ve Cüzdan Başlatma ---
solana_client = None
payer_keypair = None

# Denenecek RPC uç noktaları listesi
RPC_ENDPOINTS = [
    "https://mainnet.helius-rpc.com/?api-key=7930dbab-e806-4f3f-bf3b-716a14c6e3c3",
    "https://api.mainnet-beta.solana.com",
    "https://solana-rpc.web3auth.io",
    "https://ssc-dao.genesysgo.net",
    "https://rpc.ankr.com/solana",
    "https://solana-mainnet.rpc.extrnode.com"
]

# Aktif RPC URL'sini tutmak için global değişken
active_rpc_url = None

# Jupiter API çağrıları için global kilit ve rate limit
jupiter_api_lock = asyncio.Lock()
last_jupiter_call_time = 0
JUPITER_RATE_LIMIT_DELAY = 5.0  # Artırıldı: 5 saniye bekleme

# Jupiter API çağrıları için Semaphore
JUPITER_SEMAPHORE = asyncio.Semaphore(1)  # Tek eşzamanlı istek

# Jupiter teklifleri için cache
QUOTE_CACHE = TTLCache(maxsize=100, ttl=300)

# Sözleşme işleme için kilit
contract_processing_lock = asyncio.Lock()

async def get_healthy_client():
    """
    Sağlıklı bir Solana RPC uç noktasına bağlanmaya çalışır.
    """
    global active_rpc_url
    for url in RPC_ENDPOINTS:
        try:
            logger.info(f"RPC URL test ediliyor: {url}")
            client = Client(url)
            block_height_resp = await asyncio.to_thread(client.get_block_height)
            if isinstance(block_height_resp, GetBlockHeightResp) and block_height_resp.value is not None and block_height_resp.value > 0:
                logger.info(f"Sağlıklı RPC'ye bağlandı: {url}. Blok yüksekliği: {block_height_resp.value}")
                active_rpc_url = url
                return client
            else:
                logger.warning(f"RPC {url} sağlıksız görünüyor: {block_height_resp}")
        except Exception as e:
            logger.warning(f"RPC {url} bağlantısı başarısız: {str(e)}")
    logger.error("Tüm RPC uç noktaları başarısız oldu.")
    active_rpc_url = None
    return None

async def get_balance_with_retry(pubkey: Pubkey, retries=3):
    """
    Solana bakiyesini yeniden deneme mekanizmasıyla alır.
    """
    for i in range(retries):
        try:
            resp = await asyncio.to_thread(solana_client.get_balance, pubkey, commitment="confirmed")
            if isinstance(resp, GetBalanceResp):
                return resp.value
            else:
                logger.warning(f"get_balance için beklenmeyen yanıt: {resp}. Deneme {i+1}/{retries}")
        except Exception as e:
            logger.warning(f"Bakiye kontrol denemesi {i+1}/{retries} başarısız: {e}")
            await asyncio.sleep(1)
    return None

async def check_wallet_balance():
    """
    Cüzdanın SOL bakiyesini kontrol eder.
    """
    if not solana_client or not payer_keypair:
        logger.error("Solana istemcisi veya ödeme anahtarı başlatılmadı.")
        return None
    try:
        balance_lamports = await get_balance_with_retry(payer_keypair.pubkey())
        if balance_lamports is None:
            logger.error("Cüzdan bakiyesi alınamadı.")
            return None
        return balance_lamports / 10**9
    except Exception as e:
        logger.error(f"Bakiye kontrol hatası: {str(e)}", exc_info=True)
        return None

async def init_solana_client():
    """Solana RPC istemcisini ve cüzdanı başlatır."""
    global solana_client, payer_keypair
    solana_client = None
    payer_keypair = None
    try:
        client_temp = await get_healthy_client()
        if not client_temp:
            logger.critical("Sağlıklı RPC bulunamadı!")
            return
        priv_key = await get_bot_setting("SOLANA_PRIVATE_KEY")
        if not priv_key:
            logger.error("Özel anahtar ayarlanmamış!")
            return
        try:
            keypair_temp = Keypair.from_base58_string(priv_key)
            solana_client = client_temp
            payer_keypair = keypair_temp
            logger.info(f"Cüzdan başlatıldı: {payer_keypair.pubkey()}")
            logger.info(f"Aktif RPC URL'si: {active_rpc_url}")
            balance = await check_wallet_balance()
            logger.info(f"Başlangıç bakiyesi: {balance if balance is not None else 'Alınamadı'} SOL")
        except Exception as e:
            logger.error(f"Özel anahtardan ödeme anahtar çifti başlatılırken hata: {e}", exc_info=True)
            solana_client = None
            payer_keypair = None
    except Exception as e:
        logger.error(f"Solana istemcisi başlatma hatası: {str(e)}", exc_info=True)

# --- Veritabanı Bağlantısı ve Yönetim Fonksiyonları ---
def get_connection():
    try:
        return psycopg2.connect(
            dbname=DB_NAME, user=DB_USER, password=DB_PASS, host=DB_HOST, port=DB_PORT, sslmode="require"
        )
    except psycopg2.OperationalError as e:
        logger.error(f"Veritabanı bağlantısı başarısız: {e}")
        raise e

def init_db_sync():
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
                CREATE TABLE IF NOT EXISTS bot_settings (
                    setting_key TEXT PRIMARY KEY,
                    setting_value TEXT
                );
                CREATE TABLE IF NOT EXISTS processed_contracts (
                    contract_address TEXT PRIMARY KEY,
                    timestamp REAL NOT NULL
                );
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
            if setting in ["SOLANA_PRIVATE_KEY", "JUPITER_API_KEY"]:
                logger.info(f"Bot ayarı '{setting}' ayarlandı. Değer maskelendi.")
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
            cur.execute("SELECT 1 FROM processed_contracts WHERE contract_address = %s", (contract_address,))
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
            cur.execute("SELECT * FROM transaction_history ORDER BY timestamp DESC LIMIT 20")
            return cur.fetchall()
    except Exception as e:
        logger.error(f"İşlem geçmişi alınırken hata: {e}")
        return []
    finally:
        if conn:
            conn.close()

# --- Asenkron Veritabanı Fonksiyonları ---
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

# --- Varsayılan Ayarlar ---
DEFAULT_ADMIN_ID = int(os.environ.get("DEFAULT_ADMIN_ID", "YOUR_TELEGRAM_USER_ID"))
DEFAULT_BOT_SETTINGS = {
    "bot_status": "running",
    "auto_buy_enabled": "enabled",
    "buy_amount_sol": "0.01",  # Daha düşük bir miktar, test için
    "slippage_tolerance": "5",
    "auto_sell_enabled": "enabled",
    "profit_target_x": "5.0",
    "stop_loss_percent": "50.0",
    "SOLANA_PRIVATE_KEY": os.environ.get("SOLANA_PRIVATE_KEY", ""),
    "JUPITER_API_KEY": os.environ.get("JUPITER_API_KEY", "")
}

# --- Loglama Kurulumu ---
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

# --- Telethon Yardımcı Fonksiyonları ---
async def retry_telethon_call(coro, max_retries=5, base_delay=1.0):
    for i in range(max_retries):
        try:
            return await coro
        except Exception as e:
            logger.warning(f"Telethon çağrısı için yeniden deneme {i+1}/{max_retries} hatası: {e}")
            if i < max_retries - 1:
                delay = base_delay * (2 ** i) + random.uniform(0, 1)
                await asyncio.sleep(delay)
            else:
                logger.error(f"Telethon çağrısı için maksimum yeniden deneme sayısına ulaşıldı: {e}")
                raise
    raise RuntimeError("Yeniden deneme mantığı başarısız oldu.")

def extract_contract(text: str) -> str | None:
    m = re.findall(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b", text)
    return m[0] if m else None

def extract_token_name_from_message(text: str) -> str:
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

# --- Solana Otomatik Alım Fonksiyonları ---
async def get_current_token_price_sol(token_mint_str: str, amount_token_to_check: float = 0.000000001, max_retries=7, initial_delay=3.0):
    global last_jupiter_call_time
    if not solana_client:
        logger.error("Solana istemcisi başlatılmadı. Token fiyatı alınamıyor.")
        return None
    async with JUPITER_SEMAPHORE:
        async with jupiter_api_lock:
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
                    token_info = await asyncio.to_thread(solana_client.get_token_supply, input_mint)
                    if not token_info or not hasattr(token_info, 'value') or not hasattr(token_info.value, 'decimals'):
                        logger.warning(f"{token_mint_str} için token arz bilgisi alınamadı.")
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
                    last_jupiter_call_time = time.time()
                    return price_sol_per_token
                except requests.exceptions.RequestException as e:
                    logger.warning(f"Jupiter'den token fiyatı alınırken hata (deneme {attempt+1}/{max_retries}): {e}")
                    if e.response is not None and e.response.status_code == 429:
                        delay = initial_delay * (2 ** attempt) + random.uniform(0, 1)
                        logger.info(f"429 hatası, {delay:.2f} saniye beklenecek.")
                        await bot_client.send_message(
                            DEFAULT_ADMIN_ID,
                            f"⚠️ **Jupiter API Rate Limit Aşıldı!**\nSon hata: `{e}`\nBot {delay:.2f} saniye bekleyecek.",
                            parse_mode='md'
                        )
                        await asyncio.sleep(delay)
                    elif attempt < max_retries - 1:
                        await asyncio.sleep(initial_delay * (2 ** attempt) + random.uniform(0, 1))
                    else:
                        logger.error(f"Jupiter'den token fiyatı alınırken maksimum yeniden deneme sayısına ulaşıldı: {e}")
                        return None
                except Exception as e:
                    logger.error(f"get_current_token_price_sol içinde beklenmeyen hata: {e}", exc_info=True)
                    return None
            last_jupiter_call_time = time.time()
    return None

async def get_swap_quote(input_mint: Pubkey, output_mint: Pubkey, amount_in_lamports: int, slippage_bps: int, max_retries=7, initial_delay=3.0):
    global last_jupiter_call_time
    if not solana_client or not payer_keypair:
        logger.error("Solana istemcisi veya ödeme anahtarı başlatılmadı. Teklif alınamıyor.")
        return None
    cache_key = f"quote_{input_mint}_{output_mint}_{amount_in_lamports}_{slippage_bps}"
    if cache_key in QUOTE_CACHE:
        logger.debug(f"Cache'ten teklif alındı: {cache_key}")
        return QUOTE_CACHE[cache_key]
    async with JUPITER_SEMAPHORE:
        async with jupiter_api_lock:
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
                    logger.info(f"Jupiter teklifi {input_mint}'ten {output_mint}'e alındı: {quote_data.get('outAmount')}")
                    last_jupiter_call_time = time.time()
                    QUOTE_CACHE[cache_key] = quote_data
                    return quote_data
                except requests.exceptions.RequestException as e:
                    logger.warning(f"Jupiter teklifi alınırken hata (deneme {attempt+1}/{max_retries}): {e}")
                    if e.response is not None and e.response.status_code == 429:
                        delay = initial_delay * (2 ** attempt) + random.uniform(0, 1)
                        logger.info(f"429 hatası, {delay:.2f} saniye beklenecek.")
                        await bot_client.send_message(
                            DEFAULT_ADMIN_ID,
                            f"⚠️ **Jupiter API Rate Limit Aşıldı!**\nSon hata: `{e}`\nBot {delay:.2f} saniye bekleyecek.",
                            parse_mode='md'
                        )
                        await asyncio.sleep(delay)
                    elif attempt < max_retries - 1:
                        await asyncio.sleep(initial_delay * (2 ** attempt) + random.uniform(0, 1))
                    else:
                        logger.error(f"Jupiter teklifi alınırken maksimum yeniden deneme sayısına ulaşıldı: {e}")
                        return None
                except Exception as e:
                    logger.error(f"get_swap_quote içinde beklenmeyen hata: {e}", exc_info=True)
                    return None
            last_jupiter_call_time = time.time()
    return None

async def get_swap_transaction(quote_response: dict, payer_pubkey: Pubkey, max_retries=7, initial_delay=3.0):
    global last_jupiter_call_time
    if not solana_client or not payer_keypair:
        logger.error("Solana istemcisi veya ödeme anahtarı başlatılmadı.")
        return None
    async with JUPITER_SEMAPHORE:
        async with jupiter_api_lock:
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
                        "prioritizationFeeLamports": "auto"
                    }
                    response = requests.post(swap_url, headers=headers, json=payload)
                    response.raise_for_status()
                    swap_data = response.json()
                    if not swap_data or "swapTransaction" not in swap_data:
                        logger.error(f"Geçersiz takas işlemi verisi: {swap_data}. Deneme {attempt+1}/{max_retries}")
                        if attempt < max_retries - 1:
                            await asyncio.sleep(initial_delay * (2 ** attempt) + random.uniform(0, 1))
                        continue
                    logger.info("Jupiter'den takas işlemi başarıyla alındı.")
                    last_jupiter_call_time = time.time()
                    return swap_data
                except requests.exceptions.RequestException as e:
                    logger.warning(f"Jupiter takas işlemi alınırken hata (deneme {attempt+1}/{max_retries}): {e}")
                    if e.response is not None and e.response.status_code == 429:
                        delay = initial_delay * (2 ** attempt) + random.uniform(0, 1)
                        logger.info(f"429 hatası, {delay:.2f} saniye beklenecek.")
                        await bot_client.send_message(
                            DEFAULT_ADMIN_ID,
                            f"⚠️ **Jupiter API Rate Limit Aşıldı!**\nSon hata: `{e}`\nBot {delay:.2f} saniye bekleyecek.",
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
            last_jupiter_call_time = time.time()
    return None

async def send_and_confirm_transaction(transaction_base64: str, max_retries=5, initial_delay=5.0):
    """
    Serileştirilmiş bir işlemi Solana ağına gönderir ve onaylanmasını bekler.
    """
    if not solana_client or not payer_keypair:
        logger.error("Solana istemcisi veya ödeme anahtarı başlatılmadı.")
        return None, "Solana istemcisi başlatılmadı."
    try:
        tx_bytes = base64.b64decode(transaction_base64)
        transaction = VersionedTransaction.from_bytes(tx_bytes)
        for i in range(max_retries):
            try:
                # RPC sağlığını kontrol et
                block_height_resp = await asyncio.to_thread(solana_client.get_block_height)
                if not isinstance(block_height_resp, GetBlockHeightResp) or block_height_resp.value is None:
                    logger.warning(f"RPC sağlıksız görünüyor: {active_rpc_url}. Yeni RPC deneniyor.")
                    await init_solana_client()
                    if not solana_client:
                        logger.error("Sağlıklı RPC bulunamadı.")
                        return None, "Sağlıklı RPC bulunamadı."
                # İşlemi gönder
                tx_signature = await asyncio.to_thread(
                    solana_client.send_raw_transaction,
                    tx_bytes,
                    opts=TxOpts(skip_preflight=False, max_retries=3)
                )
                logger.info(f"İşlem gönderildi: {tx_signature}")
                # Onay bekle
                confirmation = await asyncio.to_thread(
                    solana_client.confirm_transaction,
                    tx_signature,
                    commitment="confirmed"
                )
                if confirmation.value and confirmation.value[0].err:
                    logger.error(f"İşlem hatayla başarısız oldu: {confirmation.value[0].err}")
                    return None, f"İşlem başarısız: {confirmation.value[0].err}"
                logger.info(f"İşlem onaylandı: {tx_signature}")
                return str(tx_signature), "success"
            except RPCException as e:
                logger.warning(f"İşlem gönderme/onaylama denemesi {i+1}/{max_retries} başarısız: {e}")
                if str(e).find("TransactionSignatureVerificationFailure") != -1 and i < max_retries - 1:
                    logger.info("İmza doğrulama hatası alındı. Yeniden deneniyor.")
                    await asyncio.sleep(initial_delay * (2 ** i) + random.uniform(0, 1))
                elif i < max_retries - 1:
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

async def perform_swap(quote_data: dict):
    """Jupiter Aggregator'dan alınan teklifle bir takas işlemi gerçekleştirir."""
    global last_jupiter_call_time
    if not solana_client or not payer_keypair:
        logger.error("Solana istemcisi veya ödeme anahtarı başlatılmadı.")
        return False, "Solana istemcisi veya cüzdan hazır değil.", None
    async with JUPITER_SEMAPHORE:
        async with jupiter_api_lock:
            elapsed_time = time.time() - last_jupiter_call_time
            if elapsed_time < JUPITER_RATE_LIMIT_DELAY:
                await asyncio.sleep(JUPITER_RATE_LIMIT_DELAY - elapsed_time)
            max_swap_retries = 3
            for attempt in range(max_swap_retries):
                try:
                    logger.info(f"Takas işlemi için deneme {attempt + 1}/{max_swap_retries}")
                    swap_url = f"{JUPITER_API_URL}/swap"
                    headers = {"Content-Type": "application/json"}
                    jupiter_api_key = await get_bot_setting("JUPITER_API_KEY")
                    if jupiter_api_key:
                        headers["Authorization"] = f"Bearer {jupiter_api_key}"
                    swap_response = requests.post(swap_url, json={
                        "quoteResponse": quote_data,
                        "userPublicKey": str(payer_keypair.pubkey()),
                        "wrapUnwrapSOL": True,
                        "prioritizationFeeLamports": "auto"
                    }, headers=headers)
                    swap_response.raise_for_status()
                    swap_data = swap_response.json()
                    if not swap_data or "swapTransaction" not in swap_data:
                        logger.error(f"Jupiter'den geçersiz takas verisi alındı: {swap_data}")
                        return False, "Geçersiz takas işlem verisi.", None
                    swap_transaction_str = swap_data.get("swapTransaction")
                    if not isinstance(swap_transaction_str, str):
                        error_msg = f"Jupiter API 'swapTransaction'ı dize olarak döndürmedi. Tür: {type(swap_transaction_str)}"
                        logger.error(error_msg)
                        return False, error_msg, None
                    try:
                        tx_bytes = base64.b64decode(swap_transaction_str)
                    except base64.binascii.Error as e:
                        logger.error(f"Base64 çözme hatası: {e}, swapTransaction: {swap_transaction_str[:50]}...")
                        return False, f"Base64 çözme hatası: {e}", None
                    block_height_resp = await asyncio.to_thread(solana_client.get_block_height)
                    if not isinstance(block_height_resp, GetBlockHeightResp) or block_height_resp.value is None:
                        logger.warning(f"RPC sağlıksız görünüyor: {active_rpc_url}. Yeni RPC deneniyor.")
                        await init_solana_client()
                        if not solana_client:
                            logger.error("Sağlıklı RPC bulunamadı.")
                            return False, "Sağlıklı RPC bulunamadı.", None
                    tx_signature, status = await send_and_confirm_transaction(swap_transaction_str)
                    if status != "success" or not tx_signature:
                        logger.error(f"Takas işlemi başarısız oldu: {status}")
                        if str(status).find("TransactionSignatureVerificationFailure") != -1 and attempt < max_swap_retries - 1:
                            logger.info("İmza doğrulama hatası alındı. Yeni swapTransaction talep ediliyor.")
                            break
                        return False, f"Takas işlemi başarısız: {status}", None
                    logger.info(f"İşlem onaylandı: {tx_signature}")
                    last_jupiter_call_time = time.time()
                    return True, tx_signature, quote_data
                except requests.exceptions.RequestException as e:
                    logger.error(f"Jupiter ile takas yapılırken hata: {e}")
                    if e.response is not None and e.response.status_code == 429:
                        delay = initial_delay * (2 ** attempt) + random.uniform(0, 1)
                        logger.info(f"429 hatası, {delay:.2f} saniye beklenecek.")
                        await bot_client.send_message(
                            DEFAULT_ADMIN_ID,
                            f"⚠️ **Jupiter API Rate Limit Aşıldı!**\nSon hata: `{e}`\nBot {delay:.2f} saniye bekleyecek.",
                            parse_mode='md'
                        )
                        await asyncio.sleep(delay)
                    elif attempt < max_swap_retries - 1:
                        await asyncio.sleep(initial_delay * (2 ** attempt) + random.uniform(0, 1))
                    else:
                        return False, f"HTTP istek hatası: {e}", None
                except Exception as e:
                    logger.error(f"perform_swap içinde beklenmeyen hata: {str(e)}", exc_info=True)
                    return False, f"Beklenmeyen hata: {str(e)}", None
            logger.info("Yeni swapTransaction almak için get_swap_quote çağrılıyor.")
            quote_data = await get_swap_quote(
                input_mint=Pubkey.from_string(quote_data['inputMint']),
                output_mint=Pubkey.from_string(quote_data['outputMint']),
                amount_in_lamports=int(quote_data['inAmount']),
                slippage_bps=int(quote_data['slippageBps'])
            )
            if not quote_data:
                logger.error("Yeni takas teklifi alınamadı.")
                return False, "Yeni takas teklifi alınamadı.", None
            try:
                swap_response = requests.post(swap_url, json={
                    "quoteResponse": quote_data,
                    "userPublicKey": str(payer_keypair.pubkey()),
                    "wrapUnwrapSOL": True,
                    "prioritizationFeeLamports": "auto"
                }, headers=headers)
                swap_response.raise_for_status()
                swap_data = swap_response.json()
                swap_transaction_str = swap_data.get("swapTransaction")
                tx_signature, status = await send_and_confirm_transaction(swap_transaction_str)
                if status != "success" or not tx_signature:
                    logger.error(f"Yeni işlem başarısız oldu: {status}")
                    return False, f"Yeni işlem başarısız: {status}", None
                logger.info(f"Yeni işlem onaylandı: {tx_signature}")
                last_jupiter_call_time = time.time()
                return True, tx_signature, quote_data
            except Exception as e:
                logger.error(f"Yeni takas işlemi başarısız oldu: {e}")
                return False, f"Yeni takas işlemi başarısız: {e}", None

async def auto_buy_token(contract_address: str, token_name: str):
    if not solana_client or not payer_keypair:
        logger.error("Solana istemcisi veya ödeme anahtarı başlatılmadı.")
        await bot_client.send_message(
            DEFAULT_ADMIN_ID,
            "❌ **Otomatik Alım Başarısız!**\nSolana istemcisi veya cüzdan başlatılmamış.",
            parse_mode='md'
        )
        return
    async with contract_processing_lock:
        if await is_contract_processed(contract_address):
            logger.info(f"Sözleşme {contract_address} zaten işlenmiş, atlanıyor.")
            return
        auto_buy_enabled = await get_bot_setting("auto_buy_enabled")
        if auto_buy_enabled != "enabled":
            logger.info("Otomatik alım devre dışı bırakıldı.")
            return
        buy_amount_sol_str = await get_bot_setting("buy_amount_sol")
        slippage_tolerance_str = await get_bot_setting("slippage_tolerance")
        profit_target_x_str = await get_bot_setting("profit_target_x")
        stop_loss_percent_str = await get_bot_setting("stop_loss_percent")
        try:
            buy_amount_sol = float(buy_amount_sol_str)
            slippage_bps = int(float(slippage_tolerance_str) * 100)
            profit_target_x = float(profit_target_x_str)
            stop_loss_percent = float(stop_loss_percent_str)
        except ValueError:
            logger.error("Geçersiz bot ayarları.")
            await bot_client.send_message(
                DEFAULT_ADMIN_ID,
                "❌ **Otomatik Alım Başarısız!**\nBot ayarları (alım miktarı, slippage, kar hedefi, stop loss) geçersiz.",
                parse_mode='md'
            )
            return
        current_balance = await check_wallet_balance()
        if current_balance is None or current_balance < buy_amount_sol:
            error_msg = f"Yetersiz SOL bakiyesi. Gerekli: {buy_amount_sol} SOL, Mevcut: {current_balance if current_balance is not None else 'Alınamadı'} SOL."
            logger.error(error_msg)
            await add_transaction_history(
                "N/A", "BUY", token_name, contract_address, buy_amount_sol, 0, 0, "FAILED", error_msg
            )
            await bot_client.send_message(
                DEFAULT_ADMIN_ID,
                f"❌ **Otomatik Alım Başarısız!**\nToken: **{token_name}**\nAdres: `{contract_address}`\nHata: `{error_msg}`",
                parse_mode='md'
            )
            return
        logger.info(f"Yeni token {token_name} ({contract_address}) için otomatik alım başlatılıyor...")
        await bot_client.send_message(
            DEFAULT_ADMIN_ID,
            f"⚡️ **Yeni Token Sinyali Yakalandı!**\nToken: **{token_name}**\nAdres: `{contract_address}`\nAlım işlemi başlatılıyor... ({buy_amount_sol} SOL)",
            parse_mode='md'
        )
        try:
            sol_mint = Pubkey.from_string("So11111111111111111111111111111111111111112")
            token_mint = Pubkey.from_string(contract_address)
            amount_in_lamports = int(buy_amount_sol * (10**9))
            quote = await get_swap_quote(sol_mint, token_mint, amount_in_lamports, slippage_bps)
            if not quote:
                raise Exception("Jupiter'den takas teklifi alınamadı.")
            success, tx_signature, quote_data = await perform_swap(quote)
            if not success:
                raise Exception(tx_signature)
            token_info = await asyncio.to_thread(solana_client.get_token_supply, token_mint)
            token_decimals = token_info.value.decimals if token_info and token_info.value else 0
            received_token_amount = float(quote_data['outAmount']) / (10**token_decimals)
            actual_buy_price_sol_per_token = (float(quote_data['inAmount']) / (10**9)) / (float(quote_data['outAmount']) / (10**token_decimals))
            await add_open_position(
                contract_address, token_name, actual_buy_price_sol_per_token, received_token_amount,
                tx_signature, profit_target_x, stop_loss_percent
            )
            await record_processed_contract(contract_address)
            await add_transaction_history(
                tx_signature, "BUY", token_name, contract_address, buy_amount_sol, received_token_amount,
                actual_buy_price_sol_per_token, "SUCCESS"
            )
            await bot_client.send_message(
                DEFAULT_ADMIN_ID,
                f"✅ **Alım Başarılı!**\nToken: **{token_name}**\nAlınan Miktar: `{received_token_amount:.4f} {token_name}`\n"
                f"Harcanan SOL: `{buy_amount_sol:.4f}`\nAlım Fiyatı: `{actual_buy_price_sol_per_token:.8f} SOL/{token_name}`\n"
                f"İşlem ID: [`{tx_signature}`](https://solscan.io/tx/{tx_signature})\nKar Hedefi: `{profit_target_x}x`, Stop Loss: `{stop_loss_percent}%`",
                parse_mode='md', link_preview=False
            )
            logger.info(f"Otomatik alım {token_name} için tamamlandı. İmza: {tx_signature}")
        except Exception as e:
            error_msg = f"Otomatik alım {token_name} ({contract_address}) için başarısız oldu: {e}"
            logger.error(error_msg, exc_info=True)
            await add_transaction_history(
                "N/A", "BUY", token_name, contract_address, buy_amount_sol, 0, 0, "FAILED", str(e)
            )
            await bot_client.send_message(
                DEFAULT_ADMIN_ID,
                f"❌ **Otomatik Alım Başarısız!**\nToken: **{token_name}**\nAdres: `{contract_address}`\nHata: `{e}`",
                parse_mode='md'
            )

async def auto_sell_token(contract_address: str, token_name: str, sell_reason: str, position_data: dict):
    if not solana_client or not payer_keypair:
        logger.error("Solana istemcisi veya ödeme anahtarı başlatılmadı.")
        await bot_client.send_message(
            DEFAULT_ADMIN_ID,
            "❌ **Otomatik Satış Başarısız!**\nSolana istemcisi veya cüzdan başlatılmamış.",
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
        f"🚨 **Otomatik Satış Tetiklendi!**\nToken: **{token_name}**\nAdres: `{contract_address}`\nNeden: **{sell_reason}**\nSatış işlemi başlatılıyor...",
        parse_mode='md'
    )
    try:
        token_mint = Pubkey.from_string(contract_address)
        sol_mint = Pubkey.from_string("So11111111111111111111111111111111111111112")
        amount_to_sell_token = position_data['buy_amount_token']
        token_info = await asyncio.to_thread(solana_client.get_token_supply, token_mint)
        token_decimals = token_info.value.decimals if token_info and token_info.value else 0
        amount_in_lamports = int(amount_to_sell_token * (10**token_decimals))
        slippage_tolerance_str = await get_bot_setting("slippage_tolerance")
        slippage_bps = int(float(slippage_tolerance_str) * 100)
        quote = await get_swap_quote(token_mint, sol_mint, amount_in_lamports, slippage_bps)
        if not quote:
            raise Exception("Jupiter'den takas teklifi alınamadı.")
        success, tx_signature, quote_data = await perform_swap(quote)
        if not success:
            raise Exception(tx_signature)
        received_sol_amount = float(quote_data['outAmount']) / (10**9)
        actual_sell_price_sol_per_token = (float(quote_data['outAmount']) / (10**9)) / (float(quote_data['inAmount']) / (10**token_decimals))
        await remove_open_position(contract_address)
        await add_transaction_history(
            tx_signature, "SELL", token_name, contract_address, received_sol_amount, amount_to_sell_token,
            actual_sell_price_sol_per_token, "SUCCESS"
        )
        profit_loss_sol = received_sol_amount - (position_data['buy_amount_token'] * position_data['buy_price_sol'])
        profit_loss_percent = (profit_loss_sol / (position_data['buy_amount_token'] * position_data['buy_price_sol'])) * 100 if position_data['buy_amount_token'] * position_data['buy_price_sol'] != 0 else 0
        await bot_client.send_message(
            DEFAULT_ADMIN_ID,
            f"✅ **Satış Başarılı!**\nToken: **{token_name}**\nSatılan Miktar: `{amount_to_sell_token:.4f} {token_name}`\n"
            f"Alınan SOL: `{received_sol_amount:.4f}`\nSatış Fiyatı: `{actual_sell_price_sol_per_token:.8f} SOL/{token_name}`\n"
            f"Kar/Zarar: `{profit_loss_sol:.4f} SOL ({profit_loss_percent:.2f}%)`\nİşlem ID: [`{tx_signature}`](https://solscan.io/tx/{tx_signature})",
            parse_mode='md', link_preview=False
        )
        logger.info(f"Otomatik satış {token_name} için tamamlandı. İmza: {tx_signature}")
    except Exception as e:
        error_msg = f"Otomatik satış {token_name} ({contract_address}) için başarısız oldu: {e}"
        logger.error(error_msg, exc_info=True)
        await add_transaction_history(
            "N/A", "SELL", token_name, contract_address, 0, amount_to_sell_token, 0, "FAILED", str(e)
        )
        await bot_client.send_message(
            DEFAULT_ADMIN_ID,
            f"❌ **Otomatik Satış Başarısız!**\nToken: **{token_name}**\nAdres: `{contract_address}`\nHata: `{e}`",
            parse_mode='md'
        )

# --- Telegram Bot Komutları ---
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
            f"**Yönetici Yönetimi**\n\nMevcut Yöneticiler:\n{admin_list}\n\n"
            "Kullanım:\n`/admin add <user_id>`\n`/admin remove <user_id>`",
            parse_mode='md'
        )
        return
    action = args[1].lower()
    if action == "add" and len(args) == 3:
        try:
            user_id = int(args[2])
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
    args = event.text.split(maxsplit=2)
    if len(args) == 1:
        settings = {}
        for key in DEFAULT_BOT_SETTINGS.keys():
            value = await get_bot_setting(key)
            if key in ["SOLANA_PRIVATE_KEY", "JUPITER_API_KEY"] and value:
                settings[key] = value[:5] + "..." + value[-5:]
            else:
                settings[key] = value if value is not None else "AYARLANMADI"
        settings_msg = "**Bot Ayarları:**\n\n" + "\n".join(f"• `{key}`: `{value}`" for key, value in settings.items())
        settings_msg += "\n\nAyarları değiştirmek için:\n`/settings <anahtar> <değer>`"
        await event.reply(settings_msg, parse_mode='md')
    elif len(args) == 3:
        setting_key = args[1].strip()
        setting_value = args[2].strip()
        if setting_key not in DEFAULT_BOT_SETTINGS:
            await event.reply(f"Bilinmeyen ayar anahtarı: `{setting_key}`")
            return
        if setting_key == "SOLANA_PRIVATE_KEY" and not re.match(r"^[1-9A-HJ-NP-Za-km-z]{87,88}$", setting_value):
            await event.reply("Geçersiz Solana özel anahtarı formatı.")
            return
        if setting_key in ["buy_amount_sol", "slippage_tolerance", "profit_target_x", "stop_loss_percent"]:
            try:
                float(setting_value)
            except ValueError:
                await event.reply(f"`{setting_key}` için geçerli bir sayısal değer girin.")
                return
        await set_bot_setting(setting_key, setting_value)
        if setting_key in ["SOLANA_PRIVATE_KEY", "JUPITER_API_KEY"]:
            await event.reply(f"Ayar `{setting_key}` başarıyla güncellendi (maskelendi).")
            if setting_key == "SOLANA_PRIVATE_KEY":
                await init_solana_client()
                await event.reply("Solana istemcisi özel anahtar değişikliği nedeniyle yeniden başlatıldı.")
        else:
            await event.reply(f"Ayar `{setting_key}` başarıyla `{setting_value}` olarak güncellendi.")

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
    status_msg = (
        f"**Bot Durumu:**\n\n"
        f"• Bot Durumu: `{bot_status.upper()}`\n"
        f"• Otomatik Alım: `{auto_buy_enabled.upper()}`\n"
        f"• Otomatik Satış: `{auto_sell_enabled.upper()}`\n"
        f"• Cüzdan Adresi: `{wallet_address}`\n"
        f"• SOL Bakiyesi: `{balance:.4f} SOL` if balance is not None else 'Alınamadı'\n"
        f"• Aktif RPC: `{active_rpc_url if active_rpc_url else 'Bilinmiyor'}`\n"
    )
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
        history_msg += (
            f"• Tür: **{tx['type']}** | Durum: **{tx['status']}**\n"
            f"  Token: **{tx['token_name']}** (`{tx['contract_address'][:6]}...{tx['contract_address'][-4:]}`)\n"
            f"  SOL Miktarı: `{tx['amount_sol']:.4f}` | Token Miktarı: `{tx['amount_token']:.4f}`\n"
            f"  Fiyat: `{tx['price_sol_per_token']:.8f} SOL/token`\n"
            f"  TX ID: [`{tx['tx_signature'][:6]}...{tx['tx_signature'][-4:]}`](https://solscan.io/tx/{tx['tx_signature']})\n"
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
    token_name = "Manuel Token"
    await event.reply(f"Manuel alım başlatılıyor: {sol_amount} SOL karşılığında {token_address} tokenı...")
    await auto_buy_token(token_address, token_name)
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
            f"✅ Solana istemcisi başarıyla yeniden başlatıldı.\nCüzdan: `{payer_keypair.pubkey()}`\n"
            f"Bakiye: `{balance:.4f} SOL`" if balance is not None else "Bakiye: `Alınamadı`",
            parse_mode='md'
        )
    else:
        await event.reply("❌ Solana istemcisinin yeniden başlatılması başarısız oldu. Logları kontrol edin.")

@bot_client.on(events.NewMessage(chats=SOURCE_CHANNEL_ID))
async def channel_message_handler(event):
    logger.info(f"Kanal {SOURCE_CHANNEL_ID} adresinden yeni mesaj alındı.")
    logger.debug(f"Mesaj içeriği: {event.text}")
    contract_address = extract_contract(event.text)
    token_name = extract_token_name_from_message(event.text)
    if contract_address:
        logger.info(f"Mesajdan sözleşme adresi çıkarıldı: {contract_address}")
        async with contract_processing_lock:
            if await is_contract_processed(contract_address):
                logger.info(f"Sözleşme {contract_address} zaten işlenmiş.")
                return
            bot_status = await get_bot_setting("bot_status")
            if bot_status == "running":
                asyncio.create_task(auto_buy_token(contract_address, token_name))
            else:
                logger.info("Bot duraklatıldı, otomatik alım tetiklenmedi.")
    else:
        logger.debug("Mesajda sözleşme adresi bulunamadı.")

async def monitor_open_positions():
    await bot_client.send_message(DEFAULT_ADMIN_ID, "📊 **Pozisyon İzleyici Başlatıldı.**")
    while True:
        try:
            auto_sell_enabled = await get_bot_setting("auto_sell_enabled")
            if auto_sell_enabled != "enabled":
                logger.info("Otomatik satış devre dışı, izleme duraklatıldı.")
                await asyncio.sleep(60)
                continue
            positions = await get_open_positions()
            if not positions:
                logger.debug("İzlenecek açık pozisyon yok.")
                await asyncio.sleep(30)
                continue
            for pos in positions:
                contract_address = pos['contract_address']
                token_name = pos['token_name']
                buy_price_sol = pos['buy_price_sol']
                target_profit_x = pos['target_profit_x']
                stop_loss_percent = pos['stop_loss_percent']
                current_price_sol_per_token = await get_current_token_price_sol(contract_address)
                if current_price_sol_per_token is None:
                    logger.warning(f"{token_name} ({contract_address}) için mevcut fiyat alınamadı.")
                    continue
                current_value_sol = pos['buy_amount_token'] * current_price_sol_per_token
                initial_cost_sol = pos['buy_amount_token'] * buy_price_sol
                profit_loss_sol = current_value_sol - initial_cost_sol
                profit_loss_percent = (profit_loss_sol / initial_cost_sol) * 100 if initial_cost_sol != 0 else 0
                logger.info(f"Pozisyon {token_name}: Mevcut Fiyat: {current_price_sol_per_token:.8f} SOL/token, Kar/Zarar: {profit_loss_percent:.2f}%")
                if current_price_sol_per_token >= (buy_price_sol * target_profit_x):
                    logger.info(f"Kar hedefi {target_profit_x}x ulaşıldı! {token_name} satılıyor.")
                    asyncio.create_task(auto_sell_token(contract_address, token_name, "Kar Hedefi Ulaşıldı", pos))
                elif profit_loss_percent <= -abs(stop_loss_percent):
                    logger.info(f"Stop loss {stop_loss_percent}% tetiklendi! {token_name} satılıyor.")
                    asyncio.create
