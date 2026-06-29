from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Protocol

from app.csv_processing import parse_survey_csv
from app.models import AppConfig, RawSurveyRecord, StoreConfig
from app.survey_client import TencentSurveyClient


class SubmissionSourceClient(Protocol):
    def list_records(self, store: StoreConfig) -> list[RawSurveyRecord]:
        ...

    def download_attachment(self, store: StoreConfig, record: RawSurveyRecord) -> bytes:
        ...


class TencentSurveySourceClient:
    def __init__(self, client: TencentSurveyClient) -> None:
        self.client = client

    def list_records(self, store: StoreConfig) -> list[RawSurveyRecord]:
        return parse_survey_csv(self.client.export_csv(store))

    def download_attachment(self, store: StoreConfig, record: RawSurveyRecord) -> bytes:
        file_name = (record.attachment_ref or record.attachment_name or "").strip()
        if not file_name:
            raise ValueError(
                f"Store '{store.store_key}' submission {record.submission_id} is missing attachment name."
            )
        return self.client.download_attachment(store, file_name)


class InvoiceSubmitSourceClient:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        if not self.db_path.exists():
            raise FileNotFoundError(f"invoice-submit database not found: {self.db_path}")
        connection = sqlite3.connect(f"{self.db_path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    def list_records(self, store: StoreConfig) -> list[RawSurveyRecord]:
        store_key = store.effective_invoice_submit_store_key()
        with self._connect() as connection:
            submission_id_expr = self._submission_id_expr(connection)
            rows = connection.execute(
                f"""
                SELECT
                    {submission_id_expr} AS submission_sequence,
                    id,
                    invoice_type,
                    invoice_title,
                    tax_number,
                    email,
                    contact,
                    note,
                    store_key,
                    attachment_path,
                    attachment_name,
                    attachment_content_type,
                    created_at
                FROM submissions
                WHERE store_key = ?
                ORDER BY submission_sequence ASC
                """,
                (store_key,),
            ).fetchall()
        return [self._build_record(row) for row in rows]

    def download_attachment(self, store: StoreConfig, record: RawSurveyRecord) -> bytes:
        attachment_path = Path((record.attachment_ref or "").strip())
        if not str(attachment_path):
            raise ValueError(
                f"Store '{store.store_key}' submission {record.submission_id} is missing attachment_path."
            )
        if not attachment_path.exists():
            raise FileNotFoundError(
                f"invoice-submit attachment not found for store '{store.store_key}': {attachment_path}"
            )
        return attachment_path.read_bytes()

    @staticmethod
    def _build_record(row: sqlite3.Row) -> RawSurveyRecord:
        created_at = str(row["created_at"] or "")
        raw = {
            "submission_sequence": str(row["submission_sequence"] or ""),
            "id": str(row["id"] or ""),
            "invoice_type": str(row["invoice_type"] or ""),
            "invoice_title": str(row["invoice_title"] or ""),
            "tax_number": str(row["tax_number"] or ""),
            "email": str(row["email"] or ""),
            "contact": str(row["contact"] or ""),
            "note": str(row["note"] or ""),
            "store_key": str(row["store_key"] or ""),
            "attachment_path": str(row["attachment_path"] or ""),
            "attachment_name": str(row["attachment_name"] or ""),
            "attachment_content_type": str(row["attachment_content_type"] or ""),
            "created_at": created_at,
        }
        return RawSurveyRecord(
            submission_id=int(row["submission_sequence"]),
            start_time=created_at,
            end_time=created_at,
            duration_seconds="",
            invoice_title=str(row["invoice_title"] or "").strip(),
            tax_id_raw=str(row["tax_number"] or "").strip(),
            email=str(row["email"] or "").strip(),
            attachment_name=str(row["attachment_name"] or "").strip(),
            phone=str(row["contact"] or "").strip(),
            remark=str(row["note"] or "").strip(),
            raw=raw,
            attachment_ref=str(row["attachment_path"] or "").strip(),
            attachment_content_type=str(row["attachment_content_type"] or "").strip(),
            submission_label=str(row["id"] or "").strip() or None,
        )

    @staticmethod
    def _submission_id_expr(connection: sqlite3.Connection) -> str:
        columns = {
            str(row["name"]).strip().lower()
            for row in connection.execute("PRAGMA table_info(submissions)").fetchall()
            if row["name"]
        }
        if "submit_id" in columns:
            return "submit_id"
        return "rowid"


class RoutedSubmissionSourceClient:
    def __init__(self, clients: dict[str, SubmissionSourceClient]) -> None:
        self.clients = clients

    def list_records(self, store: StoreConfig) -> list[RawSurveyRecord]:
        return self._get_client(store).list_records(store)

    def download_attachment(self, store: StoreConfig, record: RawSurveyRecord) -> bytes:
        return self._get_client(store).download_attachment(store, record)

    def _get_client(self, store: StoreConfig) -> SubmissionSourceClient:
        source = store.effective_data_source()
        client = self.clients.get(source)
        if client is None:
            raise ValueError(
                f"Store '{store.store_key}' is configured with data_source={source!r}, "
                "but the corresponding client is not initialized."
            )
        return client


def build_submission_source_client(
    config: AppConfig,
    stores: list[StoreConfig],
) -> SubmissionSourceClient:
    clients: dict[str, SubmissionSourceClient] = {}
    sources = {store.effective_data_source() for store in stores}
    if "tencent_survey" in sources:
        clients["tencent_survey"] = TencentSurveySourceClient(TencentSurveyClient(config))
    if "invoice_submit" in sources:
        if config.invoice_submit_db_path is None:
            raise ValueError("INVOICE_SUBMIT_DB_PATH is required when using data_source=invoice_submit.")
        clients["invoice_submit"] = InvoiceSubmitSourceClient(config.invoice_submit_db_path)
    return RoutedSubmissionSourceClient(clients)
