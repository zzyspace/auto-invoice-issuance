from __future__ import annotations

import os
import re
from decimal import Decimal
from pathlib import Path
from time import monotonic, sleep
from typing import Callable

from app.models import AppConfig, PortalIssueDetail, PortalIssueResult, PortalIssueRow, StoreConfig
from app.portal_sync import PortalWorkbookSyncer
from app.portal_workbook import load_portal_issue_rows, sha256_file, summarize_portal_issue_rows
from app.state import StateStore
from app.utils import ensure_parent_dir

ROLE_LABELS = {
    "legal_representative": "法定代表人",
    "tax_operator": "办税员",
}

QR_REFRESH_GRACE_SECONDS = 5.0


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
        if config.portal_user_data_dir is None:
            raise ValueError("TAX_PORTAL_USER_DATA_DIR is required for portal runner commands.")

    def run(self, stores: list[StoreConfig]) -> list[PortalIssueResult]:
        sync_playwright = self._load_sync_playwright()
        results: list[PortalIssueResult] = []
        with sync_playwright() as playwright:
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
            for store in stores:
                results.append(self._run_store(context, home_page, store))
            context.close()
        return results

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
            company_verify_name=store.effective_portal_company_verify_name(),
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
        try:
            self._log(store.store_key, f"opening portal home page: {self.config.portal_home_url}")
            self._goto(self.config.portal_home_url, home_page)
            self._ensure_logged_in(home_page, result)
            self._ensure_company(home_page, store, result)
            batch_page = context.new_page()
            active_page = batch_page
            batch_page.set_default_timeout(self.config.portal_action_timeout_ms)
            self._install_network_diag(context, batch_page, store.store_key, "batch")
            try:
                self._wait_for_batch_page(batch_page, store, result)
                self._assert_batch_page_clean(batch_page)
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
                self._open_submit_confirmation(batch_page, summary.row_count, summary.total_amount_including_tax)
                self._confirm_submit(batch_page)
                details, success_count, failure_count = self._wait_for_result_modal(batch_page)
                result.details = tuple(details)
                result.submitted_count = summary.row_count
                result.success_count = success_count
                result.failure_count = failure_count
                result.status = "success" if failure_count == 0 else "failed"
                result.step = "submit_result"
                self._log(
                    store.store_key,
                    f"step=submit_result status={result.status} "
                    f"success_count={success_count} failure_count={failure_count}",
                )
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

    def _ensure_logged_in(self, page: object, result: PortalIssueResult) -> None:
        if self._is_home_page(page):
            return
        deadline = monotonic() + self.config.portal_login_timeout_minutes * 60
        refreshed_qr = False
        clicked_public_login = False
        reauth_seen_at: float | None = None
        self._log(result.store_key, "login required; waiting for successful login...")
        while monotonic() < deadline:
            if self._is_home_page(page):
                self._log(result.store_key, "login confirmed")
                return
            if self._is_public_landing_page(page):
                if not clicked_public_login:
                    self._log(result.store_key, "public landing detected; clicking login entry")
                    clicked_public_login = True
                self._open_login_from_public_landing(page)
                sleep(1)
                continue
            if self._page_requires_reauth(page):
                now = monotonic()
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
            else:
                reauth_seen_at = None
            sleep(2)
        self._capture_artifact(page, result.artifacts_dir, "login-timeout")
        raise PortalRunnerError("Timed out waiting for tax portal login.")

    def _ensure_company(self, home_page: object, store: StoreConfig, result: PortalIssueResult) -> None:
        verify_name = store.effective_portal_company_verify_name()
        if self._page_contains(home_page, verify_name):
            self._log(store.store_key, f"company already active: {verify_name}")
            return
        result.step = "switch_company"
        self._update_store_step(
            store.store_key,
            result.step,
            "running",
            workbook_sha256=result.workbook_sha256,
            message=f"switching company to {verify_name}",
        )
        self._navigate_with_reauth(
            home_page,
            self.config.portal_identity_switch_url,
            result,
            expected_text="企业办税",
            step_name="switch company",
        )
        selected_name = self._select_company_switch_row(home_page, store)
        self._wait_for_text(home_page, "确认是否切换")
        home_page.get_by_role("button", name="确定").last.click()
        role_label = ROLE_LABELS.get(store.portal_company_role, "法定代表人")
        self._wait_for_text(home_page, "身份类型选择")
        try:
            home_page.get_by_role("radio", name=role_label).check(force=True)
        except Exception:
            home_page.get_by_text(role_label, exact=True).click()
        confirm_button = home_page.get_by_role("button", name="确定").last
        self._wait_until(lambda: confirm_button.is_enabled(), timeout_seconds=10, message="identity role confirm enable")
        confirm_button.click()
        self._navigate_with_reauth(
            home_page,
            self.config.portal_home_url,
            result,
            expected_text=verify_name,
            step_name=f"switch company to {verify_name}",
        )
        self._log(store.store_key, f"company switch confirmed: {verify_name} via candidate={selected_name}")

    def _wait_for_batch_page(self, page: object, store: StoreConfig, result: PortalIssueResult) -> None:
        result.step = "open_batch_page"
        self._update_store_step(
            store.store_key,
            result.step,
            "running",
            workbook_sha256=result.workbook_sha256,
            message="opening batch issue page",
        )
        self._navigate_with_reauth(
            page,
            self.config.portal_batch_issue_url,
            result,
            expected_text="批量开票",
            step_name="open batch issue page",
        )
        self._log(store.store_key, "batch issue page loaded")

    def _assert_batch_page_clean(self, page: object) -> None:
        body_text = self._body_text(page)
        if "共 0 条" not in body_text:
            raise PortalRunnerError("Portal batch page is not clean; refusing to import into a non-empty table.")
        if "重新选择" in body_text:
            raise PortalRunnerError("Portal batch page already has a selected workbook; refusing to continue.")

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
            page.get_by_text("选择文件", exact=True).click()
        chooser_info.value.set_files(str(workbook_path))
        self._wait_for_text(page, workbook_path.name)
        success_prefix = f"导入完成，共处理数据{summary.row_count}条，处理成功{summary.row_count}条"
        self._wait_until(
            lambda: success_prefix in self._body_text(page),
            timeout_seconds=30,
            message="import workbook result",
        )
        body_text = self._body_text(page)
        for row in rows:
            if row.buyer_name not in body_text:
                raise PortalRunnerError(f"Imported batch table is missing buyer name: {row.buyer_name}")
            for value in (row.amount_excluding_tax, row.amount_including_tax):
                amount_text = self._format_money(value)
                if amount_text not in body_text:
                    raise PortalRunnerError(f"Imported batch table is missing amount: {amount_text}")
        expected_total_text = self._format_money(summary.total_amount_including_tax)
        if expected_total_text not in body_text:
            raise PortalRunnerError(
                f"Imported batch table is missing total amount-including-tax text: {expected_total_text}"
            )
        self._log(
            store_key,
            "workbook import verified "
            f"rows={summary.row_count} "
            f"total_amount_including_tax={expected_total_text}",
        )

    def _select_all_rows(self, page: object) -> None:
        try:
            page.get_by_role("checkbox").first.check(force=True)
        except Exception:
            page.mouse.click(55, 515)
        self._wait_until(
            lambda: page.get_by_role("checkbox").first.is_checked(),
            timeout_seconds=10,
            message="select all rows",
        )

    def _open_submit_confirmation(self, page: object, row_count: int, total_amount_including_tax: Decimal) -> None:
        page.get_by_role("button", name="批量开具").click()
        expected_count_text = f"本次勾选批量开具发票{row_count}份"
        expected_amount_text = f"价税合计{self._format_money(total_amount_including_tax).rstrip('0').rstrip('.')}元"
        self._wait_until(
            lambda: expected_count_text in self._body_text(page) and expected_amount_text in self._body_text(page),
            timeout_seconds=10,
            message="open submit confirmation",
        )

    def _confirm_submit(self, page: object) -> None:
        confirm_button = page.get_by_role("button", name="确定").last
        confirm_button.click()

    def _wait_for_result_modal(self, page: object) -> tuple[list[PortalIssueDetail], int, int]:
        self._wait_until(
            lambda: "批量开具结果" in self._body_text(page),
            timeout_seconds=90,
            message="wait for portal issue result",
        )
        body_text = self._body_text(page)
        match = re.search(r"开具成功发票(\d+)份.*?开具失败发票(\d+)份", body_text)
        if not match:
            raise PortalRunnerError("Could not parse portal issue result summary.")
        success_count = int(match.group(1))
        failure_count = int(match.group(2))
        detail_pattern = re.compile(
            r"(\d+)\s+(\d+)\s+普通发票\s+(\d{20})\s+([0-9]+(?:\.[0-9]+)?)\s+(\S+@\S+)\s+(成功|失败)\s+(-|[^0-9]+?)(?=\s+\d+\s+\d+\s+普通发票|\s+共\s+\d+\s+条|$)"
        )
        details: list[PortalIssueDetail] = []
        for invoice_index, invoice_serial, digital_number, _amount, email, status, failure_reason in detail_pattern.findall(body_text):
            _ = invoice_index
            details.append(
                PortalIssueDetail(
                    invoice_serial=invoice_serial,
                    digital_invoice_number=digital_number,
                    buyer_email=email,
                    status=status,
                    failure_reason=None if failure_reason == "-" else failure_reason.strip(),
                )
            )
        return details, success_count, failure_count

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
    def _log(store_key: str, message: str) -> None:
        print(f"[tax-portal][{store_key}] {message}", flush=True)

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
                page.get_by_role("button", name="查询").click()
            except Exception:
                pass
            row = page.locator("tr").filter(has_text=candidate).first
            button = row.get_by_role("button", name="切换")
            try:
                button.click(timeout=3000)
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
        url = page.url
        return "tpass.xiamen.chinatax.gov.cn:8443/#/login" in url or self._page_contains(page, "打开电子税务局APP扫一扫")

    def _page_requires_reauth(self, page: object) -> bool:
        url = page.url
        return (
            "tpass.xiamen.chinatax.gov.cn:8443/#/login" in url
            or self._page_contains(page, "打开电子税务局APP扫一扫")
            or self._page_contains(page, "会话失效，请重新登录")
            or self._is_public_landing_page(page)
        )

    def _is_home_page(self, page: object) -> bool:
        url = page.url
        return "etax.xiamen.chinatax.gov.cn:8443/loginb/" in url and self._page_contains(page, "我要办税")

    def _open_login_from_public_landing(self, page: object) -> None:
        try:
            page.get_by_text("登录", exact=True).last.click()
        except Exception:
            self._click_text(page, "登录")

    def _try_refresh_login_qr(self, page: object) -> bool:
        candidates = (
            page.get_by_text("刷新", exact=True).first,
            page.get_by_text("请点击", exact=False).first,
        )
        for locator in candidates:
            try:
                locator.click(timeout=1000, force=True)
                return True
            except Exception:
                continue
        return False

    def _is_public_landing_page(self, page: object) -> bool:
        url = page.url.rstrip("/")
        return (
            url == "https://etax.xiamen.chinatax.gov.cn:8443"
            and self._page_contains(page, "环境检测")
            and self._page_contains(page, "电子税务局APP下载")
            and self._page_contains(page, "登录")
        )

    def _navigate_with_reauth(
        self,
        page: object,
        url: str,
        result: PortalIssueResult,
        *,
        expected_text: str | None,
        step_name: str,
        max_attempts: int = 3,
    ) -> None:
        last_error: Exception | None = None
        for _ in range(max_attempts):
            try:
                self._goto(url, page)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if "interrupted by another navigation" not in str(exc):
                    raise
            if self._page_requires_reauth(page):
                self._ensure_logged_in(page, result)
                continue
            if expected_text is None:
                return
            try:
                self._wait_for_text(page, expected_text, timeout_ms=self.config.portal_action_timeout_ms)
                return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if self._page_requires_reauth(page):
                    self._ensure_logged_in(page, result)
                    continue
                raise
        if last_error is not None:
            raise PortalRunnerError(f"Failed to {step_name}: {last_error}") from last_error
        raise PortalRunnerError(f"Failed to {step_name}.")

    @staticmethod
    def _page_contains(page: object, text: str) -> bool:
        try:
            return text in page.locator("body").inner_text()
        except Exception:
            return False

    @staticmethod
    def _click_text(page: object, text: str) -> None:
        try:
            page.get_by_text(text, exact=True).click()
        except Exception:
            page.get_by_text(text).first.click()

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
