"""SQLite 数据访问层，使用短事务并启用 WAL。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS telegram_messages (
  chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, version INTEGER NOT NULL DEFAULT 1,
  raw_text TEXT NOT NULL, received_at TEXT NOT NULL, parsed_json TEXT,
  PRIMARY KEY(chat_id, message_id, version)
);
CREATE TABLE IF NOT EXISTS trade_instances (
  trade_id TEXT PRIMARY KEY, exchange TEXT NOT NULL, instrument_key TEXT NOT NULL,
  side TEXT NOT NULL, state TEXT NOT NULL, breakeven_triggered INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id TEXT NOT NULL, exchange_order_id TEXT,
  client_order_id TEXT NOT NULL UNIQUE, order_type TEXT NOT NULL, price TEXT,
  quantity TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS positions (
  exchange TEXT NOT NULL, instrument_key TEXT NOT NULL, side TEXT NOT NULL,
  quantity TEXT NOT NULL, average_price TEXT NOT NULL, captured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(exchange, instrument_key, side)
);
CREATE TABLE IF NOT EXISTS commands (
  command_id TEXT PRIMARY KEY, trade_id TEXT, command_type TEXT NOT NULL,
  payload_json TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS exchange_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, exchange TEXT NOT NULL, event_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS audit_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT NOT NULL, subject_id TEXT,
  before_json TEXT, after_json TEXT, result TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS runtime_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(SCHEMA)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        async with self._lock:
            def run() -> int:
                with self._connection() as conn:
                    return conn.execute(sql, params).rowcount
            return await asyncio.to_thread(run)

    async def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        async with self._lock:
            def run() -> list[dict[str, Any]]:
                with self._connection() as conn:
                    return [dict(row) for row in conn.execute(sql, params).fetchall()]
            return await asyncio.to_thread(run)

    async def audit(self, category: str, subject_id: str | None, result: str,
                    before: Any = None, after: Any = None) -> None:
        await self.execute(
            "INSERT INTO audit_logs(category,subject_id,before_json,after_json,result) VALUES(?,?,?,?,?)",
            (category, subject_id, _json(before), _json(after), result),
        )


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, default=str)

