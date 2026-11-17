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
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qsl
from typing import Any

from aiohttp import ClientSession, ClientTimeout, web

BASE_DIR = Path(__file__).resolve().parent

# ===== সেটিংস: শুধু এই ৪টা লাইন বদলাও। এই ফাইল কারও সাথে শেয়ার/GitHub-এ পুশ করো না। =====
BOT_TOKEN = "9098089876:AAE2jBRPkyc_P_RhNJodm8nRix3sjPhDij8"
ADMIN_CHAT_ID_RAW = "8707046428"
DEPOSIT_WALLET = "0x779c92d10E16Ba7007d21480AC882A7C40Fda159".lower()
USDT_CONTRACT = "0x55d398326f99059ff775485246999027b3197955".lower()
BSC_RPC_URL = "https://bsc-dataseed.bnbchain.org"
BOT_WEBAPP_URL = "https://YOUR-DOMAIN.com/"  # <-- তোমার সার্ভারের পাবলিক HTTPS ডোমেইন
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8080"))
DATABASE_PATH = BASE_DIR / "trading_bot.db"
DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
MIN_CONFIRMATIONS = 12
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
        raise RuntimeError("BOT_TOKEN সঠিকভাবে বসানো নেই।")
    if not ADMIN_CHAT_ID_RAW:
        raise RuntimeError("ADMIN_CHAT_ID সেট করা নেই।")
    try:
        admin_id = int(ADMIN_CHAT_ID_RAW)
    except ValueError as exc:
        raise RuntimeError("ADMIN_CHAT_ID অবশ্যই সংখ্যায় হতে হবে।") from exc
    if not ADDRESS_RE.fullmatch(DEPOSIT_WALLET):
        raise RuntimeError("DEPOSIT_WALLET-এ সঠিক EVM wallet address দিন।")
    if not ADDRESS_RE.fullmatch(USDT_CONTRACT):
        raise RuntimeError("USDT_CONTRACT-এ সঠিক token contract address দিন।")
    if not BOT_WEBAPP_URL.startswith("https://") or "github.io" in BOT_WEBAPP_URL or "YOUR-DOMAIN" in BOT_WEBAPP_URL:
        raise RuntimeError(
            "BOT_WEBAPP_URL অবশ্যই এই সার্ভারেরই পাবলিক HTTPS ডোমেইন হতে হবে "
            "(GitHub Pages চলবে না, কারণ API এই সার্ভারে চলে)।"
        )
    return admin_id


ADMIN_CHAT_ID = validate_config()

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
    conn.commit()


init_database()


def format_units(units: int) -> str:
    value = Decimal(int(units)) / Decimal(USDT_SCALE)
    return f"{value:,.4f}"


def parse_usdt(value: Any) -> int:
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError) as exc:
        raise ValueError("সঠিক পরিমাণ লিখুন।") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("পরিমাণ শূন্যের বেশি হতে হবে।")
    scaled = amount * USDT_SCALE
    if scaled != scaled.to_integral_value():
        raise ValueError("সর্বোচ্চ ১৮ ঘর দশমিক পর্যন্ত পরিমাণ গ্রহণযোগ্য।")
    return int(scaled)


def ensure_user(user_id: int) -> None:
    conn.execute("INSERT OR IGNORE INTO users (user_id, balance, balance_units) VALUES (?, 0, '0')", (user_id,))
    conn.commit()


