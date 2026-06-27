from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import re
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
    portal_enabled: bool = False
    portal_priority: int = 1000
    portal_company_switch_name: Optional[str] = None
    portal_company_verify_name: Optional[str] = None
    portal_company_role: str = "legal_representative"
    portal_area: str = "xiamen"
    portal_area_name: Optional[str] = None

    def effective_attachment_question_id(self, default_question_id: Optional[str]) -> str:
        question_id = self.attachment_question_id or default_question_id
        if not question_id:
            raise ValueError(
                f"Store '{self.store_key}' is missing attachment_question_id and no global default is configured."
            )
        return question_id

    def effective_portal_company_switch_name(self) -> str:
        value = (self.portal_company_switch_name or "").strip()
        if not value:
            raise ValueError(f"Store '{self.store_key}' is missing portal_company_switch_name.")
        return value

    def effective_portal_company_verify_name(self) -> str:
        value = (self.portal_company_verify_name or "").strip()
        if not value:
            raise ValueError(f"Store '{self.store_key}' is missing portal_company_verify_name.")
        return value

    def effective_portal_area(self) -> str:
        value = (self.portal_area or "").strip().lower()
        if not value:
            return "xiamen"
        if not re.fullmatch(r"[a-z0-9-]+", value):
            raise ValueError(
                f"Store '{self.store_key}' has invalid portal_area={self.portal_area!r}; "
                "expected lowercase letters, numbers, or hyphens."
            )
        return value

    def effective_portal_area_name(self) -> str:
        value = (self.portal_area_name or "").strip()
        if value:
            return value
        return self.effective_portal_area()


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
    openai_ssl_verify: bool = True
    openai_ca_bundle_path: Optional[Path] = None
    survey_ssl_verify: bool = True
    survey_ca_bundle_path: Optional[Path] = None
    tax_lookup_ssl_verify: bool = True
    tax_lookup_ca_bundle_path: Optional[Path] = None
    tax_lookup_provider: str = "disabled"
    tax_lookup_alapi_token: Optional[str] = None
    tax_lookup_url_template: Optional[str] = None
    tax_lookup_extra_headers: dict[str, str] = field(default_factory=dict)
    tax_lookup_value_path: Optional[str] = None
    tax_lookup_timeout_seconds: int = 60
    tax_lookup_cache_negative_ttl_hours: int = 24
    portal_home_url: str = "https://etax.xiamen.chinatax.gov.cn:8443/loginb/"
    portal_identity_switch_url: str = (
        "https://tpass.xiamen.chinatax.gov.cn:8443/#/identitySwitch/enterprise"
        "?client_id=y56b7aay5brf48f8aa7bf24dd54d775r"
    )
    portal_batch_issue_url: str = "https://dppt.xiamen.chinatax.gov.cn:8443/blue-invoice-makeout/invoice-batch"
    portal_user_data_dir: Optional[Path] = None
    portal_artifacts_dir: Optional[Path] = None
    portal_browser_backend: str = "playwright"
    portal_chrome_cdp_url: Optional[str] = None
    portal_chrome_cdp_user_data_dir: Optional[Path] = None
    portal_chrome_executable_path: Optional[Path] = None
    portal_etax_app_username: Optional[str] = None
    portal_etax_app_password: Optional[str] = None
    portal_etax_app_path: Optional[Path] = None
    portal_browser_channel: str = "chrome"
    portal_disable_proxy: bool = False
    portal_headless: bool = False
    portal_slow_mo_ms: int = 0
    portal_action_timeout_ms: int = 15000
    portal_login_timeout_minutes: int = 30
    portal_block_on_empty_amount: bool = True
    portal_sync_from_chrome_profile: bool = False
    portal_chrome_profile_dir: Optional[Path] = None
    portal_sync_from_server: bool = False
    portal_sync_remote_host: Optional[str] = None
    portal_sync_remote_output_dir: Optional[str] = None
    portal_sync_ssh_key_path: Optional[Path] = None
    portal_sync_ssh_port: int = 22
    portal_sync_connect_timeout_seconds: int = 10
    portal_sync_strict_host_key_checking: bool = False
    portal_sync_batch_mode: bool = True

    @staticmethod
    def _render_portal_url(url_template: str, portal_area: Optional[str]) -> str:
        area = (portal_area or "").strip().lower() or "xiamen"
        return url_template.replace("{portal_area}", area).replace("{store_area}", area)

    def portal_home_url_for_store(self, store: Optional[StoreConfig]) -> str:
        area = store.effective_portal_area() if store is not None else None
        return self._render_portal_url(self.portal_home_url, area)

    def portal_identity_switch_url_for_store(self, store: Optional[StoreConfig]) -> str:
        area = store.effective_portal_area() if store is not None else None
        return self._render_portal_url(self.portal_identity_switch_url, area)

    def portal_batch_issue_url_for_store(self, store: Optional[StoreConfig]) -> str:
        area = store.effective_portal_area() if store is not None else None
        return self._render_portal_url(self.portal_batch_issue_url, area)


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
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: Optional[datetime] = None

    def finalize(self) -> "StoreRunResult":
        self.finished_at = datetime.now(timezone.utc)
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


@dataclass(frozen=True)
class TaxLookupResult:
    provider: str
    status: str
    tax_id: Optional[str]
    matched_name: Optional[str]
    candidate_count: int = 0
    from_cache: bool = False
    raw_response_json: str = ""
    message: str = ""


@dataclass(frozen=True)
class TaxLookupCacheEntry:
    provider: str
    normalized_company_name: str
    cache_status: str
    lookup_status: str
    tax_id: Optional[str]
    matched_name: Optional[str]
    candidate_count: int
    raw_response_json: str
    updated_at: datetime


@dataclass(frozen=True)
class PortalIssueRow:
    invoice_serial: str
    buyer_name: str
    buyer_tax_id: Optional[str]
    buyer_email: Optional[str]
    amount_excluding_tax: Decimal
    tax_rate: Decimal
    tax_amount: Decimal
    amount_including_tax: Decimal


@dataclass(frozen=True)
class PortalIssueDetail:
    invoice_serial: str
    digital_invoice_number: Optional[str]
    buyer_email: Optional[str]
    status: str
    failure_reason: Optional[str]


@dataclass
class PortalIssueResult:
    store_key: str
    store_name: str
    portal_area_name: str
    company_verify_name: str
    portal_company_role: str
    workbook_path: Path
    workbook_sha256: str
    mode: str
    expected_count: int
    submitted_count: int
    success_count: int
    failure_count: int
    status: str
    step: str
    details: tuple[PortalIssueDetail, ...] = ()
    artifacts_dir: Optional[Path] = None
    error: Optional[str] = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: Optional[datetime] = None

    def finalize(self) -> "PortalIssueResult":
        self.finished_at = datetime.now(timezone.utc)
        return self
