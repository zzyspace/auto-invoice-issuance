from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from app.models import StoreConfig, StoreRunResult, TaxLookupCacheEntry, TaxLookupResult
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

                CREATE TABLE IF NOT EXISTS tax_lookup_cache (
                    provider TEXT NOT NULL,
                    normalized_company_name TEXT NOT NULL,
                    cache_status TEXT NOT NULL,
                    lookup_status TEXT NOT NULL,
                    tax_id TEXT,
                    matched_name TEXT,
                    candidate_count INTEGER NOT NULL DEFAULT 0,
                    raw_response_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (provider, normalized_company_name)
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

    def get_tax_lookup_cache(
        self,
        provider: str,
        normalized_company_name: str,
        negative_ttl_hours: int,
    ) -> Optional[TaxLookupCacheEntry]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT provider, normalized_company_name, cache_status, lookup_status,
                       tax_id, matched_name, candidate_count, raw_response_json, updated_at
                FROM tax_lookup_cache
                WHERE provider = ? AND normalized_company_name = ?
                """,
                (provider, normalized_company_name),
            ).fetchone()
        if row is None:
            return None
        updated_at = datetime.fromisoformat(row["updated_at"])
        if row["cache_status"] == "miss":
            expires_at = updated_at + timedelta(hours=max(negative_ttl_hours, 0))
            if expires_at <= datetime.now(timezone.utc):
                return None
        return TaxLookupCacheEntry(
            provider=str(row["provider"]),
            normalized_company_name=str(row["normalized_company_name"]),
            cache_status=str(row["cache_status"]),
            lookup_status=str(row["lookup_status"]),
            tax_id=str(row["tax_id"]) if row["tax_id"] else None,
            matched_name=str(row["matched_name"]) if row["matched_name"] else None,
            candidate_count=int(row["candidate_count"] or 0),
            raw_response_json=str(row["raw_response_json"] or ""),
            updated_at=updated_at,
        )

    def upsert_tax_lookup_cache(
        self,
        provider: str,
        normalized_company_name: str,
        result: TaxLookupResult,
    ) -> None:
        cache_status = "hit" if result.status == "success" and result.tax_id else "miss"
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tax_lookup_cache (
                    provider,
                    normalized_company_name,
                    cache_status,
                    lookup_status,
                    tax_id,
                    matched_name,
                    candidate_count,
                    raw_response_json,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, normalized_company_name) DO UPDATE SET
                    cache_status = excluded.cache_status,
                    lookup_status = excluded.lookup_status,
                    tax_id = excluded.tax_id,
                    matched_name = excluded.matched_name,
                    candidate_count = excluded.candidate_count,
                    raw_response_json = excluded.raw_response_json,
                    updated_at = excluded.updated_at
                """,
                (
                    provider,
                    normalized_company_name,
                    cache_status,
                    result.status,
                    result.tax_id,
                    result.matched_name,
                    result.candidate_count,
                    result.raw_response_json,
                    now,
                ),
            )

    @staticmethod
    def _store_state_exists(connection: sqlite3.Connection, store_key: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM store_state WHERE store_key = ?",
            (store_key,),
        ).fetchone()
        return row is not None
