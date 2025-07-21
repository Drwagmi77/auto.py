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
import base64 # Base64 kod çözme için

# Solana Kütüphaneleri
from solana.rpc.api import Client, RPCException
from solana.rpc.types import TxOpts
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction # VersionedTransaction hala kullanılabilir, ancak doğrudan imzalanmayacak
from solders.message import MessageV0
from solders.instruction import Instruction
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.rpc.responses import GetBalanceResp # GetBalanceResp'i içe aktarın

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
SOLANA_RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
JUPITER_API_URL = os.environ.get("JUPITER_API_URL", "https://quote-api.jup.ag/v6")

SECRET_KEY = os.environ.get("SECRET_KEY", os.urandom(24).hex())

app = Flask(__name__)
app.secret_key = SECRET_KEY

# --- Telethon Client'ları ---
bot_client = TelegramClient('auto_buy_bot_session', API_ID, API_HASH)

# --- Solana Client ve Wallet Initialization ---
solana_client = None
payer_keypair = None

async def get_balance_with_retry(pubkey: Pubkey, retries=3):
    """
    Solana bakiyesini tekrar deneme mekanizmasıyla alır.
    """
    for i in range(retries):
        try:
            resp = await asyncio.to_thread(solana_client.get_balance, pubkey)
            if isinstance(resp, GetBalanceResp):
                return resp.value
            else:
                logger.warning(f"Unexpected response type for get_balance: {type(resp)}. Attempt {i+1}/{retries}")
        except Exception as e:
            logger.warning(f"Balance check attempt {i+1}/{retries} failed: {e}")
            await asyncio.sleep(1) # Kısa bir bekleme süresi
    return None

async def check_wallet_balance():
    """
    Cüzdanın SOL bakiyesini kontrol eder ve SOL cinsinden döndürür.
    Hata durumunda None döner.
    """
    if not solana_client or not payer_keypair:
        logger.error("Solana istemcisi veya ödeme anahtar çifti başlatılmadı. Bakiye kontrol edilemiyor.")
        return None
    
    try:
        balance_lamports = await get_balance_with_retry(payer_keypair.pubkey())
        
        if balance_lamports is None:
            logger.error("Cüzdan bakiyesi alınamadı.")
            return None
            
        return balance_lamports / 10**9  # SOL cinsinden
    except Exception as e:
        logger.error(f"Bakiye kontrol hatası: {str(e)}", exc_info=True)
        return None

async def init_solana_client():
    """Solana RPC istemcisini ve cüzdanı başlatır."""
    global solana_client, payer_keypair
    try:
        solana_client = Client(SOLANA_RPC_URL)
        current_private_key = await get_bot_setting("SOLANA_PRIVATE_KEY")
        if current_private_key:
            payer_keypair = Keypair.from_base58_string(current_private_key)
            logger.info(f"Solana istemcisi başlatıldı. Cüzdan genel anahtarı: {payer_keypair.pubkey()}")
            logger.info(f"RPC URL: {SOLANA_RPC_URL}")
            logger.info(f"Public Key: {payer_keypair.pubkey()}")
            
            balance = await check_wallet_balance()
            logger.info(f"Başlangıç bakiyesi: {balance if balance is not None else 'Alınamadı'} SOL")

        else:
            logger.error("Bot ayarlarında SOLANA_PRIVATE_KEY ayarlanmadı. Otomatik alım işlevi devre dışı bırakılacak.")
            solana_client = None
            payer_keypair = None
    except Exception as e:
        logger.error(f"Solana istemcisi veya cüzdan başlatılırken hata oluştu: {e}", exc_info=True)
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
            sslmode="require"
        )
    except psycopg2.OperationalError as e:
        logger.error(f"Veritabanı bağlantısı başarısız oldu: {e}")
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
        logger.error(f"Veritabanı başlatma sırasında hata oluştu: {e}")
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
        logger.error(f"Yöneticiler alınırken hata oluştu: {e}")
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
        logger.error(f"Yönetici {user_id} eklenirken hata oluştu: {e}")
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
        logger.error(f"Yönetici {user_id} kaldırılırken hata oluştu: {e}")
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
        logger.error(f"Bot ayarı '{setting}' alınırken hata oluştu: {e}")
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
            logger.info(f"Bot ayarı '{setting}' '{value}' olarak ayarlandı.")
    except Exception as e:
        logger.error(f"Bot ayarı '{setting}' ayarlanırken hata oluştu: {e}")
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
        logger.error(f"Kontrat {contract_address} işlenip işlenmediği kontrol edilirken hata oluştu: {e}")
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
            logger.info(f"İşlenmiş kontrat kaydedildi: {contract_address}.")
    except Exception as e:
        logger.error(f"İşlenmiş kontrat {contract_address} kaydedilirken hata oluştu: {e}")
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
        logger.info(f"Açık pozisyon {token_name} ({contract_address}) için eklendi/güncellendi.")
    except Exception as e:
        logger.error(f"Açık pozisyon {contract_address} için eklenirken/güncellenirken hata oluştu: {e}")
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
        logger.error(f"Açık pozisyonlar alınırken hata oluştu: {e}")
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
            logger.info(f"Açık pozisyon {contract_address} için kaldırıldı.")
        else:
            logger.warning(f"Kaldırılacak {contract_address} için açık pozisyon bulunamadı.")
    except Exception as e:
        logger.error(f"Açık pozisyon {contract_address} kaldırılırken hata oluştu: {e}")
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
            logger.info(f"İşlem kaydedildi: {tx_type} için {token_name} ({tx_signature}).")
    except Exception as e:
        logger.error(f"İşlem geçmişi {tx_signature} için kaydedilirken hata oluştu: {e}")
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
        logger.error(f"İşlem geçmişi alınırken hata oluştu: {e}")
        return []
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

