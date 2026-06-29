from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.models import StoreConfig
from app.submission_source import InvoiceSubmitSourceClient


SCHEMA_SQL = """
CREATE TABLE submissions (
  id TEXT PRIMARY KEY,
  invoice_type TEXT NOT NULL,
  invoice_title TEXT NOT NULL,
  tax_number TEXT,
  email TEXT NOT NULL,
  contact TEXT,
  note TEXT,
  store_key TEXT,
  attachment_path TEXT NOT NULL,
  attachment_name TEXT NOT NULL,
  attachment_content_type TEXT NOT NULL,
  attachment_size_bytes INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
"""


class InvoiceSubmitSourceClientTests(unittest.TestCase):
    def test_reads_filtered_records_and_downloads_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            db_path = tmp_path / "invoice-submit.db"
            attachment_path = tmp_path / "uploads" / "receipt-a.png"
            attachment_path.parent.mkdir(parents=True, exist_ok=True)
            attachment_path.write_bytes(b"png-bytes")

            with sqlite3.connect(db_path) as connection:
                connection.executescript(SCHEMA_SQL)
                connection.execute(
                    """
                    INSERT INTO submissions (
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
                        attachment_size_bytes,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "submission-a",
                        "enterprise",
                        "上海示例科技有限公司",
                        "91310000MA000001",
                        "finance@example.com",
                        "13800000000",
                        "备注A",
                        "peanut",
                        str(attachment_path),
                        "receipt-a.png",
                        "image/png",
                        9,
                        "2026-06-30T10:00:00.000Z",
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO submissions (
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
                        attachment_size_bytes,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "submission-b",
                        "personal",
                        "王小明",
                        None,
                        "user@example.com",
                        "",
                        "",
                        "other-store",
                        str(tmp_path / "uploads" / "receipt-b.png"),
                        "receipt-b.png",
                        "image/png",
                        8,
                        "2026-06-30T11:00:00.000Z",
                    ),
                )

            client = InvoiceSubmitSourceClient(db_path)
            store = StoreConfig(
                store_key="store_a",
                store_name="门店A",
                survey_id="",
                output_xlsx_path=tmp_path / "output.xlsx",
                initial_last_processed_id=0,
                data_source="invoice_submit",
                invoice_submit_store_key="peanut",
            )

            records = client.list_records(store)

            self.assertEqual(1, len(records))
            self.assertEqual("submission-a", records[0].submission_label)
            self.assertEqual("上海示例科技有限公司", records[0].invoice_title)
            self.assertEqual("91310000MA000001", records[0].tax_id_raw)
            self.assertEqual("finance@example.com", records[0].email)
            self.assertEqual("image/png", records[0].attachment_content_type)
            self.assertEqual(b"png-bytes", client.download_attachment(store, records[0]))
