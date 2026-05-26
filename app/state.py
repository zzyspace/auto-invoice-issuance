from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

from app.models import StoreConfig, StoreRunResult
from app.utils import ensure_parent_dir


class StateStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        ensure_parent_dir(db_path)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS store_state (
                    store_key TEXT PRIMARY KEY,
                    last_processed_id INTEGER NOT NULL,
                    last_status TEXT,
                    last_output_path TEXT,
                    last_warning_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS run_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    store_key TEXT NOT NULL,
                    store_name TEXT NOT NULL,
                    survey_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    processed_count INTEGER NOT NULL,
                    last_processed_id_before INTEGER NOT NULL,
                    last_processed_id_after INTEGER NOT NULL,
                    output_path TEXT,
                    backup_path TEXT,
                    warnings_json TEXT NOT NULL,
                    error TEXT
                );
                """
            )

    def get_last_processed_id(self, store: StoreConfig) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT last_processed_id FROM store_state WHERE store_key = ?",
                (store.store_key,),
            ).fetchone()
        if row:
            return int(row["last_processed_id"])
        return store.initial_last_processed_id

    def record_result(self, result: StoreRunResult, update_progress: bool = True) -> None:
        if result.finished_at is None:
            raise ValueError("Result must be finalized before recording.")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO run_history (
                    store_key,
                    store_name,
                    survey_id,
                    started_at,
                    finished_at,
                    status,
                    processed_count,
                    last_processed_id_before,
                    last_processed_id_after,
                    output_path,
                    backup_path,
                    warnings_json,
                    error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.store_key,
                    result.store_name,
                    result.survey_id,
                    result.started_at.isoformat(),
                    result.finished_at.isoformat(),
                    result.status,
                    result.processed_count,
                    result.last_processed_id_before,
                    result.last_processed_id_after,
                    str(result.output_path) if result.output_path else None,
                    str(result.backup_path) if result.backup_path else None,
                    json.dumps(result.warnings, ensure_ascii=False),
                    result.error,
                ),
            )
            if update_progress and result.status in {"success", "no_new_data"}:
                connection.execute(
                    """
                    INSERT INTO store_state (
                        store_key,
                        last_processed_id,
                        last_status,
                        last_output_path,
                        last_warning_count,
                        last_error,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(store_key) DO UPDATE SET
                        last_processed_id = excluded.last_processed_id,
                        last_status = excluded.last_status,
                        last_output_path = excluded.last_output_path,
                        last_warning_count = excluded.last_warning_count,
                        last_error = excluded.last_error,
                        updated_at = excluded.updated_at
                    """,
                    (
                        result.store_key,
                        result.last_processed_id_after,
                        result.status,
                        str(result.output_path) if result.output_path else None,
                        len(result.warnings),
                        result.error,
                        result.finished_at.isoformat(),
                    ),
                )
            elif not self._store_state_exists(connection, result.store_key):
                connection.execute(
                    """
                    INSERT INTO store_state (
                        store_key,
                        last_processed_id,
                        last_status,
                        last_output_path,
                        last_warning_count,
                        last_error,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        result.store_key,
                        result.last_processed_id_before,
                        result.status,
                        str(result.output_path) if result.output_path else None,
                        len(result.warnings),
                        result.error,
                        result.finished_at.isoformat(),
                    ),
                )

    @staticmethod
    def _store_state_exists(connection: sqlite3.Connection, store_key: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM store_state WHERE store_key = ?",
            (store_key,),
        ).fetchone()
        return row is not None