# --- Varsayılan Ayarlar ---
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
logger.info("🔥 Otomatik Alım/Satım Botu Günlük Ayarları tamamlandı. Bot başlatılıyor...")

# --- Telethon Yardımcı Fonksiyonlar ---
async def retry_telethon_call(coro, max_retries=5, base_delay=1.0):
    """Telethon çağrıları için tekrar deneme mekanizması."""
    for i in range(max_retries):
        try:
            return await coro
        except Exception as e:
            logger.warning(f"Telethon çağrısı için tekrar deneme {i+1}/{max_retries} hatası nedeniyle: {e}")
            if i < max_retries - 1:
                delay = base_delay * (2 ** i) + random.uniform(0, 1)
                await asyncio.sleep(delay)
            else:
                logger.error(f"Telethon çağrısı için maksimum deneme sayısına ulaşıldı: {e}")
                raise
    raise RuntimeError("Tekrar deneme mantığı başarısız oldu veya max_retries 0 idi")

def extract_contract(text: str) -> str | None:
    """Metinden Solana kontrat adresini (Base58, 32-44 karakter) çıkarır."""
    m = re.findall(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b", text)
    return m[0] if m else None

def extract_token_name_from_message(text: str) -> str:
    """Mesajdan token adını ($TOKEN_NAME formatında) çıkarır."""
    lines = text.strip().splitlines()
    if not lines:
        logger.debug("Token çıkarma için boş mesaj alındı; 'bilinmiyor' döndürülüyor.")
        return "unknown"
    for line in lines:
        match = re.search(r"\$([A-Za-z0-9_]+)", line)
        if match:
            token = match.group(1)
            logger.debug(f"Token çıkarıldı: '{token}' satırdan: '{line}'")
            return token
    logger.debug("Mesajda geçerli bir token ($WORD) bulunamadı; 'bilinmiyor' döndürülüyor.")
    return "unknown"

# --- Solana Auto-Buy Fonksiyonları ---
async def get_current_token_price_sol(token_mint_str: str, amount_token_to_check: float = 0.000000001):
    """Belirli bir token'ın anlık SOL fiyatını tahmin eder."""
    if not solana_client:
        logger.error("Solana istemcisi başlatılmadı. Token fiyatı alınamıyor.")
        return None

    try:
        token_mint = Pubkey.from_string(token_mint_str)
        input_mint = token_mint
        output_mint = Pubkey.from_string("So11111111111111111111111111111111111111112")

        token_info = await asyncio.to_thread(solana_client.get_token_supply, input_mint) # input_mint kullanıldı
        if not token_info or not hasattr(token_info, 'value') or not hasattr(token_info.value, 'decimals'):
            logger.warning(f"{token_mint_str} için token arz bilgisi alınamadı. Ondalıklar belirlenemiyor.")
            return None
        decimals = token_info.value.decimals
        
        amount_in_lamports = int(amount_token_to_check * (10**decimals))

        quote_url = f"{JUPITER_API_URL}/quote?inputMint={input_mint}&outputMint={output_mint}&amount={amount_in_lamports}&slippageBps=0"
        response = requests.get(quote_url)
        response.raise_for_status()
        quote_data = response.json()

        if not quote_data or "outAmount" not in quote_data or "inAmount" not in quote_data:
            logger.warning(f"Fiyat kontrolü için geçersiz teklif verisi: {quote_data}")
            return None
        
        price_sol_per_token = (float(quote_data['outAmount']) / (10**9)) / (float(quote_data['inAmount']) / (10**decimals))
        logger.debug(f"{token_mint_str} için güncel fiyat: {price_sol_per_token} SOL/token")
        return price_sol_per_token

    except requests.exceptions.RequestException as e:
        logger.error(f"Jupiter'den token fiyatı alınırken hata oluştu: {e}")
        return None
    except Exception as e:
        logger.error(f"get_current_token_price_sol içinde beklenmeyen hata: {e}")
        return None

async def get_swap_quote(input_mint: Pubkey, output_mint: Pubkey, amount_in_lamports: int, slippage_bps: int):
    """Jupiter Aggregator'dan takas teklifi alır."""
    if not solana_client or not payer_keypair:
        logger.error("Solana istemcisi veya ödeme anahtar çifti başlatılmadı. Teklif alınamıyor.")
        return None

    try:
        quote_url = f"{JUPITER_API_URL}/quote?inputMint={input_mint}&outputMint={output_mint}&amount={amount_in_lamports}&slippageBps={slippage_bps}"
        response = requests.get(quote_url)
        response.raise_for_status()
        quote_data = response.json()
        
        if not quote_data or "swapMode" not in quote_data:
            logger.error(f"Geçersiz teklif verisi alındı: {quote_data}")
            return None

        logger.info(f"{input_mint} ile {output_mint} arasındaki Jupiter teklifi alındı: {quote_data.get('outAmount')} {quote_data.get('outputToken', {}).get('symbol')}")
        return quote_data

    except requests.exceptions.RequestException as e:
        logger.error(f"Jupiter teklifi alınırken hata oluştu: {e}")
        return None
    except Exception as e:
        logger.error(f"get_swap_quote içinde beklenmeyen hata: {e}")
        return None

async def perform_swap(quote_data: dict):
    """Jupiter Aggregator'dan alınan teklifle takas işlemini gerçekleştirir."""
    if not solana_client or not payer_keypair:
        logger.error("Solana istemcisi veya ödeme anahtar çifti başlatılmadı. Takas yapılamıyor.")
        return False, "Solana istemcisi veya cüzdan hazır değil.", None

    try:
        swap_url = f"{JUPITER_API_URL}/swap"
        swap_response = requests.post(swap_url, json={
            "quoteResponse": quote_data,
            "userPublicKey": str(payer_keypair.pubkey()),
            "wrapUnwrapSOL": True,
            "prioritizationFeeLamports": 100000
        })
        swap_response.raise_for_status()
        swap_data = swap_response.json()

        if not swap_data or "swapTransaction" not in swap_data:
            logger.error(f"Jupiter'den geçersiz takas verisi alındı: {swap_data}")
            return False, "Geçersiz takas işlem verisi.", None

        # Base64 decode transaction
        tx_bytes = base64.b64decode(swap_data["swapTransaction"])
        
        # Yeni yöntem: Transaction'ı doğrudan raw olarak gönder
        tx_signature = await asyncio.to_thread(
            solana_client.send_raw_transaction,
            tx_bytes,
            opts=TxOpts(skip_preflight=True)
        )
        
        logger.info(f"Takas işlemi gönderildi: {tx_signature}")

        # Onayı bekle
        confirmation = await asyncio.to_thread(
            solana_client.confirm_transaction,
            tx_signature,
            commitment="confirmed"
        )
        
        # Onay kontrolü
        if confirmation.value and confirmation.value[0].err:
            logger.error(f"İşlem hatayla sonuçlandı: {confirmation.value[0].err}")
            return False, f"İşlem başarısız: {confirmation.value[0].err}", None
        else:
            logger.info(f"İşlem onaylandı: {tx_signature}")
            return True, tx_signature, quote_data

    except requests.exceptions.RequestException as e:
        logger.error(f"Jupiter ile takas yapılırken hata oluştu: {e}")
        return False, f"HTTP istek hatası: {e}", None
    except RPCException as e:
        logger.error(f"Takas sırasında Solana RPC hatası: {e}")
        return False, f"Solana RPC hatası: {e}", None
    except Exception as e:
        logger.error(f"perform_swap içinde beklenmeyen hata: {str(e)}", exc_info=True)
        return False, f"Beklenmeyen hata: {str(e)}", None

async def auto_buy_token(contract_address: str, token_name: str, buy_amount_sol: float, slippage_tolerance_percent: float):
    """Belirtilen kontrat adresindeki token'ı otomatik olarak satın alır."""
    if not solana_client or not payer_keypair:
        logger.error("Otomatik alım atlandı: Solana istemcisi veya cüzdan başlatılmadı.")
        return False, "Cüzdan hazır değil.", None, None

    if await is_contract_processed(contract_address):
        logger.info(f"Kontrat {contract_address} zaten otomatik alım için işlendi. Atlanıyor.")
        return False, "Kontrat zaten işlendi.", None, None

    # Bakiye kontrolü
    current_balance = await check_wallet_balance()
    if current_balance is None:
        await add_transaction_history(
            "N/A", 'buy', token_name, contract_address,
            buy_amount_sol, 0.0, 0.0, 'failed', "Cüzdan bakiyesi alınamadı."
        )
        return False, "Cüzdan bakiyesi alınamadı", None, None
        
    if current_balance < buy_amount_sol:
        error_msg = f"Yetersiz SOL bakiyesi. Gerekli: {buy_amount_sol} SOL, Mevcut: {current_balance:.4f} SOL."
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

    logger.info(f"{contract_address} ({token_name}) için {buy_amount_sol} SOL ve %{slippage_tolerance_percent} kayma ile otomatik alım deneniyor.")

    quote_data = await get_swap_quote(input_mint, output_mint, amount_in_lamports, slippage_bps)
    if not quote_data:
        await add_transaction_history(
            "N/A", 'buy', token_name, contract_address,
            buy_amount_sol, 0.0, 0.0, 'failed', "Takas teklifi alınamadı."
        )
        logger.error(f"{contract_address} için takas teklifi alınamadı.")
        return False, "Takas teklifi alınamadı.", None, None

    success, tx_signature, final_quote_data = await perform_swap(quote_data)
    if success:
        await record_processed_contract(contract_address)

        output_token_decimals = final_quote_data.get('outputToken', {}).get('decimals')
        if output_token_decimals is None:
            logger.warning(f"{token_name} için ondalıklar belirlenemedi. Satın alınan miktar hesaplanamıyor.")
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
        logger.info(f"Token {contract_address} başarıyla otomatik olarak satın alındı. Tx: {tx_signature}")
        return True, f"Token {token_name} başarıyla satın alındı. Tx: {tx_signature}", actual_buy_price_sol, bought_amount_token
    else:
        await add_transaction_history(
            "N/A", 'buy', token_name, contract_address,
            buy_amount_sol, 0.0, 0.0, 'failed', tx_signature # tx_signature burada hata mesajı
        )
        logger.error(f"Token {contract_address} otomatik olarak satın alınamadı: {tx_signature}")
        return False, f"Token {token_name} satın alınamadı: {tx_signature}", None, None

async def auto_sell_token(contract_address: str, token_name: str, amount_to_sell_token: float, slippage_tolerance_percent: float):
    """Belirtilen token'ı otomatik olarak satar."""
    if not solana_client or not payer_keypair:
        logger.error("Otomatik satım atlandı: Solana istemcisi veya cüzdan başlatılmadı.")
        return False, "Cüzdan hazır değil."

    input_mint = Pubkey.from_string(contract_address)
    output_mint = Pubkey.from_string("So11111111111111111111111111111111111111112")
    slippage_bps = int(slippage_tolerance_percent * 100)

    token_info = await asyncio.to_thread(solana_client.get_token_supply, input_mint)
    if not token_info or not hasattr(token_info, 'value') or not hasattr(token_info.value, 'decimals'):
        await add_transaction_history(
            "N/A", 'sell', token_name, contract_address,
            0.0, amount_to_sell_token, 0.0, 'failed', "Satış için token ondalıkları alınamadı."
        )
        logger.warning(f"{token_name} için token arz bilgisi alınamadı. Satış için ondalıklar belirlenemiyor.")
        return False, "Token ondalıkları alınamadı."
    decimals = token_info.value.decimals
    
    amount_in_lamports = int(amount_to_sell_token * (10**decimals))

    logger.info(f"{amount_to_sell_token} {token_name} ({contract_address}) için %{slippage_tolerance_percent} kayma ile otomatik satım deneniyor.")

    quote_data = await get_swap_quote(input_mint, output_mint, amount_in_lamports, slippage_bps)
    if not quote_data:
        await add_transaction_history(
            "N/A", 'sell', token_name, contract_address,
            0.0, amount_to_sell_token, 0.0, 'failed', "Satış için takas teklifi alınamadı."
        )
        logger.error(f"{token_name} satışı için takas teklifi alınamadı.")
        return False, "Satış için takas teklifi alınamadı."

    success, tx_signature, final_quote_data = await perform_swap(quote_data)
    if success:
        received_sol_lamports = int(final_quote_data['outAmount'])
        received_sol = received_sol_lamports / (10**9)
        sell_price_sol_per_token = received_sol / amount_to_sell_token if amount_to_sell_token > 0 else 0.0

        await add_transaction_history(
            tx_signature, 'sell', token_name, contract_address,
            received_sol, amount_to_sell_token, sell_price_sol_per_token, 'success'
        )
        logger.info(f"Token {token_name} başarıyla otomatik olarak satıldı. Tx: {tx_signature}")
        return True, f"Token {token_name} başarıyla satıldı. Tx: {tx_signature}"
    else:
        await add_transaction_history(
            "N/A", 'sell', token_name, contract_address,
            0.0, amount_to_sell_token, 0.0, 'failed', tx_signature
        )
        logger.error(f"Token {token_name} otomatik olarak satılamadı: {tx_signature}")
        return False, f"Token {token_name} satılamadı: {tx_signature}"

async def monitor_positions_task():
    """Açık pozisyonları izler ve kar/zarar hedeflerine göre otomatik satım yapar."""
    while True:
        await asyncio.sleep(30)

        auto_sell_enabled = await get_bot_setting("auto_sell_enabled")
        if auto_sell_enabled != "enabled":
            logger.debug("Otomatik satım devre dışı. Pozisyon izleme atlanıyor.")
            continue

        positions = await get_open_positions()
        if not positions:
            logger.debug("İzlenecek açık pozisyon yok.")
            continue

        slippage_tolerance_str = await get_bot_setting("slippage_tolerance")
        try:
            slippage_tolerance_percent = float(slippage_tolerance_str)
        except ValueError:
            logger.error("Otomatik satım için geçersiz kayma toleransı ayarı. Varsayılan %5 kullanılıyor.")
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
                logger.warning(f"{token_name} için güncel fiyat alınamadı. Bu pozisyon için izleme atlanıyor.")
                continue

            profit_threshold_price = buy_price_sol * target_profit_x
            stop_loss_threshold_price = buy_price_sol * (1 - (stop_loss_percent / 100))

            logger.info(f"{token_name} ({contract_address}) izleniyor: Alım Fiyatı: {buy_price_sol:.8f} SOL/token, Güncel Fiyat: {current_price_sol:.8f} SOL/token")
            logger.info(f"  Kar Hedefi Fiyatı: {profit_threshold_price:.8f} SOL/token (x{target_profit_x}), Stop-Loss Fiyatı: {stop_loss_threshold_price:.8f} SOL/token ({-stop_loss_percent}%)")

            should_sell = False
            sell_reason = ""

            if current_price_sol >= profit_threshold_price:
                should_sell = True
                sell_reason = f"{token_name} için kar hedefi ({target_profit_x}x) ulaşıldı."
            elif current_price_sol <= stop_loss_threshold_price:
                should_sell = True
                sell_reason = f"{token_name} için stop-loss ({stop_loss_percent}%) tetiklendi."

            if should_sell:
                logger.info(f"{token_name} için otomatik satım başlatılıyor: {sell_reason}")
                success, message = await auto_sell_token(contract_address, token_name, buy_amount_token, slippage_tolerance_percent)
                if success:
                    await remove_open_position(contract_address)
                    await bot_client.send_message(
                        DEFAULT_ADMIN_ID,
                        f"✅ Otomatik satım başarılı!\nToken: `{token_name}`\nSebep: `{sell_reason}`\nİşlem: `{message}`",
                        parse_mode='md'
                    )
                    logger.info(f"{token_name} için otomatik satım başarılı. Pozisyon kaldırıldı.")
                else:
                    await bot_client.send_message(
                        DEFAULT_ADMIN_ID,
                        f"❌ Otomatik satım başarısız!\nToken: `{token_name}`\nSebep: `{sell_reason}`\nHata: `{message}`",
                        parse_mode='md'
                    )
                    logger.error(f"{token_name} için otomatik satım başarısız: {message}")
            else:
                logger.debug(f"{token_name} için satış koşulu karşılanmadı.")

# --- Flask Web Sunucusu ---
pending_input = {}

@app.route('/')
def root():
    """Botun durumunu gösteren ana sayfa."""
    return jsonify(status="ok", message="Bot çalışıyor"), 200

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
        logger.warning(f"Kullanıcı ID'si {uid} adresinden yetkisiz geri arama sorgusu.")
        return await event.answer("❌ Yetkiniz yok.")

    data = event.data.decode()
    logger.info(f"Yönetici {uid} geri aramayı tetikledi: {data}")

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
        
        if data == 'admin_auto_trade_settings':
            return await event.edit(await get_admin_dashboard(),
                                    buttons=await build_auto_trade_keyboard(), link_preview=False)
        
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
                                       buttons=[[Button.inline("� Geri", b"admin_admins")]], link_preview=False)
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
        
        if data == 'admin_wallet_settings':
            return await event.edit(await get_wallet_settings_dashboard(),
                                    buttons=await build_wallet_settings_keyboard(), link_preview=False)
        if data == 'admin_set_wallet_private_key':
            pending_input[uid] = {'action': 'set_wallet_private_key'}
            kb = [[Button.inline("🔙 Geri", b"admin_wallet_settings")]]
            return await event.edit(
                "⚠️ *DİKKAT: ÇOK HASSAS BİLGİ!* ⚠️\n\n"
                "Lütfen Solana özel anahtarınızı (Base58 formatında) girin. "
                "Bu anahtar botun cüzdanınıza erişmesini sağlar. "
                "Yanlış veya kötü niyetli kullanımda fonlarınız risk altında olabilir.\n\n"
                "Yeni özel anahtarınızı buraya yapıştırın:",
                buttons=kb, parse_mode='md', link_preview=False
            )
        if data == 'admin_transaction_history':
            history = await get_transaction_history()
            if not history:
                history_text = "📜 *İşlem Geçmişi*\n\nHenüz bir işlem bulunmamaktadır."
            else:
                history_text = "📜 *Son 20 İşlem*\n\n"
                for tx in history:
                    status_emoji = "✅" if tx['status'] == 'success' else "❌"
                    tx_type_emoji = "⬆️" if tx['type'] == 'buy' else "⬇️"
                    tx_time = datetime.fromtimestamp(tx['timestamp']).strftime('%Y-%m-%d %H:%M:%S')
                    tx_sig_short = tx['tx_signature'][:6] + "..." + tx['tx_signature'][-4:] if tx['tx_signature'] and tx['tx_signature'] != "N/A" else "N/A"
                    contract_addr_short = tx['contract_address'][:6] + "..." + tx['contract_address'][-4:] if tx['contract_address'] else "N/A"

                    history_text += (
                        f"{status_emoji} {tx_type_emoji} `{tx_time}`\n"
                        f"  Token: *{tx['token_name']}*\n"
                        f"  Kontrat: `{contract_addr_short}`\n"
                        f"  Miktar: `{tx['amount_token']:.4f}` Token / `{tx['amount_sol']:.4f}` SOL\n"
                        f"  Fiyat: `{tx['price_sol_per_token']:.8f}` SOL/Token\n"
                        f"  TX: `{tx_sig_short}`\n"
                    )
                    if tx['error_message']:
                        history_text += f"  Hata: `{tx['error_message']}`\n"
                    history_text += "\n"
            
            kb = [[Button.inline("🔙 Geri", b"admin_home")]]
            return await event.edit(history_text, buttons=kb, parse_mode='md', link_preview=False)

        await event.answer("Bilinmeyen eylem.")

    except Exception as e:
        logger.error(f"Yönetici {uid}, veri {data} için admin_callback_handler içinde hata: {e}")
        await event.answer("❌ Bir hata oluştu.")
        await event.edit(f"❌ Bir hata oluştu: {e}", buttons=[[Button.inline("🔙 Geri", b"admin_home")]], parse_mode='md', link_preview=False)

