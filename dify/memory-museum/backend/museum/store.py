from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path


class ScopeStore:
    """Stable user-to-PowerContext-Scope mapping."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS user_scopes (
                    user_id TEXT PRIMARY KEY,
                    scope_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

    def get_scope(self, user_id: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT scope_id FROM user_scopes WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return str(row["scope_id"]) if row else None

    def put_scope(self, user_id: str, scope_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO user_scopes(user_id, scope_id, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET scope_id = excluded.scope_id
                """,
                (user_id, scope_id, datetime.now(UTC).isoformat()),
            )
