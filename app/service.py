from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.csv_processing import parse_survey_csv, select_new_records
from app.excel_writer import InvoiceExcelWriter
from app.mailer import SummaryMailer
from app.models import BatchRunSummary, NormalizedInvoice, StoreConfig, StoreRunResult, TaxLookupResult
from app.state import StateStore
from app.survey_client import TencentSurveyClient
from app.tax_lookup import TaxLookupClient
from app.utils import (
    format_decimal_text,
    looks_like_natural_person,
    looks_like_valid_enterprise_tax_id,
    normalize_tax_id,
)
from app.vision_client import OpenAICompatibleVisionClient


@dataclass(frozen=True)
class Services:
    survey_client: TencentSurveyClient
    vision_client: OpenAICompatibleVisionClient
    tax_lookup_client: TaxLookupClient
    excel_writer: InvoiceExcelWriter
    state_store: StateStore
    mailer: SummaryMailer


class BatchProcessor:
    def __init__(self, services: Services) -> None:
        self.services = services

    def run(self, stores: list[StoreConfig]) -> BatchRunSummary:
        self.services.vision_client.smoke_test()
        results: list[StoreRunResult] = []
        for store in stores:
            if not store.enabled:
                continue
            result = self._run_store(store)
            results.append(result)
        summary = BatchRunSummary(results=results)
        email_error: Optional[Exception] = None
        try:
            self.services.mailer.send_summary(summary)
        except Exception as exc:  # noqa: BLE001
            email_error = exc
            for result in results:
                result.warnings.append(f"汇总邮件发送失败: {exc}")
        for result in results:
            self.services.state_store.record_result(result, update_progress=email_error is None)
        if email_error is not None:
            raise RuntimeError(f"Failed to send summary email: {email_error}") from email_error
        return summary

    def _run_store(self, store: StoreConfig) -> StoreRunResult:
        last_processed_id = self.services.state_store.get_last_processed_id(store)
        result = StoreRunResult(
            store_key=store.store_key,
            store_name=store.store_name,
            survey_id=store.survey_id,
            status="failed",
            processed_count=0,
            last_processed_id_before=last_processed_id,
            last_processed_id_after=last_processed_id,
        )
        try:
            csv_text = self.services.survey_client.export_csv(store)
            records = parse_survey_csv(csv_text)
            new_records = select_new_records(records, last_processed_id)
            if new_records:
                invoices = self._normalize_records(store, new_records, result)
                result.status = "success"
                result.processed_count = len(invoices)
                result.last_processed_id_after = max(record.submission_id for record in new_records)
            else:
                invoices = []
                result.status = "no_new_data"
            backup_result = self.services.excel_writer.write_store_workbook(store, invoices)
            result.output_path = backup_result.output_path
            result.backup_path = backup_result.backup_path
            return result.finalize()
        except Exception as exc:  # noqa: BLE001
            result.error = str(exc)
            result.status = "failed"
            return result.finalize()

    def _normalize_records(
        self,
        store: StoreConfig,
        records: list[object],
        result: StoreRunResult,
    ) -> list[NormalizedInvoice]:
        invoices: list[NormalizedInvoice] = []
        for index, record in enumerate(records, start=1):
            warnings: list[str] = []
            natural_person = looks_like_natural_person(record.invoice_title)
            provided_tax_id = normalize_tax_id(record.tax_id_raw)
            tax_id = provided_tax_id
            invalid_enterprise_tax_id = False
            if not natural_person and tax_id and not looks_like_valid_enterprise_tax_id(tax_id):
                invalid_enterprise_tax_id = True
                tax_id = None
            if not natural_person and not tax_id:
                try:
                    looked_up = self.services.tax_lookup_client.lookup(record.invoice_title)
                except Exception as exc:  # noqa: BLE001
                    if invalid_enterprise_tax_id and provided_tax_id:
                        warnings.append(
                            "编号 "
                            f"{record.submission_id} [tax_lookup:invalid_tax_id] "
                            f"企业抬头原税号格式异常，未能自动修正: {provided_tax_id}"
                        )
                    warnings.append(
                        f"编号 {record.submission_id} [tax_lookup:provider_error] 税号查询失败: {exc}"
                    )
                else:
                    if looked_up.tax_id:
                        tax_id = looked_up.tax_id
                        if invalid_enterprise_tax_id and provided_tax_id:
                            warnings.append(
                                "编号 "
                                f"{record.submission_id} [tax_lookup:replaced_invalid_tax_id] "
                                f"企业抬头原税号格式异常，已改用查询结果: {provided_tax_id} -> {tax_id}"
                            )
                    else:
                        if invalid_enterprise_tax_id and provided_tax_id:
                            warnings.append(
                                "编号 "
                                f"{record.submission_id} [tax_lookup:invalid_tax_id] "
                                f"企业抬头原税号格式异常，且未查询到可替代税号: "
                                f"{record.invoice_title} ({provided_tax_id})"
                            )
                        warning = self._build_tax_lookup_warning(record.submission_id, record.invoice_title, looked_up)
                        if warning:
                            warnings.append(warning)

            amount_text: Optional[str] = None
            if record.attachment_name:
                try:
                    image_bytes = self.services.survey_client.download_attachment(
                        store, record.attachment_name
                    )
                    amount = self.services.vision_client.extract_total_amount(
                        image_bytes, record.attachment_name
                    )
                    amount_text = format_decimal_text(amount.total_amount)
                    if amount_text is None:
                        warnings.append(
                            f"编号 {record.submission_id} 金额识别失败: {amount.notes or '未返回金额'}"
                        )
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"编号 {record.submission_id} 图片处理失败: {exc}")
            else:
                warnings.append(f"编号 {record.submission_id} 未上传付款截图")

            invoice = NormalizedInvoice(
                source_submission_id=record.submission_id,
                invoice_serial=str(index),
                invoice_title=record.invoice_title,
                is_natural_person=natural_person,
                tax_id=tax_id,
                email=record.email,
                amount_text=amount_text,
                remark=record.remark,
                warnings=tuple(warnings),
            )
            invoices.append(invoice)
            result.warnings.extend(warnings)
        return invoices

    @staticmethod
    def _build_tax_lookup_warning(
        submission_id: int,
        invoice_title: str,
        looked_up: TaxLookupResult,
    ) -> Optional[str]:
        if looked_up.status == "disabled":
            return None
        if looked_up.from_cache:
            return (
                f"编号 {submission_id} [tax_lookup:cache_hit_miss] 企业抬头缓存未命中税号: "
                f"{invoice_title}"
            )
        if looked_up.status == "no_exact_match":
            suffix = f"候选 {looked_up.candidate_count} 条" if looked_up.candidate_count else "无精确候选"
            return (
                f"编号 {submission_id} [tax_lookup:no_exact_match] 企业抬头未命中精确公司名: "
                f"{invoice_title} ({suffix})"
            )
        if looked_up.status == "no_result":
            return f"编号 {submission_id} [tax_lookup:no_result] 企业抬头未查询到税号: {invoice_title}"
        return f"编号 {submission_id} [tax_lookup:{looked_up.status}] 企业抬头未查询到税号: {invoice_title}"
