from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from time import monotonic, sleep
from typing import Callable
from urllib.parse import urlparse
from urllib.request import ProxyHandler, build_opener

from app.models import AppConfig, PortalIssueDetail, PortalIssueResult, PortalIssueRow, StoreConfig
from app.photos_qr_cleanup import (
    ImportedPhotosQr,
    PhotosQrCleanupError,
    delete_imported_qr_from_photos,
)
from app.portal_local_login import PortalLocalLoginError, PortalMacLoginAutomator
from app.portal_sync import PortalWorkbookSyncer
from app.portal_workbook import load_portal_issue_rows, sha256_file, summarize_portal_issue_rows
from app.state import StateStore
from app.utils import ensure_parent_dir

ROLE_LABELS = {
    "legal_representative": "法定代表人",
    "tax_operator": "办税员",
}

QR_REFRESH_GRACE_SECONDS = 10.0
LOGIN_WAIT_HEARTBEAT_SECONDS = 15.0
HOME_PAGE_BEFORE_BATCH_PAGE_DELAY_SECONDS = 3.0
DPPT_INVOICE_BUSINESS_POST_READY_DELAY_SECONDS = 3.0
DPPT_SENSITIVE_NAVIGATION_SETTLE_SECONDS = 3.0
POST_SUBMIT_SUCCESS_WAIT_SECONDS = 3.0
BROWSER_CLICK_DELAY_MS = 1000
BROWSER_CLICK_DELAY_SECONDS = BROWSER_CLICK_DELAY_MS / 1000
RAW_CDP_SUBMIT_ACTION_DELAY_SECONDS = 1.0
HOME_PAGE_SHELL_MIN_TIMEOUT_MS = 60000
HOME_PAGE_NETWORKIDLE_GRACE_MS = 30000
AUTHENTICATED_PAGE_STABILITY_MS = 1500
AUTHENTICATED_PAGE_STABILITY_POLL_MS = 250
BROWSER_SESSION_SYNC_ENTRIES = (
    "Cookies",
    "Cookies-journal",
    "Cookies-shm",
    "Cookies-wal",
    "Login Data",
    "Login Data-journal",
    "Login Data-shm",
    "Login Data-wal",
    "Local Storage",
    "Session Storage",
    "IndexedDB",
    "WebStorage",
    "Network",
)
PORTAL_TPASS_LOGIN_RE = re.compile(r"^https://tpass\.([a-z0-9-]+)\.chinatax\.gov\.cn:8443/#/login(?:[/?#].*)?$")
PORTAL_TPASS_OAUTH_API_RE = re.compile(
    r"^https://tpass\.([a-z0-9-]+)\.chinatax\.gov\.cn:8443/api/v1\.0/auth/oauth2/login(?:\?.*)?$"
)
PORTAL_ETAX_HOST_RE = re.compile(r"^https://etax\.([a-z0-9-]+)\.chinatax\.gov\.cn:8443(?:[/?#].*)?$")
PORTAL_TPASS_HOST_RE = re.compile(r"^https://tpass\.([a-z0-9-]+)\.chinatax\.gov\.cn:8443(?:[/?#].*)?$")
PORTAL_DPPT_HOST_RE = re.compile(r"^https://dppt\.([a-z0-9-]+)\.chinatax\.gov\.cn:8443(?:[/?#].*)?$")


class PortalRunnerError(RuntimeError):
    pass


@dataclass(frozen=True)
class _RawBatchSession:
    target: dict[str, object]