@bot_client.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    """Bot başlatıldığında veya /start komutu alındığında çalışır."""
    uid = event.sender_id
    admins = await get_admins()
    if uid not in admins:
        if not admins:
            await add_admin(uid, event.sender.first_name, event.sender.last_name, is_default=True)
            logger.info(f"Varsayılan yönetici ayarlandı: {uid}")
            await event.reply("🎉 Hoş geldiniz! Varsayılan yönetici olarak ayarlandınız. Botu yönetmek için `/admin` komutunu kullanın.")
        else:
            logger.warning(f"Kullanıcı ID'si {uid} adresinden yetkisiz /start komutu.")
            return await event.reply("❌ Bu botu kullanmaya yetkiniz yok.")
    
    await event.reply(await get_admin_dashboard(), buttons=await build_admin_keyboard(), parse_mode='md', link_preview=False)

@bot_client.on(events.NewMessage(pattern='/admin'))
async def admin_handler(event):
    """/admin komutu alındığında admin panelini gösterir."""
    uid = event.sender_id
    admins = await get_admins()
    if uid not in admins:
        logger.warning(f"Kullanıcı ID'si {uid} adresinden yetkisiz /admin komutu.")
        return await event.reply("❌ Yönetici paneline erişmeye yetkiniz yok.")
    
    await event.reply(await get_admin_dashboard(), buttons=await build_admin_keyboard(), parse_mode='md', link_preview=False)

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
        "⚙️ *Yönetici Paneli*\n\n"
        f"🤖 Bot Durumu: *{bot_status.upper()}*\n"
        f"💰 Otomatik Alım: *{auto_buy_status.upper()}*\n"
        f"  - Alım Miktarı: `{buy_amount} SOL`\n"
        f"  - Kayma Toleransı: `{slippage}%`\n"
        f"📈 Otomatik Satım: *{auto_sell_status.upper()}*\n"
        f"  - Kar Hedefi: `{profit_target}x`\n"
        f"  - Stop-Loss: `{stop_loss}%`\n"
    )
    return dashboard_text

