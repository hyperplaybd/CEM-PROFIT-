#!/usr/bin/env python3
"""CEM PROFIT Telegram Mini App backend and bot.

- aiohttp only (plus Python standard library)
- Serves the Mini App and its authenticated JSON API from the same HTTPS origin
- Verifies USDT BEP-20 transfers on BNB Smart Chain using JSON-RPC
- Credits balances atomically and prevents transaction-hash reuse
- Validates Telegram Mini App initData before trusting a user ID
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qsl
from typing import Any

from aiohttp import ClientSession, ClientTimeout, FormData, web

BASE_DIR = Path(__file__).resolve().parent

# ===== Configuration: keep credentials private and never commit this file. =====
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip() or "9098089876:AAE2jBRPkyc_P_RhNJodm8nRix3sjPhDij8"
BOT_USERNAME = os.environ.get("BOT_USERNAME", "").strip().lstrip("@")
ADMIN_CHAT_ID_RAW = "8707046428"
DEPOSIT_WALLET = "0x779c92d10E16Ba7007d21480AC882A7C40Fda159".lower()
USDT_CONTRACT = "0x55d398326f99059ff775485246999027b3197955".lower()
BSC_RPC_URL = "https://bsc-dataseed.bnbchain.org"
# Render automatically provides RENDER_EXTERNAL_URL.
SELF_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
BOT_WEBAPP_URL = (SELF_URL + "/") if SELF_URL else "https://YOUR-DOMAIN.com/"
KEEPALIVE_INTERVAL = 600   # seconds: keep the Render service warm
BACKUP_INTERVAL = 60       # seconds: send a Telegram database backup after changes
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8080"))
DATABASE_PATH = BASE_DIR / "trading_bot.db"
DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
MIN_CONFIRMATIONS = 12
MIN_DEPOSIT_UNITS = 10**17  # 0.10 USDT (BEP-20 USDT uses 18 decimals)
MIN_WITHDRAWAL_UNITS = 3 * 10**17  # 0.30 USDT
PLAN_AMOUNTS = {10**17, 2 * 10**17, 5 * 10**17, 10**18, 15 * 10**17, 2 * 10**18,
                3 * 10**18, 5 * 10**18, 8 * 10**18, 10 * 10**18, 13 * 10**18,
                15 * 10**18, 18 * 10**18, 20 * 10**18}
AUTH_MAX_AGE = 86400
USDT_SCALE = 10**18

logging.basicConfig(
    level="INFO",
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("cem-profit")

TX_HASH_RE = re.compile(r"^0x[a-fA-F0-9]{64}$")
ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def validate_config() -> int:
    if not BOT_TOKEN or ":" not in BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing or invalid.")
    if not ADMIN_CHAT_ID_RAW:
        raise RuntimeError("ADMIN_CHAT_ID is not configured.")
    try:
        admin_id = int(ADMIN_CHAT_ID_RAW)
    except ValueError as exc:
        raise RuntimeError("ADMIN_CHAT_ID must be numeric.") from exc
    if not ADDRESS_RE.fullmatch(DEPOSIT_WALLET):
        raise RuntimeError("Set a valid EVM wallet address in DEPOSIT_WALLET.")
    if not ADDRESS_RE.fullmatch(USDT_CONTRACT):
        raise RuntimeError("Set a valid token contract address in USDT_CONTRACT.")
    if not BOT_WEBAPP_URL.startswith("https://") or "github.io" in BOT_WEBAPP_URL or "YOUR-DOMAIN" in BOT_WEBAPP_URL:
        raise RuntimeError(
            "BOT_WEBAPP_URL must be this server’s public HTTPS domain "
            "(GitHub Pages is not supported because the API runs on this server)."
        )
    return admin_id


ADMIN_CHAT_ID = validate_config()


def restore_db_if_missing() -> None:
    """Restore the latest pinned database backup if the deployment disk is ephemeral.
    Stop startup on restoration failure to avoid overwriting a valid backup with an empty database."""
    if DATABASE_PATH.exists() and DATABASE_PATH.stat().st_size > 0:
        return
    base = f"https://api.telegram.org/bot{BOT_TOKEN}"

    def fetch(url: str) -> bytes:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.read()

    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            try:
                chat = json.loads(fetch(f"{base}/getChat?chat_id={ADMIN_CHAT_ID}"))
            except urllib.error.HTTPError as http_exc:
                if http_exc.code in (400, 403):
                    logger.warning("Administrator chat not found (send /start to the bot first); starting with a new database")
                    return
                raise
            doc = ((chat.get("result") or {}).get("pinned_message") or {}).get("document") or {}
            if not str(doc.get("file_name", "")).startswith("cem_backup"):
                logger.info("No Telegram backup found; starting with a new database")
                return
            info = json.loads(fetch(f"{base}/getFile?file_id={doc['file_id']}"))
            blob = fetch(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{info['result']['file_path']}")
            if not blob.startswith(b"SQLite format 3"):
                raise RuntimeError("Backup is not a valid SQLite database")
            DATABASE_PATH.write_bytes(blob)
            logger.info("Database restored from Telegram backup (%d bytes)", len(blob))
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logger.warning("Restore attempt %d failed: %s", attempt, exc)
            time.sleep(3)
    raise RuntimeError(f"Could not restore database from backup; startup stopped: {last_error}")


restore_db_if_missing()

# One process / one event loop uses this connection. SQL writes that affect funds
# are protected by BEGIN IMMEDIATE and are kept atomic.
conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False, timeout=30)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA busy_timeout=30000")


def init_database() -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            balance REAL DEFAULT 0.0,
            balance_units TEXT DEFAULT '0',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    if "balance" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN balance REAL DEFAULT 0.0")
    if "balance_units" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN balance_units TEXT DEFAULT '0'")
        for row in conn.execute("SELECT user_id, balance FROM users").fetchall():
            legacy_balance = Decimal(str(row["balance"] or 0))
            units = int((legacy_balance * USDT_SCALE).to_integral_value())
            conn.execute("UPDATE users SET balance_units = ? WHERE user_id = ?", (str(units), row["user_id"]))
    else:
        # Normalize values to decimal strings, including databases previously
        # created by an earlier build of this application.
        for row in conn.execute("SELECT user_id, balance_units FROM users").fetchall():
            units = str(int(row["balance_units"] or 0))
            conn.execute("UPDATE users SET balance_units = ? WHERE user_id = ?", (units, row["user_id"]))

    conn.execute(
        """CREATE TABLE IF NOT EXISTS used_txids (
            txid TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            amount REAL DEFAULT 0.0,
            amount_units TEXT DEFAULT '0',
            block_number INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    tx_columns = {row["name"] for row in conn.execute("PRAGMA table_info(used_txids)")}
    if "amount" not in tx_columns:
        conn.execute("ALTER TABLE used_txids ADD COLUMN amount REAL DEFAULT 0.0")
    if "amount_units" not in tx_columns:
        conn.execute("ALTER TABLE used_txids ADD COLUMN amount_units TEXT DEFAULT '0'")
        for row in conn.execute("SELECT txid, amount FROM used_txids").fetchall():
            legacy_amount = Decimal(str(row["amount"] or 0))
            units = int((legacy_amount * USDT_SCALE).to_integral_value())
            conn.execute("UPDATE used_txids SET amount_units = ? WHERE txid = ?", (str(units), row["txid"]))
    else:
        for row in conn.execute("SELECT txid, amount_units FROM used_txids").fetchall():
            units = str(int(row["amount_units"] or 0))
            conn.execute("UPDATE used_txids SET amount_units = ? WHERE txid = ?", (units, row["txid"]))
    if "block_number" not in tx_columns:
        conn.execute("ALTER TABLE used_txids ADD COLUMN block_number INTEGER")
    if "created_at" not in tx_columns:
        conn.execute("ALTER TABLE used_txids ADD COLUMN created_at TEXT")
    conn.execute("UPDATE used_txids SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL")

    conn.execute(
        """CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount_units TEXT NOT NULL,
            wallet_address TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            processed_at TEXT
        )"""
    )
    withdrawal_columns = {row["name"] for row in conn.execute("PRAGMA table_info(withdrawals)")}
    if "source" not in withdrawal_columns:
        conn.execute("ALTER TABLE withdrawals ADD COLUMN source TEXT NOT NULL DEFAULT 'balance'")
    conn.execute("CREATE TABLE IF NOT EXISTS plans (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, amount_units TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'balance', status TEXT NOT NULL DEFAULT 'active', started_at TEXT DEFAULT CURRENT_TIMESTAMP, ends_at TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS referrals (id INTEGER PRIMARY KEY AUTOINCREMENT, referrer_id INTEGER NOT NULL, referred_id INTEGER NOT NULL UNIQUE, joined_at TEXT DEFAULT CURRENT_TIMESTAMP, plan_purchased INTEGER NOT NULL DEFAULT 0)")
    conn.execute("""CREATE TABLE IF NOT EXISTS plan_earnings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        amount_units TEXT NOT NULL,
        period_key TEXT NOT NULL,
        available_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        claimed_at TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(plan_id, period_key)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS profit_claims (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        amount_units TEXT NOT NULL,
        claimed_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    if "commission_units" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN commission_units TEXT DEFAULT '0'")
    conn.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT OR IGNORE INTO app_settings(key,value) VALUES ('daily_rate_percent','50')")
    conn.commit()


init_database()


def format_units(units: int) -> str:
    value = Decimal(int(units)) / Decimal(USDT_SCALE)
    return f"{value:,.4f}"


def parse_usdt(value: Any) -> int:
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError) as exc:
        raise ValueError("Enter a valid amount.") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Amount must be greater than zero.")
    scaled = amount * USDT_SCALE
    if scaled != scaled.to_integral_value():
        raise ValueError("Amounts may contain up to 18 decimal places.")
    return int(scaled)


def ensure_user(user_id: int) -> None:
    conn.execute("INSERT OR IGNORE INTO users (user_id, balance, balance_units) VALUES (?, 0, '0')", (user_id,))
    conn.commit()


def get_user_balance(user_id: int) -> int:
    ensure_user(user_id)
    row = conn.execute("SELECT balance_units FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return int(row["balance_units"] or 0)


def get_reserved_withdrawals(user_id: int, source: str = "balance") -> int:
    rows = conn.execute(
        "SELECT amount_units FROM withdrawals WHERE user_id = ? AND status = 'pending' AND source = ?",
        (user_id, source),
    ).fetchall()
    return sum(int(row["amount_units"] or 0) for row in rows)


def credit_deposit(txid: str, user_id: int, amount_units: int, block_number: int) -> int:
    """Insert a unique TXID and credit the user in one SQLite transaction."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        previous = conn.execute("SELECT user_id, amount_units FROM used_txids WHERE txid = ?", (txid,)).fetchone()
        if previous:
            raise DuplicateTransaction(int(previous["user_id"]), int(previous["amount_units"] or 0))
        conn.execute(
            "INSERT INTO used_txids (txid, user_id, amount, amount_units, block_number, created_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (txid, user_id, float(Decimal(amount_units) / Decimal(USDT_SCALE)), str(amount_units), block_number),
        )
        conn.execute("INSERT OR IGNORE INTO users (user_id, balance, balance_units) VALUES (?, 0, '0')", (user_id,))
        row = conn.execute("SELECT balance_units FROM users WHERE user_id = ?", (user_id,)).fetchone()
        new_balance = int(row["balance_units"] or 0) + int(amount_units)
        conn.execute(
            "UPDATE users SET balance_units = ?, balance = ? WHERE user_id = ?",
            (str(new_balance), float(Decimal(new_balance) / Decimal(USDT_SCALE)), user_id),
        )
        conn.commit()
        return new_balance
    except Exception:
        conn.rollback()
        raise


class DuplicateTransaction(Exception):
    def __init__(self, owner_id: int, amount_units: int):
        self.owner_id = owner_id
        self.amount_units = amount_units
        super().__init__("Transaction hash already used")


def create_withdrawal(user_id: int, amount_units: int, wallet: str, source: str = "balance") -> int:
    if source not in ("balance", "commission"):
        raise ValueError("Invalid withdrawal source.")
    if amount_units < MIN_WITHDRAWAL_UNITS:
        raise ValueError("Minimum withdrawal is 0.30 USDT.")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("INSERT OR IGNORE INTO users (user_id, balance, balance_units) VALUES (?, 0, '0')", (user_id,))
        col = "commission_units" if source == "commission" else "balance_units"
        row = conn.execute(f"SELECT {col} AS funds FROM users WHERE user_id = ?", (user_id,)).fetchone()
        reserved = get_reserved_withdrawals(user_id, source)
        available = int(row["funds"] or 0) - reserved
        if amount_units > available:
            raise ValueError("Insufficient available balance in the selected wallet.")
        cur = conn.execute(
            "INSERT INTO withdrawals (user_id, amount_units, wallet_address, source) VALUES (?, ?, ?, ?)",
            (user_id, str(amount_units), wallet, source),
        )
        withdrawal_id = int(cur.lastrowid)
        conn.commit()
        return withdrawal_id
    except Exception:
        conn.rollback()
        raise


def process_withdrawal(withdrawal_id: int, action: str) -> tuple[int, int, str] | None:
    """Admin confirms a manually sent payout or rejects the request."""
    if action not in ("paid", "rejected"):
        return None
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM withdrawals WHERE id = ?", (withdrawal_id,)).fetchone()
        if not row or row["status"] != "pending":
            conn.rollback()
            return None
        user_id = int(row["user_id"])
        amount_units = int(row["amount_units"])
        wallet = str(row["wallet_address"])
        source = str(row["source"] or "balance")
        column = "commission_units" if source == "commission" else "balance_units"
        if action == "paid":
            balance_row = conn.execute(f"SELECT {column} AS units FROM users WHERE user_id = ?", (user_id,)).fetchone()
            balance = int(balance_row["units"] or 0) if balance_row else 0
            if balance < amount_units:
                raise ValueError("User balance is lower than the pending withdrawal.")
            new_balance = balance - amount_units
            if source == "commission":
                conn.execute("UPDATE users SET commission_units = ? WHERE user_id = ?", (str(new_balance), user_id))
            else:
                conn.execute(
                    "UPDATE users SET balance_units = ?, balance = ? WHERE user_id = ?",
                    (str(new_balance), float(Decimal(new_balance) / Decimal(USDT_SCALE)), user_id),
                )
        conn.execute(
            "UPDATE withdrawals SET status = ?, processed_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'pending'",
            (action, withdrawal_id),
        )
        conn.commit()
        return user_id, amount_units, wallet
    except Exception:
        conn.rollback()
        raise


def get_history(user_id: int, limit: int = 20) -> list[dict[str, Any]]:
    deposits = conn.execute(
        "SELECT txid, amount_units, created_at, block_number FROM used_txids WHERE user_id = ? ORDER BY rowid DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    withdrawals = conn.execute(
        "SELECT id, amount_units, wallet_address, status, created_at, source FROM withdrawals WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    items: list[dict[str, Any]] = []
    for row in deposits:
        items.append({
            "kind": "deposit",
            "amount": format_units(int(row["amount_units"] or 0)),
            "reference": row["txid"],
            "created_at": row["created_at"] or "",
            "status": "completed",
            "block_number": row["block_number"],
        })
    for row in withdrawals:
        items.append({
            "kind": "withdrawal",
            "amount": format_units(int(row["amount_units"])),
            "reference": row["wallet_address"],
            "created_at": row["created_at"] or "",
            "status": row["status"],
            "request_id": int(row["id"]),
            "source": str(row["source"] or "balance"),
        })
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return items[:limit]


def validate_telegram_init_data(raw_data: str) -> int:
    """Validate Telegram's signed initData and return the trusted user ID."""
    if not raw_data or len(raw_data) > 16_384:
        raise ValueError("Telegram session not found. Open the app inside Telegram.")
    pairs = parse_qsl(raw_data, keep_blank_values=True, strict_parsing=True)
    values: dict[str, str] = {}
    for key, value in pairs:
        if key in values:
            raise ValueError("Invalid Telegram session.")
        values[key] = value
    received_hash = values.pop("hash", "")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", received_hash):
        raise ValueError("Telegram session signature is missing.")
    data_check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated_hash, received_hash.lower()):
        raise ValueError("Telegram session could not be verified. Close and reopen the app.")
    try:
        auth_date = int(values.get("auth_date", "0"))
        if auth_date <= 0 or time.time() - auth_date > AUTH_MAX_AGE or auth_date - time.time() > 300:
            raise ValueError("Telegram session has expired. Reopen the app.")
        user_data = json.loads(values["user"])
        user_id = int(user_data["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("Telegram session"):
            raise
        raise ValueError("Telegram user information is missing.") from exc
    if user_id <= 0:
        raise ValueError("Invalid Telegram user ID.")
    return user_id


async def authorized_user(request: web.Request) -> int:
    raw_data = request.headers.get("X-Telegram-Init-Data", "")
    try:
        user_id = validate_telegram_init_data(raw_data)
        register_referral_from_init(raw_data, user_id)
        return user_id
    except ValueError as exc:
        raise web.HTTPUnauthorized(text=json.dumps({"error": str(exc)}, ensure_ascii=False), content_type="application/json") from exc


async def rpc_call(session: ClientSession, method: str, params: list[Any]) -> Any:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    async with session.post(BSC_RPC_URL, json=payload) as response:
        response.raise_for_status()
        data = await response.json(content_type=None)
    if not isinstance(data, dict):
        raise RuntimeError("RPC response format is invalid")
    if data.get("error"):
        raise RuntimeError(str(data["error"].get("message", "BNB Smart Chain RPC error")))
    if "result" not in data:
        raise RuntimeError("RPC response has no result")
    return data["result"]


async def verify_bep20_usdt_tx(session: ClientSession, txid: str) -> dict[str, Any]:
    """Return pending / failed / success; never trusts client-supplied amount."""
    try:
        chain_id, receipt = await asyncio.gather(
            rpc_call(session, "eth_chainId", []),
            rpc_call(session, "eth_getTransactionReceipt", [txid]),
        )
        if int(chain_id, 16) != 56:
            return {"status": "error", "message": "The RPC endpoint is not BNB Smart Chain Mainnet."}
        if receipt is None:
            tx = await rpc_call(session, "eth_getTransactionByHash", [txid])
            if tx is None:
                return {"status": "not_found", "message": "This transaction hash was not found on BNB Smart Chain. Check the hash and network."}
            return {"status": "pending", "confirmations": 0, "required_confirmations": MIN_CONFIRMATIONS,
                    "message": "Transaction detected. Waiting for block inclusion."}
        if str(receipt.get("status", "")).lower() != "0x1":
            return {"status": "failed", "message": "This transaction failed on BNB Smart Chain."}
        block_number = int(receipt.get("blockNumber", "0x0"), 16)
        latest_block = int(await rpc_call(session, "eth_blockNumber", []), 16)
        confirmations = max(0, latest_block - block_number + 1)
        if confirmations < MIN_CONFIRMATIONS:
            return {
                "status": "pending",
                "confirmations": confirmations,
                "required_confirmations": MIN_CONFIRMATIONS,
                "message": "Transaction detected. Waiting for the required confirmations.",
            }

        amount_units = 0
        for log in receipt.get("logs", []):
            topics = log.get("topics") or []
            if (
                str(log.get("address", "")).lower() == USDT_CONTRACT
                and len(topics) >= 3
                and str(topics[0]).lower() == TRANSFER_TOPIC
            ):
                receiver = "0x" + str(topics[2])[-40:].lower()
                if receiver == DEPOSIT_WALLET:
                    try:
                        amount_units += int(str(log.get("data", "0x0")), 16)
                    except ValueError:
                        continue
        if amount_units <= 0:
            return {
                "status": "failed",
                "message": "No valid USDT (BEP-20) transfer to the designated wallet was found in this transaction.",
            }
        return {
            "status": "success",
            "amount_units": amount_units,
            "amount": format_units(amount_units),
            "block_number": block_number,
            "confirmations": confirmations,
        }
    except asyncio.TimeoutError:
        return {"status": "error", "message": "BNB Smart Chain RPC is taking too long to respond. Please try again."}
    except Exception as exc:
        logger.exception("BSC transaction verification error: %s", exc)
        return {"status": "error", "message": "The blockchain verification service is temporarily unavailable. Please try again later."}


async def telegram_call(session: ClientSession, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    async with session.post(url, json=payload) as response:
        data = await response.json(content_type=None)
    if not data.get("ok"):
        logger.warning("Telegram %s failed: %s", method, data)
    return data


async def notify_admin(session: ClientSession, text: str, reply_markup: dict[str, Any] | None = None) -> bool:
    payload: dict[str, Any] = {"chat_id": ADMIN_CHAT_ID, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        result = await telegram_call(session, "sendMessage", payload)
        return bool(result.get("ok"))
    except Exception:
        logger.exception("Could not notify admin chat")
        return False


def register_referral_from_init(raw_data: str, user_id: int) -> None:
    try:
        params = dict(parse_qsl(raw_data, keep_blank_values=True))
        start_param = params.get("start_param") or params.get("startapp") or params.get("tgWebAppStartParam", "")
        match = re.fullmatch(r"ref_(\d+)", start_param)
        if not match:
            return
        referrer = int(match.group(1))
        if referrer == user_id:
            return
        known_referrer = conn.execute("SELECT 1 FROM users WHERE user_id=?", (referrer,)).fetchone()
        if not known_referrer:
            return
        ensure_user(user_id)
        conn.execute("INSERT OR IGNORE INTO referrals(referrer_id,referred_id) VALUES (?,?)", (referrer,user_id))
        conn.commit()
    except Exception:
        logger.exception("Referral registration failed")


def register_referral_from_start(user_id: int, start_payload: str) -> bool:
    """Attribute a Telegram /start ref_<user_id> deep link exactly once."""
    match = re.fullmatch(r"ref_(\d{1,20})", str(start_payload or "").strip())
    if not match:
        return False
    referrer_id = int(match.group(1))
    if referrer_id <= 0 or referrer_id == user_id:
        return False
    known_referrer = conn.execute("SELECT 1 FROM users WHERE user_id=?", (referrer_id,)).fetchone()
    if not known_referrer:
        logger.info("Ignoring referral payload from unknown referrer %s", referrer_id)
        return False
    ensure_user(user_id)
    existing = conn.execute("SELECT referrer_id FROM referrals WHERE referred_id=?", (user_id,)).fetchone()
    if existing:
        return int(existing["referrer_id"]) == referrer_id
    conn.execute(
        "INSERT OR IGNORE INTO referrals(referrer_id,referred_id) VALUES (?,?)",
        (referrer_id, user_id),
    )
    conn.commit()
    logger.info("Referral attributed: referred=%s referrer=%s", user_id, referrer_id)
    return True


async def api_config(request: web.Request) -> web.Response:
    row = conn.execute("SELECT value FROM app_settings WHERE key='daily_rate_percent'").fetchone()
    return web.json_response({"daily_rate_percent": float(row["value"]) if row else 50.0})


async def api_plans(request: web.Request) -> web.Response:
    user_id = await authorized_user(request)
    now = conn.execute("SELECT CURRENT_TIMESTAMP AS now, strftime('%s','now') AS now_ts").fetchone()
    conn.execute(
        "UPDATE plans SET status='completed' WHERE user_id=? AND status='active' AND datetime(ends_at) <= CURRENT_TIMESTAMP",
        (user_id,),
    )
    conn.commit()
    rows = conn.execute(
        "SELECT id, amount_units, status, started_at, ends_at, "
        "CAST(strftime('%s', started_at) AS INTEGER) AS started_at_ts, "
        "CAST(strftime('%s', ends_at) AS INTEGER) AS ends_at_ts "
        "FROM plans WHERE user_id=? ORDER BY id DESC",
        (user_id,),
    ).fetchall()
    items = []
    total_claimed = 0
    total_unclaimed = 0
    for row in rows:
        plan_id = int(row["id"])
        claimed_rows = conn.execute(
            "SELECT amount_units FROM plan_earnings WHERE plan_id=? AND claimed_at IS NOT NULL", (plan_id,)
        ).fetchall()
        unclaimed_rows = conn.execute(
            "SELECT amount_units FROM plan_earnings WHERE plan_id=? AND claimed_at IS NULL", (plan_id,)
        ).fetchall()
        claimed = sum(int(item["amount_units"] or 0) for item in claimed_rows)
        unclaimed = sum(int(item["amount_units"] or 0) for item in unclaimed_rows)
        total_claimed += claimed
        total_unclaimed += unclaimed
        items.append({
            "id": plan_id,
            "amount": format_units(int(row["amount_units"])),
            "status": row["status"],
            "started_at": row["started_at"],
            "ends_at": row["ends_at"],
            "started_at_ts": int(row["started_at_ts"] or 0),
            "ends_at_ts": int(row["ends_at_ts"] or 0),
            "realized_pnl": format_units(claimed + unclaimed),
            "claimed_profit": format_units(claimed),
            "unclaimed_profit": format_units(unclaimed),
        })
    return web.json_response({
        "items": items,
        "claimable_profit": format_units(total_unclaimed),
        "unclaimed_profit": format_units(total_unclaimed),
        "claimed_profit": format_units(total_claimed),
        "total_realized_profit": format_units(total_claimed + total_unclaimed),
        "can_claim": total_unclaimed > 0,
        "next_claim_at": None,
        "server_time": now["now"],
        "server_time_ts": int(now["now_ts"] or 0),
    })


async def api_purchase_plan(request: web.Request) -> web.Response:
    user_id = await authorized_user(request)
    try:
        body = await request.json()
        amount = parse_usdt(body.get("amount", ""))
        source = str(body.get("source", "balance"))
        if source == "main":
            source = "balance"
        if amount not in PLAN_AMOUNTS:
            raise ValueError("Select one of the available plan amounts.")
        if source not in ("balance", "commission"):
            raise ValueError("Invalid payment source.")
        ensure_user(user_id)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE plans SET status='completed' WHERE user_id=? AND status='active' AND datetime(ends_at) <= CURRENT_TIMESTAMP",
            (user_id,),
        )
        active_same_plan = conn.execute(
            "SELECT id, ends_at FROM plans WHERE user_id=? AND amount_units=? AND status='active' "
            "AND datetime(ends_at) > CURRENT_TIMESTAMP ORDER BY id DESC LIMIT 1",
            (user_id, str(amount)),
        ).fetchone()
        if active_same_plan:
            raise ValueError(
                f"You already have an active {format_units(amount)} USDT plan (Plan #{int(active_same_plan['id'])}). "
                f"You can buy this plan again after it expires on {active_same_plan['ends_at']} UTC."
            )
        col = "commission_units" if source == "commission" else "balance_units"
        row = conn.execute(f"SELECT {col} AS funds FROM users WHERE user_id=?", (user_id,)).fetchone()
        funds = int(row["funds"] or 0)
        if funds < amount:
            raise ValueError("Insufficient balance in the selected wallet.")
        conn.execute(f"UPDATE users SET {col}=? WHERE user_id=?", (str(funds-amount), user_id))
        cur = conn.execute(
            "INSERT INTO plans(user_id,amount_units,source,ends_at) VALUES (?,?,?,datetime('now','+10 days'))",
            (user_id, str(amount), source),
        )
        plan_id = int(cur.lastrowid)
        referral = conn.execute(
            "SELECT referrer_id, plan_purchased FROM referrals WHERE referred_id=?", (user_id,)
        ).fetchone()
        commission_added = 0
        if referral and not referral["plan_purchased"] and int(referral["referrer_id"]) != user_id:
            commission_added = parse_usdt("0.05")
            conn.execute("INSERT OR IGNORE INTO users(user_id,balance,balance_units) VALUES (?,0,'0')", (int(referral["referrer_id"]),))
            referrer_balance = conn.execute(
                "SELECT commission_units FROM users WHERE user_id=?", (int(referral["referrer_id"]),)
            ).fetchone()
            current_commission = int(referrer_balance["commission_units"] or 0) if referrer_balance else 0
            conn.execute(
                "UPDATE users SET commission_units=? WHERE user_id=?",
                (str(current_commission + commission_added), int(referral["referrer_id"])),
            )
            conn.execute("UPDATE referrals SET plan_purchased=1 WHERE referred_id=?", (user_id,))
        conn.commit()
        return web.json_response({
            "status": "success",
            "message": "Plan activated successfully.",
            "plan_id": plan_id,
            "amount": format_units(amount),
            "term_days": 10,
            "referral_commission_added": format_units(commission_added),
        })
    except (json.JSONDecodeError, ValueError) as exc:
        conn.rollback()
        raise web.HTTPBadRequest(text=json.dumps({"error": str(exc)}, ensure_ascii=False), content_type="application/json") from exc
    except Exception:
        conn.rollback()
        logger.exception("Plan purchase failed")
        raise


async def api_profit_claim(request: web.Request) -> web.Response:
    user_id = await authorized_user(request)
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute(
            "SELECT id, amount_units FROM plan_earnings WHERE user_id=? AND claimed_at IS NULL "
            "AND datetime(available_at) <= CURRENT_TIMESTAMP",
            (user_id,),
        ).fetchall()
        amount_units = sum(int(row["amount_units"] or 0) for row in rows)
        if amount_units <= 0:
            raise ValueError("No verified trading profit is available to claim yet.")
        conn.execute("INSERT OR IGNORE INTO users(user_id,balance,balance_units) VALUES (?,0,'0')", (user_id,))
        balance_row = conn.execute("SELECT balance_units FROM users WHERE user_id=?", (user_id,)).fetchone()
        new_balance = int(balance_row["balance_units"] or 0) + amount_units
        conn.execute(
            "UPDATE users SET balance_units=?, balance=? WHERE user_id=?",
            (str(new_balance), float(Decimal(new_balance) / Decimal(USDT_SCALE)), user_id),
        )
        ids = [int(row["id"]) for row in rows]
        conn.executemany(
            "UPDATE plan_earnings SET claimed_at=CURRENT_TIMESTAMP WHERE id=? AND claimed_at IS NULL",
            [(earning_id,) for earning_id in ids],
        )
        conn.execute("INSERT INTO profit_claims(user_id,amount_units) VALUES (?,?)", (user_id, str(amount_units)))
        conn.commit()
        return web.json_response({
            "status": "success",
            "amount": format_units(amount_units),
            "balance": format_units(new_balance),
            "message": "Verified profit has been credited to your main balance.",
        })
    except (ValueError, json.JSONDecodeError) as exc:
        conn.rollback()
        raise web.HTTPBadRequest(text=json.dumps({"error": str(exc)}, ensure_ascii=False), content_type="application/json") from exc
    except Exception:
        conn.rollback()
        logger.exception("Profit claim failed")
        raise


async def api_admin_plans(request: web.Request) -> web.Response:
    admin_id = await authorized_user(request)
    if admin_id != ADMIN_CHAT_ID:
        raise web.HTTPForbidden(text=json.dumps({"error": "Administrator access required."}), content_type="application/json")
    rows = conn.execute(
        "SELECT id,user_id,amount_units,status,started_at,ends_at FROM plans ORDER BY id DESC LIMIT 500"
    ).fetchall()
    return web.json_response({"items": [
        {"plan_id": int(row["id"]), "user_id": int(row["user_id"]),
         "amount": format_units(int(row["amount_units"])), "status": row["status"],
         "started_at": row["started_at"], "ends_at": row["ends_at"]}
        for row in rows
    ]})


async def api_admin_settle_profit(request: web.Request) -> web.Response:
    admin_id = await authorized_user(request)
    if admin_id != ADMIN_CHAT_ID:
        raise web.HTTPForbidden(text=json.dumps({"error": "Administrator access required."}), content_type="application/json")
    try:
        body = await request.json()
        plan_id = int(body.get("plan_id", 0))
        amount_units = parse_usdt(body.get("amount", ""))
        period_key = str(body.get("period_key", "")).strip()
        if plan_id <= 0 or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", period_key):
            raise ValueError("Provide a valid plan_id and unique period_key.")
        plan = conn.execute("SELECT user_id,status FROM plans WHERE id=?", (plan_id,)).fetchone()
        if not plan:
            raise ValueError("Plan not found.")
        conn.execute(
            "INSERT INTO plan_earnings(plan_id,user_id,amount_units,period_key,available_at) VALUES (?,?,?,?,CURRENT_TIMESTAMP)",
            (plan_id, int(plan["user_id"]), str(amount_units), period_key),
        )
        conn.commit()
        return web.json_response({"status": "success", "plan_id": plan_id, "amount": format_units(amount_units)})
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise web.HTTPConflict(text=json.dumps({"error": "Profit for this plan and period has already been recorded."}), content_type="application/json") from exc
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        conn.rollback()
        raise web.HTTPBadRequest(text=json.dumps({"error": str(exc)}), content_type="application/json") from exc



async def api_referrals(request: web.Request) -> web.Response:
    global BOT_USERNAME
    user_id = await authorized_user(request)
    ensure_user(user_id)
    rows = conn.execute(
        "SELECT r.referred_id,r.joined_at,r.plan_purchased FROM referrals r "
        "WHERE r.referrer_id=? ORDER BY r.id DESC",
        (user_id,),
    ).fetchall()
    user = conn.execute("SELECT commission_units FROM users WHERE user_id=?", (user_id,)).fetchone()
    commission_reserved = get_reserved_withdrawals(user_id, "commission")
    commission_total = int(user["commission_units"] or 0)
    if not BOT_USERNAME:
        try:
            async with request.app["http_session"].get(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe") as resp:
                bot_data = await resp.json(content_type=None)
            if bot_data.get("ok"):
                BOT_USERNAME = str((bot_data.get("result") or {}).get("username", "")).lstrip("@")
        except Exception:
            logger.exception("Could not resolve Telegram bot username")
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}" if BOT_USERNAME else ""
    successful = sum(1 for r in rows if r["plan_purchased"])
    return web.json_response({
        "total": len(rows),
        "successful": successful,
        "pending": len(rows) - successful,
        "referral_link": link,
        "referral_link_ready": bool(link),
        "commission_balance": format_units(commission_total),
        "commission_available": format_units(max(0, commission_total - commission_reserved)),
        "items": [
            {"user_id": r["referred_id"], "joined_at": r["joined_at"], "plan_purchased": bool(r["plan_purchased"])}
            for r in rows
        ],
    })


async def api_me(request: web.Request) -> web.Response:
    user_id = await authorized_user(request)
    balance = get_user_balance(user_id)
    reserved = get_reserved_withdrawals(user_id)
    ensure_user(user_id)
    commission = conn.execute("SELECT commission_units FROM users WHERE user_id=?", (user_id,)).fetchone()
    commission_reserved = get_reserved_withdrawals(user_id, "commission")
    return web.json_response({
        "user_id": user_id,
        "balance": format_units(balance),
        "balance_units": balance,
        "commission_balance": format_units(int(commission["commission_units"] or 0)),
        "commission_reserved": format_units(commission_reserved),
        "commission_available": format_units(max(0, int(commission["commission_units"] or 0) - commission_reserved)),
        "reserved": format_units(reserved),
        "available": format_units(max(0, balance - reserved)),
        "wallet": DEPOSIT_WALLET,
        "network": "BNB Smart Chain",
        "token": "USDT (BEP-20)",
        "min_confirmations": MIN_CONFIRMATIONS,
    })


async def api_history(request: web.Request) -> web.Response:
    user_id = await authorized_user(request)
    return web.json_response({"items": get_history(user_id)})


async def api_verify_deposit(request: web.Request) -> web.Response:
    user_id = await authorized_user(request)
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        raise web.HTTPBadRequest(text=json.dumps({"error": "Send valid JSON data."}, ensure_ascii=False), content_type="application/json")
    txid = str(body.get("txid", "")).strip().lower()
    if not TX_HASH_RE.fullmatch(txid):
        raise web.HTTPBadRequest(text=json.dumps({"error": "Enter a valid 66-character transaction hash beginning with 0x."}, ensure_ascii=False), content_type="application/json")

    previous = conn.execute("SELECT user_id, amount_units FROM used_txids WHERE txid = ?", (txid,)).fetchone()
    if previous:
        if int(previous["user_id"]) == user_id:
            return web.json_response({
                "status": "already_credited",
                "message": "This transaction has already been credited to your balance.",
                "amount": format_units(int(previous["amount_units"] or 0)),
                "balance": format_units(get_user_balance(user_id)),
            })
        return web.json_response({"status": "duplicate", "message": "This transaction hash has already been used by another account."}, status=409)

    result = await verify_bep20_usdt_tx(request.app["http_session"], txid)
    if result["status"] != "success":
        return web.json_response(result)
    if int(result["amount_units"]) < MIN_DEPOSIT_UNITS:
        return web.json_response({
            "status": "below_minimum",
            "message": "Minimum deposit is 0.10 USDT. This transfer has not been credited.",
            "amount": format_units(int(result["amount_units"])),
            "minimum": "0.1000",
        })
    try:
        new_balance = credit_deposit(txid, user_id, int(result["amount_units"]), int(result["block_number"]))
    except DuplicateTransaction as exc:
        if exc.owner_id == user_id:
            return web.json_response({
                "status": "already_credited",
                "message": "This transaction has already been credited to your balance.",
                "amount": format_units(exc.amount_units),
                "balance": format_units(get_user_balance(user_id)),
            })
        return web.json_response({"status": "duplicate", "message": "This transaction hash has already been used by another account."}, status=409)

    amount_text = format_units(int(result["amount_units"]))
    await notify_admin(
        request.app["http_session"],
        "CEM PROFIT | Deposit verified\n"
        f"User ID: {user_id}\nAmount: {amount_text} USDT\n"
        f"TxID: {txid}\nBlock: {result['block_number']}",
    )
    return web.json_response({
        "status": "success",
        "message": "Deposit verified and credited successfully.",
        "amount": amount_text,
        "balance": format_units(new_balance),
        "confirmations": result["confirmations"],
        "txid": txid,
    })


async def api_withdrawal(request: web.Request) -> web.Response:
    user_id = await authorized_user(request)
    try:
        body = await request.json()
        amount_units = parse_usdt(body.get("amount", ""))
        wallet = str(body.get("wallet", "")).strip()
        source = str(body.get("source", "balance"))
        if source == "main":
            source = "balance"
        if source not in ("balance", "commission"):
            raise ValueError("Invalid withdrawal source.")
        if not ADDRESS_RE.fullmatch(wallet):
            raise ValueError("Enter a valid BEP-20 wallet address.")
        withdrawal_id = create_withdrawal(user_id, amount_units, wallet, source)
    except (json.JSONDecodeError, ValueError) as exc:
        raise web.HTTPBadRequest(text=json.dumps({"error": str(exc)}, ensure_ascii=False), content_type="application/json") from exc

    amount_text = format_units(amount_units)
    markup = {"inline_keyboard": [[
        {"text": "Mark as paid", "callback_data": f"wd_paid:{withdrawal_id}"},
        {"text": "Reject request", "callback_data": f"wd_reject:{withdrawal_id}"},
    ]]}
    admin_notified = await notify_admin(
        request.app["http_session"],
        "CEM PROFIT | Withdrawal request\n"
        f"Request: #{withdrawal_id}\nUser ID: {user_id}\n"
        f"Amount: {amount_text} USDT\nSource: {source}\nWallet: {wallet}\n\n"
        "After sending the funds manually, select Mark as paid. The balance is deducted only then.",
        markup,
    )
    if not admin_notified:
        conn.execute(
            "UPDATE withdrawals SET status = 'rejected', processed_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'pending'",
            (withdrawal_id,),
        )
        conn.commit()
        raise web.HTTPServiceUnavailable(
            text=json.dumps({"error": "The request could not be sent to the administrator. Please try again later."}, ensure_ascii=False),
            content_type="application/json",
        )
    return web.json_response({
        "status": "pending",
        "message": "Withdrawal request submitted. The administrator will review and process it manually.",
        "request_id": withdrawal_id,
        "available": format_units(max(0, (get_user_balance(user_id) if source == "balance" else int(conn.execute("SELECT commission_units FROM users WHERE user_id=?", (user_id,)).fetchone()["commission_units"] or 0)) - get_reserved_withdrawals(user_id, source))),
    })


async def index_page(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(BASE_DIR / "index.html")


async def health_page(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "cem-profit", "chain": 56})


async def logo_file(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(BASE_DIR / "logo.svg")


def build_app(session: ClientSession) -> web.Application:
    app = web.Application(client_max_size=32 * 1024)
    app["http_session"] = session
    app.router.add_get("/", index_page)
    app.router.add_get("/health", health_page)
    app.router.add_get("/logo.svg", logo_file)
    app.router.add_get("/api/config", api_config)
    app.router.add_get("/api/plans", api_plans)
    app.router.add_post("/api/plans/purchase", api_purchase_plan)
    app.router.add_post("/api/profits/claim", api_profit_claim)
    app.router.add_get("/api/admin/plans", api_admin_plans)
    app.router.add_post("/api/admin/plan-profit", api_admin_settle_profit)
    app.router.add_get("/api/referrals", api_referrals)
    app.router.add_get("/api/me", api_me)
    app.router.add_get("/api/history", api_history)
    app.router.add_post("/api/deposits/verify", api_verify_deposit)
    app.router.add_post("/api/withdrawals", api_withdrawal)
    return app


async def keepalive_loop(session: ClientSession) -> None:
    if not SELF_URL:
        logger.info("RENDER_EXTERNAL_URL not set; keep-alive disabled")
        return
    while True:
        await asyncio.sleep(KEEPALIVE_INTERVAL)
        try:
            async with session.get(f"{SELF_URL}/health") as resp:
                logger.info("keep-alive ping: %s", resp.status)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("keep-alive ping failed: %s", exc)


async def backup_loop(session: ClientSession) -> None:
    last_total = conn.total_changes
    last_msg_id: int | None = None
    tmp_path = BASE_DIR / "backup_tmp.db"
    while True:
        await asyncio.sleep(BACKUP_INTERVAL)
        try:
            snapshot = conn.total_changes
            if snapshot == last_total:
                continue
            if tmp_path.exists():
                tmp_path.unlink()
            dst = sqlite3.connect(tmp_path)
            try:
                conn.backup(dst)
            finally:
                dst.close()
            form = FormData()
            form.add_field("chat_id", str(ADMIN_CHAT_ID))
            form.add_field("disable_notification", "true")
            form.add_field("caption", "CEM PROFIT auto backup - Do not delete")
            form.add_field("document", tmp_path.read_bytes(),
                           filename="cem_backup.db", content_type="application/octet-stream")
            async with session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument", data=form) as resp:
                data = await resp.json(content_type=None)
            if not data.get("ok"):
                logger.warning("Backup upload failed: %s", data)
                continue
            msg_id = data["result"]["message_id"]
            pin = await telegram_call(session, "pinChatMessage",
                                      {"chat_id": ADMIN_CHAT_ID, "message_id": msg_id, "disable_notification": True})
            last_total = snapshot
            if pin.get("ok") and last_msg_id:
                await telegram_call(session, "deleteMessage", {"chat_id": ADMIN_CHAT_ID, "message_id": last_msg_id})
            if pin.get("ok"):
                last_msg_id = msg_id
            logger.info("Backup sent to Telegram (message %s, pinned=%s)", msg_id, pin.get("ok"))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Backup error")
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass


async def send_start_message(
    session: ClientSession,
    chat_id: int,
    first_name: str = "",
    user_id: int | None = None,
    start_payload: str = "",
) -> None:
    if user_id:
        ensure_user(user_id)
        if start_payload:
            register_referral_from_start(user_id, start_payload)
    name = first_name.strip()[:60]
    greeting = f"Welcome, {name}.\n\n" if name else "Welcome.\n\n"
    keyboard = {
        "inline_keyboard": [
            [{"text": "Open CEM PROFIT", "web_app": {"url": BOT_WEBAPP_URL}}],
            [{"text": "Check balance", "callback_data": "my_bal"}],
        ]
    }
    await telegram_call(session, "sendMessage", {
        "chat_id": chat_id,
        "text": greeting + "Open the CEM PROFIT Mini App to view your account, deposits and withdrawal requests.",
        "reply_markup": keyboard,
    })


async def handle_callback(session: ClientSession, callback: dict[str, Any]) -> None:
    callback_id = callback.get("id", "")
    from_user = callback.get("from") or {}
    user_id = int(from_user.get("id", 0))
    data = str(callback.get("data", ""))
    if data == "my_bal":
        balance = get_user_balance(user_id)
        reserved = get_reserved_withdrawals(user_id)
        await telegram_call(session, "answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": f"Available: {format_units(max(0, balance - reserved))} USDT",
            "show_alert": True,
        })
        return
    if data.startswith("wd_paid:") or data.startswith("wd_reject:"):
        if user_id != ADMIN_CHAT_ID:
            await telegram_call(session, "answerCallbackQuery", {
                "callback_query_id": callback_id, "text": "Not authorized.", "show_alert": True,
            })
            return
        action, request_text = data.split(":", 1)
        try:
            request_id = int(request_text)
            result = process_withdrawal(request_id, "paid" if action == "wd_paid" else "rejected")
        except (ValueError, TypeError):
            result = None
        if not result:
            await telegram_call(session, "answerCallbackQuery", {
                "callback_query_id": callback_id, "text": "Request already processed or not found.", "show_alert": True,
            })
            return
        target_user, amount_units, _wallet = result
        status = "paid" if action == "wd_paid" else "rejected"
        await telegram_call(session, "answerCallbackQuery", {
            "callback_query_id": callback_id, "text": f"Withdrawal request #{request_id}: {status}", "show_alert": False,
        })
        await telegram_call(session, "sendMessage", {
            "chat_id": target_user,
            "text": (
                f"Your withdrawal request #{request_id} has been processed. {format_units(amount_units)} USDT has been deducted from the selected wallet."
                if status == "paid" else
                f"Your withdrawal request #{request_id} was rejected. The reserved amount is available again."
            ),
        })


async def polling_loop(session: ClientSession) -> None:
    # Clear a stale webhook before starting getUpdates polling.
    await telegram_call(session, "deleteWebhook", {"drop_pending_updates": False})
    await telegram_call(session, "setChatMenuButton", {
        "menu_button": {"type": "web_app", "text": "CEM PROFIT", "web_app": {"url": BOT_WEBAPP_URL}}
    })
    offset = 0
    logger.info("Telegram bot polling started")
    while True:
        try:
            async with session.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                json={"offset": offset, "timeout": 25, "allowed_updates": ["message", "callback_query"]},
                timeout=ClientTimeout(total=35),
            ) as response:
                data = await response.json(content_type=None)
            if not data.get("ok"):
                logger.warning("getUpdates returned an error: %s", data)
                await asyncio.sleep(3)
                continue
            for update in data.get("result", []):
                offset = int(update["update_id"]) + 1
                if "message" in update:
                    message = update["message"]
                    sender = message.get("from") or {}
                    chat_id = int((message.get("chat") or {}).get("id", sender.get("id", 0)))
                    text = str(message.get("text", "")).strip()
                    if text.startswith("/start") or text.startswith("/help"):
                        command_parts = text.split(maxsplit=1)
                        command = command_parts[0].split("@", 1)[0]
                        payload = command_parts[1].strip() if len(command_parts) > 1 else ""
                        if command == "/start":
                            await send_start_message(
                                session,
                                chat_id,
                                str(sender.get("first_name", "")),
                                int(sender.get("id", 0)) or None,
                                payload,
                            )
                        else:
                            await send_start_message(session, chat_id, str(sender.get("first_name", "")), int(sender.get("id", 0)) or None)
                elif "callback_query" in update:
                    await handle_callback(session, update["callback_query"])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Telegram polling error")
            await asyncio.sleep(3)


async def main() -> None:
    timeout = ClientTimeout(total=25, connect=10, sock_read=20)
    async with ClientSession(timeout=timeout) as session:
        app = build_app(session)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, HOST, PORT)
        await site.start()
        logger.info("CEM PROFIT web service listening on %s:%s", HOST, PORT)
        logger.info("Mini App URL: %s", BOT_WEBAPP_URL)
        tasks = [
            asyncio.create_task(polling_loop(session)),
            asyncio.create_task(keepalive_loop(session)),
            asyncio.create_task(backup_loop(session)),
        ]
        try:
            await tasks[0]
        finally:
            for task in tasks:
                task.cancel()
            await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("CEM PROFIT stopped")