class TaxPortalRunner:
    def __init__(self, config: AppConfig, state_store: StateStore, submit: bool) -> None:
        self.config = config
        self.state_store = state_store
        self.submit = submit
        self.syncer = PortalWorkbookSyncer(config)
        self.network_diag_enabled = self._env_flag("TAX_PORTAL_NETWORK_DIAG")
        self.network_diag_threshold_ms = float(os.environ.get("TAX_PORTAL_NETWORK_DIAG_THRESHOLD_MS", "800"))
        self._attached_cdp_browser_type: object | None = None
        self._attached_cdp_url: str | None = None
        self._attached_cdp_connections: list[object] = []
        self._sync_playwright_factory: Callable[[], object] | None = None
        self._attached_playwright: object | None = None
        self._attached_cdp_auth_connection: object | None = None
        self._attached_cdp_dppt_connections: dict[str, object] = {}
        self._observed_attached_pages: list[object] = []
        self._dialog_inert_context_ids: set[int] = set()
        self._dialog_inert_page_ids: set[int] = set()
        if config.portal_browser_backend not in {"playwright", "chrome_cdp"}:
            raise ValueError(
                "TAX_PORTAL_BROWSER_BACKEND must be one of: playwright, chrome_cdp."
            )
        if config.portal_browser_backend == "playwright" and config.portal_user_data_dir is None:
            raise ValueError("TAX_PORTAL_USER_DATA_DIR is required for portal runner commands.")

    def run(self, stores: list[StoreConfig]) -> list[PortalIssueResult]:
        sync_playwright = self._load_sync_playwright()
        if self.config.portal_browser_backend == "chrome_cdp":
            self._sync_playwright_factory = sync_playwright
            playwright = sync_playwright().start()
            self._attached_playwright = playwright
            try:
                return self._run_with_attached_chrome(playwright, stores)
            finally:
                active_playwright = self._attached_playwright
                self._attached_playwright = None
                self._sync_playwright_factory = None
                if active_playwright is not None:
                    try:
                        active_playwright.stop()
                    except Exception:
                        pass
        with sync_playwright() as playwright:
            return self._run_with_playwright_context(playwright, stores)

    def _run_with_playwright_context(self, playwright: object, stores: list[StoreConfig]) -> list[PortalIssueResult]:
        results: list[PortalIssueResult] = []
        self._sync_portal_profile_from_chrome()
        launch_args = self._launch_args()
        if self.config.portal_disable_proxy:
            self._log("runner", "launching Chrome with proxy disabled for tax portal requests")
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.config.portal_user_data_dir),
            channel=self.config.portal_browser_channel or None,
            args=launch_args or None,
            headless=self.config.portal_headless,
            slow_mo=max(self.config.portal_slow_mo_ms, 0) or None,
        )
        context.set_default_timeout(self.config.portal_action_timeout_ms)
        home_page = context.pages[0] if context.pages else context.new_page()
        self._install_network_diag(context, home_page, "runner", "home")
        try:
            for store in stores:
                store_result = self._run_store(context, home_page, store)
                results.append(store_result)
                if self._should_abort_remaining_stores(store_result):
                    self._log(
                        "runner",
                        "stopping remaining stores because the previous store did not finish safely "
                        f"store_key={store_result.store_key} error={store_result.error or '<none>'}",
                    )
                    break
        finally:
            context.close()
        return results

    def _run_with_attached_chrome(self, playwright: object, stores: list[StoreConfig]) -> list[PortalIssueResult]:
        results: list[PortalIssueResult] = []
        cdp_url = self.config.portal_chrome_cdp_url or "http://127.0.0.1:9222"
        self._log("runner", f"connecting to current Chrome session via CDP: {cdp_url}")
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        self._attached_cdp_browser_type = playwright.chromium
        self._attached_cdp_url = cdp_url
        self._attached_cdp_connections = [browser]
        if not browser.contexts:
            raise PortalRunnerError(
                "Connected to Chrome via CDP, but no browser context was available."
            )
        context = browser.contexts[0]
        context.set_default_timeout(self.config.portal_action_timeout_ms)
        self._observed_attached_pages = list(getattr(context, "pages", []))
        on_context_event = getattr(context, "on", None)
        if callable(on_context_event):
            on_context_event("page", self._record_attached_page)
        home_page = self._resolve_attached_home_page(context)
        created_home_pages = [home_page] if getattr(home_page, "_codex_created_page", False) else []
        self._install_network_diag(context, home_page, "runner", "home")
        try:
            for store in stores:
                if not self._attached_cdp_connections:
                    browser = self._restart_attached_cdp_transport(store)
                else:
                    browser = self._attached_cdp_connections[0]
                if not browser.contexts:
                    raise PortalRunnerError(
                        "Connected to Chrome via CDP, but no browser context was available."
                    )
                context = browser.contexts[0]
                context.set_default_timeout(self.config.portal_action_timeout_ms)
                home_page = self._find_attached_portal_page(context, store)
                if home_page is None:
                    home_page = self._resolve_attached_home_page(context)
                if (
                    getattr(home_page, "_codex_created_page", False)
                    and home_page not in created_home_pages
                ):
                    created_home_pages.append(home_page)
                store_result = self._run_store(context, home_page, store)
                results.append(store_result)
                if self._should_abort_remaining_stores(store_result):
                    self._log(
                        "runner",
                        "stopping remaining stores because the previous store did not finish safely "
                        f"store_key={store_result.store_key} error={store_result.error or '<none>'}",
                    )
                    break
        finally:
            for created_home_page in created_home_pages:
                try:
                    created_home_page.close()
                except Exception:
                    pass
            self._attached_cdp_browser_type = None
            self._attached_cdp_url = None
            self._attached_cdp_connections = []
            self._attached_cdp_auth_connection = None
            self._attached_cdp_dppt_connections = {}
            self._observed_attached_pages = []
            self._dialog_inert_context_ids = set()
            self._dialog_inert_page_ids = set()
        return results

    def _find_attached_portal_page(
        self,
        context: object,
        store: StoreConfig | None = None,
    ) -> object | None:
        portal_candidate: object | None = None
        for candidate in getattr(context, "pages", []):
            try:
                if not self._page_matches_portal_area(candidate, store):
                    continue
                if self._is_home_page(candidate):
                    return candidate
                if portal_candidate is None and self._is_portal_related_page(candidate):
                    portal_candidate = candidate
            except Exception:
                continue
        return portal_candidate

    def _resolve_attached_home_page(self, context: object) -> object:
        portal_candidate = self._find_attached_portal_page(context)
        if portal_candidate is not None:
            return portal_candidate
        page = context.new_page()
        try:
            setattr(page, "_codex_created_page", True)
        except Exception:
            pass
        return page

    def _run_store(self, context: object, home_page: object, store: StoreConfig) -> PortalIssueResult:
        active_page = home_page
        if self.config.portal_sync_from_server:
            remote_output_dir = (self.config.portal_sync_remote_output_dir or "").rstrip("/")
            self._update_store_step(
                store.store_key,
                "sync_workbook",
                "running",
                message=(
                    "syncing workbook from "
                    f"{self.config.portal_sync_remote_host}:{remote_output_dir}/{store.output_xlsx_path.name}"
                ),
            )
            self.syncer.sync_store_workbook(store)
            self._log(
                store.store_key,
                f"step=sync_workbook status=running synced workbook to {store.output_xlsx_path}",
            )
        rows = load_portal_issue_rows(
            store.output_xlsx_path,
            block_on_empty_amount=self.config.portal_block_on_empty_amount,
        )
        summary = summarize_portal_issue_rows(rows)
        workbook_sha = sha256_file(store.output_xlsx_path)
        result = PortalIssueResult(
            store_key=store.store_key,
            store_name=store.store_name,
            portal_area_name=store.effective_portal_area_name(),
            company_verify_name=store.effective_portal_company_verify_name(),
            portal_company_role=store.portal_company_role,
            workbook_path=store.output_xlsx_path,
            workbook_sha256=workbook_sha,
            mode="submit" if self.submit else "dry_run",
            expected_count=summary.row_count,
            submitted_count=0,
            success_count=0,
            failure_count=0,
            status="running",
            step="prepare_workbook",
            artifacts_dir=self._prepare_artifacts_dir(store.store_key),
        )
        self._update_store_step(
            store.store_key,
            result.step,
            result.status,
            workbook_sha256=result.workbook_sha256,
            message=(
                f"loaded workbook path={store.output_xlsx_path} "
                f"rows={summary.row_count} "
                f"total_amount_including_tax={self._format_money(summary.total_amount_including_tax)} "
                f"sha256={result.workbook_sha256[:12]}"
            ),
        )
        if not rows:
            result.status = "skipped"
            result.step = "skip_empty_workbook"
            self._log(
                store.store_key,
                "step=skip_empty_workbook status=skipped workbook has no portal-issuable rows; skipping store",
            )
            self._update_store_step(
                store.store_key,
                result.step,
                result.status,
                workbook_sha256=result.workbook_sha256,
                message="workbook has no portal-issuable rows; skipping store",
            )
            return self._finalize_result(result)
        try:
            reusable_home_page: object | None = None
            if self.config.portal_browser_backend == "chrome_cdp":
                reusable_home_page = self._find_attached_portal_page(context, store)
                if reusable_home_page is not None:
                    home_page = reusable_home_page
            if (
                self.config.portal_browser_backend == "chrome_cdp"
                and reusable_home_page is not None
                and self._is_home_page(home_page)
            ):
                self._log(store.store_key, f"reusing attached Chrome home page: {home_page.url}")
            elif (
                self.config.portal_browser_backend == "chrome_cdp"
                and reusable_home_page is not None
                and self._is_portal_related_page(home_page)
            ):
                self._log(store.store_key, f"reusing attached Chrome portal page: {home_page.url}")
            else:
                home_url = self.config.portal_home_url_for_store(store)
                self._log(store.store_key, f"opening portal home page: {home_url}")
                self._goto(home_url, home_page)
            home_page = self._ensure_logged_in(home_page, result, store)
            home_page = self._ensure_authenticated_home_page(home_page, result, store)
            context = getattr(home_page, "context", context)
            home_page = self._ensure_company(home_page, store, result)
            self._wait_for_home_page_ready(home_page, store)
            self._wait_before_open_batch_page(store.store_key)
            batch_page: object | None = None
            try:
                batch_page = self._wait_for_batch_page(home_page, store, result)
                if isinstance(batch_page, _RawBatchSession):
                    result.step = "validated"
                    self._update_store_step(
                        store.store_key,
                        result.step,
                        "validated",
                        workbook_sha256=result.workbook_sha256,
                        message=(
                            "raw CDP import validation passed "
                            f"rows={summary.row_count} "
                            f"total_amount_including_tax="
                            f"{self._format_money(summary.total_amount_including_tax)}"
                        ),
                    )
                    self._raw_cdp_select_all_batch_rows(
                        batch_page.target,
                        store.store_key,
                        result.expected_count,
                    )
                    if not self.submit:
                        self._log(
                            store.store_key,
                            "dry run select-all is ready; waiting "
                            f"{RAW_CDP_SUBMIT_ACTION_DELAY_SECONDS:.0f}s before closing batch tab",
                        )
                        sleep(RAW_CDP_SUBMIT_ACTION_DELAY_SECONDS)
                        self._close_raw_cdp_target(batch_page.target, store.store_key)
                        result.status = "validated"
                        self._log(
                            store.store_key,
                            "dry run finished after raw CDP validation and select-all verification; "
                            "batch tab closed and submission skipped",
                        )
                        return self._finalize_result(result)

                    self._log(
                        store.store_key,
                        "step=submit_result status=running "
                        f"submitting rows={summary.row_count} "
                        f"total_amount_including_tax="
                        f"{self._format_money(summary.total_amount_including_tax)} "
                        "through raw CDP",
                    )
                    result.step = "submit_result"
                    try:
                        details, success_count, failure_count, _result_modal_text = (
                            self._raw_cdp_submit_batch(
                                batch_page.target,
                                store.store_key,
                                result,
                            )
                        )
                    except Exception:
                        self._capture_raw_cdp_artifact(
                            batch_page.target,
                            result.artifacts_dir,
                            f"{store.store_key}-raw-submit-failure",
                        )
                        raise
                    result.details = tuple(details)
                    result.success_count = success_count
                    result.failure_count = failure_count
                    result.status = "success" if failure_count == 0 else "failed"
                    result.step = "submit_result"
                    if failure_count > 0:
                        self._capture_raw_cdp_artifact(
                            batch_page.target,
                            result.artifacts_dir,
                            f"{store.store_key}-submit-result",
                        )
                        parsed_failed_details = [
                            detail for detail in details if detail.status == "失败"
                        ]
                        if len(parsed_failed_details) < failure_count:
                            self._log(
                                store.store_key,
                                "submit result reported failures but parsed fewer failed detail rows "
                                "than expected; saved raw CDP submit result artifact",
                            )
                    self._log(
                        store.store_key,
                        f"step=submit_result status={result.status} "
                        f"success_count={success_count} failure_count={failure_count}",
                    )
                    if result.status == "success":
                        self._log(
                            store.store_key,
                            "submission succeeded; waiting "
                            f"{POST_SUBMIT_SUCCESS_WAIT_SECONDS:.0f}s before continuing",
                        )
                        sleep(POST_SUBMIT_SUCCESS_WAIT_SECONDS)
                    return self._finalize_result(result)
                active_page = batch_page
                self._install_network_diag(context, batch_page, store.store_key, "batch")
                if getattr(batch_page, "_tax_portal_raw_import_ready", False):
                    success_prefix = (
                        f"导入完成，共处理数据{summary.row_count}条，"
                        f"处理成功{summary.row_count}条"
                    )
                    self._wait_for_batch_import_ready(
                        batch_page,
                        store.store_key,
                        store.output_xlsx_path.name,
                        success_prefix,
                    )
                    self._log(
                        store.store_key,
                        f"workbook import verified after raw CDP upload rows={summary.row_count}",
                    )
                else:
                    self._ensure_batch_page_clean(batch_page, store.store_key)
                    self._log(store.store_key, "batch issue page is clean and ready for workbook import")
                    self._import_workbook(
                        batch_page,
                        store.store_key,
                        store.output_xlsx_path,
                        rows,
                        summary,
                    )
                result.step = "validated"
                self._update_store_step(
                    store.store_key,
                    result.step,
                    "validated",
                    workbook_sha256=result.workbook_sha256,
                    message=(
                        "import validation passed "
                        f"rows={summary.row_count} "
                        f"total_amount_including_tax={self._format_money(summary.total_amount_including_tax)}"
                    ),
                )
                self._select_all_rows(batch_page)
                if not self.submit:
                    result.status = "validated"
                    self._log(
                        store.store_key,
                        "dry run finished after validation and select-all verification; submission skipped",
                    )
                    return self._finalize_result(result)

                self._log(
                    store.store_key,
                    "step=submit_result status=running "
                    f"submitting rows={summary.row_count} "
                    f"total_amount_including_tax={self._format_money(summary.total_amount_including_tax)}",
                )
                self._open_submit_confirmation(
                    batch_page,
                    store.store_key,
                    summary.row_count,
                    summary.total_amount_including_tax,
                )
                self._confirm_submit(batch_page)
                details, success_count, failure_count, result_modal_text = self._wait_for_result_modal(
                    batch_page,
                    store.store_key,
                )
                result.details = tuple(details)
                result.submitted_count = summary.row_count
                result.success_count = success_count
                result.failure_count = failure_count
                result.status = "success" if failure_count == 0 else "failed"
                result.step = "submit_result"
                if failure_count > 0:
                    self._capture_submit_result_artifacts(
                        batch_page,
                        result.artifacts_dir,
                        store.store_key,
                        result_modal_text,
                    )
                    parsed_failed_details = [detail for detail in details if detail.status == "失败"]
                    if len(parsed_failed_details) < failure_count:
                        self._log(
                            store.store_key,
                            "submit result reported failures but parsed fewer failed detail rows than expected; "
                            "saved submit result artifacts for manual inspection",
                        )
                self._log(
                    store.store_key,
                    f"step=submit_result status={result.status} "
                    f"success_count={success_count} failure_count={failure_count}",
                )
                if result.status == "success":
                    self._log(
                        store.store_key,
                        f"submission succeeded; waiting {POST_SUBMIT_SUCCESS_WAIT_SECONDS:.0f}s before continuing",
                    )
                    sleep(POST_SUBMIT_SUCCESS_WAIT_SECONDS)
                return self._finalize_result(result)
            finally:
                try:
                    if batch_page is not None:
                        batch_page.close()
                except Exception:
                    pass
        except Exception as exc:  # noqa: BLE001
            result.status = "failed"
            result.step = result.step or "unknown"
            result.error = str(exc)
            self._log(store.store_key, f"step={result.step} status=failed error={result.error}")
            self._capture_artifact(active_page, result.artifacts_dir, f"{store.store_key}-failure")
            return self._finalize_result(result)

    @staticmethod
    def _should_abort_remaining_stores(result: PortalIssueResult) -> bool:
        return getattr(result, "status", None) == "failed"

    def _finalize_result(self, result: PortalIssueResult) -> PortalIssueResult:
        result.finalize()
        history_id = self.state_store.record_portal_issue_result(result)
        self._update_store_step(
            result.store_key,
            result.step,
            result.status,
            workbook_sha256=result.workbook_sha256,
            error=result.error,
            last_history_id=history_id,
            message=f"finished history_id={history_id}",
        )
        return result

    def _ensure_logged_in(
        self,
        page: object,
        result: PortalIssueResult,
        store: StoreConfig | None = None,
    ) -> object:
        imported_login_qrs: list[ImportedPhotosQr] = []
        authenticated_page = self._confirmed_authenticated_page(page, store)
        if authenticated_page is not None:
            return authenticated_page
        deadline = monotonic() + self.config.portal_login_timeout_minutes * 60
        refreshed_qr = False
        clicked_public_login = False
        local_app_login_attempted = False
        reauth_seen_at: float | None = None
        login_challenge_url: str | None = None
        last_heartbeat_at: float | None = None
        last_cdp_refresh_at: float | None = None
        gateway_restart_attempted = False
        self._log(result.store_key, "login required; waiting for successful login...")
        while monotonic() < deadline:
            if self.config.portal_browser_backend == "chrome_cdp" and store is not None:
                gateway_failure = self._blank_tpass_gateway_failure(store)
                if gateway_failure is not None:
                    gateway_target, gateway_snapshot = gateway_failure
                    gateway_url = str(gateway_target.get("url") or "")
                    gateway_status = int(gateway_snapshot.get("navigationStatus") or 0)
                    if gateway_restart_attempted:
                        raise PortalRunnerError(
                            "TPass gateway challenge remained on a blank OAuth API page after one "
                            f"dedicated Chrome restart url={gateway_url} http_status={gateway_status or 'unknown'}."
                        )
                    self._log(
                        result.store_key,
                        "error: detected blank TPass OAuth API gateway challenge "
                        f"url={gateway_url} http_status={gateway_status or 'unknown'}; "
                        "restarting dedicated Chrome once",
                    )
                    page = self._restart_dedicated_chrome_after_gateway_error(store)
                    gateway_restart_attempted = True
                    refreshed_qr = False
                    clicked_public_login = False
                    local_app_login_attempted = False
                    reauth_seen_at = None
                    login_challenge_url = None
                    last_cdp_refresh_at = None
                    continue
            authenticated_page = self._confirmed_authenticated_page(page, store)
            if authenticated_page is not None:
                if authenticated_page is page:
                    self._log(result.store_key, "login confirmed")
                else:
                    self._log(
                        result.store_key,
                        f"login confirmed on another page: {getattr(authenticated_page, 'url', '<unknown>')}",
                    )
                self._cleanup_imported_login_qrs(imported_login_qrs, result.store_key)
                return authenticated_page
            now = monotonic()
            if self.config.portal_browser_backend == "chrome_cdp":
                if last_cdp_refresh_at is None or now - last_cdp_refresh_at >= 5.0:
                    refreshed_page = self._confirmed_authenticated_page(
                        self._refresh_attached_authenticated_page(store),
                        store,
                    )
                    last_cdp_refresh_at = now
                    if refreshed_page is not None:
                        if refreshed_page is page:
                            self._log(result.store_key, "login confirmed after refreshing attached Chrome pages")
                        else:
                            self._log(
                                result.store_key,
                                "login confirmed after refreshing attached Chrome pages: "
                                f"{getattr(refreshed_page, 'url', '<unknown>')}",
                            )
                        self._cleanup_imported_login_qrs(imported_login_qrs, result.store_key)
                        return refreshed_page
                refreshed_login_page = self._refresh_attached_login_page(page, store)
                if refreshed_login_page is not None and refreshed_login_page is not page:
                    self._log(
                        result.store_key,
                        "adopting refreshed attached Chrome login page: "
                        f"{getattr(refreshed_login_page, 'url', '<unknown>')}",
                    )
                    page = refreshed_login_page
                    refreshed_qr = False
                    local_app_login_attempted = False
                    reauth_seen_at = None
                    login_challenge_url = None
            if self._is_public_landing_page(page):
                if not clicked_public_login:
                    self._log(result.store_key, "public landing detected; clicking login entry")
                    clicked_public_login = True
                self._open_login_from_public_landing(page)
                sleep(1)
                continue
            if self._page_requires_reauth(page):
                if (
                    not local_app_login_attempted
                    and self._is_login_page(page)
                    and self._local_app_login_enabled()
                ):
                    local_app_login_attempted = True
                    imported_qr = self._attempt_local_app_login(page, result, store)
                    if isinstance(imported_qr, ImportedPhotosQr):
                        imported_login_qrs.append(imported_qr)
                if reauth_seen_at is None:
                    reauth_seen_at = now
                    current_url = str(getattr(page, "url", ""))
                    if self._is_tpass_login_url(current_url):
                        login_challenge_url = current_url
                    self._log(
                        result.store_key,
                        f"login page detected; waiting {QR_REFRESH_GRACE_SECONDS:.0f}s before QR refresh",
                    )
                elif not refreshed_qr and now - reauth_seen_at >= QR_REFRESH_GRACE_SECONDS:
                    if (
                        self.config.portal_browser_backend == "chrome_cdp"
                        and store is not None
                    ):
                        should_restart, restart_reason = self._chrome_oauth_restart_readiness(
                            page,
                            store,
                            login_challenge_url,
                        )
                        if should_restart:
                            self._log(
                                result.store_key,
                                "login QR is explicitly expired on the original TPass challenge; "
                                "restarting OAuth login once for a fresh state and QR",
                            )
                            self._goto(self.config.portal_home_url_for_store(store), page)
                            refreshed_qr = True
                        else:
                            self._log(
                                result.store_key,
                                "suppressing automatic OAuth restart while waiting for login completion "
                                f"reason={restart_reason}",
                            )
                            reauth_seen_at = now
                    else:
                        self._log(
                            result.store_key,
                            "login page still pending; attempting QR refresh once",
                        )
                        self._try_refresh_login_qr(page)
                        refreshed_qr = True
                    if refreshed_qr and self._local_app_login_enabled():
                        local_app_login_attempted = False
                        self._log(
                            result.store_key,
                            "login challenge refreshed; scheduling one automatic rescan with the local app",
                        )
                if last_heartbeat_at is None or now - last_heartbeat_at >= LOGIN_WAIT_HEARTBEAT_SECONDS:
                    self._log(
                        result.store_key,
                        "still waiting for login completion "
                        f"current_url={getattr(page, 'url', '<unknown>')}",
                    )
                    last_heartbeat_at = now
            else:
                reauth_seen_at = None
            sleep(2)
        self._capture_artifact(page, result.artifacts_dir, "login-timeout")
        raise PortalRunnerError("Timed out waiting for tax portal login.")

    def _local_app_login_enabled(self) -> bool:
        return PortalMacLoginAutomator.is_enabled(self.config)

    def _attempt_local_app_login(
        self,
        page: object,
        result: PortalIssueResult,
        store: StoreConfig | None = None,
    ) -> ImportedPhotosQr | None:
        role_label = ROLE_LABELS.get(result.portal_company_role, "法定代表人")
        self._log(result.store_key, "starting local app scan-login automation")
        portal_area_name = (
            store.effective_portal_area_name() if store is not None else getattr(result, "portal_area_name", None)
        )
        portal_company_switch_name = (
            store.effective_portal_company_switch_name() if store is not None else None
        )
        automator = PortalMacLoginAutomator(
            self.config,
            result.store_key,
            role_label,
            self._log,
            portal_area_name=portal_area_name,
            portal_company_switch_name=portal_company_switch_name,
        )
        try:
            imported_qr = automator.automate(page, result.artifacts_dir)
        except PortalLocalLoginError as exc:
            self._log(
                result.store_key,
                f"local app scan-login automation failed; falling back to manual login wait error={exc}",
            )
            imported_qr = getattr(automator, "imported_qr", None)
            return imported_qr if isinstance(imported_qr, ImportedPhotosQr) else None
        self._log(
            result.store_key,
            "local app scan-login automation finished "
            f"qr_path={imported_qr.qr_path} photos_asset_id={imported_qr.asset_id}",
        )
        return imported_qr

    def _cleanup_imported_login_qrs(
        self,
        imported_qrs: list[ImportedPhotosQr],
        store_key: str,
    ) -> None:
        for imported_qr in imported_qrs:
            try:
                status = delete_imported_qr_from_photos(imported_qr)
            except PhotosQrCleanupError as exc:
                self._log(
                    store_key,
                    "warning: kept imported login QR in Photos because exact cleanup could not be verified "
                    f"asset_id={imported_qr.asset_id} filename={imported_qr.original_filename} error={exc}",
                )
                continue
            if status == "deleted":
                self._log(
                    store_key,
                    "deleted verified imported login QR from Photos after login confirmation "
                    f"asset_id={imported_qr.asset_id} filename={imported_qr.original_filename}",
                )
            else:
                self._log(
                    store_key,
                    "imported login QR was already absent from Photos after login confirmation "
                    f"asset_id={imported_qr.asset_id} filename={imported_qr.original_filename}",
                )

    def _ensure_company(self, home_page: object, store: StoreConfig, result: PortalIssueResult) -> object:
        verify_name = store.effective_portal_company_verify_name()
        target_area = store.effective_portal_area()
        current_area = self._portal_area_from_url(getattr(home_page, "url", ""))
        if current_area and current_area != target_area:
            target_area_name = store.effective_portal_area_name()
            self._log(
                store.store_key,
                f"portal area changed: {current_area} -> {target_area}; reopening {target_area_name} portal home",
            )
            home_page = self._navigate_with_reauth(
                home_page,
                self.config.portal_home_url_for_store(store),
                result,
                store=store,
                expected_text="我要办税",
                step_name=f"open {target_area_name} portal home",
            )
        self._wait_for_home_page_shell_ready(home_page, store.store_key)
        if self._page_contains(home_page, verify_name):
            self._log(store.store_key, f"company already active: {verify_name}")
            return home_page
        if self._wait_for_company_name(home_page, verify_name, timeout_seconds=5.0):
            self._log(store.store_key, f"company became visible after page settled: {verify_name}")
            return home_page
        result.step = "switch_company"
        self._update_store_step(
            store.store_key,
            result.step,
            "running",
            workbook_sha256=result.workbook_sha256,
            message=f"switching company to {verify_name}",
        )
        home_page = self._navigate_with_reauth(
            home_page,
            self.config.portal_identity_switch_url_for_store(store),
            result,
            store=store,
            expected_text="企业办税",
            step_name="switch company",
        )
        self._wait_for_switch_page_ready(home_page, store.store_key)
        if self._switch_page_shows_active_company(home_page, verify_name):
            self._log(store.store_key, f"company already active on switch page: {verify_name}")
            return self._navigate_with_reauth(
                home_page,
                self.config.portal_home_url_for_store(store),
                result,
                store=store,
                expected_text="我要办税",
                step_name=f"return home with active company {verify_name}",
            )
        selected_name = self._select_company_switch_row(home_page, store)
        self._wait_for_switch_confirmation_ready(home_page, store.store_key)
        self._click(
            self._visible_button_in_dialog(home_page, "确认是否切换", "确定"),
            page=home_page,
        )
        role_label = ROLE_LABELS.get(store.portal_company_role, "法定代表人")
        self._wait_for_role_selection_ready(home_page, store.store_key, role_label)
        try:
            self._click(
                self._visible_text_in_dialog(home_page, "身份类型选择", role_label),
                page=home_page,
            )
        except Exception:
            role_radio = self._visible_radio_in_dialog(home_page, "身份类型选择", role_label)
            try:
                self._check(role_radio, page=home_page)
            except Exception:
                self._click(role_radio, page=home_page, force=True)
        sleep(0.2)
        confirm_button = self._visible_button_in_dialog(home_page, "身份类型选择", "确定")
        self._wait_until(lambda: confirm_button.is_enabled(), timeout_seconds=10, message="identity role confirm enable")
        self._click(confirm_button, page=home_page)
        self._wait_for_company_switch_completion(home_page, store.store_key, verify_name)
        if not self._page_contains(home_page, verify_name):
                home_page = self._navigate_with_reauth(
                    home_page,
                    self.config.portal_home_url_for_store(store),
                    result,
                    store=store,
                    expected_text=verify_name,
                    step_name=f"switch company to {verify_name}",
                )
        self._log(store.store_key, f"company switch confirmed: {verify_name} via candidate={selected_name}")
        return home_page

    def _wait_for_batch_page(
        self,
        home_page: object,
        store: StoreConfig,
        result: PortalIssueResult,
        *,
        allow_security_reentry: bool = True,
    ) -> object:
        result.step = "open_batch_page"
        self._update_store_step(
            store.store_key,
            result.step,
            "running",
            workbook_sha256=result.workbook_sha256,
            message="opening batch issue page through official invoice business navigation",
        )
        context = getattr(home_page, "context", None)
        if context is None:
            raise PortalRunnerError("Authenticated portal home page has no browser context.")
        if self.config.portal_browser_backend == "chrome_cdp":
            closed_dppt_targets = self._close_raw_attached_dppt_targets(store)
            if closed_dppt_targets:
                self._log(
                    store.store_key,
                    f"closed stale DPPT tabs before official navigation count={closed_dppt_targets}",
                )
                sleep(0.5)
            reload_page = getattr(home_page, "reload", None)
            if callable(reload_page):
                self._log(
                    store.store_key,
                    "refreshing authenticated portal home page before official invoice business navigation",
                )
                reload_page(
                    wait_until="domcontentloaded",
                    timeout=self.config.portal_action_timeout_ms,
                )
                self._wait_for_home_page_ready(home_page, store)
                sleep(HOME_PAGE_BEFORE_BATCH_PAGE_DELAY_SECONDS)
        existing_pages = list(getattr(context, "pages", []))
        page_holder: dict[str, object] = {}
        for navigation_attempt in range(1, 3):
            page_holder.clear()
            self._attached_cdp_dppt_connections.pop(store.effective_portal_area(), None)
            raw_ready_since: dict[tuple[str, str], float] = {}
            raw_ready_logged: set[tuple[str, str]] = set()
            transport_state = {"switched": False}
            pre_click_target_signatures = {
                (str(target.get("id") or ""), str(target.get("url") or ""))
                for target in self._raw_attached_dppt_targets(store)
            }
            pre_click_candidates = list(getattr(context, "pages", []))
            for candidate in self._observed_attached_pages:
                if candidate not in pre_click_candidates:
                    pre_click_candidates.append(candidate)
            pre_click_invoice_urls = {
                str(getattr(candidate, "url", ""))
                for candidate in pre_click_candidates
                if self._is_invoice_business_page(candidate, store)
            }
            for candidate in pre_click_candidates:
                if (
                    self._is_invoice_business_handoff_page(candidate, store)
                    and self._invoice_business_handoff_status(candidate) >= 400
                ):
                    try:
                        candidate.close()
                    except Exception:
                        pass
            self._log(
                store.store_key,
                "opening invoice business from the authenticated portal home page via DOM click"
                f" attempt={navigation_attempt}",
            )
            self._dom_click_exact_text(home_page, "发票业务", class_hint="item-info")

            def find_invoice_business_page_or_failed_handoff() -> bool:
                candidates = (
                    []
                    if transport_state["switched"]
                    else list(getattr(context, "pages", []))
                )
                raw_gate_required = (
                    self.config.portal_browser_backend == "chrome_cdp"
                    and bool(self._attached_cdp_url)
                )
                if raw_gate_required:
                    raw_target = next(
                        (
                            target
                            for target in self._raw_attached_dppt_targets(store)
                            if (
                                str(target.get("id") or ""),
                                str(target.get("url") or ""),
                            )
                            not in pre_click_target_signatures
                        ),
                        None,
                    )
                    if raw_target is None:
                        return False
                    raw_url = str(raw_target.get("url") or "")
                    raw_is_handoff = "/szzhzz/spHandler" in raw_url
                    raw_is_invoice_business = "/invoice-business" in raw_url
                    if not raw_is_handoff and not raw_is_invoice_business:
                        return False
                    if raw_is_invoice_business:
                        raw_key = (str(raw_target.get("id") or ""), raw_url)
                        readiness: dict[str, object] = {}
                        if self._sync_playwright_factory is not None:
                            readiness = self._raw_cdp_invoice_business_readiness(raw_target)
                            failed_status = int(readiness.get("failedStatus") or 0)
                            if failed_status >= 400:
                                page_holder["failed_security_status"] = failed_status
                                page_holder["failed_security_path"] = str(
                                    readiness.get("failedPath") or "security initialization"
                                )
                                page_holder["failed_raw_target"] = raw_target
                                return True
                            if not readiness.get("ready"):
                                raw_ready_since.pop(raw_key, None)
                                return False
                        ready_since = raw_ready_since.setdefault(raw_key, monotonic())
                        if raw_key not in raw_ready_logged:
                            if readiness:
                                public_key_status = int(
                                    readiness.get("securityPublicKeyStatus") or 0
                                )
                                public_key_summary = (
                                    f"security_public_key_http={public_key_status}"
                                    if public_key_status
                                    else "security_public_key=preinitialized"
                                )
                                readiness_status = (
                                    "invoice_business_http="
                                    f"{int(readiness.get('navigationStatus') or 0)} "
                                    f"{public_key_summary} "
                                    "security_getYwqxbz_http="
                                    f"{int(readiness.get('securityPermissionStatus') or 0)}"
                                )
                            else:
                                readiness_status = "security_initialization=successful"
                            self._log(
                                store.store_key,
                                "invoice business readiness confirmed "
                                "page_rendered=true blue_invoice_clickable=true "
                                f"{readiness_status}; waiting 3s before continuing",
                            )
                            raw_ready_logged.add(raw_key)
                        elapsed = monotonic() - ready_since
                        if elapsed < DPPT_INVOICE_BUSINESS_POST_READY_DELAY_SECONDS:
                            return False
                        if (
                            self._sync_playwright_factory is not None
                            and not transport_state["switched"]
                        ):
                            self._stop_attached_playwright_transport()
                            raw_batch_target = self._raw_cdp_open_batch_page(
                                raw_target,
                                store.store_key,
                            )
                            self._raw_cdp_import_workbook(
                                raw_batch_target,
                                store.store_key,
                                result.workbook_path,
                                result.expected_count,
                            )
                            transport_state["switched"] = True
                            page_holder["page"] = _RawBatchSession(raw_batch_target)
                            page_holder["raw_batch_ready"] = True
                            page_holder["raw_import_ready"] = True
                            return True
                    if not transport_state["switched"]:
                        if self._sync_playwright_factory is not None:
                            self._restart_attached_cdp_transport(store)
                        transport_state["switched"] = True
                    refreshed_target = self._refresh_attached_invoice_business_target(store)
                    if refreshed_target is not None and refreshed_target not in candidates:
                        candidates.append(refreshed_target)
                for candidate in candidates:
                    if (
                        self._is_invoice_business_handoff_page(candidate, store)
                        and self._invoice_business_handoff_status(candidate) >= 400
                    ):
                        page_holder["failed_handoff"] = candidate
                        return True
                for candidate in candidates:
                    candidate_url = str(getattr(candidate, "url", ""))
                    if (
                        self._is_invoice_business_page(candidate, store)
                        and candidate_url not in pre_click_invoice_urls
                        and not self._page_is_closed(candidate)
                    ):
                        page_holder["page"] = candidate
                        return True
                return False

            self._wait_until(
                find_invoice_business_page_or_failed_handoff,
                timeout_seconds=max(
                    self.config.portal_action_timeout_ms / 1000,
                    DPPT_INVOICE_BUSINESS_POST_READY_DELAY_SECONDS + 15.0,
                ),
                message="open invoice business page from official portal entry",
                interval_seconds=0.2,
            )
            if "page" in page_holder:
                break
            failed_handoff = page_holder.get("failed_handoff")
            failed_security_status = int(page_holder.get("failed_security_status") or 0)
            status = failed_security_status or self._invoice_business_handoff_status(failed_handoff)
            failure_path = str(page_holder.get("failed_security_path") or "security handoff")
            if navigation_attempt >= 2:
                raise PortalRunnerError(
                    f"DPPT invoice business {failure_path} failed with HTTP {status} after retry."
                )
            self._log(
                store.store_key,
                f"DPPT invoice business {failure_path} returned HTTP {status}; "
                "retrying official portal entry",
            )
            if page_holder.get("failed_raw_target") is not None:
                self._close_raw_attached_dppt_targets(store)
            else:
                try:
                    failed_handoff.close()
                except Exception:
                    pass
            refreshed_home_page = self._refresh_attached_authenticated_page(store)
            if refreshed_home_page is not None:
                home_page = refreshed_home_page
                context = getattr(home_page, "context", context)

        page = page_holder["page"]
        if isinstance(page, _RawBatchSession):
            self._log(
                store.store_key,
                "batch issue page and workbook import ready through raw CDP official navigation",
            )
            return page
        page_source = "new page" if page not in existing_pages else "reused page"
        self._log(
            store.store_key,
            f"invoice business page detected from official portal entry via {page_source}",
        )
        page_context = getattr(page, "context", context)
        self._install_network_diag(page_context, page, store.store_key, "invoice-business")
        self._install_dialog_diag(page, store.store_key, "invoice-business")
        if page_holder.get("raw_batch_ready"):
            page.set_default_timeout(self.config.portal_action_timeout_ms)
            self._wait_for_batch_page_ready(page, store.store_key)
            if page_holder.get("raw_import_ready"):
                try:
                    setattr(page, "_tax_portal_raw_import_ready", True)
                except Exception:
                    pass
            self._log(
                store.store_key,
                "batch issue page loaded through raw CDP sensitive navigation and official entry",
            )
            return page
        for attempt in range(1, 4):
            try:
                page.set_default_timeout(self.config.portal_action_timeout_ms)
                self._wait_for_invoice_business_page_ready(page, store.store_key)
                break
            except Exception as exc:  # noqa: BLE001
                page_closed = self._page_is_closed(page) or "has been closed" in str(exc)
                if (
                    not page_closed
                    and allow_security_reentry
                    and not self._page_contains(page, "蓝字发票开具")
                ):
                    self._log_page_runtime_snapshot(page, store.store_key, "invoice-business")
                    self._log(
                        store.store_key,
                        "invoice business security initialization did not complete; "
                        "retrying from the official portal entry once without clearing site data",
                    )
                    try:
                        page.close()
                    except Exception:
                        pass
                    sleep(0.5)
                    return self._wait_for_batch_page(
                        home_page,
                        store,
                        result,
                        allow_security_reentry=False,
                    )
                if not page_closed or attempt >= 3:
                    self._log_page_runtime_snapshot(page, store.store_key, "invoice-business")
                    raise
                self._log(
                    store.store_key,
                    f"invoice business target closed during startup; refreshing CDP target attempt={attempt}",
                )
                sleep(0.2)
                refreshed_holder: dict[str, object] = {}

                def find_live_refreshed_page() -> bool:
                    refreshed_page = self._refresh_attached_invoice_business_page(store)
                    if refreshed_page is None or self._page_is_closed(refreshed_page):
                        return False
                    refreshed_holder["page"] = refreshed_page
                    return True

                self._wait_until(
                    find_live_refreshed_page,
                    timeout_seconds=5.0,
                    message="refresh live invoice business CDP target",
                    interval_seconds=0.2,
                )
                page = refreshed_holder["page"]

        self._log(store.store_key, "opening blue invoice makeout via DOM click")
        self._dom_click_exact_text(page, "蓝字发票开具", class_hint="app_name")
        if self.config.portal_browser_backend == "chrome_cdp":
            sleep(DPPT_SENSITIVE_NAVIGATION_SETTLE_SECONDS)
        self._wait_for_blue_invoice_page_ready(page, store.store_key)

        self._log(store.store_key, "opening batch issue page via DOM click")
        self._dom_click_exact_text(page, "批量开票")
        if self.config.portal_browser_backend == "chrome_cdp":
            sleep(DPPT_SENSITIVE_NAVIGATION_SETTLE_SECONDS)
        self._wait_for_batch_page_ready(page, store.store_key)
        self._log(store.store_key, "batch issue page loaded through official invoice business navigation")
        return page

    def _raw_attached_dppt_targets(self, store: StoreConfig) -> list[dict[str, object]]:
        if self.config.portal_browser_backend != "chrome_cdp":
            return []
        targets: list[dict[str, object]] = []
        for target in self._raw_attached_targets():
            if not isinstance(target, dict) or target.get("type") != "page":
                continue
            url = str(target.get("url") or "")
            match = PORTAL_DPPT_HOST_RE.match(url)
            if match is None or match.group(1) != store.effective_portal_area():
                continue
            if "/invoice-business" not in url and "/szzhzz/spHandler" not in url:
                continue
            targets.append(target)
        return targets

    def _raw_attached_targets(self) -> list[dict[str, object]]:
        cdp_url = self._attached_cdp_url
        if not cdp_url:
            return []
        try:
            opener = build_opener(ProxyHandler({}))
            with opener.open(f"{cdp_url.rstrip('/')}/json/list", timeout=2.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception:
            return []
        if not isinstance(payload, list):
            return []
        return [target for target in payload if isinstance(target, dict)]

    def _restart_attached_cdp_transport(self, store: StoreConfig | None = None) -> object:
        cdp_url = self._attached_cdp_url
        sync_playwright = self._sync_playwright_factory
        if not cdp_url or sync_playwright is None:
            raise PortalRunnerError("Chrome CDP transport cannot be restarted in the current runner.")
        active_playwright = self._attached_playwright
        if active_playwright is not None:
            try:
                active_playwright.stop()
            except Exception:
                pass
        playwright = sync_playwright().start()
        self._attached_playwright = playwright
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise PortalRunnerError(
                "Reconnected to Chrome via CDP, but no browser context was available."
            )
        context = browser.contexts[0]
        context.set_default_timeout(self.config.portal_action_timeout_ms)
        self._attached_cdp_browser_type = playwright.chromium
        self._attached_cdp_connections = [browser]
        self._attached_cdp_auth_connection = None
        self._attached_cdp_dppt_connections = {}
        if store is not None:
            self._attached_cdp_dppt_connections[store.effective_portal_area()] = browser
        self._observed_attached_pages = list(getattr(context, "pages", []))
        self._dialog_inert_context_ids = set()
        self._dialog_inert_page_ids = set()
        on_context_event = getattr(context, "on", None)
        if callable(on_context_event):
            on_context_event("page", self._record_attached_page)
        return browser

    def _stop_attached_playwright_transport(self) -> None:
        active_playwright = self._attached_playwright
        self._attached_playwright = None
        if active_playwright is not None:
            try:
                active_playwright.stop()
            except Exception:
                pass
        self._attached_cdp_browser_type = None
        self._attached_cdp_connections = []
        self._attached_cdp_auth_connection = None
        self._attached_cdp_dppt_connections = {}
        self._observed_attached_pages = []

    def _raw_cdp_invoice_business_readiness(
        self,
        target: dict[str, object],
    ) -> dict[str, object]:
        result = self._raw_cdp_evaluate(
            target,
            """
            (() => {
              const isRendered = (element) => {
                if (!element) return false;
                const style = getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.display !== 'none' &&
                  style.visibility !== 'hidden' &&
                  Number(style.opacity || 1) !== 0 &&
                  element.getClientRects().length > 0 &&
                  rect.width > 0 &&
                  rect.height > 0;
              };
              const blueLabels = [...document.querySelectorAll('*')].filter((element) =>
                element.children.length === 0 &&
                (element.textContent || '').trim() === '蓝字发票开具'
              );
              const blueLabel = blueLabels.find((element) =>
                String(element.className || '').includes('app_name') && isRendered(element)
              ) || blueLabels.find(isRendered) || null;
              const blueAction = blueLabel?.closest('button, a, [role="button"]') || blueLabel;
              const disabledContainer = blueAction?.closest(
                '[disabled], [aria-disabled="true"], .is-disabled, .t-is-disabled'
              );
              const blueInvoiceVisible = isRendered(blueLabel);
              const blueInvoiceClickable = blueInvoiceVisible &&
                isRendered(blueAction) &&
                !disabledContainer &&
                getComputedStyle(blueAction).pointerEvents !== 'none';

              const navigation = performance.getEntriesByType('navigation')[0] || null;
              const navigationStatus = Number(navigation?.responseStatus || 0);
              const securityPaths = [
                '/szzhzz/cssSecurity/v1/getPublicKey',
                '/szzhzz/swszzhCtr/v1/getYwqxbz',
              ];
              const resourceEntries = performance.getEntriesByType('resource');
              const securityRequests = securityPaths.map((path) => {
                const matches = resourceEntries.filter((entry) => {
                  try {
                    return new URL(entry.name).pathname === path;
                  } catch (_) {
                    return false;
                  }
                });
                const entry = matches[matches.length - 1] || null;
                return {
                  path,
                  seen: Boolean(entry),
                  status: Number(entry?.responseStatus || 0),
                };
              });
              const navigationSuccessful = navigationStatus >= 200 && navigationStatus < 400;
              const publicKeyRequest = securityRequests[0];
              const permissionRequest = securityRequests[1];
              const publicKeyHealthy = !publicKeyRequest.seen || (
                publicKeyRequest.status >= 200 && publicKeyRequest.status < 400
              );
              const permissionRequestSuccessful = permissionRequest.seen &&
                permissionRequest.status >= 200 && permissionRequest.status < 400;
              const securityInitialized = publicKeyHealthy && permissionRequestSuccessful;
              const pageRendered = location.pathname === '/invoice-business' &&
                document.readyState === 'complete' &&
                isRendered(document.querySelector('.page_app_list'));
              const failedSecurityRequest = securityRequests.find((request) =>
                request.status >= 400
              ) || null;
              const failedStatus = navigationStatus >= 400
                ? navigationStatus
                : Number(failedSecurityRequest?.status || 0);
              const failedPath = navigationStatus >= 400
                ? '/invoice-business navigation'
                : String(failedSecurityRequest?.path || '');
              return {
                url: location.href,
                readyState: document.readyState,
                pageRendered,
                blueInvoiceVisible,
                blueInvoiceClickable,
                navigationStatus,
                securityRequests,
                securityPublicKeyStatus: securityRequests[0].status,
                securityPermissionStatus: securityRequests[1].status,
                securityInitialized,
                failedStatus,
                failedPath,
                ready: pageRendered &&
                  blueInvoiceClickable &&
                  navigationSuccessful &&
                  securityInitialized,
              };
            })()
            """,
        )
        return result if isinstance(result, dict) else {}

    def _raw_cdp_open_batch_page(
        self,
        target: dict[str, object],
        store_key: str,
    ) -> dict[str, object]:
        target_id = str(target.get("id") or "")
        if not target_id:
            raise PortalRunnerError("DPPT target has no CDP target id.")
        self._log(store_key, "opening blue invoice makeout via raw CDP DOM click")
        self._raw_cdp_click_exact_text(target, "蓝字发票开具", class_hint="app_name")
        sleep(DPPT_SENSITIVE_NAVIGATION_SETTLE_SECONDS)
        blue_target = self._wait_for_raw_target_url(
            target_id,
            "/blue-invoice-makeout",
            message="open blue invoice makeout page through raw CDP",
        )
        self._log(store_key, "opening batch issue page via raw CDP DOM click")
        self._raw_cdp_click_exact_text(blue_target, "批量开票")
        sleep(DPPT_SENSITIVE_NAVIGATION_SETTLE_SECONDS)
        return self._wait_for_raw_target_url(
            target_id,
            "/blue-invoice-makeout/invoice-batch",
            message="open batch issue page through raw CDP",
        )

    def _raw_cdp_import_workbook(
        self,
        target: dict[str, object],
        store_key: str,
        workbook_path: Path,
        row_count: int,
    ) -> None:
        snapshot = self._raw_cdp_batch_import_snapshot(target)
        body_text = str(snapshot.get("bodyText") or "")
        if "共 0 条" not in body_text or "重新选择" in body_text:
            self._log(
                store_key,
                "raw CDP batch page contains unsubmitted imported rows; clearing them before import",
            )
            self._raw_cdp_click_exact_text(target, "清空导入")
            self._wait_until(
                lambda: "是否清空所有已导入内容" in self._raw_cdp_body_text(target),
                timeout_seconds=10,
                message="open raw CDP clear imported rows confirmation",
                interval_seconds=0.2,
            )
            self._raw_cdp_click_exact_text(target, "确定")
            self._wait_until(
                lambda: (lambda text: "共 0 条" in text and "重新选择" not in text)(
                    self._raw_cdp_body_text(target)
                ),
                timeout_seconds=15,
                message="clear raw CDP imported batch rows",
                interval_seconds=0.2,
            )
            self._log(store_key, "cleared unsubmitted imported rows through raw CDP")

        self._log(
            store_key,
            f"importing workbook through raw CDP {workbook_path.name} rows={row_count}",
        )
        self._raw_cdp_set_file_input_files(
            target,
            'input[type="file"]',
            [str(workbook_path.resolve())],
        )

        success_prefix = f"导入完成，共处理数据{row_count}条，处理成功{row_count}条"
        deadline = monotonic() + 30.0
        last_snapshot = snapshot
        while monotonic() < deadline:
            last_snapshot = self._raw_cdp_batch_import_snapshot(target)
            body_text = str(last_snapshot.get("bodyText") or "")
            if success_prefix in body_text:
                self._log(
                    store_key,
                    f"raw CDP workbook import verified rows={row_count}",
                )
                return
            failed_statuses = [
                int(item.get("status") or 0)
                for item in last_snapshot.get("importResources") or []
                if int(item.get("status") or 0) >= 400
            ]
            if failed_statuses:
                self._log(
                    store_key,
                    "raw CDP workbook import rejected "
                    f"snapshot={json.dumps(last_snapshot, ensure_ascii=False)}",
                )
                raise PortalRunnerError(
                    "DPPT rejected the raw CDP batch workbook import request "
                    f"with HTTP {failed_statuses[-1]}."
                )
            sleep(0.25)
        self._log(
            store_key,
            "raw CDP workbook import timed out "
            f"snapshot={json.dumps(last_snapshot, ensure_ascii=False)}",
        )
        raise PortalRunnerError("Timed out waiting for raw CDP workbook import result.")

    def _raw_cdp_batch_import_snapshot(
        self,
        target: dict[str, object],
    ) -> dict[str, object]:
        result = self._raw_cdp_evaluate(
            target,
            """
            (() => {
              const bodyText = (document.body && document.body.innerText || '')
                .replace(/\\s+/g, ' ')
                .trim();
              const importResources = performance.getEntriesByType('resource')
                .filter((entry) => String(entry.name || '').includes('/kpfw/excel/v1/importPlkj'))
                .map((entry) => {
                  const url = new URL(entry.name);
                  return {
                    path: url.pathname,
                    queryKeys: [...url.searchParams.keys()].sort(),
                    status: Number(entry.responseStatus || 0),
                    durationMs: Math.round(Number(entry.duration || 0)),
                  };
                });
              return {
                url: location.href,
                title: document.title,
                bodyText: bodyText.slice(0, 800),
                cookieNames: document.cookie.split(';').map((part) => part.split('=')[0].trim()).filter(Boolean).sort(),
                localStorageKeys: Object.keys(localStorage).sort(),
                sessionStorageKeys: Object.keys(sessionStorage).sort(),
                importResources,
              };
            })()
            """,
        )
        return result if isinstance(result, dict) else {}

    def _raw_cdp_select_all_batch_rows(
        self,
        target: dict[str, object],
        store_key: str,
        expected_count: int,
    ) -> None:
        self._log(
            store_key,
            "raw CDP import is ready; waiting "
            f"{RAW_CDP_SUBMIT_ACTION_DELAY_SECONDS:.0f}s before selecting invoices",
        )
        sleep(RAW_CDP_SUBMIT_ACTION_DELAY_SECONDS)
        self._wait_until(
            lambda: int(
                self._raw_cdp_evaluate(
                    target,
                    """
                    (() => [...document.querySelectorAll(
                      'td.t-table__cell-check label.t-checkbox'
                    )].filter((element) => {
                      const rect = element.getBoundingClientRect();
                      return rect.width > 0 && rect.height > 0;
                    }).length)()
                    """,
                )
                or 0
            )
            == expected_count,
            timeout_seconds=15,
            message="render all imported batch rows for raw CDP selection",
            interval_seconds=0.2,
        )
        selection = self._raw_cdp_evaluate(
            target,
            """
            (() => {
              const headerLabels = [...document.querySelectorAll(
                'th.t-table__cell-check label.t-checkbox, thead label.t-checkbox'
              )].filter((element) => {
                const rect = element.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
              });
              const headerLabel = headerLabels[0] || null;
              const checkbox = headerLabel?.querySelector('input[type="checkbox"]') || null;
              if (headerLabel && checkbox && !checkbox.checked) headerLabel.click();
              return {
                found: Boolean(headerLabel && checkbox),
                checked: Boolean(checkbox?.checked),
                count: headerLabels.length,
              };
            })()
            """,
        )
        if not isinstance(selection, dict) or not selection.get("found"):
            raise PortalRunnerError("Raw CDP could not find the visible batch table select-all checkbox.")
        self._wait_until(
            lambda: int(
                self._raw_cdp_evaluate(
                    target,
                    """
                    (() => [...document.querySelectorAll(
                      'td.t-table__cell-check label.t-checkbox'
                    )].filter((element) => {
                      const rect = element.getBoundingClientRect();
                      const checkbox = element.querySelector('input[type="checkbox"]');
                      return rect.width > 0 && rect.height > 0 && checkbox?.checked;
                    }).length)()
                    """,
                )
                or 0
            )
            == expected_count,
            timeout_seconds=10,
            message="select all imported rows through raw CDP",
            interval_seconds=0.2,
        )
        self._log(store_key, f"raw CDP select-all verified rows={expected_count}")

    def _raw_cdp_submit_batch(
        self,
        target: dict[str, object],
        store_key: str,
        result: PortalIssueResult,
    ) -> tuple[list[PortalIssueDetail], int, int, str]:
        self._log(
            store_key,
            "raw CDP select-all is ready; waiting "
            f"{RAW_CDP_SUBMIT_ACTION_DELAY_SECONDS:.0f}s before batch issue",
        )
        sleep(RAW_CDP_SUBMIT_ACTION_DELAY_SECONDS)
        self._raw_cdp_click_exact_text(target, "批量开具")
        self._wait_until(
            lambda: "本次勾选批量开具发票" in self._raw_cdp_body_text(target),
            timeout_seconds=10,
            message="open raw CDP submit confirmation",
            interval_seconds=0.2,
        )
        sleep(RAW_CDP_SUBMIT_ACTION_DELAY_SECONDS)
        self._raw_cdp_click_exact_text(target, "确定")
        result.submitted_count = result.expected_count
        self._log(
            store_key,
            f"raw CDP submit confirmed rows={result.submitted_count}; waiting for result",
        )

        result_modal_text = ""
        deadline = monotonic() + 90.0
        while monotonic() < deadline:
            result_modal_text = self._raw_cdp_body_text(target)
            if "批量开具结果" in result_modal_text:
                break
            sleep(0.25)
        else:
            raise PortalRunnerError("Timed out waiting for raw CDP portal issue result.")

        compact_text = re.sub(r"\s+", "", result_modal_text)
        match = re.search(r"开具成功发票(\d+)份.*?开具失败发票(\d+)份", compact_text)
        if not match:
            raise PortalRunnerError("Could not parse raw CDP portal issue result summary.")
        success_count = int(match.group(1))
        failure_count = int(match.group(2))
        details = self._parse_result_modal_details(result_modal_text)
        success_count, failure_count = self._reconcile_result_counts_from_details(
            details,
            success_count,
            failure_count,
            expected_count=result.expected_count,
        )
        self._raw_cdp_click_dialog_button(target, "批量开具结果", "关闭")
        self._wait_until(
            lambda: "批量开具结果" not in self._raw_cdp_body_text(target),
            timeout_seconds=10,
            message="close raw CDP portal issue result dialog",
            interval_seconds=0.2,
        )
        self._log(store_key, "raw CDP portal issue result confirmed and closed")
        self._close_raw_cdp_target(target, store_key)
        return details, success_count, failure_count, result_modal_text

    def _raw_cdp_body_text(self, target: dict[str, object]) -> str:
        value = self._raw_cdp_evaluate(
            target,
            """
            (() => (document.body && document.body.innerText || '').slice(0, 50000))()
            """,
        )
        return str(value or "")

    def _capture_raw_cdp_artifact(
        self,
        target: dict[str, object],
        artifacts_dir: Path | None,
        name: str,
    ) -> None:
        if artifacts_dir is None:
            return
        try:
            payload = self._raw_cdp_command(
                target,
                "Page.captureScreenshot",
                {"format": "png", "captureBeyondViewport": True},
            )
            encoded = str(payload.get("data") or "")
            if not encoded:
                return
            artifact_path = artifacts_dir / f"{name}.png"
            ensure_parent_dir(artifact_path)
            artifact_path.write_bytes(base64.b64decode(encoded))
        except Exception:
            pass

    @staticmethod
    def _raw_cdp_set_file_input_files(
        target: dict[str, object],
        selector: str,
        files: list[str],
    ) -> None:
        websocket_url = str(target.get("webSocketDebuggerUrl") or "")
        if not websocket_url:
            raise PortalRunnerError("DPPT target has no WebSocket debugger URL.")
        try:
            from websocket import create_connection
        except ModuleNotFoundError as exc:
            raise PortalRunnerError(
                "websocket-client is required for raw Chrome CDP navigation."
            ) from exc
        connection = create_connection(
            websocket_url,
            timeout=5,
            suppress_origin=True,
        )
        request_id = 0

        def send_command(method: str, params: dict[str, object]) -> dict[str, object]:
            nonlocal request_id
            request_id += 1
            current_id = request_id
            connection.send(
                json.dumps(
                    {
                        "id": current_id,
                        "method": method,
                        "params": params,
                    }
                )
            )
            while True:
                payload = json.loads(connection.recv())
                if payload.get("id") != current_id:
                    continue
                if payload.get("error"):
                    raise PortalRunnerError(
                        f"Raw Chrome CDP command failed method={method}: {payload['error']}"
                    )
                result = payload.get("result") or {}
                return result if isinstance(result, dict) else {}

        try:
            root = send_command(
                "DOM.getDocument",
                {"depth": -1, "pierce": True},
            ).get("root") or {}
            node_id = int(root.get("nodeId") or 0)
            if not node_id:
                raise PortalRunnerError("Raw Chrome CDP could not read the batch page DOM.")
            query_result = send_command(
                "DOM.querySelector",
                {"nodeId": node_id, "selector": selector},
            )
            file_input_node_id = int(query_result.get("nodeId") or 0)
            if not file_input_node_id:
                raise PortalRunnerError(
                    "Portal batch page does not expose a workbook file input to raw Chrome CDP."
                )
            send_command(
                "DOM.setFileInputFiles",
                {"files": files, "nodeId": file_input_node_id},
            )
        finally:
            connection.close()

    def _raw_cdp_click_exact_text(
        self,
        target: dict[str, object],
        text: str,
        *,
        class_hint: str = "",
    ) -> None:
        argument = json.dumps(
            {"text": text, "classHint": class_hint},
            ensure_ascii=False,
        )
        result = self._raw_cdp_evaluate(
            target,
            f"""
            (() => {{
              const {{text, classHint}} = {argument};
              const targets = [...document.querySelectorAll('*')].filter((element) =>
                element.children.length === 0 &&
                (element.textContent || '').trim() === text &&
                getComputedStyle(element).display !== 'none' &&
                getComputedStyle(element).visibility !== 'hidden' &&
                element.getClientRects().length > 0 &&
                element.getBoundingClientRect().width > 0 &&
                element.getBoundingClientRect().height > 0
              );
              const target = targets.find((element) =>
                classHint && String(element.className || '').includes(classHint)
              ) || targets[0];
              if (!target) return {{clicked: false, count: targets.length}};
              target.click();
              return {{
                clicked: true,
                count: targets.length,
                tag: target.tagName,
                className: String(target.className || ''),
              }};
            }})()
            """,
        )
        if not isinstance(result, dict) or not result.get("clicked"):
            raise PortalRunnerError(f"Could not raw-CDP click visible exact text: {text}")

    def _raw_cdp_click_dialog_button(
        self,
        target: dict[str, object],
        dialog_text: str,
        button_text: str,
    ) -> None:
        argument = json.dumps(
            {"dialogText": dialog_text, "buttonText": button_text},
            ensure_ascii=False,
        )
        expression = f"""
            (() => {{
              const {{dialogText, buttonText}} = {argument};
              const isRendered = (element) => {{
                if (!element) return false;
                const style = getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.display !== 'none' &&
                  style.visibility !== 'hidden' &&
                  Number(style.opacity || 1) !== 0 &&
                  element.getClientRects().length > 0 &&
                  rect.width > 0 && rect.height > 0;
              }};
              const dialogs = [...document.querySelectorAll(
                '[role="dialog"], .t-dialog, .t-dialog__ctx, .el-dialog__wrapper, .el-message-box'
              )].filter((element) =>
                isRendered(element) && (element.innerText || '').includes(dialogText)
              ).sort((left, right) => {{
                const leftRect = left.getBoundingClientRect();
                const rightRect = right.getBoundingClientRect();
                return leftRect.width * leftRect.height - rightRect.width * rightRect.height;
              }});
              const dialog = dialogs[0] || null;
              if (!dialog) return {{clicked: false, reason: 'dialog-not-found'}};
              const labels = [...dialog.querySelectorAll('button, [role="button"], a, span')]
                .filter((element) =>
                  isRendered(element) &&
                  (element.textContent || '').trim() === buttonText
                );
              const label = labels.find((element) =>
                element.matches('button, [role="button"], a')
              ) || labels[0] || null;
              const button = label?.closest('button, [role="button"], a') || label;
              const disabledContainer = button?.closest(
                '[disabled], [aria-disabled="true"], .is-disabled, .t-is-disabled'
              );
              if (!button || disabledContainer) {{
                return {{clicked: false, reason: 'button-not-ready'}};
              }}
              button.click();
              return {{clicked: true, tag: button.tagName, className: String(button.className || '')}};
            }})()
            """
        last_result: object = None

        def click_ready_dialog_button() -> bool:
            nonlocal last_result
            last_result = self._raw_cdp_evaluate(target, expression)
            return isinstance(last_result, dict) and bool(last_result.get("clicked"))

        self._wait_until(
            click_ready_dialog_button,
            timeout_seconds=10,
            message=f"enable raw CDP dialog button {dialog_text}/{button_text}",
            interval_seconds=0.2,
        )

    @staticmethod
    def _raw_cdp_evaluate(target: dict[str, object], expression: str) -> object:
        result = TaxPortalRunner._raw_cdp_command(
            target,
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        evaluation = result.get("result") or {}
        if evaluation.get("subtype") == "error":
            raise PortalRunnerError(
                f"Raw Chrome CDP JavaScript failed: {evaluation.get('description') or evaluation}"
            )
        return evaluation.get("value")

    @staticmethod
    def _raw_cdp_command(
        target: dict[str, object],
        method: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        websocket_url = str(target.get("webSocketDebuggerUrl") or "")
        if not websocket_url:
            raise PortalRunnerError("DPPT target has no WebSocket debugger URL.")
        try:
            from websocket import create_connection
        except ModuleNotFoundError as exc:
            raise PortalRunnerError(
                "websocket-client is required for raw Chrome CDP navigation."
            ) from exc
        connection = create_connection(
            websocket_url,
            timeout=5,
            suppress_origin=True,
        )
        try:
            request_id = 1
            connection.send(
                json.dumps(
                    {
                        "id": request_id,
                        "method": method,
                        "params": params or {},
                    }
                )
            )
            while True:
                payload = json.loads(connection.recv())
                if payload.get("id") != request_id:
                    continue
                if payload.get("error"):
                    raise PortalRunnerError(
                        f"Raw Chrome CDP command failed method={method}: {payload['error']}"
                    )
                result = payload.get("result") or {}
                return result if isinstance(result, dict) else {}
        finally:
            connection.close()

    def _wait_for_raw_target_url(
        self,
        target_id: str,
        url_fragment: str,
        *,
        message: str,
    ) -> dict[str, object]:
        holder: dict[str, dict[str, object]] = {}

        def find_target() -> bool:
            target = next(
                (
                    item
                    for item in self._raw_attached_targets()
                    if str(item.get("id") or "") == target_id
                    and url_fragment in str(item.get("url") or "")
                ),
                None,
            )
            if target is None:
                return False
            holder["target"] = target
            return True

        self._wait_until(
            find_target,
            timeout_seconds=max(self.config.portal_action_timeout_ms / 1000, 10.0),
            message=message,
            interval_seconds=0.2,
        )
        return holder["target"]

    def _batch_page_from_attached_browser(
        self,
        browser: object,
        store: StoreConfig,
    ) -> object | None:
        try:
            for context in list(getattr(browser, "contexts", [])):
                context.set_default_timeout(self.config.portal_action_timeout_ms)
                for page in list(getattr(context, "pages", [])):
                    url = str(getattr(page, "url", ""))
                    match = PORTAL_DPPT_HOST_RE.match(url)
                    if (
                        match is not None
                        and match.group(1) == store.effective_portal_area()
                        and "/blue-invoice-makeout/invoice-batch" in url
                    ):
                        return page
        except Exception:
            return None
        return None

    def _close_raw_attached_dppt_targets(self, store: StoreConfig) -> int:
        cdp_url = self._attached_cdp_url
        if not cdp_url:
            return 0
        targets = []
        for target in self._raw_attached_targets():
            if target.get("type") != "page":
                continue
            url = str(target.get("url") or "")
            match = PORTAL_DPPT_HOST_RE.match(url)
            if match is not None and match.group(1) == store.effective_portal_area():
                targets.append(target)
        closed = 0
        opener = build_opener(ProxyHandler({}))
        for target in targets:
            target_id = str(target.get("id") or "")
            if not target_id:
                continue
            try:
                with opener.open(
                    f"{cdp_url.rstrip('/')}/json/close/{target_id}",
                    timeout=2.0,
                ):
                    closed += 1
            except Exception:
                continue
        return closed

    def _close_raw_cdp_target(
        self,
        target: dict[str, object],
        store_key: str,
    ) -> None:
        cdp_url = self._attached_cdp_url
        target_id = str(target.get("id") or "")
        if not cdp_url or not target_id:
            raise PortalRunnerError("Raw CDP batch target cannot be closed without its CDP URL and target id.")
        opener = build_opener(ProxyHandler({}))
        try:
            with opener.open(
                f"{cdp_url.rstrip('/')}/json/close/{target_id}",
                timeout=2.0,
            ):
                pass
        except Exception as exc:
            raise PortalRunnerError("Could not request closure of the raw CDP batch target.") from exc

        def target_is_closed() -> bool:
            try:
                with opener.open(f"{cdp_url.rstrip('/')}/json/list", timeout=2.0) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except Exception:
                return False
            if not isinstance(payload, list):
                return False
            return all(
                str(item.get("id") or "") != target_id
                for item in payload
                if isinstance(item, dict)
            )

        self._wait_until(
            target_is_closed,
            timeout_seconds=10,
            message="close raw CDP batch issue tab",
            interval_seconds=0.2,
        )
        self._log(store_key, "raw CDP batch issue tab closed")

    def _reset_attached_dppt_site_data(self, home_page: object, store: StoreConfig) -> bool:
        if self.config.portal_browser_backend != "chrome_cdp":
            return False
        context = getattr(home_page, "context", None)
        new_cdp_session = getattr(context, "new_cdp_session", None)
        if not callable(new_cdp_session):
            return False
        session = None
        try:
            session = new_cdp_session(home_page)
            session.send(
                "Storage.clearDataForOrigin",
                {
                    "origin": (
                        f"https://dppt.{store.effective_portal_area()}.chinatax.gov.cn:8443"
                    ),
                    "storageTypes": "all",
                },
            )
            pages = list(getattr(context, "pages", []))
            for browser in self._attached_cdp_connections:
                for attached_context in list(getattr(browser, "contexts", [])):
                    for candidate in list(getattr(attached_context, "pages", [])):
                        if candidate not in pages:
                            pages.append(candidate)
            for candidate in pages:
                url = str(getattr(candidate, "url", ""))
                match = PORTAL_DPPT_HOST_RE.match(url)
                if match is None or match.group(1) != store.effective_portal_area():
                    continue
                try:
                    candidate.close()
                except Exception:
                    pass
            self._attached_cdp_dppt_connections.pop(store.effective_portal_area(), None)
            return True
        except Exception:
            return False
        finally:
            detach = getattr(session, "detach", None)
            if callable(detach):
                try:
                    detach()
                except Exception:
                    pass

    @staticmethod
    def _is_invoice_business_page(page: object, store: StoreConfig) -> bool:
        url = str(getattr(page, "url", ""))
        match = PORTAL_DPPT_HOST_RE.match(url)
        return (
            match is not None
            and match.group(1) == store.effective_portal_area()
            and "/invoice-business" in url
        )

    @staticmethod
    def _is_invoice_business_handoff_page(page: object, store: StoreConfig) -> bool:
        url = str(getattr(page, "url", ""))
        match = PORTAL_DPPT_HOST_RE.match(url)
        return (
            match is not None
            and match.group(1) == store.effective_portal_area()
            and "/szzhzz/spHandler" in url
            and "cdlj=invoice-business" in url
        )

    @staticmethod
    def _invoice_business_handoff_status(page: object | None) -> int:
        if page is None:
            return 0
        try:
            status = page.evaluate(
                """
                () => {
                  const entry = performance.getEntriesByType('navigation')[0];
                  return Number(entry && entry.responseStatus || 0);
                }
                """
            )
            return int(status or 0)
        except Exception:
            return 0

    def _refresh_attached_invoice_business_target(self, store: StoreConfig) -> object | None:
        targets = self._refresh_attached_dppt_pages(store)
        for candidate in reversed(targets):
            if self._is_invoice_business_handoff_page(candidate, store):
                return candidate
        invoice_pages = [candidate for candidate in targets if self._is_invoice_business_page(candidate, store)]
        if invoice_pages:
            return max(
                invoice_pages,
                key=lambda candidate: str(getattr(candidate, "url", "")),
            )
        return None

    def _refresh_attached_invoice_business_page(self, store: StoreConfig) -> object | None:
        target = self._refresh_attached_invoice_business_target(store)
        if target is not None and self._is_invoice_business_page(target, store):
            return target
        return None

    def _refresh_attached_dppt_pages(self, store: StoreConfig) -> list[object]:
        if self.config.portal_browser_backend != "chrome_cdp":
            return []
        browser_type = self._attached_cdp_browser_type
        cdp_url = self._attached_cdp_url
        if browser_type is None or not cdp_url:
            return []
        area = store.effective_portal_area()
        browser = self._attached_cdp_dppt_connections.get(area)
        if browser is None:
            self._make_attached_connections_dialog_inert()
            try:
                browser = browser_type.connect_over_cdp(cdp_url)
            except Exception:
                return []
            self._attached_cdp_connections.append(browser)
            self._attached_cdp_dppt_connections[area] = browser
        return self._dppt_pages_from_attached_browser(browser, store)

    def _dppt_pages_from_attached_browser(self, browser: object, store: StoreConfig) -> list[object]:
        try:
            if not browser.contexts:
                return []
            context = browser.contexts[0]
            context.set_default_timeout(self.config.portal_action_timeout_ms)
            return [
                candidate
                for candidate in list(getattr(context, "pages", []))
                if (
                    not self._page_is_closed(candidate)
                    and (
                        self._is_invoice_business_page(candidate, store)
                        or self._is_invoice_business_handoff_page(candidate, store)
                    )
                )
            ]
        except Exception:
            return []

    @staticmethod
    def _page_is_closed(page: object) -> bool:
        is_closed = getattr(page, "is_closed", None)
        if not callable(is_closed):
            return False
        try:
            return bool(is_closed())
        except Exception:
            return True

    def _wait_for_page_stable(
        self,
        page: object,
        store_key: str,
        page_name: str,
        *,
        required_texts: tuple[str, ...] = (),
        content_predicate: Callable[[], bool] | None = None,
        content_message: str | None = None,
        wait_for_load_states: bool = True,
        timeout_ms_override: int | None = None,
        networkidle_timeout_ms: int | None = None,
        allow_networkidle_timeout: bool = False,
    ) -> None:
        timeout_ms = (
            timeout_ms_override
            if timeout_ms_override is not None
            else self.config.portal_action_timeout_ms
        )
        effective_networkidle_timeout_ms = (
            networkidle_timeout_ms
            if networkidle_timeout_ms is not None
            else timeout_ms
        )
        self._log(store_key, f"waiting for {page_name} elements and requests to finish loading")
        if wait_for_load_states:
            for state in ("domcontentloaded", "load"):
                try:
                    page.wait_for_load_state(state, timeout=timeout_ms)
                except Exception as exc:  # noqa: BLE001
                    raise PortalRunnerError(
                        f"Timed out waiting for {page_name} load state={state}: {exc}"
                    ) from exc
        for text in required_texts:
            self._wait_for_text(page, text, timeout_ms=timeout_ms)
        try:
            page.wait_for_load_state("networkidle", timeout=effective_networkidle_timeout_ms)
        except Exception as exc:  # noqa: BLE001
            if not allow_networkidle_timeout:
                raise PortalRunnerError(
                    f"Timed out waiting for {page_name} network requests to finish: {exc}"
                ) from exc
            self._log(
                store_key,
                f"{page_name} network requests did not go idle within "
                f"{effective_networkidle_timeout_ms}ms; continuing because required content is visible",
            )
        predicate = content_predicate
        if predicate is None and required_texts:
            predicate = lambda: all(text in self._body_text(page) for text in required_texts)
        if predicate is not None:
            self._wait_until(
                predicate,
                timeout_seconds=max(timeout_ms / 1000, 5.0),
                message=content_message or f"render {page_name} content",
                interval_seconds=0.2,
            )

    def _wait_for_home_page_shell_ready(self, page: object, store_key: str) -> None:
        self._wait_for_page_stable(
            page,
            store_key,
            "authenticated home page shell",
            required_texts=("我要办税",),
            timeout_ms_override=max(self.config.portal_action_timeout_ms, HOME_PAGE_SHELL_MIN_TIMEOUT_MS),
            networkidle_timeout_ms=HOME_PAGE_NETWORKIDLE_GRACE_MS,
            allow_networkidle_timeout=True,
        )

    def _wait_for_home_page_ready(self, page: object, store: StoreConfig) -> None:
        verify_name = store.effective_portal_company_verify_name()
        self._wait_for_page_stable(
            page,
            store.store_key,
            "authenticated home page",
            required_texts=("我要办税", verify_name),
            networkidle_timeout_ms=min(self.config.portal_action_timeout_ms, HOME_PAGE_NETWORKIDLE_GRACE_MS),
            allow_networkidle_timeout=True,
        )
        self._log(store.store_key, "authenticated home page is stable")

    def _wait_before_open_batch_page(self, store_key: str) -> None:
        self._log(
            store_key,
            f"authenticated home page is stable; waiting {HOME_PAGE_BEFORE_BATCH_PAGE_DELAY_SECONDS:.0f}s before opening batch issue page",
        )
        sleep(HOME_PAGE_BEFORE_BATCH_PAGE_DELAY_SECONDS)

    def _wait_for_switch_page_ready(self, page: object, store_key: str) -> None:
        self._wait_for_page_stable(
            page,
            store_key,
            "identity switch page",
            required_texts=("企业办税",),
        )

    def _wait_for_company_name(self, page: object, verify_name: str, *, timeout_seconds: float) -> bool:
        try:
            self._wait_until(
                lambda: self._page_contains(page, verify_name),
                timeout_seconds=timeout_seconds,
                message=f"show active company {verify_name}",
                interval_seconds=0.2,
            )
        except PortalRunnerError:
            return False
        return True

    def _switch_page_shows_active_company(self, page: object, verify_name: str) -> bool:
        try:
            body_text = re.sub(r"\s+", " ", self._body_text(page))
        except Exception:
            return False
        pattern = rf"纳税人名称[:：]?\s*{re.escape(verify_name)}"
        return re.search(pattern, body_text) is not None

    def _wait_for_batch_page_ready(self, page: object, store_key: str) -> None:
        self._wait_for_page_stable(
            page,
            store_key,
            "batch issue page",
            required_texts=("批量开票",),
            content_predicate=lambda: (
                "/blue-invoice-makeout/invoice-batch" in str(getattr(page, "url", ""))
                and self._page_contains(page, "选择文件")
            ),
            content_message="render batch issue page with file import control",
            networkidle_timeout_ms=min(self.config.portal_action_timeout_ms, HOME_PAGE_NETWORKIDLE_GRACE_MS),
            allow_networkidle_timeout=True,
        )

    def _wait_for_invoice_business_page_ready(self, page: object, store_key: str) -> None:
        self._wait_for_page_stable(
            page,
            store_key,
            "invoice business page",
            required_texts=("发票业务", "蓝字发票开具", "可用发票额度"),
            content_predicate=lambda: "/invoice-business" in str(getattr(page, "url", "")),
            content_message="render invoice business page after DPPT security initialization",
            networkidle_timeout_ms=min(self.config.portal_action_timeout_ms, HOME_PAGE_NETWORKIDLE_GRACE_MS),
            allow_networkidle_timeout=True,
        )

    def _wait_for_blue_invoice_page_ready(self, page: object, store_key: str) -> None:
        self._wait_for_page_stable(
            page,
            store_key,
            "blue invoice makeout page",
            required_texts=("蓝字发票开具", "立即开票", "批量开票"),
            content_predicate=lambda: (
                "/blue-invoice-makeout" in str(getattr(page, "url", ""))
                and "/invoice-batch" not in str(getattr(page, "url", ""))
            ),
            content_message="render blue invoice makeout page",
            networkidle_timeout_ms=min(self.config.portal_action_timeout_ms, HOME_PAGE_NETWORKIDLE_GRACE_MS),
            allow_networkidle_timeout=True,
        )

    def _wait_for_switch_query_results_ready(self, page: object, store_key: str) -> None:
        self._wait_for_page_stable(
            page,
            store_key,
            "identity switch query results",
            required_texts=("企业办税",),
            wait_for_load_states=False,
        )

    def _wait_for_batch_import_ready(
        self,
        page: object,
        store_key: str,
        workbook_name: str,
        success_prefix: str,
    ) -> None:
        self._wait_for_page_stable(
            page,
            store_key,
            "batch issue import result",
            required_texts=(workbook_name, success_prefix),
            wait_for_load_states=False,
        )

    def _wait_for_submit_confirmation_ready(
        self,
        page: object,
        store_key: str,
        row_count: int,
        total_amount_including_tax: Decimal,
    ) -> None:
        self._wait_for_page_stable(
            page,
            store_key,
            "submit confirmation dialog",
            required_texts=("本次勾选批量开具发票",),
            wait_for_load_states=False,
        )
        self._wait_until(
            lambda: self._visible_button_in_dialog(page, "本次勾选批量开具发票", "确定").is_visible()
            and self._visible_button_in_dialog(page, "本次勾选批量开具发票", "确定").is_enabled(),
            timeout_seconds=10,
            message="show submit confirmation button",
            interval_seconds=0.2,
        )

    def _wait_for_result_modal_ready(self, page: object, store_key: str) -> None:
        self._wait_for_page_stable(
            page,
            store_key,
            "portal issue result dialog",
            required_texts=("批量开具结果",),
            wait_for_load_states=False,
        )

    def _wait_for_switch_confirmation_ready(self, page: object, store_key: str) -> None:
        self._wait_for_page_stable(
            page,
            store_key,
            "switch confirmation dialog",
            required_texts=("确认是否切换",),
            wait_for_load_states=False,
        )
        self._wait_until(
            lambda: self._visible_button_in_dialog(page, "确认是否切换", "确定").is_visible()
            and self._visible_button_in_dialog(page, "确认是否切换", "确定").is_enabled(),
            timeout_seconds=10,
            message="show switch confirmation button",
            interval_seconds=0.2,
        )

    def _wait_for_role_selection_ready(self, page: object, store_key: str, role_label: str) -> None:
        self._wait_for_page_stable(
            page,
            store_key,
            "role selection dialog",
            required_texts=("身份类型选择",),
            wait_for_load_states=False,
        )
        self._wait_until(
            lambda: (
                self._visible_button_in_dialog(page, "身份类型选择", "确定").is_visible()
                and (
                    self._visible_text_in_dialog(page, "身份类型选择", role_label).is_visible()
                    or self._visible_radio_in_dialog(page, "身份类型选择", role_label).is_visible()
                )
            ),
            timeout_seconds=10,
            message=f"show role selection controls for {role_label}",
            interval_seconds=0.2,
        )

    def _wait_for_company_switch_completion(self, page: object, store_key: str, verify_name: str) -> None:
        self._log(store_key, f"waiting for company switch to complete for {verify_name}")
        self._wait_until(
            lambda: (
                self._page_contains(page, verify_name)
                and self._page_contains(page, "我要办税")
                and not self._page_contains(page, "身份类型选择")
            )
            or self._page_contains(page, "会话失效，请重新登录"),
            timeout_seconds=max(self.config.portal_action_timeout_ms / 1000, 10.0),
            message=f"complete company switch to {verify_name}",
            interval_seconds=0.2,
        )

    def _ensure_batch_page_clean(self, page: object, store_key: str) -> None:
        body_text = self._body_text(page)
        if "共 0 条" in body_text and "重新选择" not in body_text:
            return
        self._log(store_key, "batch issue page is not clean; attempting to clear imported rows before continuing")
        clear_button = page.get_by_role("button", name="清空导入")
        try:
            self._click(clear_button, page=page, timeout=5000)
        except Exception as exc:  # noqa: BLE001
            raise PortalRunnerError("Portal batch page is not clean and could not be cleared automatically.") from exc
        self._wait_until(
            lambda: "是否清空所有已导入内容？" in self._body_text(page),
            timeout_seconds=10,
            message="open clear imported rows confirmation",
            interval_seconds=0.2,
        )
        self._click(self._visible_button(page, "确定"), page=page)
        self._wait_until(
            lambda: "共 0 条" in self._body_text(page) and "重新选择" not in self._body_text(page),
            timeout_seconds=15,
            message="clear imported batch rows",
            interval_seconds=0.2,
        )

    def _import_workbook(
        self,
        page: object,
        store_key: str,
        workbook_path: Path,
        rows: list[PortalIssueRow],
        summary: object,
    ) -> None:
        self._log(
            store_key,
            f"importing workbook {workbook_path.name} rows={summary.row_count}",
        )
        import_error: dict[str, object] = {}

        def capture_import_error(response: object) -> None:
            if "/kpfw/excel/v1/importPlkj" not in str(getattr(response, "url", "")):
                return
            status = int(getattr(response, "status", 0) or 0)
            if status < 400:
                return
            headers = getattr(response, "headers", {}) or {}
            import_error.update(
                status=status,
                content_type=str(headers.get("content-type") or "unknown"),
            )

        response_listener_installed = False
        on_response = getattr(page, "on", None)
        if callable(on_response):
            on_response("response", capture_import_error)
            response_listener_installed = True
        success_prefix = f"导入完成，共处理数据{summary.row_count}条，处理成功{summary.row_count}条"
        try:
            file_inputs = page.locator('input[type="file"]')
            if file_inputs.count() != 1:
                raise PortalRunnerError("Portal batch page does not expose exactly one workbook file input.")
            file_inputs.set_input_files(str(workbook_path))
            self._wait_for_text(page, workbook_path.name)
            self._wait_until(
                lambda: bool(import_error) or success_prefix in self._body_text(page),
                timeout_seconds=30,
                message="import workbook result",
            )
            if import_error:
                raise PortalRunnerError(
                    "DPPT rejected the batch workbook import request "
                    f"with HTTP {import_error['status']} content_type={import_error['content_type']}."
                )
        finally:
            remove_listener = getattr(page, "remove_listener", None)
            if response_listener_installed and callable(remove_listener):
                remove_listener("response", capture_import_error)
        self._wait_for_batch_import_ready(page, store_key, workbook_path.name, success_prefix)
        self._log(
            store_key,
            "workbook import verified "
            f"rows={summary.row_count}",
        )

    def _select_all_rows(self, page: object) -> None:
        try:
            self._check(page.get_by_role("checkbox").first, page=page, force=True)
        except Exception:
            self._mouse_click(page, 55, 515)
        self._wait_until(
            lambda: page.get_by_role("checkbox").first.is_checked(),
            timeout_seconds=10,
            message="select all rows",
        )

    def _open_submit_confirmation(
        self,
        page: object,
        store_key: str,
        row_count: int,
        total_amount_including_tax: Decimal,
    ) -> None:
        self._click(page.get_by_role("button", name="批量开具"), page=page)
        self._wait_for_submit_confirmation_ready(page, store_key, row_count, total_amount_including_tax)
        self._wait_until(
            lambda: self._visible_button_in_dialog(page, "本次勾选批量开具发票", "确定").is_enabled(),
            timeout_seconds=10,
            message="enable submit confirmation button",
        )

    def _confirm_submit(self, page: object) -> None:
        confirm_button = self._visible_button_in_dialog(page, "本次勾选批量开具发票", "确定")
        self._click(confirm_button, page=page)

    def _wait_for_result_modal(self, page: object, store_key: str) -> tuple[list[PortalIssueDetail], int, int, str]:
        self._wait_until(
            lambda: "批量开具结果" in self._body_text(page),
            timeout_seconds=90,
            message="wait for portal issue result",
        )
        self._wait_for_result_modal_ready(page, store_key)
        modal_text = self._result_modal_text(page)
        compact_text = re.sub(r"\s+", "", modal_text)
        match = re.search(r"开具成功发票(\d+)份.*?开具失败发票(\d+)份", compact_text)
        if not match:
            raise PortalRunnerError("Could not parse portal issue result summary.")
        success_count = int(match.group(1))
        failure_count = int(match.group(2))
        details = self._parse_result_modal_details(modal_text)
        success_count, failure_count = self._reconcile_result_counts_from_details(
            details,
            success_count,
            failure_count,
        )
        return details, success_count, failure_count, modal_text

    @staticmethod
    def _reconcile_result_counts_from_details(
        details: list[PortalIssueDetail],
        success_count: int,
        failure_count: int,
        *,
        expected_count: int | None = None,
    ) -> tuple[int, int]:
        parsed_success_count = sum(detail.status == "成功" for detail in details)
        parsed_failure_count = sum(detail.status == "失败" for detail in details)
        parsed_total = parsed_success_count + parsed_failure_count
        if parsed_total == 0:
            return success_count, failure_count
        if expected_count is not None and parsed_total != expected_count:
            return success_count, failure_count
        if parsed_total != len(details):
            return success_count, failure_count
        return parsed_success_count, parsed_failure_count

    @staticmethod
    def _normalize_modal_text(text: str) -> str:
        return re.sub(r"\s+", " ", text or "").strip()

    def _result_modal_text(self, page: object) -> str:
        if hasattr(page, "locator"):
            try:
                dialog = page.locator('[role="dialog"], .el-message-box, .el-dialog__wrapper').filter(
                    has_text="批量开具结果"
                )
                count = dialog.count()
                for index in range(count):
                    candidate_dialog = dialog.nth(index)
                    if not candidate_dialog.is_visible():
                        continue
                    text = candidate_dialog.inner_text()
                    if text.strip():
                        return text
                fallback = dialog.last.inner_text()
                if fallback.strip():
                    return fallback
            except Exception:
                pass
        return self._body_text(page)

    def _parse_result_modal_details(self, modal_text: str) -> list[PortalIssueDetail]:
        details: list[PortalIssueDetail] = []
        for segment in self._result_detail_segments(modal_text):
            detail = self._parse_result_detail_segment(segment)
            if detail is not None:
                details.append(detail)
        return details

    def _result_detail_segments(self, modal_text: str) -> list[str]:
        line_segments = [
            self._normalize_modal_text(line)
            for line in (modal_text or "").splitlines()
            if self._normalize_modal_text(line)
        ]
        parsed_lines = [segment for segment in line_segments if self._looks_like_result_detail_segment(segment)]
        if parsed_lines:
            return parsed_lines

        normalized = self._normalize_modal_text(modal_text)
        if not normalized:
            return []

        row_start_pattern = re.compile(r"(?<!\S)\d+\s+\d+\s+普通发票\b")
        matches = list(row_start_pattern.finditer(normalized))
        if not matches:
            return []

        total_pattern = re.compile(r"\s+共\s*\d+\s*条")
        segments: list[str] = []
        for index, match in enumerate(matches):
            start = match.start()
            if index + 1 < len(matches):
                end = matches[index + 1].start()
            else:
                total_match = total_pattern.search(normalized, pos=start)
                end = total_match.start() if total_match else len(normalized)
            segment = normalized[start:end].strip()
            if segment:
                segments.append(segment)
        return segments

    @staticmethod
    def _looks_like_result_detail_segment(segment: str) -> bool:
        normalized = TaxPortalRunner._normalize_modal_text(segment)
        return bool(
            re.match(r"^\d+\s+\d+\s+普通发票\b", normalized)
            and re.search(r"\b(成功|失败)\b", normalized)
        )

    @staticmethod
    def _looks_like_amount_token(token: str) -> bool:
        return bool(re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", token))

    def _parse_result_detail_segment(self, segment: str) -> PortalIssueDetail | None:
        normalized = self._normalize_modal_text(segment)
        match = re.match(r"^(?P<row_index>\d+)\s+(?P<invoice_serial>\d+)\s+普通发票\s+(?P<tail>.+)$", normalized)
        if not match:
            return None
        tokens = match.group("tail").split(" ")
        status_index: int | None = None
        for index in range(len(tokens) - 1, -1, -1):
            if tokens[index] in {"成功", "失败"}:
                status_index = index
                break
        if status_index is None:
            return None

        status = tokens[status_index]
        failure_reason_text = " ".join(tokens[status_index + 1 :]).strip()
        failure_reason = None if not failure_reason_text or failure_reason_text == "-" else failure_reason_text

        cursor = status_index - 1
        buyer_email: str | None = None
        if cursor >= 0 and ("@" in tokens[cursor] or tokens[cursor] == "-"):
            buyer_email = None if tokens[cursor] == "-" else tokens[cursor]
            cursor -= 1

        if cursor >= 0 and self._looks_like_amount_token(tokens[cursor]):
            cursor -= 1

        digital_invoice_number: str | None = None
        if cursor >= 0:
            digital_token = tokens[cursor]
            digital_invoice_number = None if digital_token == "-" else digital_token

        return PortalIssueDetail(
            invoice_serial=match.group("invoice_serial"),
            digital_invoice_number=digital_invoice_number,
            buyer_email=buyer_email,
            status=status,
            failure_reason=failure_reason,
        )

    def _prepare_artifacts_dir(self, store_key: str) -> Path:
        timestamp = Path(str(int(monotonic() * 1000)))
        root = (self.config.portal_artifacts_dir or Path("data/tax-portal-artifacts")).resolve()
        path = root / store_key / str(timestamp)
        ensure_parent_dir(path / "placeholder.txt")
        return path

    def _capture_artifact(self, page: object, artifacts_dir: Path | None, stem: str) -> None:
        if artifacts_dir is None:
            return
        ensure_parent_dir(artifacts_dir / "placeholder.txt")
        target = artifacts_dir / f"{stem}.png"
        try:
            page.screenshot(path=str(target), full_page=True)
        except Exception:
            return

    @staticmethod
    def _write_artifact_text(artifacts_dir: Path | None, filename: str, text: str) -> None:
        if artifacts_dir is None:
            return
        ensure_parent_dir(artifacts_dir / "placeholder.txt")
        target = artifacts_dir / filename
        try:
            target.write_text(text, encoding="utf-8")
        except Exception:
            return

    def _capture_submit_result_artifacts(
        self,
        page: object,
        artifacts_dir: Path | None,
        store_key: str,
        modal_text: str,
    ) -> None:
        stem = f"{store_key}-submit-result"
        self._capture_artifact(page, artifacts_dir, stem)
        self._write_artifact_text(artifacts_dir, f"{stem}.txt", modal_text)

    @staticmethod
    def _log(store_key: str, message: str) -> None:
        print(f"[tax-portal][{store_key}] {message}", flush=True)

    def _sync_portal_profile_from_chrome(self) -> None:
        if not self.config.portal_sync_from_chrome_profile:
            return
        if self.config.portal_user_data_dir is None:
            raise PortalRunnerError("TAX_PORTAL_USER_DATA_DIR is required when syncing from a Chrome profile.")
        source_dir = self._resolve_chrome_profile_sync_source_dir()
        target_dir = self.config.portal_user_data_dir / "Default"
        ensure_parent_dir(target_dir / "placeholder.txt")
        self._log("runner", f"syncing portal browser session from Chrome profile: {source_dir}")
        for entry in BROWSER_SESSION_SYNC_ENTRIES:
            self._copy_profile_entry(source_dir / entry, target_dir / entry)
        self._log("runner", f"synced portal browser session to {target_dir}")

    def _resolve_chrome_profile_sync_source_dir(self) -> Path:
        configured = self.config.portal_chrome_profile_dir
        if configured is not None:
            if not configured.exists():
                raise PortalRunnerError(f"Configured Chrome profile directory does not exist: {configured}")
            return configured
        root = Path.home() / "Library/Application Support/Google/Chrome"
        local_state = root / "Local State"
        if not local_state.exists():
            raise PortalRunnerError(
                "TAX_PORTAL_SYNC_FROM_CHROME_PROFILE is enabled, but Chrome Local State was not found. "
                "Set TAX_PORTAL_CHROME_PROFILE_DIR explicitly."
            )
        try:
            data = json.loads(local_state.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise PortalRunnerError(
                "Could not parse Chrome Local State. Set TAX_PORTAL_CHROME_PROFILE_DIR explicitly."
            ) from exc
        last_used = str((data.get("profile") or {}).get("last_used") or "").strip() or "Default"
        profile_dir = root / last_used
        if not profile_dir.exists():
            raise PortalRunnerError(
                f"Detected Chrome profile directory does not exist: {profile_dir}. "
                "Set TAX_PORTAL_CHROME_PROFILE_DIR explicitly."
            )
        return profile_dir

    @staticmethod
    def _copy_profile_entry(source: Path, target: Path) -> None:
        if not source.exists():
            return
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        if source.is_dir():
            shutil.copytree(source, target)
            return
        ensure_parent_dir(target)
        shutil.copy2(source, target)

    @staticmethod
    def _env_flag(name: str) -> bool:
        return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}

    def _launch_args(self) -> list[str]:
        args: list[str] = []
        if self.config.portal_disable_proxy:
            args.extend(["--no-proxy-server", "--proxy-bypass-list=*"])
        return args

    def _install_network_diag(self, context: object, page: object, store_key: str, page_label: str) -> None:
        if not self.network_diag_enabled:
            return
        try:
            session = context.new_cdp_session(page)
            session.send("Network.enable")
        except Exception as exc:  # noqa: BLE001
            self._log(store_key, f"network[{page_label}] diagnostics unavailable error={exc}")
            return

        requests: dict[str, dict[str, object]] = {}
        threshold_ms = self.network_diag_threshold_ms

        def on_request(params: dict[str, object]) -> None:
            request_id = str(params.get("requestId") or "")
            request = params.get("request") or {}
            requests[request_id] = {
                "started_at": monotonic(),
                "type": params.get("type") or "Other",
                "url": str((request or {}).get("url") or ""),
                "method": str((request or {}).get("method") or ""),
                "status": None,
                "from_disk_cache": False,
                "from_service_worker": False,
                "failed": False,
            }

        def on_response(params: dict[str, object]) -> None:
            request_id = str(params.get("requestId") or "")
            response = params.get("response") or {}
            item = requests.setdefault(
                request_id,
                {
                    "started_at": monotonic(),
                    "type": params.get("type") or "Other",
                    "url": str((response or {}).get("url") or ""),
                    "method": "",
                    "status": None,
                    "from_disk_cache": False,
                    "from_service_worker": False,
                    "failed": False,
                },
            )
            item["status"] = int((response or {}).get("status") or 0)
            item["url"] = str((response or {}).get("url") or item["url"])
            item["from_disk_cache"] = bool(params.get("fromDiskCache"))
            item["from_service_worker"] = bool(params.get("fromServiceWorker"))

        def emit(item: dict[str, object], source: str) -> None:
            duration_ms = (monotonic() - float(item["started_at"])) * 1000
            item_type = str(item.get("type") or "Other")
            status = item.get("status") or "-"
            url = self._short_url(str(item.get("url") or ""))
            should_log = (
                duration_ms >= threshold_ms
                or source != "network"
                or item_type in {"Document", "Script", "Stylesheet", "XHR", "Fetch"}
            )
            if not should_log:
                return
            self._log(
                store_key,
                f"network[{page_label}] source={source} type={item_type} status={status} "
                f"dur_ms={duration_ms:.0f} method={item.get('method') or '-'} url={url}",
            )
            response_body = str(item.get("response_body") or "").strip()
            if response_body:
                compact_body = re.sub(r"\s+", " ", response_body)
                self._log(
                    store_key,
                    f"network[{page_label}] status={status} response_body={compact_body}",
                )

        def on_finished(params: dict[str, object]) -> None:
            request_id = str(params.get("requestId") or "")
            item = requests.pop(request_id, None)
            if not item:
                return
            status = item.get("status")
            if isinstance(status, int) and status >= 400:
                try:
                    response_body = session.send("Network.getResponseBody", {"requestId": request_id})
                    item["response_body"] = str((response_body or {}).get("body") or "")[:500]
                except Exception:
                    pass
            source = "network"
            if item.get("from_service_worker"):
                source = "service-worker"
            elif item.get("from_disk_cache"):
                source = "disk-cache"
            emit(item, source)

        def on_failed(params: dict[str, object]) -> None:
            request_id = str(params.get("requestId") or "")
            item = requests.pop(request_id, None)
            if not item:
                return
            item["failed"] = True
            item["status"] = params.get("errorText") or "failed"
            emit(item, "failed")

        session.on("Network.requestWillBeSent", on_request)
        session.on("Network.responseReceived", on_response)
        session.on("Network.loadingFinished", on_finished)
        session.on("Network.loadingFailed", on_failed)
        self._log(store_key, f"network[{page_label}] diagnostics enabled threshold_ms={threshold_ms:.0f}")

    def _install_dialog_diag(self, page: object, store_key: str, page_label: str) -> None:
        on_event = getattr(page, "on", None)
        if not callable(on_event):
            return

        def on_dialog(dialog: object) -> None:
            message = str(getattr(dialog, "message", "") or "")
            dialog_type = str(getattr(dialog, "type", "") or "unknown")
            self._log(
                store_key,
                f"dialog[{page_label}] type={dialog_type} message={message[:300]}",
            )
            try:
                dialog.dismiss()
            except Exception:
                pass

        on_event("dialog", on_dialog)

    def _log_page_runtime_snapshot(self, page: object, store_key: str, page_label: str) -> None:
        try:
            snapshot = page.evaluate(
                """
                () => {
                  const nav = performance.getEntriesByType('navigation')[0];
                  const bodyText = (document.body && document.body.innerText || '')
                    .replace(/\s+/g, ' ')
                    .trim();
                  return {
                    url: location.href,
                    readyState: document.readyState,
                    responseStatus: Number(nav && nav.responseStatus || 0),
                    bodyText: bodyText.slice(0, 500),
                    scriptCount: document.scripts.length,
                    resourceCount: performance.getEntriesByType('resource').length,
                  };
                }
                """
            )
            self._log(
                store_key,
                f"snapshot[{page_label}] {json.dumps(snapshot, ensure_ascii=False)}",
            )
        except Exception as exc:  # noqa: BLE001
            self._log(store_key, f"snapshot[{page_label}] unavailable error={exc}")

    @staticmethod
    def _short_url(url: str, limit: int = 120) -> str:
        if len(url) <= limit:
            return url
        return url[: limit - 3] + "..."

    def _select_company_switch_row(self, page: object, store: StoreConfig) -> str:
        last_error: Exception | None = None
        for candidate in self._company_switch_candidates(store):
            self._log(store.store_key, f"trying company switch candidate: {candidate}")
            try:
                page.get_by_placeholder("请输入纳税人名称").fill(candidate)
                self._click(page.get_by_role("button", name="查询"), page=page)
                self._wait_for_switch_query_results_ready(page, store.store_key)
            except Exception:
                pass
            row = page.locator("tr").filter(has_text=candidate).first
            button = row.get_by_role("button", name="切换")
            try:
                self._click(button, page=page, timeout=3000)
                return candidate
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                continue
        candidates_text = ", ".join(self._company_switch_candidates(store))
        raise PortalRunnerError(
            "Could not find a switchable company row for any candidate: "
            f"{candidates_text}"
        ) from last_error

    @staticmethod
    def _company_switch_candidates(store: StoreConfig) -> list[str]:
        candidates: list[str] = []
        for candidate in (
            store.effective_portal_company_switch_name(),
            store.effective_portal_company_verify_name(),
        ):
            if candidate not in candidates:
                candidates.append(candidate)
        return candidates

    def _update_store_step(
        self,
        store_key: str,
        step: str,
        status: str,
        *,
        workbook_sha256: str | None = None,
        error: str | None = None,
        last_history_id: int | None = None,
        message: str | None = None,
    ) -> None:
        self.state_store.update_portal_issue_state(
            store_key,
            current_step=step,
            last_status=status,
            workbook_sha256=workbook_sha256,
            error=error,
            last_history_id=last_history_id,
        )
        if message:
            self._log(store_key, f"step={step} status={status} {message}")

    @staticmethod
    def _load_sync_playwright() -> Callable[[], object]:
        try:
            from playwright.sync_api import sync_playwright
        except ModuleNotFoundError as exc:  # pragma: no cover - exercised only in runtime envs without playwright
            raise RuntimeError(
                "Playwright is not installed. Run `pip install -r requirements.txt` and `playwright install chromium`."
            ) from exc
        return sync_playwright

    @staticmethod
    def _goto(url: str, page: object) -> None:
        page.goto(url, wait_until="domcontentloaded")

    def _is_login_page(self, page: object) -> bool:
        url = getattr(page, "url", "")
        return self._is_tpass_login_url(url) or self._page_contains(page, "打开电子税务局APP扫一扫")

    def _is_portal_related_page(self, page: object) -> bool:
        url = getattr(page, "url", "")
        return self._portal_area_from_url(url) is not None

    def _page_matches_portal_area(self, page: object, store: StoreConfig | None) -> bool:
        if store is None:
            return True
        current_area = self._portal_area_from_url(getattr(page, "url", ""))
        if current_area is None:
            return False
        return current_area == store.effective_portal_area()

    def _is_authenticated_portal_shell_page(self, page: object) -> bool:
        if not self._is_portal_related_page(page):
            return False
        if self._is_login_page(page) or self._page_contains(page, "会话失效，请重新登录"):
            return False
        if self._page_contains(page, "我要办税"):
            return True
        if not self._page_contains(page, "首页"):
            return False
        return (
            self._page_contains(page, "我要查询")
            or self._page_contains(page, "我的提醒")
            or self._page_contains(page, "我的待办")
        )

    def _is_loginb_pending_page(self, page: object) -> bool:
        url = getattr(page, "url", "")
        return self._is_etax_url(url) and "/loginb/" in url and not self._is_home_page(page)

    def _page_requires_reauth(self, page: object) -> bool:
        url = getattr(page, "url", "")
        return (
            self._is_tpass_login_url(url)
            or self._is_loginb_pending_page(page)
            or self._page_contains(page, "打开电子税务局APP扫一扫")
            or self._page_contains(page, "会话失效，请重新登录")
            or self._is_public_landing_page(page)
        )

    def _is_home_page(self, page: object) -> bool:
        url = getattr(page, "url", "")
        return self._is_etax_url(url) and self._page_contains(page, "我要办税")

    def _ensure_authenticated_home_page(
        self,
        page: object,
        result: PortalIssueResult,
        store: StoreConfig | None = None,
    ) -> object:
        if self._is_home_page(page) or not self._is_authenticated_portal_shell_page(page):
            return page
        self._log(
            result.store_key,
            f"authenticated portal shell detected outside home; opening portal home from {getattr(page, 'url', '<unknown>')}",
        )
        return self._navigate_with_reauth(
            page,
            self.config.portal_home_url_for_store(store),
            result,
            store=store,
            expected_text="我要办税",
            step_name="open authenticated portal home",
        )

    def _open_login_from_public_landing(self, page: object) -> None:
        try:
            self._click(page.get_by_text("登录", exact=True).last, page=page)
        except Exception:
            self._click_text(page, "登录")

    def _try_refresh_login_qr(self, page: object) -> bool:
        candidates = (
            page.get_by_text("刷新", exact=True).first,
            page.get_by_text("请点击", exact=False).first,
        )
        for locator in candidates:
            try:
                self._click(locator, page=page, timeout=1000, force=True)
                return True
            except Exception:
                continue
        return False

    def _chrome_oauth_restart_readiness(
        self,
        page: object,
        store: StoreConfig,
        original_challenge_url: str | None,
    ) -> tuple[bool, str]:
        target_area = store.effective_portal_area()
        same_challenge_visible = False
        tpass_login_visible = False
        for target in self._raw_attached_targets():
            if target.get("type") != "page":
                continue
            target_url = str(target.get("url") or "")
            etax_match = PORTAL_ETAX_HOST_RE.match(target_url)
            if etax_match is not None and etax_match.group(1) == target_area:
                return False, "etax-transition-visible"
            tpass_match = PORTAL_TPASS_LOGIN_RE.match(target_url)
            if tpass_match is None or tpass_match.group(1) != target_area:
                continue
            tpass_login_visible = True
            if original_challenge_url is not None and target_url == original_challenge_url:
                same_challenge_visible = True
        if not tpass_login_visible:
            return False, "tpass-target-unavailable-or-transitioning"
        if original_challenge_url is None or not same_challenge_visible:
            return False, "tpass-challenge-changed"
        if not self._login_qr_requires_refresh(page):
            return False, "login-qr-still-active"
        return True, "original-login-qr-expired"

    def _login_qr_requires_refresh(self, page: object) -> bool:
        return any(
            self._page_contains(page, marker)
            for marker in (
                "二维码已失效",
                "二维码已过期",
                "请点击刷新",
                "点击刷新二维码",
            )
        ) or (
            self._page_contains(page, "请点击")
            and self._page_contains(page, "刷新")
        )

    def _is_public_landing_page(self, page: object) -> bool:
        url = page.url.rstrip("/")
        return (
            bool(PORTAL_ETAX_HOST_RE.fullmatch(url))
            and self._page_contains(page, "环境检测")
            and self._page_contains(page, "电子税务局APP下载")
            and self._page_contains(page, "登录")
        )

    @staticmethod
    def _is_tpass_login_url(url: str) -> bool:
        return PORTAL_TPASS_LOGIN_RE.match(url) is not None

    @staticmethod
    def _is_etax_url(url: str) -> bool:
        return PORTAL_ETAX_HOST_RE.match(url) is not None

    @staticmethod
    def _portal_area_from_url(url: str) -> str | None:
        for pattern in (PORTAL_ETAX_HOST_RE, PORTAL_TPASS_HOST_RE, PORTAL_DPPT_HOST_RE):
            match = pattern.match(url)
            if match is not None:
                return match.group(1)
        return None

    def _navigate_with_reauth(
        self,
        page: object,
        url: str,
        result: PortalIssueResult,
        *,
        store: StoreConfig | None = None,
        expected_text: str | None,
        step_name: str,
        max_attempts: int = 3,
    ) -> object:
        last_error: Exception | None = None
        for _ in range(max_attempts):
            try:
                self._goto(url, page)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if "interrupted by another navigation" not in str(exc):
                    raise
            if self._page_requires_reauth(page):
                page = self._ensure_logged_in(page, result, store)
                continue
            if expected_text is None:
                return page
            try:
                self._wait_for_text(page, expected_text, timeout_ms=self.config.portal_action_timeout_ms)
                return page
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if self._page_requires_reauth(page):
                    page = self._ensure_logged_in(page, result, store)
                    continue
                raise
        if last_error is not None:
            raise PortalRunnerError(f"Failed to {step_name}: {last_error}") from last_error
        raise PortalRunnerError(f"Failed to {step_name}.")

    def _first_authenticated_page(
        self,
        pages: list[object],
        store: StoreConfig | None = None,
    ) -> object | None:
        shell_page: object | None = None
        for candidate in pages:
            try:
                if not self._page_matches_portal_area(candidate, store):
                    continue
                if self._is_home_page(candidate):
                    return candidate
                if shell_page is None and self._is_authenticated_portal_shell_page(candidate):
                    shell_page = candidate
            except Exception:
                continue
        return shell_page

    def _find_authenticated_page(self, page: object, store: StoreConfig | None = None) -> object | None:
        return self._first_authenticated_page(self._page_candidates(page), store)

    def _confirmed_authenticated_page(
        self,
        page: object | None,
        store: StoreConfig | None = None,
    ) -> object | None:
        if page is None:
            return None
        authenticated_page = self._find_authenticated_page(page, store)
        if authenticated_page is None:
            return None
        wait_for_load_state = getattr(authenticated_page, "wait_for_load_state", None)
        if callable(wait_for_load_state):
            try:
                wait_for_load_state("load", timeout=min(self.config.portal_action_timeout_ms, 5000))
            except Exception:
                pass
        return self._stabilized_authenticated_page(authenticated_page, store)

    def _stabilized_authenticated_page(
        self,
        page: object,
        store: StoreConfig | None = None,
    ) -> object | None:
        deadline = monotonic() + (AUTHENTICATED_PAGE_STABILITY_MS / 1000)
        current_page = page
        while True:
            authenticated_page = self._find_authenticated_page(current_page, store)
            if authenticated_page is None or self._page_requires_reauth(authenticated_page):
                return None
            if monotonic() >= deadline:
                return authenticated_page
            current_page = authenticated_page
            wait_for_timeout = getattr(current_page, "wait_for_timeout", None)
            if not callable(wait_for_timeout):
                return authenticated_page
            try:
                wait_for_timeout(AUTHENTICATED_PAGE_STABILITY_POLL_MS)
            except Exception:
                sleep(AUTHENTICATED_PAGE_STABILITY_POLL_MS / 1000)

    def _refresh_attached_authenticated_page(self, store: StoreConfig | None = None) -> object | None:
        if self.config.portal_browser_backend != "chrome_cdp":
            return None
        candidates = list(self._observed_attached_pages)
        if self._attached_cdp_connections:
            browser = self._attached_cdp_connections[0]
            try:
                for context in list(getattr(browser, "contexts", [])):
                    for candidate in list(getattr(context, "pages", [])):
                        if candidate not in candidates:
                            candidates.append(candidate)
            except Exception:
                pass
        authenticated_page = self._first_authenticated_page(candidates, store)
        if authenticated_page is not None:
            return authenticated_page
        target_area = store.effective_portal_area() if store is not None else None
        raw_home_visible = any(
            (
                (match := PORTAL_ETAX_HOST_RE.match(str(target.get("url") or "")))
                is not None
                and (target_area is None or match.group(1) == target_area)
            )
            for target in self._raw_attached_targets()
            if target.get("type") == "page"
        )
        if not raw_home_visible or self._sync_playwright_factory is None:
            return None
        try:
            browser = self._restart_attached_cdp_transport(store)
        except Exception:
            return None
        return self._authenticated_page_from_attached_browser(browser, store)

    def _refresh_attached_login_page(
        self,
        page: object,
        store: StoreConfig | None = None,
    ) -> object | None:
        if self.config.portal_browser_backend != "chrome_cdp":
            return None
        candidates = self._page_candidates(page)
        for browser in self._attached_cdp_connections:
            try:
                for context in list(getattr(browser, "contexts", [])):
                    for candidate in list(getattr(context, "pages", [])):
                        if candidate not in candidates:
                            candidates.append(candidate)
            except Exception:
                continue
        login_page = self._first_login_page(candidates, store)
        if login_page is not None:
            return login_page

        target_area = store.effective_portal_area() if store is not None else None
        raw_login_visible = any(
            (
                (match := PORTAL_TPASS_HOST_RE.match(str(target.get("url") or "")))
                is not None
                and (target_area is None or match.group(1) == target_area)
            )
            for target in self._raw_attached_targets()
            if target.get("type") == "page"
        )
        if not raw_login_visible or self._sync_playwright_factory is None:
            return None
        try:
            browser = self._restart_attached_cdp_transport(store)
        except Exception:
            return None
        try:
            pages = [
                candidate
                for context in list(getattr(browser, "contexts", []))
                for candidate in list(getattr(context, "pages", []))
            ]
        except Exception:
            return None
        return self._first_login_page(pages, store)

    def _blank_tpass_gateway_failure(
        self,
        store: StoreConfig,
    ) -> tuple[dict[str, object], dict[str, object]] | None:
        target_area = store.effective_portal_area()
        try:
            targets = self._raw_attached_targets()
        except Exception:
            return None
        for target in targets:
            if target.get("type") != "page":
                continue
            target_url = str(target.get("url") or "")
            match = PORTAL_TPASS_OAUTH_API_RE.match(target_url)
            if match is None or match.group(1) != target_area:
                continue
            try:
                snapshot = self._raw_cdp_evaluate(
                    target,
                    """
                    (() => {
                      const navigation = performance.getEntriesByType('navigation')[0] || null;
                      return {
                        readyState: document.readyState,
                        title: document.title,
                        bodyText: (document.body && document.body.innerText || '').trim(),
                        navigationStatus: Number(navigation?.responseStatus || 0),
                      };
                    })()
                    """,
                )
            except Exception:
                continue
            if not isinstance(snapshot, dict):
                continue
            body_text = str(snapshot.get("bodyText") or "").strip()
            ready_state = str(snapshot.get("readyState") or "")
            navigation_status = int(snapshot.get("navigationStatus") or 0)
            if ready_state == "complete" and not body_text and navigation_status >= 400:
                return target, snapshot
        return None

    def _restart_dedicated_chrome_after_gateway_error(self, store: StoreConfig) -> object:
        cdp_url = self._attached_cdp_url
        user_data_dir = self.config.portal_chrome_cdp_user_data_dir
        sync_playwright = self._sync_playwright_factory
        if not cdp_url or user_data_dir is None or sync_playwright is None:
            raise PortalRunnerError(
                "Dedicated Chrome cannot be restarted because its CDP URL, user data directory, "
                "or Playwright factory is unavailable."
            )

        chrome_executable = self._resolve_dedicated_chrome_executable()
        parsed_cdp_url = urlparse(cdp_url)
        if parsed_cdp_url.port is None:
            raise PortalRunnerError(f"Dedicated Chrome CDP URL has no port: {cdp_url}")

        self._stop_attached_playwright_transport()
        completed = subprocess.run(
            ["pkill", "-f", "--", f"--user-data-dir={Path(user_data_dir).resolve()}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode not in {0, 1}:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise PortalRunnerError(
                "Could not stop the dedicated Chrome after a TPass gateway challenge error: "
                f"{detail or f'pkill exit {completed.returncode}'}"
            )

        shutdown_deadline = monotonic() + 10.0
        while monotonic() < shutdown_deadline and self._raw_cdp_endpoint_available():
            sleep(0.2)
        if self._raw_cdp_endpoint_available():
            raise PortalRunnerError("Dedicated Chrome did not stop within 10 seconds after gateway failure.")

        user_data_path = Path(user_data_dir).resolve()
        user_data_path.mkdir(parents=True, exist_ok=True)
        log_path = user_data_path / "chrome-cdp.log"
        launch_url = self.config.portal_home_url_for_store(store)
        with log_path.open("ab") as log_file:
            process = subprocess.Popen(
                [
                    chrome_executable,
                    f"--remote-debugging-port={parsed_cdp_url.port}",
                    f"--user-data-dir={user_data_path}",
                    "--no-first-run",
                    "--new-window",
                    launch_url,
                ],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

        ready_deadline = monotonic() + 30.0
        while monotonic() < ready_deadline:
            if self._raw_cdp_endpoint_available() and any(
                target.get("type") == "page" for target in self._raw_attached_targets()
            ):
                break
            sleep(0.5)
        else:
            raise PortalRunnerError(
                "Dedicated Chrome restarted after a TPass gateway challenge error but CDP did not "
                f"become ready within 30 seconds. Check {log_path}."
            )

        self._log(
            store.store_key,
            "dedicated Chrome restarted after TPass gateway challenge error "
            f"pid={process.pid} launch_url={launch_url}",
        )
        browser = self._restart_attached_cdp_transport(store)
        if not browser.contexts:
            raise PortalRunnerError("Restarted dedicated Chrome has no browser context.")
        context = browser.contexts[0]
        context.set_default_timeout(self.config.portal_action_timeout_ms)
        page = self._find_attached_portal_page(context, store)
        return page if page is not None else self._resolve_attached_home_page(context)

    def _raw_cdp_endpoint_available(self) -> bool:
        cdp_url = self._attached_cdp_url
        if not cdp_url:
            return False
        try:
            opener = build_opener(ProxyHandler({}))
            with opener.open(f"{cdp_url.rstrip('/')}/json/version", timeout=2.0) as response:
                return int(getattr(response, "status", 200) or 200) == 200
        except Exception:
            return False

    def _resolve_dedicated_chrome_executable(self) -> str:
        configured = self.config.portal_chrome_executable_path
        if configured is not None:
            return str(configured)
        for candidate in (
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path(
                "/Applications/Google Chrome for Testing.app/Contents/MacOS/"
                "Google Chrome for Testing"
            ),
        ):
            if candidate.exists():
                return str(candidate)
        raise PortalRunnerError(
            "Could not locate a Chrome executable for automatic dedicated Chrome restart."
        )

    def _first_login_page(
        self,
        pages: list[object],
        store: StoreConfig | None = None,
    ) -> object | None:
        for candidate in pages:
            try:
                if not self._page_matches_portal_area(candidate, store):
                    continue
                if self._is_login_page(candidate):
                    return candidate
            except Exception:
                continue
        return None

    def _authenticated_page_from_attached_browser(
        self,
        browser: object,
        store: StoreConfig | None,
    ) -> object | None:
        try:
            if not browser.contexts:
                return None
            context = browser.contexts[0]
            context.set_default_timeout(self.config.portal_action_timeout_ms)
            authenticated_page = self._first_authenticated_page(list(getattr(context, "pages", [])), store)
            if authenticated_page is None:
                return None
            return authenticated_page
        except Exception:
            return None

    def _make_attached_connections_dialog_inert(self) -> None:
        for browser in self._attached_cdp_connections:
            try:
                contexts = list(getattr(browser, "contexts", []))
            except Exception:
                continue
            for context in contexts:
                context_id = id(context)
                if context_id not in self._dialog_inert_context_ids:
                    on_context_event = getattr(context, "on", None)
                    if callable(on_context_event):
                        try:
                            on_context_event("page", self._make_attached_page_dialog_inert)
                            self._dialog_inert_context_ids.add(context_id)
                        except Exception:
                            pass
                for page in list(getattr(context, "pages", [])):
                    self._make_attached_page_dialog_inert(page)

    def _make_attached_page_dialog_inert(self, page: object) -> None:
        page_id = id(page)
        if page_id in self._dialog_inert_page_ids:
            return
        on_event = getattr(page, "on", None)
        if not callable(on_event):
            return
        try:
            on_event("dialog", self._leave_dialog_for_active_connection)
            self._dialog_inert_page_ids.add(page_id)
        except Exception:
            return

    @staticmethod
    def _leave_dialog_for_active_connection(dialog: object) -> None:
        return None

    def _record_attached_page(self, page: object) -> None:
        if page not in self._observed_attached_pages:
            self._observed_attached_pages.append(page)

    def _page_candidates(self, page: object) -> list[object]:
        candidates = [page]
        context = getattr(page, "context", None)
        context_pages = getattr(context, "pages", None)
        if context_pages is None:
            return candidates
        try:
            for candidate in list(context_pages):
                if candidate not in candidates:
                    candidates.append(candidate)
        except Exception:
            pass
        for candidate in self._observed_attached_pages:
            if candidate not in candidates:
                candidates.append(candidate)
        return candidates

    @staticmethod
    def _page_contains(page: object, text: str) -> bool:
        try:
            return text in page.locator("body").inner_text()
        except Exception:
            return False

    def _click_text(self, page: object, text: str) -> None:
        try:
            self._click(page.get_by_text(text, exact=True), page=page)
        except Exception:
            self._click(page.get_by_text(text).first, page=page)

    def _dom_click_exact_text(self, page: object, text: str, *, class_hint: str = "") -> None:
        self._wait_before_browser_click(page)
        result = page.evaluate(
            """
            ({text, classHint}) => {
              const targets = [...document.querySelectorAll('*')].filter((element) =>
                element.children.length === 0 &&
                (element.textContent || '').trim() === text &&
                getComputedStyle(element).display !== 'none' &&
                getComputedStyle(element).visibility !== 'hidden'
              );
              const target = targets.find((element) =>
                classHint && String(element.className || '').includes(classHint)
              ) || targets[0];
              if (!target) {
                return {clicked: false, count: targets.length};
              }
              target.click();
              return {
                clicked: true,
                count: targets.length,
                tag: target.tagName,
                className: String(target.className || ''),
              };
            }
            """,
            {"text": text, "classHint": class_hint},
        )
        if not isinstance(result, dict) or not result.get("clicked"):
            raise PortalRunnerError(f"Could not find a visible DOM element with exact text: {text}")

    @staticmethod
    def _wait_before_browser_click(page: object | None) -> None:
        wait_for_timeout = getattr(page, "wait_for_timeout", None)
        if callable(wait_for_timeout):
            wait_for_timeout(BROWSER_CLICK_DELAY_MS)
            return
        if page is None:
            sleep(BROWSER_CLICK_DELAY_SECONDS)

    def _click(self, target: object, *, page: object | None = None, **kwargs: object) -> None:
        self._wait_before_browser_click(page or getattr(target, "page", None))
        target.click(**kwargs)

    def _check(self, target: object, *, page: object | None = None, **kwargs: object) -> None:
        self._wait_before_browser_click(page or getattr(target, "page", None))
        target.check(**kwargs)

    def _mouse_click(self, page: object, x: int, y: int, **kwargs: object) -> None:
        self._wait_before_browser_click(page)
        page.mouse.click(x, y, **kwargs)

    @staticmethod
    def _visible_button(page: object, name: str) -> object:
        locator = page.get_by_role("button", name=name)
        try:
            count = locator.count()
        except Exception:
            return locator.last
        for index in range(count):
            candidate = locator.nth(index)
            try:
                if candidate.is_visible():
                    return candidate
            except Exception:
                continue
        return locator.last

    @staticmethod
    def _normalized_label(text: str) -> str:
        return re.sub(r"\s+", "", text)

    def _visible_button_in_dialog(self, page: object, dialog_text: str, button_name: str) -> object:
        if not hasattr(page, "locator"):
            return self._visible_button(page, button_name)
        try:
            dialog = page.locator('[role="dialog"], .el-message-box, .el-dialog__wrapper').filter(has_text=dialog_text)
        except Exception:
            return self._visible_button(page, button_name)
        try:
            count = dialog.count()
        except Exception:
            return self._visible_button(page, button_name)
        for index in range(count):
            candidate_dialog = dialog.nth(index)
            try:
                if not candidate_dialog.is_visible():
                    continue
                buttons = candidate_dialog.get_by_role("button", name=button_name)
                button_count = buttons.count()
                for button_index in range(button_count):
                    candidate_button = buttons.nth(button_index)
                    if candidate_button.is_visible():
                        return candidate_button
            except Exception:
                pass
            try:
                buttons = candidate_dialog.get_by_role("button")
                button_count = buttons.count()
                for button_index in range(button_count):
                    candidate_button = buttons.nth(button_index)
                    if not candidate_button.is_visible():
                        continue
                    if self._normalized_label(candidate_button.inner_text()) == self._normalized_label(button_name):
                        return candidate_button
            except Exception:
                continue
        return self._visible_button(page, button_name)

    def _visible_radio_in_dialog(self, page: object, dialog_text: str, radio_name: str) -> object:
        if not hasattr(page, "locator"):
            return page.get_by_role("radio", name=radio_name)
        try:
            dialog = page.locator('[role="dialog"], .el-message-box, .el-dialog__wrapper').filter(has_text=dialog_text)
        except Exception:
            return page.get_by_role("radio", name=radio_name)
        try:
            count = dialog.count()
        except Exception:
            return page.get_by_role("radio", name=radio_name)
        for index in range(count):
            candidate_dialog = dialog.nth(index)
            try:
                if not candidate_dialog.is_visible():
                    continue
                radios = candidate_dialog.get_by_role("radio", name=radio_name)
                radio_count = radios.count()
                for radio_index in range(radio_count):
                    candidate_radio = radios.nth(radio_index)
                    if candidate_radio.is_visible():
                        return candidate_radio
            except Exception:
                continue
        return page.get_by_role("radio", name=radio_name)

    def _visible_text_in_dialog(self, page: object, dialog_text: str, text: str) -> object:
        if not hasattr(page, "locator"):
            return page.get_by_text(text, exact=True)
        try:
            dialog = page.locator('[role="dialog"], .el-message-box, .el-dialog__wrapper').filter(has_text=dialog_text)
        except Exception:
            return page.get_by_text(text, exact=True)
        try:
            count = dialog.count()
        except Exception:
            return page.get_by_text(text, exact=True)
        for index in range(count):
            candidate_dialog = dialog.nth(index)
            try:
                if not candidate_dialog.is_visible():
                    continue
                texts = candidate_dialog.get_by_text(text, exact=True)
                text_count = texts.count()
                for text_index in range(text_count):
                    candidate_text = texts.nth(text_index)
                    if candidate_text.is_visible():
                        return candidate_text
            except Exception:
                continue
        return page.get_by_text(text, exact=True)

    @staticmethod
    def _wait_for_text(page: object, text: str, timeout_ms: int = 15000) -> None:
        wait_for_function = getattr(page, "wait_for_function", None)
        if callable(wait_for_function):
            wait_for_function(
                """
                (text) => Boolean(
                  document.body &&
                  (document.body.innerText || '').includes(text)
                )
                """,
                arg=text,
                timeout=timeout_ms,
            )
            return
        page.get_by_text(text, exact=False).first.wait_for(timeout=timeout_ms)

    @staticmethod
    def _body_text(page: object) -> str:
        return page.locator("body").inner_text()

    @staticmethod
    def _wait_until(
        predicate: Callable[[], bool],
        *,
        timeout_seconds: float,
        message: str,
        interval_seconds: float = 0.5,
    ) -> None:
        deadline = monotonic() + timeout_seconds
        last_error: Exception | None = None
        while monotonic() < deadline:
            try:
                if predicate():
                    return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
            sleep(interval_seconds)
        if last_error is not None:
            raise PortalRunnerError(f"Timed out waiting to {message}: {last_error}") from last_error
        raise PortalRunnerError(f"Timed out waiting to {message}.")

    @staticmethod
    def _format_money(value: Decimal) -> str:
        return format(value.quantize(Decimal("0.01")), "f")
