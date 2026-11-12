"""SQLite storage: searches, terms, seen items, daily counters, hits, key/value state.

One connection shared by the search loop and the Telegram thread, guarded by a lock.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    profile    TEXT NOT NULL DEFAULT 'standard',
    params     TEXT NOT NULL DEFAULT '{}',
    max_price  REAL,
    min_price  REAL,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS terms (
    search_id INTEGER NOT NULL,
    term      TEXT NOT NULL,
    seeded    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (search_id, term)
);
CREATE TABLE IF NOT EXISTS seen (
    search_id INTEGER NOT NULL,
    item_id   INTEGER NOT NULL,
    ts        REAL NOT NULL,
    PRIMARY KEY (search_id, item_id)
);
CREATE TABLE IF NOT EXISTS stats (
    day   TEXT NOT NULL,
    key   TEXT NOT NULL,
    value REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (day, key)
);
CREATE TABLE IF NOT EXISTS hits (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    search_id INTEGER,
    item_id   INTEGER,
    title     TEXT,
    price     REAL,
    url       TEXT,
    briefing  TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


@dataclass
class Search:
    id: int
    name: str
    profile: str = "standard"
    params: dict[str, str] = field(default_factory=dict)
    max_price: float | None = None
    min_price: float | None = None
    active: bool = True
    terms: list[str] = field(default_factory=list)


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.commit()

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur

    def _all(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # --- searches ---------------------------------------------------------

    def add_search(
        self,
        name: str,
        profile: str = "standard",
        terms: list[str] | None = None,
        params: dict | None = None,
        max_price: float | None = None,
        min_price: float | None = None,
    ) -> int:
        with self._lock:
            cur = self._exec(
                "INSERT INTO searches (name, profile, params, max_price, min_price, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (name, profile, json.dumps(params or {}), max_price, min_price, time.time()),
            )
            search_id = int(cur.lastrowid)
            for term in terms or [""]:
                self.add_term(search_id, term)
            return search_id

    def _row_to_search(self, row: sqlite3.Row) -> Search:
        return Search(
            id=row["id"],
            name=row["name"],
            profile=row["profile"],
            params=json.loads(row["params"] or "{}"),
            max_price=row["max_price"],
            min_price=row["min_price"],
            active=bool(row["active"]),
            terms=self.terms(row["id"]),
        )

    def get_search(self, search_id: int) -> Search | None:
        rows = self._all("SELECT * FROM searches WHERE id = ?", (search_id,))
        return self._row_to_search(rows[0]) if rows else None

    def find_search_by_name(self, name: str) -> Search | None:
        rows = self._all("SELECT * FROM searches WHERE lower(name) = lower(?)", (name,))
        return self._row_to_search(rows[0]) if rows else None

    def list_searches(self, active_only: bool = False) -> list[Search]:
        sql = (
            "SELECT * FROM searches" + (" WHERE active = 1" if active_only else "") + " ORDER BY id"
        )
        return [self._row_to_search(r) for r in self._all(sql)]

    _UPDATABLE = {"name", "profile", "params", "max_price", "min_price", "active"}

    def update_search(self, search_id: int, **fields) -> None:
        unknown = set(fields) - self._UPDATABLE
        if unknown:
            raise ValueError(f"unknown fields: {unknown}")
        for key, value in fields.items():
            if key == "params":
                value = json.dumps(value)
            elif key == "active":
                value = int(bool(value))
            self._exec(f"UPDATE searches SET {key} = ? WHERE id = ?", (value, search_id))

    def delete_search(self, search_id: int) -> None:
        with self._lock:
            for table, col in (("searches", "id"), ("terms", "search_id"), ("seen", "search_id")):
                self._exec(f"DELETE FROM {table} WHERE {col} = ?", (search_id,))

    # --- terms ------------------------------------------------------------

    def terms(self, search_id: int) -> list[str]:
        rows = self._all("SELECT term FROM terms WHERE search_id = ? ORDER BY rowid", (search_id,))
        return [r["term"] for r in rows]

    def add_term(self, search_id: int, term: str) -> bool:
        term = term.strip()
        with self._lock:
            # A real search term replaces the empty "filter only" placeholder
            if term:
                self._exec("DELETE FROM terms WHERE search_id = ? AND term = ''", (search_id,))
            cur = self._exec(
                "INSERT OR IGNORE INTO terms (search_id, term) VALUES (?, ?)", (search_id, term)
            )
            return cur.rowcount > 0

    def remove_term(self, search_id: int, term: str) -> None:
        with self._lock:
            self._exec("DELETE FROM terms WHERE search_id = ? AND term = ?", (search_id, term))
            if not self.terms(search_id):
                self.add_term(search_id, "")

    def is_seeded(self, search_id: int, term: str) -> bool:
        rows = self._all(
            "SELECT seeded FROM terms WHERE search_id = ? AND term = ?", (search_id, term)
        )
        return bool(rows and rows[0]["seeded"])

    def mark_seeded(self, search_id: int, term: str) -> None:
        self._exec(
            "UPDATE terms SET seeded = 1 WHERE search_id = ? AND term = ?", (search_id, term)
        )

    def reset_seeding(self, search_id: int) -> None:
        self._exec("UPDATE terms SET seeded = 0 WHERE search_id = ?", (search_id,))

    # --- seen -------------------------------------------------------------

    def is_seen(self, search_id: int, item_id: int) -> bool:
        return bool(
            self._all(
                "SELECT 1 FROM seen WHERE search_id = ? AND item_id = ?", (search_id, item_id)
            )
        )

    def mark_seen(self, search_id: int, item_id: int) -> None:
        self._exec(
            "INSERT OR IGNORE INTO seen (search_id, item_id, ts) VALUES (?, ?, ?)",
            (search_id, item_id, time.time()),
        )

    def unmark_seen(self, search_id: int, item_id: int) -> None:
        self._exec("DELETE FROM seen WHERE search_id = ? AND item_id = ?", (search_id, item_id))

    def prune_seen(self, older_than_days: int) -> int:
        cutoff = time.time() - older_than_days * 86400
        return self._exec("DELETE FROM seen WHERE ts < ?", (cutoff,)).rowcount

    # --- stats / hits / kv ------------------------------------------------

    def incr(self, day: str, key: str, amount: float = 1) -> None:
        self._exec(
            "INSERT INTO stats (day, key, value) VALUES (?, ?, ?)"
            " ON CONFLICT(day, key) DO UPDATE SET value = value + excluded.value",
            (day, key, amount),
        )

    def stats(self, day: str) -> dict[str, float]:
        return {
            r["key"]: r["value"]
            for r in self._all("SELECT key, value FROM stats WHERE day = ?", (day,))
        }

    def add_hit(
        self, search_id: int, item_id: int, title: str, price: float, url: str, briefing: str
    ) -> None:
        self._exec(
            "INSERT INTO hits (ts, search_id, item_id, title, price, url, briefing)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (time.time(), search_id, item_id, title, price, url, briefing),
        )

    def get(self, key: str, default: str | None = None) -> str | None:
        rows = self._all("SELECT value FROM kv WHERE key = ?", (key,))
        return rows[0]["value"] if rows else default

    def set(self, key: str, value: str) -> None:
        self._exec(
            "INSERT INTO kv (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def delete(self, key: str) -> None:
        self._exec("DELETE FROM kv WHERE key = ?", (key,))
