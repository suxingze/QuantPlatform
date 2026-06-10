from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

DB_PATH = Path(os.environ.get("QUANT_DB_PATH", Path(__file__).resolve().parent.parent / "quant_risk.db"))


def now_ms() -> int:
    return int(time.time() * 1000)


def get_conn(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = DELETE")
    return conn


def dict_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def dict_rows(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def password_hash(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS instructions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            status TEXT NOT NULL,
            submitter_id INTEGER NOT NULL,
            reviewer_id INTEGER,
            review_time INTEGER,
            review_reason TEXT,
            instrument_id TEXT,
            side TEXT,
            price REAL,
            volume INTEGER,
            order_id TEXT,
            risk_error_code TEXT,
            risk_error_message TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            FOREIGN KEY(submitter_id) REFERENCES users(id),
            FOREIGN KEY(reviewer_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            client_order_id TEXT NOT NULL,
            instruction_id INTEGER,
            instrument_id TEXT NOT NULL,
            gen_time INTEGER NOT NULL,
            side TEXT NOT NULL,
            price REAL NOT NULL,
            original_volume INTEGER NOT NULL,
            traded_volume INTEGER NOT NULL DEFAULT 0,
            canceled_volume INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            exchange_accepted INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            version INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(instruction_id) REFERENCES instructions(id)
        );

        CREATE TABLE IF NOT EXISTS order_events (
            event_id TEXT PRIMARY KEY,
            order_id TEXT NOT NULL,
            instrument_id TEXT NOT NULL,
            gen_time INTEGER NOT NULL,
            status TEXT,
            traded_volume INTEGER NOT NULL DEFAULT 0,
            canceled_volume INTEGER NOT NULL DEFAULT 0,
            trade_price REAL,
            message TEXT,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            order_id TEXT NOT NULL,
            instrument_id TEXT NOT NULL,
            side TEXT NOT NULL,
            price REAL NOT NULL,
            volume INTEGER NOT NULL,
            amount REAL NOT NULL,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS risk_counters (
            key TEXT PRIMARY KEY,
            value INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS cancel_requests (
            order_id TEXT PRIMARY KEY,
            last_request_at INTEGER NOT NULL
        );
        """
    )
    seed_users(conn)
    conn.commit()


def seed_users(conn: sqlite3.Connection) -> None:
    ts = now_ms()
    for username, password, role in (
        ("trader_a", "password_a", "trader"),
        ("trader_b", "password_b", "trader"),
    ):
        conn.execute(
            """
            INSERT OR IGNORE INTO users(username, password_hash, role, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (username, password_hash(password), role, ts),
        )
