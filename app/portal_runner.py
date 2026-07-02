from __future__ import annotations

import json
import os
import re
import shutil
from decimal import Decimal
from pathlib import Path
from time import monotonic, sleep
from typing import Callable

from app.models import AppConfig, PortalIssueDetail, PortalIssueResult, PortalIssueRow, StoreConfig
from app.portal_local_login import PortalLocalLoginError, PortalMacLoginAutomator
from app.portal_sync import PortalWorkbookSyncer
from app.portal_workbook import load_portal_issue_rows, sha256_file, summarize_portal_issue_rows
from app.state import StateStore
from app.utils import ensure_parent_dir

ROLE_LABELS = {
    "legal_representative": "法定代表人",
    "tax_operator": "办税员",
}

QR_REFRESH_GRACE_SECONDS = 5.0
LOGIN_WAIT_HEARTBEAT_SECONDS = 15.0
HOME_PAGE_BEFORE_BATCH_PAGE_DELAY_SECONDS = 3.0
POST_SUBMIT_SUCCESS_WAIT_SECONDS = 3.0
BROWSER_CLICK_DELAY_MS = 1000
BROWSER_CLICK_DELAY_SECONDS = BROWSER_CLICK_DELAY_MS / 1000
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
PORTAL_ETAX_HOST_RE = re.compile(r"^https://etax\.([a-z0-9-]+)\.chinatax\.gov\.cn:8443(?:[/?#].*)?$")
PORTAL_TPASS_HOST_RE = re.compile(r"^https://tpass\.([a-z0-9-]+)\.chinatax\.gov\.cn:8443(?:[/?#].*)?$")
PORTAL_DPPT_HOST_RE = re.compile(r"^https://dppt\.([a-z0-9-]+)\.chinatax\.gov\.cn:8443(?:[/?#].*)?$")


