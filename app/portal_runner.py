from __future__ import annotations

import json
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


class PortalRunnerError(RuntimeError):
    pass


class TaxPortalRunner:
    def __init__(self, config: AppConfig, state_store: StateStore, submit: bool) -> None:
        self.config = config
        self.state_store = state_store
        self.submit = submit
        self.syncer = PortalWorkbookSyncer(config)
        if config.portal_user_data_dir is None:
            raise ValueError("TAX_PORTAL_USER_DATA_DIR is required for portal runner commands.")

    def run(self, stores: list[StoreConfig]) -> list[PortalIssueResult]:
        sync_playwright = self._load_sync_playwright()
        results: list[PortalIssueResult] = []
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.config.portal_user_data_dir),
                channel=self.config.portal_browser_channel or None,
                headless=self.config.portal_headless,
                slow_mo=max(self.config.portal_slow_mo_ms, 0) or None,
            )
            context.set_default_timeout(self.config.portal_action_timeout_ms)
            home_page = context.pages[0] if context.pages else context.new_page()
            for store in stores:
                results.append(self._run_store(context, home_page, store))
            context.close()
        return results

    def _run_store(self, context: object, home_page: object, store: StoreConfig) -> PortalIssueResult:
        if self.config.portal_sync_from_server:
            self.state_store.update_portal_issue_state(
                store.store_key,
                current_step="sync_workbook",
                last_status="running",
            )
            self.syncer.sync_store_workbook(store)
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
        self.state_store.update_portal_issue_state(
            store.store_key,
            current_step=result.step,
            last_status=result.status,
            workbook_sha256=result.workbook_sha256,
        )
        try:
            self._goto(self.config.portal_home_url, home_page)
            self._ensure_logged_in(home_page, result)
            self._ensure_company(home_page, store, result)
            batch_page = context.new_page()
            batch_page.set_default_timeout(self.config.portal_action_timeout_ms)
            try:
                self._goto(self.config.portal_batch_issue_url, batch_page)
                self._wait_for_batch_page(batch_page, store, result)
                self._assert_batch_page_clean(batch_page)
                self._import_workbook(batch_page, store.output_xlsx_path, rows, summary)
                result.step = "validated"
                self.state_store.update_portal_issue_state(
                    store.store_key,
                    current_step=result.step,
                    last_status="validated",
                    workbook_sha256=result.workbook_sha256,
                )
                if not self.submit:
                    result.status = "validated"
                    return self._finalize_result(result)

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
            self._capture_artifact(home_page, result.artifacts_dir, f"{store.store_key}-failure")
            return self._finalize_result(result)

    def _finalize_result(self, result: PortalIssueResult) -> PortalIssueResult:
        result.finalize()
        history_id = self.state_store.record_portal_issue_result(result)
        self.state_store.update_portal_issue_state(
            result.store_key,
            current_step=result.step,
            last_status=result.status,
            workbook_sha256=result.workbook_sha256,
            error=result.error,
            last_history_id=history_id,
        )
        return result

    def _ensure_logged_in(self, page: object, result: PortalIssueResult) -> None:
        if self._is_home_page(page):
            return
        deadline = monotonic() + self.config.portal_login_timeout_minutes * 60
        refreshed_qr = False
        print("Tax portal login required. Waiting for successful login...")
        while monotonic() < deadline:
            if self._is_home_page(page):
                return
            if self._is_login_page(page) and not refreshed_qr and self._page_contains(page, "刷新"):
                self._click_text(page, "刷新")
                refreshed_qr = True
            sleep(2)
        self._capture_artifact(page, result.artifacts_dir, "login-timeout")
        raise PortalRunnerError("Timed out waiting for tax portal login.")

    def _ensure_company(self, home_page: object, store: StoreConfig, result: PortalIssueResult) -> None:
        verify_name = store.effective_portal_company_verify_name()
        if self._page_contains(home_page, verify_name):
            return
        result.step = "switch_company"
        self.state_store.update_portal_issue_state(
            store.store_key,
            current_step=result.step,
            last_status="running",
            workbook_sha256=result.workbook_sha256,
        )
        self._goto(self.config.portal_identity_switch_url, home_page)
        self._wait_for_text(home_page, "企业办税")
        switch_name = store.effective_portal_company_switch_name()
        try:
            home_page.get_by_placeholder("请输入纳税人名称").fill(switch_name)
            home_page.get_by_role("button", name="查询").click()
        except Exception:
            pass
        row = home_page.locator("tr").filter(has_text=switch_name).first
        row.get_by_role("button", name="切换").click()
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
        self._goto(self.config.portal_home_url, home_page)
        self._wait_until(
            lambda: self._page_contains(home_page, verify_name),
            timeout_seconds=20,
            message=f"switch company to {verify_name}",
        )

    def _wait_for_batch_page(self, page: object, store: StoreConfig, result: PortalIssueResult) -> None:
        result.step = "open_batch_page"
        self.state_store.update_portal_issue_state(
            store.store_key,
            current_step=result.step,
            last_status="running",
            workbook_sha256=result.workbook_sha256,
        )
        self._wait_for_text(page, "批量开票")
        self._wait_for_text(page, store.effective_portal_company_verify_name())

    def _assert_batch_page_clean(self, page: object) -> None:
        body_text = self._body_text(page)
        if "共 0 条" not in body_text:
            raise PortalRunnerError("Portal batch page is not clean; refusing to import into a non-empty table.")
        if "重新选择" in body_text:
            raise PortalRunnerError("Portal batch page already has a selected workbook; refusing to continue.")

    def _import_workbook(
        self,
        page: object,
        workbook_path: Path,
        rows: list[PortalIssueRow],
        summary: object,
    ) -> None:
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

    def _is_home_page(self, page: object) -> bool:
        url = page.url
        return "etax.xiamen.chinatax.gov.cn:8443/loginb/" in url and self._page_contains(page, "我要办税")

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
