from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class StoreConfig:
    store_key: str
    store_name: str
    survey_id: str
    output_xlsx_path: Path
    initial_last_processed_id: int
    enabled: bool = True
    attachment_question_id: Optional[str] = None

    def effective_attachment_question_id(self, default_question_id: Optional[str]) -> str:
        question_id = self.attachment_question_id or default_question_id
        if not question_id:
            raise ValueError(
                f"Store '{self.store_key}' is missing attachment_question_id and no global default is configured."
            )
        return question_id


@dataclass(frozen=True)
class AppConfig:
    timezone: str
    survey_cookie: str
    survey_export_url: str
    survey_export_method: str
    survey_export_body_template: str
    survey_export_download_url_path: Optional[str]
    survey_extra_headers: dict[str, str]
    default_attachment_question_id: Optional[str]
    openai_base_url: str
    openai_api_key: str
    openai_model: str
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_from: str
    smtp_to: list[str]
    template_xlsx_path: Path
    state_db_path: Path
    stores_config_path: Path
    backups_root: Path
    run_hour: int = 22
    run_minute: int = 30
    openai_timeout_seconds: int = 60
    survey_timeout_seconds: int = 60
    tax_lookup_url_template: Optional[str] = None
    tax_lookup_extra_headers: dict[str, str] = field(default_factory=dict)
    tax_lookup_value_path: Optional[str] = None


@dataclass(frozen=True)
class RawSurveyRecord:
    submission_id: int
    start_time: str
    end_time: str
    duration_seconds: str
    invoice_title: str
    tax_id_raw: str
    email: str
    attachment_name: str
    phone: str
    remark: str
    raw: dict[str, str]


@dataclass(frozen=True)
class AmountExtraction:
    total_amount: Optional[Decimal]
    confidence: Optional[Decimal]
    notes: str
    raw_response: str


@dataclass(frozen=True)
class NormalizedInvoice:
    source_submission_id: int
    invoice_serial: str
    invoice_title: str
    is_natural_person: bool
    tax_id: Optional[str]
    email: str
    amount_text: Optional[str]
    remark: str
    warnings: tuple[str, ...] = ()


@dataclass
class StoreRunResult:
    store_key: str
    store_name: str
    survey_id: str
    status: str
    processed_count: int
    last_processed_id_before: int
    last_processed_id_after: int
    output_path: Optional[Path] = None
    backup_path: Optional[Path] = None
    warnings: list[str] = field(default_factory=list)
    error: Optional[str] = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: Optional[datetime] = None

    def finalize(self) -> "StoreRunResult":
        self.finished_at = datetime.now(UTC)
        return self


@dataclass(frozen=True)
class BatchRunSummary:
    results: list[StoreRunResult]

    @property
    def succeeded(self) -> list[StoreRunResult]:
        return [result for result in self.results if result.status == "success"]

    @property
    def no_new_data(self) -> list[StoreRunResult]:
        return [result for result in self.results if result.status == "no_new_data"]

    @property
    def failed(self) -> list[StoreRunResult]:
        return [result for result in self.results if result.status == "failed"]

    @property
    def warning_results(self) -> list[StoreRunResult]:
        return [result for result in self.results if result.warnings]