async def get_wallet_settings_dashboard():
    """Cüzdan ayarları paneli için gösterge tablosu metnini oluşturur."""
    wallet_pubkey = "N/A"
    wallet_balance = "N/A"
    if payer_keypair:
        wallet_pubkey = str(payer_keypair.pubkey())
        balance = await check_wallet_balance() # Yeni check_wallet_balance fonksiyonunu kullan
        if balance is not None:
            wallet_balance = f"{balance:.4f} SOL"
        else:
            wallet_balance = "Bakiye alınamadı (Hata)"

    dashboard_text = (
        "💳 *Cüzdan Ayarları*\n\n"
        f"Aktif Cüzdan Public Key: `{wallet_pubkey}`\n"
        f"Bakiye: `{wallet_balance}`\n\n"
        "⚠️ *Özel anahtarınızı girerken çok dikkatli olun! Bu anahtar botun cüzdanınıza tam erişimini sağlar. "
        "Yanlış veya kötü niyetli kullanımda fonlarınız risk altında olabilir.*"
    )
    return dashboard_text

async def build_admin_keyboard():
    """Admin paneli ana klavyesini oluşturur."""
    bot_status = await get_bot_setting("bot_status")
    
    keyboard = [
        [Button.inline("👤 Adminler", b"admin_admins"), Button.inline("💳 Cüzdan Ayarları", b"admin_wallet_settings")],
        [Button.inline("📈 Otomatik Alım/Satım Ayarları", b"admin_auto_trade_settings")],
        [Button.inline("📜 İşlem Geçmişi", b"admin_transaction_history")]
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
    if auto_buy_status == "enabled":
        keyboard.append([Button.inline("❌ Otomatik Alımı Devre Dışı Bırak", b"admin_disable_auto_buy")])
    else:
        keyboard.append([Button.inline("✅ Otomatik Alımı Etkinleştir", b"admin_enable_auto_buy")])
    
    keyboard.append([
        Button.inline("💲 Alım Miktarını Ayarla", b"admin_set_buy_amount"),
        Button.inline("⚙️ Kayma Toleransını Ayarla", b"admin_set_slippage")
    ])

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

async def build_wallet_settings_keyboard():
    """Cüzdan ayarları klavyesini oluşturur."""
    keyboard = [
        [Button.inline("🔑 Yeni Özel Anahtar Ayarla", b"admin_set_wallet_private_key")],
        [Button.inline("🔙 Geri", b"admin_home")]
    ]
    return keyboard

@bot_client.on(events.NewMessage)
async def handle_admin_input(event):
    """Adminlerden gelen metin girdilerini işler (ayarları değiştirmek için)."""
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
                await event.reply(f"✅ Bot {minutes} dakika duraklatıldı.")
                logger.info(f"Yönetici {uid} tarafından bot {minutes} dakika duraklatıldı.")
            except ValueError:
                await event.reply("❌ Geçersiz giriş. Lütfen dakika için bir sayı girin.")
            finally:
                del pending_input[uid]
            return await event.reply(await get_admin_dashboard(),
                                     buttons=await build_admin_keyboard(), parse_mode='md', link_preview=False)
        
        elif action == 'confirm_add_admin':
            try:
                new_admin_id = int(text_input)
                await add_admin(new_admin_id, f"Kullanıcı_{new_admin_id}", "")
                await event.reply(f"✅ Yönetici {new_admin_id} eklendi.")
                logger.info(f"Yönetici {uid} yeni yönetici {new_admin_id} ekledi.")
            except ValueError:
                await event.reply("❌ Geçersiz kullanıcı ID'si. Lütfen sayısal bir kullanıcı ID'si girin.")
            except Exception as e:
                await event.reply(f"❌ Yönetici eklenirken hata oluştu: {e}")
                logger.error(f"Yönetici {text_input} eklenirken hata: {e}")
            finally:
                del pending_input[uid]
            return await event.reply(await get_admin_dashboard(),
                                     buttons=await build_admin_keyboard(), parse_mode='md', link_preview=False)
        
        elif action == 'set_buy_amount':
            try:
                amount = float(text_input)
                if amount <= 0:
                    raise ValueError("Miktar pozitif olmalı.")
                await set_bot_setting("buy_amount_sol", str(amount))
                await event.reply(f"✅ Otomatik alım miktarı `{amount} SOL` olarak ayarlandı.")
                logger.info(f"Yönetici {uid} otomatik alım miktarını {amount} SOL olarak ayarladı.")
            except ValueError:
                await event.reply("❌ Geçersiz miktar. Lütfen pozitif bir sayı girin (örn: `0.01`, `0.5`).")
            finally:
                del pending_input[uid]
            return await event.reply(await get_admin_dashboard(),
                                     buttons=await build_auto_trade_keyboard(), parse_mode='md', link_preview=False)
        
        elif action == 'set_slippage':
            try:
                slippage = float(text_input)
                if not (0 <= slippage <= 100):
                    raise ValueError("Kayma toleransı 0 ile 100 arasında olmalı.")
                await set_bot_setting("slippage_tolerance", str(slippage))
                await event.reply(f"✅ Kayma toleransı `{slippage}%` olarak ayarlandı.")
                logger.info(f"Yönetici {uid} kayma toleransını %{slippage} olarak ayarladı.")
            finally:
                del pending_input[uid]
            return await event.reply(await get_admin_dashboard(),
                                     buttons=await build_auto_trade_keyboard(), parse_mode='md', link_preview=False)
        
        elif action == 'set_profit_target':
            try:
                target_x = float(text_input)
                if target_x <= 1.0:
                    raise ValueError("Kar hedefi 1.0'dan büyük olmalı (örn: 2.0 for 2x).")
                await set_bot_setting("profit_target_x", str(target_x))
                await event.reply(f"✅ Kar hedefi `{target_x}x` olarak ayarlandı.")
                logger.info(f"Yönetici {uid} kar hedefini {target_x}x olarak ayarladı.")
            finally:
                del pending_input[uid]
            return await event.reply(await get_admin_dashboard(),
                                     buttons=await build_auto_trade_keyboard(), parse_mode='md', link_preview=False)
        
        elif action == 'set_stop_loss':
            try:
                stop_loss = float(text_input)
                if not (0 <= stop_loss < 100):
                    raise ValueError("Stop-loss 0 ile 100 arasında olmalı (100 hariç).")
                await set_bot_setting("stop_loss_percent", str(stop_loss))
                await event.reply(f"✅ Stop-loss `{stop_loss}%` olarak ayarlandı.")
                logger.info(f"Yönetici {uid} stop-loss'u %{stop_loss} olarak ayarladı.")
            finally:
                del pending_input[uid]
            return await event.reply(await get_admin_dashboard(),
                                     buttons=await build_auto_trade_keyboard(), parse_mode='md', link_preview=False)
        
        elif action == 'set_wallet_private_key':
            try:
                new_private_key = text_input.strip()
                if not new_private_key:
                    raise ValueError("Özel anahtar boş olamaz.")
                
                await set_bot_setting("SOLANA_PRIVATE_KEY", new_private_key)
                
                await init_solana_client()

                test_keypair = None
                try:
                    test_keypair = Keypair.from_base58_string(new_private_key)
                except Exception as e:
                    await event.reply(f"❌ Girilen özel anahtar geçersiz formatta: {e}")
                    logger.error(f"Yönetici {uid} tarafından girilen geçersiz özel anahtar formatı: {e}")
                    return await event.reply(await get_wallet_settings_dashboard(),
                                             buttons=await build_wallet_settings_keyboard(), parse_mode='md', link_preview=False)

                if payer_keypair and str(payer_keypair.pubkey()) == str(test_keypair.pubkey()):
                     await event.reply(f"✅ Yeni özel anahtar başarıyla ayarlandı. Yeni Genel Anahtar: `{payer_keypair.pubkey()}`")
                     logger.info(f"Yönetici {uid} yeni Solana özel anahtarını ayarladı.")
                else:
                    await event.reply("❌ Özel anahtar ayarlanırken bir sorun oluştu veya geçersiz anahtar.")
                    logger.error(f"Yönetici {uid} için yeni özel anahtar ayarlanamadı.")

            except ValueError as ve:
                await event.reply(f"❌ Geçersiz özel anahtar formatı: {ve}")
            except Exception as e:
                await event.reply(f"❌ Özel anahtar ayarlanırken hata oluştu: {e}")
                logger.error(f"Yönetici {uid} için yeni özel anahtar ayarlanırken hata: {e}")
            finally:
                del pending_input[uid]
            return await event.reply(await get_wallet_settings_dashboard(),
                                     buttons=await build_wallet_settings_keyboard(), parse_mode='md', link_preview=False)

# --- Telegram Mesaj İşleyici (Sinyal Kanalı) ---
@bot_client.on(events.NewMessage(chats=SOURCE_CHANNEL_ID))
async def handle_incoming_signal(event):
    """Belirlenen kaynak kanaldan gelen yeni mesajları işler."""
    message_text = event.message.text
    if not message_text:
        logger.debug("Boş mesaj metni alındı. Atlanıyor.")
        return

    logger.info(f"Kaynak kanaldan mesaj alındı {event.chat_id}: {message_text[:100]}...")

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
            await set_bot_setting("bot_status", "running")
            logger.info("Bot duraklatması sona erdi. İşlemler devam ediyor.")

    contract_address = extract_contract(message_text)
    token_name = extract_token_name_from_message(message_text)

    if contract_address:
        logger.info(f"Kontrat adresi bulundu: {contract_address}. Otomatik alım başlatılıyor.")
        
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
                await bot_client.send_message(DEFAULT_ADMIN_ID, "❌ Otomatik alım/satım ayarları geçersiz. Lütfen kontrol edin.", parse_mode='md')
                return

            success, result_message, actual_buy_price_sol, bought_amount_token = await auto_buy_token(
                contract_address, token_name, buy_amount_sol, slippage_tolerance_percent
            )
            
            admin_message = f"💰 Otomatik Alım Durumu: {result_message}"
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
    
    admins = await get_admins()
    if not admins:
        await add_admin(DEFAULT_ADMIN_ID, "Varsayılan", "Yönetici", is_default=True)
        logger.info(f"Varsayılan yönetici {DEFAULT_ADMIN_ID} eklendi.")
    
    for setting_key, default_value in DEFAULT_BOT_SETTINGS.items():
        current_value = await get_bot_setting(setting_key)
        if current_value is None:
            await set_bot_setting(setting_key, default_value)
            logger.info(f"Varsayılan ayar {setting_key} -> {default_value} olarak ayarlandı.")
        else:
            logger.info(f"Ayar {setting_key} zaten mevcut: {current_value}")

    await init_solana_client()

    logger.info("Telegram istemcisi bağlanıyor...")
    await bot_client.start(bot_token=BOT_TOKEN)
    logger.info("Telegram istemcisi bağlandı.")

    me_bot = await bot_client.get_me()
    logger.info(f"Otomatik Alım/Satım Botu: @{me_bot.username} ({me_bot.id})")
    logger.info(f"Otomatik Alım/Satım Botu şu anda kanal ID'sini dinliyor: {SOURCE_CHANNEL_ID}")
    logger.info(f"Otomatik Alım Miktarı: {await get_bot_setting('buy_amount_sol')} SOL")
    logger.info(f"Kayma Toleransı: {await get_bot_setting('slippage_tolerance')}%")
    logger.info(f"Kar Hedefi: {await get_bot_setting('profit_target_x')}x")
    logger.info(f"Stop-Loss: {await get_bot_setting('stop_loss_percent')}%")

    asyncio.create_task(monitor_positions_task())
    logger.info("Pozisyon izleme görevi başlatıldı.")

    def run_flask():
        app.run(host='0.0.0.0', port=os.environ.get('PORT', 5000))

    flask_thread = threading.Thread(target=run_flask)
    flask_thread.start()
    logger.info("Flask web sunucusu başlatıldı.")

    logger.info("Bot çalışıyor. Durdurmak için Ctrl+C tuşlarına basın.")
    await bot_client.run_until_disconnected()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot kullanıcı tarafından durduruldu.")
    except Exception as e:
        logger.critical(f"Beklenmeyen bir hata oluştu: {e}", exc_info=True)