def get_user_balance(user_id: int) -> int:
    ensure_user(user_id)
    row = conn.execute("SELECT balance_units FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return int(row["balance_units"] or 0)


def get_reserved_withdrawals(user_id: int) -> int:
    rows = conn.execute(
        "SELECT amount_units FROM withdrawals WHERE user_id = ? AND status = 'pending'",
        (user_id,),
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


def create_withdrawal(user_id: int, amount_units: int, wallet: str) -> int:
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("INSERT OR IGNORE INTO users (user_id, balance, balance_units) VALUES (?, 0, '0')", (user_id,))
        row = conn.execute("SELECT balance_units FROM users WHERE user_id = ?", (user_id,)).fetchone()
        reserved_rows = conn.execute(
            "SELECT amount_units FROM withdrawals WHERE user_id = ? AND status = 'pending'",
            (user_id,),
        ).fetchall()
        reserved = sum(int(item["amount_units"] or 0) for item in reserved_rows)
        available = int(row["balance_units"] or 0) - reserved
        if amount_units > available:
            raise ValueError("আপনার উত্তোলনযোগ্য ব্যালেন্স পর্যাপ্ত নয়।")
        cur = conn.execute(
            "INSERT INTO withdrawals (user_id, amount_units, wallet_address) VALUES (?, ?, ?)",
            (user_id, str(amount_units), wallet),
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
        if action == "paid":
            balance_row = conn.execute("SELECT balance_units FROM users WHERE user_id = ?", (user_id,)).fetchone()
            balance = int(balance_row["balance_units"] or 0) if balance_row else 0
            if balance < amount_units:
                raise ValueError("User balance is lower than the pending withdrawal.")
            new_balance = balance - amount_units
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
        "SELECT id, amount_units, wallet_address, status, created_at FROM withdrawals WHERE user_id = ? ORDER BY id DESC LIMIT ?",
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
        })
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return items[:limit]


def validate_telegram_init_data(raw_data: str) -> int:
    """Validate Telegram's signed initData and return the trusted user ID."""
    if not raw_data or len(raw_data) > 16_384:
        raise ValueError("Telegram session পাওয়া যায়নি। Telegram-এর ভেতর থেকে অ্যাপটি খুলুন।")
    pairs = parse_qsl(raw_data, keep_blank_values=True, strict_parsing=True)
    values: dict[str, str] = {}
    for key, value in pairs:
        if key in values:
            raise ValueError("অবৈধ Telegram session।")
        values[key] = value
    received_hash = values.pop("hash", "")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", received_hash):
        raise ValueError("Telegram session signature পাওয়া যায়নি।")
    data_check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated_hash, received_hash.lower()):
        raise ValueError("Telegram session যাচাই করা যায়নি। অ্যাপটি বন্ধ করে আবার খুলুন।")
    try:
        auth_date = int(values.get("auth_date", "0"))
        if auth_date <= 0 or time.time() - auth_date > AUTH_MAX_AGE or auth_date - time.time() > 300:
            raise ValueError("Telegram session-এর মেয়াদ শেষ হয়েছে। অ্যাপটি আবার খুলুন।")
        user_data = json.loads(values["user"])
        user_id = int(user_data["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("Telegram session"):
            raise
        raise ValueError("Telegram user তথ্য পাওয়া যায়নি।") from exc
    if user_id <= 0:
        raise ValueError("Telegram user ID অবৈধ।")
    return user_id


async def authorized_user(request: web.Request) -> int:
    raw_data = request.headers.get("X-Telegram-Init-Data", "")
    try:
        return validate_telegram_init_data(raw_data)
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
            return {"status": "error", "message": "RPC endpoint BNB Smart Chain Mainnet নয়।"}
        if receipt is None:
            tx = await rpc_call(session, "eth_getTransactionByHash", [txid])
            if tx is None:
                return {"status": "not_found", "message": "এই TxID এখনো BNB Smart Chain-এ পাওয়া যায়নি। Hash এবং নেটওয়ার্ক পরীক্ষা করুন।"}
            return {"status": "pending", "confirmations": 0, "required_confirmations": MIN_CONFIRMATIONS,
                    "message": "ট্রানজেকশন নেটওয়ার্কে আছে, ব্লকে যুক্ত হওয়ার অপেক্ষায়।"}
        if str(receipt.get("status", "")).lower() != "0x1":
            return {"status": "failed", "message": "এই ট্রানজেকশনটি BNB Smart Chain-এ ব্যর্থ হয়েছে।"}
        block_number = int(receipt.get("blockNumber", "0x0"), 16)
        latest_block = int(await rpc_call(session, "eth_blockNumber", []), 16)
        confirmations = max(0, latest_block - block_number + 1)
        if confirmations < MIN_CONFIRMATIONS:
            return {
                "status": "pending",
                "confirmations": confirmations,
                "required_confirmations": MIN_CONFIRMATIONS,
                "message": "ট্রানজেকশন পাওয়া গেছে। নিরাপদ কনফার্মেশন সম্পন্ন হওয়ার অপেক্ষায়।",
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
                "message": "এই TxID-তে নির্ধারিত ওয়ালেটে সঠিক USDT (BEP-20) জমা পাওয়া যায়নি।",
            }
        return {
            "status": "success",
            "amount_units": amount_units,
            "amount": format_units(amount_units),
            "block_number": block_number,
            "confirmations": confirmations,
        }
    except asyncio.TimeoutError:
        return {"status": "error", "message": "BNB Smart Chain RPC-তে সাড়া পেতে দেরি হচ্ছে। আবার চেষ্টা করুন।"}
    except Exception as exc:
        logger.exception("BSC transaction verification error: %s", exc)
        return {"status": "error", "message": "ব্লকচেইন যাচাই সার্ভিসে সাময়িক সমস্যা হয়েছে। কিছুক্ষণ পর আবার চেষ্টা করুন।"}


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


async def api_me(request: web.Request) -> web.Response:
    user_id = await authorized_user(request)
    balance = get_user_balance(user_id)
    reserved = get_reserved_withdrawals(user_id)
    return web.json_response({
        "user_id": user_id,
        "balance": format_units(balance),
        "balance_units": balance,
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
        raise web.HTTPBadRequest(text=json.dumps({"error": "সঠিক JSON data পাঠান।"}, ensure_ascii=False), content_type="application/json")
    txid = str(body.get("txid", "")).strip().lower()
    if not TX_HASH_RE.fullmatch(txid):
        raise web.HTTPBadRequest(text=json.dumps({"error": "সঠিক 0x দিয়ে শুরু হওয়া ৬৬ অক্ষরের transaction hash দিন।"}, ensure_ascii=False), content_type="application/json")

    previous = conn.execute("SELECT user_id, amount_units FROM used_txids WHERE txid = ?", (txid,)).fetchone()
    if previous:
        if int(previous["user_id"]) == user_id:
            return web.json_response({
                "status": "already_credited",
                "message": "এই TxID-এর অর্থ আগেই আপনার ব্যালেন্সে যোগ হয়েছে।",
                "amount": format_units(int(previous["amount_units"] or 0)),
                "balance": format_units(get_user_balance(user_id)),
            })
        return web.json_response({"status": "duplicate", "message": "এই TxID ইতোমধ্যে অন্য একটি অ্যাকাউন্টে ব্যবহার হয়েছে।"}, status=409)

    result = await verify_bep20_usdt_tx(request.app["http_session"], txid)
    if result["status"] != "success":
        return web.json_response(result)
    try:
        new_balance = credit_deposit(txid, user_id, int(result["amount_units"]), int(result["block_number"]))
    except DuplicateTransaction as exc:
        if exc.owner_id == user_id:
            return web.json_response({
                "status": "already_credited",
                "message": "এই TxID-এর অর্থ আগেই আপনার ব্যালেন্সে যোগ হয়েছে।",
                "amount": format_units(exc.amount_units),
                "balance": format_units(get_user_balance(user_id)),
            })
        return web.json_response({"status": "duplicate", "message": "এই TxID ইতোমধ্যে অন্য একটি অ্যাকাউন্টে ব্যবহার হয়েছে।"}, status=409)

    amount_text = format_units(int(result["amount_units"]))
    await notify_admin(
        request.app["http_session"],
        "CEM PROFIT | Deposit verified\n"
        f"User ID: {user_id}\nAmount: {amount_text} USDT\n"
        f"TxID: {txid}\nBlock: {result['block_number']}",
    )
    return web.json_response({
        "status": "success",
        "message": "ডিপোজিট সফলভাবে যাচাই হয়েছে এবং ব্যালেন্সে যোগ হয়েছে।",
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
        if not ADDRESS_RE.fullmatch(wallet):
            raise ValueError("সঠিক BEP-20 wallet address দিন।")
        withdrawal_id = create_withdrawal(user_id, amount_units, wallet)
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
        f"Amount: {amount_text} USDT\nWallet: {wallet}\n\n"
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
            text=json.dumps({"error": "অ্যাডমিনকে অনুরোধ পাঠানো যায়নি। কিছুক্ষণ পর আবার চেষ্টা করুন।"}, ensure_ascii=False),
            content_type="application/json",
        )
    return web.json_response({
        "status": "pending",
        "message": "উত্তোলনের অনুরোধ জমা হয়েছে। অ্যাডমিন যাচাই করে ম্যানুয়ালি প্রসেস করবেন।",
        "request_id": withdrawal_id,
        "available": format_units(max(0, get_user_balance(user_id) - get_reserved_withdrawals(user_id))),
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
    app.router.add_get("/api/me", api_me)
    app.router.add_get("/api/history", api_history)
    app.router.add_post("/api/deposits/verify", api_verify_deposit)
    app.router.add_post("/api/withdrawals", api_withdrawal)
    return app


async def send_start_message(session: ClientSession, chat_id: int, first_name: str = "") -> None:
    name = first_name.strip()[:60]
    greeting = f"স্বাগতম, {name}.\n\n" if name else "স্বাগতম।\n\n"
    keyboard = {
        "inline_keyboard": [
            [{"text": "Open CEM PROFIT", "web_app": {"url": BOT_WEBAPP_URL}}],
            [{"text": "Check balance", "callback_data": "my_bal"}],
        ]
    }
    await telegram_call(session, "sendMessage", {
        "chat_id": chat_id,
        "text": greeting + "আপনার অ্যাকাউন্ট, ডিপোজিট এবং উত্তোলনের অনুরোধ পরিচালনা করতে Mini App খুলুন।",
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
                f"আপনার উত্তোলনের অনুরোধ #{request_id} সম্পন্ন হয়েছে। {format_units(amount_units)} USDT ব্যালেন্স থেকে কাটা হয়েছে।"
                if status == "paid" else
                f"আপনার উত্তোলনের অনুরোধ #{request_id} বাতিল করা হয়েছে। সংরক্ষিত ব্যালেন্স আবার ব্যবহারযোগ্য।"
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
                        await send_start_message(session, chat_id, str(sender.get("first_name", "")))
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
        poll_task = asyncio.create_task(polling_loop(session))
        try:
            await poll_task
        finally:
            poll_task.cancel()
            await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("CEM PROFIT stopped")
