from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from app.models import AmountExtraction, AppConfig, BatchRunSummary, StoreConfig, TaxLookupResult
from app.service import BatchProcessor, Services
from app.state import StateStore
from app.utils import BackupResult


CSV_TEMPLATE = """编号,开始答题时间,结束答题时间,答题时长,1.发票抬头,2.税号,3.邮箱,4.上传结账单或付款截图,5.手机号,6.备注
{rows}
"""


@dataclass
class FakeSurveyClient:
    csv_by_store: dict[str, str]

    def export_csv(self, store: StoreConfig) -> str:
        payload = self.csv_by_store.get(store.store_key)
        if payload is None:
            raise RuntimeError(f"missing csv for {store.store_key}")
        return payload

    def download_attachment(self, store: StoreConfig, file_name: str) -> bytes:
        return f"{store.store_key}:{file_name}".encode("utf-8")


class FakeVisionClient:
    def __init__(self) -> None:
        self.smoke_test_calls = 0

    def smoke_test(self) -> None:
        self.smoke_test_calls += 1

    def extract_total_amount(self, image_bytes: bytes, file_name: str) -> AmountExtraction:
        if file_name == "broken.png":
            raise RuntimeError("vision failed")
        return AmountExtraction(
            total_amount=Decimal("123.45"),
            confidence=Decimal("0.90"),
            notes="ok",
            raw_response='{"total_amount":"123.45"}',
        )


class FakeTaxLookupClient:
    def lookup(self, company_name: str) -> TaxLookupResult:
        if "失败" in company_name:
            raise RuntimeError("lookup failed")
        if "公司" in company_name:
            return TaxLookupResult(
                provider="fake",
                status="success",
                tax_id="AB12345678",
                matched_name=company_name,
                candidate_count=1,
                message="ok",
            )
        return TaxLookupResult(
            provider="fake",
            status="no_result",
            tax_id=None,
            matched_name=None,
            message="missing",
        )


class FakeExcelWriter:
    def __init__(self, output_root: Path) -> None:
        self.output_root = output_root

    def write_store_workbook(self, store: StoreConfig, invoices: list[object]) -> BackupResult:
        output_path = self.output_root / f"{store.store_key}.xlsx"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(str(len(invoices)), encoding="utf-8")
        return BackupResult(backup_path=None, output_path=output_path)


class FakeMailer:
    def __init__(self, should_fail: bool = False) -> None:
        self.last_summary: BatchRunSummary | None = None
        self.should_fail = should_fail

    def send_summary(self, summary: BatchRunSummary) -> None:
        self.last_summary = summary
        if self.should_fail:
            raise RuntimeError("smtp down")


class BatchProcessorTests(unittest.TestCase):
    def test_batch_processor_isolates_store_failures_and_updates_state_per_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            store_a = StoreConfig(
                store_key="store_a",
                store_name="门店A",
                survey_id="22512014",
                output_xlsx_path=tmp_path / "out_a.xlsx",
                initial_last_processed_id=307,
            )
            store_b = StoreConfig(
                store_key="store_b",
                store_name="门店B",
                survey_id="22512015",
                output_xlsx_path=tmp_path / "out_b.xlsx",
                initial_last_processed_id=500,
            )
            csv_a = CSV_TEMPLATE.format(
                rows="\n".join(
                    [
                        "309,2026/5/26 12:00,2026/5/26 12:01,30,深圳易思商务咨询有限公司厦门分公司,,a@example.com,a.png,,",
                        "308,2026/5/26 11:00,2026/5/26 11:01,31,吴翔,无税号 个人抬头,b@example.com,broken.png,,",
                    ]
                )
            )
            survey_client = FakeSurveyClient({"store_a": csv_a})
            vision_client = FakeVisionClient()
            mailer = FakeMailer()
            state_store = StateStore(tmp_path / "state.db")
            processor = BatchProcessor(
                Services(
                    survey_client=survey_client,
                    vision_client=vision_client,
                    tax_lookup_client=FakeTaxLookupClient(),
                    excel_writer=FakeExcelWriter(tmp_path / "outputs"),
                    state_store=state_store,
                    mailer=mailer,
                )
            )

            summary = processor.run([store_a, store_b])

            self.assertEqual(1, vision_client.smoke_test_calls)
            self.assertIsNotNone(mailer.last_summary)
            self.assertEqual(1, len(summary.succeeded))
            self.assertEqual(1, len(summary.failed))
            self.assertEqual("store_a", summary.succeeded[0].store_key)
            self.assertEqual("store_b", summary.failed[0].store_key)
            self.assertEqual(309, state_store.get_last_processed_id(store_a))
            self.assertEqual(500, state_store.get_last_processed_id(store_b))
            self.assertTrue(any("金额识别失败" in warning or "图片处理失败" in warning for warning in summary.succeeded[0].warnings))

    def test_email_failure_does_not_advance_store_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            store = StoreConfig(
                store_key="store_a",
                store_name="门店A",
                survey_id="22512014",
                output_xlsx_path=tmp_path / "out_a.xlsx",
                initial_last_processed_id=307,
            )
            csv_text = CSV_TEMPLATE.format(
                rows="308,2026/5/26 11:00,2026/5/26 11:01,31,吴翔,,a@example.com,a.png,,"
            )
            state_store = StateStore(tmp_path / "state.db")
            processor = BatchProcessor(
                Services(
                    survey_client=FakeSurveyClient({"store_a": csv_text}),
                    vision_client=FakeVisionClient(),
                    tax_lookup_client=FakeTaxLookupClient(),
                    excel_writer=FakeExcelWriter(tmp_path / "outputs"),
                    state_store=state_store,
                    mailer=FakeMailer(should_fail=True),
                )
            )

            with self.assertRaises(RuntimeError):
                processor.run([store])

            self.assertEqual(307, state_store.get_last_processed_id(store))