class PortalRunnerError(RuntimeError):
    pass


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
        if config.portal_browser_backend not in {"playwright", "chrome_cdp"}:
            raise ValueError(
                "TAX_PORTAL_BROWSER_BACKEND must be one of: playwright, chrome_cdp."
            )
        if config.portal_browser_backend == "playwright" and config.portal_user_data_dir is None:
            raise ValueError("TAX_PORTAL_USER_DATA_DIR is required for portal runner commands.")

    def run(self, stores: list[StoreConfig]) -> list[PortalIssueResult]:
        sync_playwright = self._load_sync_playwright()
        results: list[PortalIssueResult] = []
        with sync_playwright() as playwright:
            if self.config.portal_browser_backend == "chrome_cdp":
                results = self._run_with_attached_chrome(playwright, stores)
            else:
                results = self._run_with_playwright_context(playwright, stores)
        return results

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
                results.append(self._run_store(context, home_page, store))
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
        home_page = self._resolve_attached_home_page(context)
        self._install_network_diag(context, home_page, "runner", "home")
        try:
            for store in stores:
                results.append(self._run_store(context, home_page, store))
        finally:
            try:
                if getattr(home_page, "_codex_created_page", False):
                    home_page.close()
            except Exception:
                pass
            for connection in self._attached_cdp_connections[1:]:
                try:
                    connection.close()
                except Exception:
                    pass
            self._attached_cdp_browser_type = None
            self._attached_cdp_url = None
            self._attached_cdp_connections = []
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
            batch_page = context.new_page()
            active_page = batch_page
            batch_page.set_default_timeout(self.config.portal_action_timeout_ms)
            self._install_network_diag(context, batch_page, store.store_key, "batch")
            try:
                batch_page = self._wait_for_batch_page(batch_page, store, result)
                self._ensure_batch_page_clean(batch_page, store.store_key)
                self._log(store.store_key, "batch issue page is clean and ready for workbook import")
                self._import_workbook(batch_page, store.store_key, store.output_xlsx_path, rows, summary)
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
                if not self.submit:
                    result.status = "validated"
                    self._log(store.store_key, "dry run finished after validation; submission skipped")
                    return self._finalize_result(result)

                self._log(
                    store.store_key,
                    "step=submit_result status=running "
                    f"submitting rows={summary.row_count} "
                    f"total_amount_including_tax={self._format_money(summary.total_amount_including_tax)}",
                )
                self._select_all_rows(batch_page)
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
        authenticated_page = self._confirmed_authenticated_page(page, store)
        if authenticated_page is not None:
            return authenticated_page
        deadline = monotonic() + self.config.portal_login_timeout_minutes * 60
        refreshed_qr = False
        clicked_public_login = False
        local_app_login_attempted = False
        reauth_seen_at: float | None = None
        last_heartbeat_at: float | None = None
        last_cdp_refresh_at: float | None = None
        self._log(result.store_key, "login required; waiting for successful login...")
        while monotonic() < deadline:
            authenticated_page = self._confirmed_authenticated_page(page, store)
            if authenticated_page is not None:
                if authenticated_page is page:
                    self._log(result.store_key, "login confirmed")
                else:
                    self._log(
                        result.store_key,
                        f"login confirmed on another page: {getattr(authenticated_page, 'url', '<unknown>')}",
                    )
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
                        return refreshed_page
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
                    self._attempt_local_app_login(page, result, store)
                if reauth_seen_at is None:
                    reauth_seen_at = now
                    self._log(
                        result.store_key,
                        f"login page detected; waiting {QR_REFRESH_GRACE_SECONDS:.0f}s before QR refresh",
                    )
                elif not refreshed_qr and now - reauth_seen_at >= QR_REFRESH_GRACE_SECONDS:
                    self._log(result.store_key, "login page still pending; attempting QR refresh once")
                    self._try_refresh_login_qr(page)
                    refreshed_qr = True
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
    ) -> bool:
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
            qr_path = automator.automate(page, result.artifacts_dir)
        except PortalLocalLoginError as exc:
            self._log(
                result.store_key,
                f"local app scan-login automation failed; falling back to manual login wait error={exc}",
            )
            return False
        self._log(result.store_key, f"local app scan-login automation finished qr_path={qr_path}")
        return True

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

    def _wait_for_batch_page(self, page: object, store: StoreConfig, result: PortalIssueResult) -> object:
        result.step = "open_batch_page"
        self._update_store_step(
            store.store_key,
            result.step,
            "running",
            workbook_sha256=result.workbook_sha256,
            message="opening batch issue page",
        )
        page = self._navigate_with_reauth(
            page,
            self.config.portal_batch_issue_url_for_store(store),
            result,
            store=store,
            expected_text=None,
            step_name="open batch issue page",
        )
        self._wait_for_batch_page_ready_with_recovery(page, store.store_key)
        self._log(store.store_key, "batch issue page loaded")
        return page

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
        )

    def _wait_for_batch_page_ready_with_recovery(self, page: object, store_key: str) -> None:
        timeout_seconds = max(self.config.portal_action_timeout_ms / 1000, 5.0)
        self._wait_until(
            lambda: self._page_contains(page, "批量开票") or self._batch_page_shows_session_invalid_prompt(page),
            timeout_seconds=timeout_seconds,
            message="show batch issue page or session recovery prompt",
            interval_seconds=0.2,
        )
        if self._page_contains(page, "批量开票"):
            self._wait_for_batch_page_ready(page, store_key)
            return
        self._log(
            store_key,
            "batch page reports that the e-invoice platform session is invalid; waiting for the page to recover automatically",
        )
        self._wait_until(
            lambda: self._page_contains(page, "批量开票"),
            timeout_seconds=timeout_seconds,
            message="recover batch issue page after session prompt",
            interval_seconds=0.2,
        )
        self._wait_for_batch_page_ready(page, store_key)

    @staticmethod
    def _batch_page_shows_session_invalid_prompt(page: object) -> bool:
        try:
            body_text = page.locator("body").inner_text()
        except Exception:
            return False
        return (
            "系统检测到电票平台会话已失效或电局账号已退出，需刷新重新操作" in body_text
            or "功能地址检查失败" in body_text
            or "此用户无当前页面的操作权限" in body_text
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
        self._wait_until(
            lambda: "批量开具结果" in self._body_text(page),
            timeout_seconds=max(self.config.portal_action_timeout_ms / 1000, 10.0),
            message="show portal issue result dialog",
            interval_seconds=0.2,
        )
        self._wait_until(
            lambda: self._visible_button_in_dialog(page, "批量开具结果", "确定").is_visible()
            and self._visible_button_in_dialog(page, "批量开具结果", "确定").is_enabled(),
            timeout_seconds=10,
            message="show portal issue result confirm button",
            interval_seconds=0.2,
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
        with page.expect_file_chooser() as chooser_info:
            self._click(page.get_by_text("选择文件", exact=True), page=page)
        chooser_info.value.set_files(str(workbook_path))
        self._wait_for_text(page, workbook_path.name)
        success_prefix = f"导入完成，共处理数据{summary.row_count}条，处理成功{summary.row_count}条"
        self._wait_until(
            lambda: success_prefix in self._body_text(page),
            timeout_seconds=30,
            message="import workbook result",
        )
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
        return details, success_count, failure_count, modal_text

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

        def on_finished(params: dict[str, object]) -> None:
            request_id = str(params.get("requestId") or "")
            item = requests.pop(request_id, None)
            if not item:
                return
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
        browser_type = self._attached_cdp_browser_type
        cdp_url = self._attached_cdp_url
        if browser_type is None or not cdp_url:
            return None
        try:
            browser = browser_type.connect_over_cdp(cdp_url)
        except Exception:
            return None
        try:
            if not browser.contexts:
                browser.close()
                return None
            context = browser.contexts[0]
            context.set_default_timeout(self.config.portal_action_timeout_ms)
            authenticated_page = self._first_authenticated_page(list(getattr(context, "pages", [])), store)
            if authenticated_page is None:
                browser.close()
                return None
            self._attached_cdp_connections.append(browser)
            return authenticated_page
        except Exception:
            try:
                browser.close()
            except Exception:
                pass
            return None

    @staticmethod
    def _page_candidates(page: object) -> list[object]:
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
            return candidates
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
