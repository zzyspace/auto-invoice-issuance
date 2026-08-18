from __future__ import annotations

import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.models import AppConfig, PortalIssueDetail, StoreConfig
from app.photos_qr_cleanup import ImportedPhotosQr, PhotosQrCleanupError
from app.portal_runner import (
    BROWSER_CLICK_DELAY_MS,
    HOME_PAGE_NETWORKIDLE_GRACE_MS,
    QR_REFRESH_GRACE_SECONDS,
    PortalRunnerError,
    TaxPortalRunner,
    _RawBatchSession,
)
from app.state import StateStore


class PortalRunnerUrlTests(unittest.TestCase):
    @staticmethod
    def _imported_qr_for_test() -> ImportedPhotosQr:
        return ImportedPhotosQr(
            qr_path=Path("/tmp/login-qr-123.png"),
            asset_id="photos-asset-123",
            original_filename="login-qr-123.png",
            sha256="a" * 64,
            width=240,
            height=240,
        )

    def test_cleanup_imported_login_qr_deletes_only_recorded_asset(self) -> None:
        runner = object.__new__(TaxPortalRunner)
        imported_qr = self._imported_qr_for_test()

        with patch(
            "app.portal_runner.delete_imported_qr_from_photos",
            return_value="deleted",
        ) as mocked_delete:
            with patch.object(runner, "_log") as mocked_log:
                runner._cleanup_imported_login_qrs([imported_qr], "fuzzy")  # noqa: SLF001

        mocked_delete.assert_called_once_with(imported_qr)
        self.assertIn("deleted verified imported login QR", mocked_log.call_args.args[1])

    def test_cleanup_imported_login_qr_is_nonfatal_when_identity_cannot_be_verified(self) -> None:
        runner = object.__new__(TaxPortalRunner)
        imported_qr = self._imported_qr_for_test()

        with patch(
            "app.portal_runner.delete_imported_qr_from_photos",
            side_effect=PhotosQrCleanupError("checksum mismatch"),
        ):
            with patch.object(runner, "_log") as mocked_log:
                runner._cleanup_imported_login_qrs([imported_qr], "fuzzy")  # noqa: SLF001

        self.assertIn("kept imported login QR", mocked_log.call_args.args[1])

    def test_ensure_logged_in_cleans_recorded_qr_only_after_authentication(self) -> None:
        runner = object.__new__(TaxPortalRunner)
        runner.config = SimpleNamespace(
            portal_login_timeout_minutes=1,
            portal_browser_backend="playwright",
        )
        imported_qr = self._imported_qr_for_test()
        page = SimpleNamespace(url="https://tpass.example/#/login")
        authenticated_page = object()
        result = SimpleNamespace(
            artifacts_dir=None,
            store_key="fuzzy",
            portal_company_role="legal_representative",
        )
        monotonic_values = iter([0.0, 1.0, 2.0, 3.0])

        with patch("app.portal_runner.monotonic", side_effect=lambda: next(monotonic_values)):
            with patch("app.portal_runner.sleep", return_value=None):
                with patch.object(
                    runner,
                    "_confirmed_authenticated_page",
                    side_effect=[None, None, authenticated_page],
                ):
                    with patch.object(runner, "_is_public_landing_page", return_value=False):
                        with patch.object(runner, "_page_requires_reauth", return_value=True):
                            with patch.object(runner, "_is_login_page", return_value=True):
                                with patch.object(runner, "_local_app_login_enabled", return_value=True):
                                    with patch.object(
                                        runner,
                                        "_attempt_local_app_login",
                                        return_value=imported_qr,
                                    ):
                                        with patch.object(runner, "_cleanup_imported_login_qrs") as mocked_cleanup:
                                            with patch.object(runner, "_log"):
                                                returned = runner._ensure_logged_in(page, result)  # noqa: SLF001

        self.assertIs(authenticated_page, returned)
        mocked_cleanup.assert_called_once_with([imported_qr], "fuzzy")

    def test_ensure_logged_in_keeps_recorded_qr_when_login_times_out(self) -> None:
        runner = object.__new__(TaxPortalRunner)
        runner.config = SimpleNamespace(
            portal_login_timeout_minutes=1,
            portal_browser_backend="playwright",
        )
        imported_qr = self._imported_qr_for_test()
        page = SimpleNamespace(url="https://tpass.example/#/login")
        result = SimpleNamespace(
            artifacts_dir=None,
            store_key="fuzzy",
            portal_company_role="legal_representative",
        )
        monotonic_values = iter([0.0, 1.0, 2.0, 61.0])

        with patch("app.portal_runner.monotonic", side_effect=lambda: next(monotonic_values)):
            with patch("app.portal_runner.sleep", return_value=None):
                with patch.object(runner, "_confirmed_authenticated_page", return_value=None):
                    with patch.object(runner, "_is_public_landing_page", return_value=False):
                        with patch.object(runner, "_page_requires_reauth", return_value=True):
                            with patch.object(runner, "_is_login_page", return_value=True):
                                with patch.object(runner, "_local_app_login_enabled", return_value=True):
                                    with patch.object(
                                        runner,
                                        "_attempt_local_app_login",
                                        return_value=imported_qr,
                                    ):
                                        with patch.object(runner, "_cleanup_imported_login_qrs") as mocked_cleanup:
                                            with patch.object(runner, "_capture_artifact"):
                                                with patch.object(runner, "_log"):
                                                    with self.assertRaisesRegex(PortalRunnerError, "Timed out"):
                                                        runner._ensure_logged_in(page, result)  # noqa: SLF001

        mocked_cleanup.assert_not_called()

    def test_update_store_step_logs_and_persists_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state_store = StateStore(tmp_path / "state.db")
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
            )
            runner = TaxPortalRunner(config, state_store, submit=False)

            with patch("builtins.print") as mocked_print:
                runner._update_store_step(  # noqa: SLF001
                    "fuzzy",
                    "prepare_workbook",
                    "running",
                    workbook_sha256="abc123",
                    message="loaded workbook rows=2",
                )

            mocked_print.assert_called_once_with(
                "[tax-portal][fuzzy] step=prepare_workbook status=running loaded workbook rows=2",
                flush=True,
            )
            with sqlite3.connect(tmp_path / "state.db") as connection:
                row = connection.execute(
                    "SELECT store_key, last_status, current_step, workbook_sha256 FROM portal_issue_state"
                ).fetchone()
            self.assertEqual(("fuzzy", "running", "prepare_workbook", "abc123"), row)

    def test_launch_args_disable_proxy_only_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            base_kwargs = dict(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
            )
            state_store = StateStore(tmp_path / "state.db")

            disabled_runner = TaxPortalRunner(
                AppConfig(**base_kwargs, portal_disable_proxy=True),
                state_store,
                submit=False,
            )
            enabled_runner = TaxPortalRunner(
                AppConfig(**base_kwargs, portal_disable_proxy=False),
                state_store,
                submit=False,
            )

            self.assertEqual(["--no-proxy-server", "--proxy-bypass-list=*"], disabled_runner._launch_args())  # noqa: SLF001
            self.assertEqual([], enabled_runner._launch_args())  # noqa: SLF001

    def test_init_allows_chrome_cdp_backend_without_portal_user_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=None,
                portal_browser_backend="chrome_cdp",
                portal_chrome_cdp_url="http://127.0.0.1:9222",
            )

            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)

            self.assertEqual("chrome_cdp", runner.config.portal_browser_backend)

    def test_resolve_attached_home_page_reuses_existing_portal_page_before_opening_new_tab(self) -> None:
        class FakeLocator:
            def __init__(self, page: "FakePage") -> None:
                self.page = page

            def inner_text(self) -> str:
                return self.page.body_text

        class FakePage:
            def __init__(self, url: str, body_text: str) -> None:
                self.url = url
                self.body_text = body_text

            def locator(self, selector: str) -> FakeLocator:
                return FakeLocator(self)

        class FakeContext:
            def __init__(self, pages: list[FakePage]) -> None:
                self.pages = pages

            def new_page(self) -> object:
                raise AssertionError("new_page should not be called when a portal page is already attached")

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=None,
                portal_browser_backend="chrome_cdp",
                portal_chrome_cdp_url="http://127.0.0.1:9222",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            new_tab_page = FakePage("chrome://new-tab-page/", "")
            login_page = FakePage(
                "https://tpass.xiamen.chinatax.gov.cn:8443/#/login",
                "打开电子税务局APP扫一扫",
            )

            resolved_page = runner._resolve_attached_home_page(  # noqa: SLF001
                FakeContext([new_tab_page, login_page])
            )

            self.assertIs(login_page, resolved_page)

    def test_find_attached_portal_page_requires_matching_portal_area(self) -> None:
        class FakeLocator:
            def __init__(self, page: "FakePage") -> None:
                self.page = page

            def inner_text(self) -> str:
                return self.page.body_text

        class FakePage:
            def __init__(self, url: str, body_text: str) -> None:
                self.url = url
                self.body_text = body_text

            def locator(self, selector: str) -> FakeLocator:
                return FakeLocator(self)

        class FakeContext:
            def __init__(self, pages: list[FakePage]) -> None:
                self.pages = pages

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=None,
                portal_browser_backend="chrome_cdp",
                portal_chrome_cdp_url="http://127.0.0.1:9222",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            wrong_area_home_page = FakePage(
                "https://etax.xiamen.chinatax.gov.cn:8443/loginb/",
                "首页 我要办税 厦门市思明区浮几创意餐厅",
            )
            store = StoreConfig(
                store_key="fuzzy_qz",
                store_name="FuzzyQZ",
                survey_id="1",
                output_xlsx_path=tmp_path / "output.xlsx",
                initial_last_processed_id=1,
                portal_enabled=True,
                portal_company_switch_name="泉州市鲤城区浮几餐饮店（个体工商户）",
                portal_company_verify_name="泉州市鲤城区浮几餐饮店（个体工商户）",
                portal_area="quanzhou",
                portal_area_name="泉州",
            )

            resolved_page = runner._find_attached_portal_page(  # noqa: SLF001
                FakeContext([wrong_area_home_page]),
                store,
            )

            self.assertIsNone(resolved_page)

    def test_run_with_attached_chrome_stops_after_any_store_failure(self) -> None:
        class FakePage:
            def __init__(self, url: str, body_text: str) -> None:
                self.url = url
                self.body_text = body_text

            def locator(self, selector: str) -> object:
                class FakeLocator:
                    def __init__(self, page: "FakePage") -> None:
                        self.page = page

                    def inner_text(self) -> str:
                        return self.page.body_text

                return FakeLocator(self)

            def close(self) -> None:
                self.closed = True

        class FakeContext:
            def __init__(self, page: FakePage) -> None:
                self.pages = [page]
                self.timeout = None

            def set_default_timeout(self, timeout: int) -> None:
                self.timeout = timeout

            def new_page(self) -> FakePage:
                raise AssertionError("new_page should not be called when an existing home page tab is available")

        class FakeBrowser:
            def __init__(self, page: FakePage) -> None:
                self.context = FakeContext(page)
                self.contexts = [self.context]

        class FakeChromium:
            def __init__(self, browser: FakeBrowser) -> None:
                self.browser = browser
                self.received_cdp_url: str | None = None

            def connect_over_cdp(self, cdp_url: str) -> FakeBrowser:
                self.received_cdp_url = cdp_url
                return self.browser

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=None,
                portal_browser_backend="chrome_cdp",
                portal_chrome_cdp_url="http://127.0.0.1:9333",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            existing_home_page = FakePage(
                "https://etax.xiamen.chinatax.gov.cn:8443/loginb/",
                "首页 我要办税 厦门市思明区浮几创意餐厅",
            )
            browser = FakeBrowser(existing_home_page)
            chromium = FakeChromium(browser)
            playwright = SimpleNamespace(chromium=chromium)
            store = StoreConfig(
                store_key="fuzzy",
                store_name="Fuzzy",
                survey_id="1",
                output_xlsx_path=tmp_path / "output.xlsx",
                initial_last_processed_id=1,
                portal_enabled=True,
                portal_company_switch_name="厦门市思明区浮几创意餐厅",
                portal_company_verify_name="厦门市思明区浮几创意餐厅",
            )
            next_store = StoreConfig(
                store_key="peanut",
                store_name="Peanut",
                survey_id="2",
                output_xlsx_path=tmp_path / "peanut.xlsx",
                initial_last_processed_id=1,
                portal_enabled=True,
                portal_company_verify_name="厦门市思明区花生创意餐厅（个体工商户）",
            )
            failed_result = SimpleNamespace(
                status="failed",
                submitted_count=0,
                store_key="fuzzy",
                error="select-all did not finish",
            )

            with patch.object(runner, "_install_network_diag"):
                with patch.object(runner, "_run_store", return_value=failed_result) as mocked_run:
                    with patch.object(runner, "_log") as mocked_log:
                        results = runner._run_with_attached_chrome(  # noqa: SLF001
                            playwright,
                            [store, next_store],
                        )

            self.assertEqual("http://127.0.0.1:9333", chromium.received_cdp_url)
            self.assertEqual(config.portal_action_timeout_ms, browser.context.timeout)
            mocked_run.assert_called_once_with(browser.context, existing_home_page, store)
            self.assertEqual(1, len(results))
            self.assertIn(
                "stopping remaining stores because the previous store did not finish safely",
                [call.args[1].split(" store_key=")[0] for call in mocked_log.call_args_list],
            )

    def test_run_with_attached_chrome_prefers_existing_home_page_tab(self) -> None:
        class FakePage:
            def __init__(self, url: str, body_text: str) -> None:
                self.url = url
                self.body_text = body_text

            def locator(self, selector: str) -> object:
                class FakeLocator:
                    def __init__(self, page: "FakePage") -> None:
                        self.page = page

                    def inner_text(self) -> str:
                        return self.page.body_text

                return FakeLocator(self)

        class FakeContext:
            def __init__(self, page: FakePage) -> None:
                self.pages = [page]
                self.timeout = None

            def set_default_timeout(self, timeout: int) -> None:
                self.timeout = timeout

            def new_page(self) -> FakePage:
                raise AssertionError("new_page should not be called when an existing home page tab is available")

        class FakeBrowser:
            def __init__(self, context: FakeContext) -> None:
                self.contexts = [context]

        class FakeChromium:
            def __init__(self, browser: FakeBrowser) -> None:
                self.browser = browser

            def connect_over_cdp(self, cdp_url: str) -> FakeBrowser:
                self.received_cdp_url = cdp_url
                return self.browser

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=None,
                portal_browser_backend="chrome_cdp",
                portal_chrome_cdp_url="http://127.0.0.1:9333",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            existing_home_page = FakePage(
                "https://etax.xiamen.chinatax.gov.cn:8443/loginb/",
                "首页 我要办税 厦门市思明区浮几创意餐厅",
            )
            context = FakeContext(existing_home_page)
            chromium = FakeChromium(FakeBrowser(context))
            playwright = SimpleNamespace(chromium=chromium)
            store = StoreConfig(
                store_key="fuzzy",
                store_name="Fuzzy",
                survey_id="1",
                output_xlsx_path=tmp_path / "output.xlsx",
                initial_last_processed_id=1,
                portal_enabled=True,
                portal_company_switch_name="厦门市思明区浮几创意餐厅",
                portal_company_verify_name="厦门市思明区浮几创意餐厅",
            )

            with patch.object(runner, "_install_network_diag"):
                with patch.object(runner, "_run_store", return_value=SimpleNamespace(status="validated")) as mocked_run:
                    runner._run_with_attached_chrome(playwright, [store])  # noqa: SLF001

            mocked_run.assert_called_once_with(context, existing_home_page, store)

    def test_run_with_attached_chrome_reconnects_before_next_store_after_raw_transport_stop(self) -> None:
        class FakePage:
            def __init__(self, url: str, body_text: str) -> None:
                self.url = url
                self.body_text = body_text

            def locator(self, selector: str) -> object:
                class FakeLocator:
                    def __init__(self, page: "FakePage") -> None:
                        self.page = page

                    def inner_text(self) -> str:
                        return self.page.body_text

                return FakeLocator(self)

        class FakeContext:
            def __init__(self, page: FakePage) -> None:
                self.pages = [page]
                self.timeout = None

            def set_default_timeout(self, timeout: int) -> None:
                self.timeout = timeout

            def new_page(self) -> FakePage:
                raise AssertionError("a matching portal page should be reused")

        class FakeBrowser:
            def __init__(self, page: FakePage) -> None:
                self.contexts = [FakeContext(page)]

        class FakeChromium:
            def __init__(self, browser: FakeBrowser) -> None:
                self.browser = browser
                self.connect_count = 0

            def connect_over_cdp(self, cdp_url: str) -> FakeBrowser:
                self.connect_count += 1
                return self.browser

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=None,
                portal_browser_backend="chrome_cdp",
                portal_chrome_cdp_url="http://127.0.0.1:9333",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            xiamen_page = FakePage(
                "https://etax.xiamen.chinatax.gov.cn:8443/loginb/",
                "首页 我要办税 厦门市思明区浮几创意餐厅",
            )
            fujian_page = FakePage(
                "https://etax.fujian.chinatax.gov.cn:8443/loginb/",
                "首页 我要办税 泉州市鲤城区浮几餐饮店（个体工商户）",
            )
            initial_chromium = FakeChromium(FakeBrowser(xiamen_page))
            peanut_chromium = FakeChromium(FakeBrowser(xiamen_page))
            fujian_chromium = FakeChromium(FakeBrowser(fujian_page))
            initial_playwright = SimpleNamespace(chromium=initial_chromium)
            restarted_playwrights = iter(
                [
                    SimpleNamespace(chromium=peanut_chromium, stop=lambda: None),
                    SimpleNamespace(chromium=fujian_chromium, stop=lambda: None),
                ]
            )
            runner._sync_playwright_factory = lambda: SimpleNamespace(  # noqa: SLF001
                start=lambda: next(restarted_playwrights)
            )
            stores = [
                StoreConfig(
                    store_key="fuzzy",
                    store_name="Fuzzy",
                    survey_id="1",
                    output_xlsx_path=tmp_path / "fuzzy.xlsx",
                    initial_last_processed_id=1,
                    portal_enabled=True,
                    portal_area="xiamen",
                    portal_company_verify_name="厦门市思明区浮几创意餐厅",
                ),
                StoreConfig(
                    store_key="peanut",
                    store_name="Peanut",
                    survey_id="2",
                    output_xlsx_path=tmp_path / "peanut.xlsx",
                    initial_last_processed_id=1,
                    portal_enabled=True,
                    portal_area="xiamen",
                    portal_company_verify_name="厦门市思明区花生创意餐厅（个体工商户）",
                ),
                StoreConfig(
                    store_key="fuzzy_qz",
                    store_name="FuzzyQZ",
                    survey_id="3",
                    output_xlsx_path=tmp_path / "fuzzy_qz.xlsx",
                    initial_last_processed_id=1,
                    portal_enabled=True,
                    portal_area="fujian",
                    portal_company_verify_name="泉州市鲤城区浮几餐饮店（个体工商户）",
                ),
            ]

            def fake_run_store(context: object, page: object, store: StoreConfig) -> object:
                if store.store_key in {"fuzzy", "peanut"}:
                    runner._stop_attached_playwright_transport()  # noqa: SLF001
                return SimpleNamespace(status="validated")

            with patch.object(runner, "_install_network_diag"):
                with patch.object(runner, "_run_store", side_effect=fake_run_store) as mocked_run:
                    results = runner._run_with_attached_chrome(initial_playwright, stores)  # noqa: SLF001

            self.assertEqual(3, len(results))
            self.assertEqual(1, peanut_chromium.connect_count)
            self.assertEqual(1, fujian_chromium.connect_count)
            self.assertIs(initial_chromium.browser.contexts[0], mocked_run.call_args_list[0].args[0])
            self.assertIs(xiamen_page, mocked_run.call_args_list[0].args[1])
            self.assertIs(peanut_chromium.browser.contexts[0], mocked_run.call_args_list[1].args[0])
            self.assertIs(xiamen_page, mocked_run.call_args_list[1].args[1])
            self.assertIs(fujian_chromium.browser.contexts[0], mocked_run.call_args_list[2].args[0])
            self.assertIs(fujian_page, mocked_run.call_args_list[2].args[1])

    def test_run_with_attached_chrome_falls_back_to_new_home_page_tab(self) -> None:
        class FakePage:
            def __init__(self, url: str, body_text: str) -> None:
                self.url = url
                self.body_text = body_text
                self.closed = False

            def locator(self, selector: str) -> object:
                class FakeLocator:
                    def __init__(self, page: "FakePage") -> None:
                        self.page = page

                    def inner_text(self) -> str:
                        return self.page.body_text

                return FakeLocator(self)

            def close(self) -> None:
                self.closed = True

        class FakeContext:
            def __init__(self) -> None:
                self.pages = [FakePage("chrome://new-tab-page/", "")]
                self.timeout = None
                self.created_page: FakePage | None = None

            def set_default_timeout(self, timeout: int) -> None:
                self.timeout = timeout

            def new_page(self) -> FakePage:
                self.created_page = FakePage(
                    "https://etax.xiamen.chinatax.gov.cn:8443/loginb/",
                    "首页 我要办税 厦门市思明区浮几创意餐厅",
                )
                return self.created_page

        class FakeBrowser:
            def __init__(self, context: FakeContext) -> None:
                self.contexts = [context]

        class FakeChromium:
            def __init__(self, browser: FakeBrowser) -> None:
                self.browser = browser

            def connect_over_cdp(self, cdp_url: str) -> FakeBrowser:
                return self.browser

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=None,
                portal_browser_backend="chrome_cdp",
                portal_chrome_cdp_url="http://127.0.0.1:9333",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            chromium = FakeChromium(FakeBrowser(FakeContext()))
            playwright = SimpleNamespace(chromium=chromium)
            store = StoreConfig(
                store_key="fuzzy",
                store_name="Fuzzy",
                survey_id="1",
                output_xlsx_path=tmp_path / "output.xlsx",
                initial_last_processed_id=1,
                portal_enabled=True,
                portal_company_switch_name="厦门市思明区浮几创意餐厅",
                portal_company_verify_name="厦门市思明区浮几创意餐厅",
            )

            with patch.object(runner, "_install_network_diag"):
                with patch.object(runner, "_run_store", return_value=SimpleNamespace(status="validated")) as mocked_run:
                    results = runner._run_with_attached_chrome(playwright, [store])  # noqa: SLF001

            created_page = chromium.browser.contexts[0].created_page
            self.assertIsNotNone(created_page)
            self.assertTrue(created_page.closed)
            mocked_run.assert_called_once_with(chromium.browser.contexts[0], created_page, store)
            self.assertEqual(1, len(results))

    def test_sync_portal_profile_from_chrome_copies_session_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            source_dir = tmp_path / "chrome-profile"
            target_dir = tmp_path / "playwright-profile"
            (source_dir / "Local Storage").mkdir(parents=True)
            (source_dir / "Cookies").write_text("cookie-data", encoding="utf-8")
            (source_dir / "Local Storage" / "leveldb.txt").write_text("local-storage", encoding="utf-8")
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=target_dir,
                portal_sync_from_chrome_profile=True,
                portal_chrome_profile_dir=source_dir,
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)

            with patch.object(runner, "_log"):
                runner._sync_portal_profile_from_chrome()  # noqa: SLF001

            self.assertEqual("cookie-data", (target_dir / "Default" / "Cookies").read_text(encoding="utf-8"))
            self.assertEqual(
                "local-storage",
                (target_dir / "Default" / "Local Storage" / "leveldb.txt").read_text(encoding="utf-8"),
            )

    def test_is_home_page_requires_loginb_workbench(self) -> None:
        class FakeLocator:
            def inner_text(self) -> str:
                return "首页 我要办税 我要查询"

        class FakePage:
            url = "https://etax.xiamen.chinatax.gov.cn:8443/loginb/"

            def locator(self, selector: str) -> FakeLocator:
                self.last_selector = selector
                return FakeLocator()

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)

            self.assertTrue(runner._is_home_page(FakePage()))  # noqa: SLF001

    def test_is_home_page_accepts_authenticated_etax_page_outside_loginb_path(self) -> None:
        class FakeLocator:
            def inner_text(self) -> str:
                return "首页 我要办税 我要查询"

        class FakePage:
            url = "https://etax.xiamen.chinatax.gov.cn:8443/workbench/home"

            def locator(self, selector: str) -> FakeLocator:
                self.last_selector = selector
                return FakeLocator()

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)

            self.assertTrue(runner._is_home_page(FakePage()))  # noqa: SLF001

    def test_public_landing_page_is_not_treated_as_home_page(self) -> None:
        class FakeLocator:
            def inner_text(self) -> str:
                return "环境检测 电子税务局APP下载 公众服务 办税指南 办税日历 登录"

        class FakePage:
            url = "https://etax.xiamen.chinatax.gov.cn:8443/"

            def locator(self, selector: str) -> FakeLocator:
                self.last_selector = selector
                return FakeLocator()

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)

            self.assertFalse(runner._is_home_page(FakePage()))  # noqa: SLF001
            self.assertTrue(runner._is_public_landing_page(FakePage()))  # noqa: SLF001

    def test_loginb_page_without_workbench_text_requires_reauth(self) -> None:
        class FakeLocator:
            def inner_text(self) -> str:
                return "欢迎登录 厦门市电子税务局"

        class FakePage:
            url = "https://etax.xiamen.chinatax.gov.cn:8443/loginb/"

            def locator(self, selector: str) -> FakeLocator:
                self.last_selector = selector
                return FakeLocator()

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)

            self.assertTrue(runner._page_requires_reauth(FakePage()))  # noqa: SLF001

    def test_ensure_logged_in_clicks_login_on_public_landing_page(self) -> None:
        class FakeLocator:
            def __init__(self, page: "FakePage") -> None:
                self.page = page

            def inner_text(self) -> str:
                return self.page.body_text

        class FakePage:
            def __init__(self) -> None:
                self.url = "https://etax.xiamen.chinatax.gov.cn:8443/"
                self.body_text = "环境检测 电子税务局APP下载 公众服务 办税指南 办税日历 登录"

            def locator(self, selector: str) -> FakeLocator:
                return FakeLocator(self)

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
                portal_login_timeout_minutes=1,
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            result = type("FakeResult", (), {"artifacts_dir": None, "store_key": "fuzzy"})()
            page = FakePage()

            def fake_open_login(page_obj: FakePage) -> None:
                page_obj.url = "https://etax.xiamen.chinatax.gov.cn:8443/loginb/"
                page_obj.body_text = "首页 我要办税 我要查询"

            with patch("app.portal_runner.sleep", lambda _: None):
                with patch("builtins.print") as mocked_print:
                    with patch.object(
                        runner,
                        "_open_login_from_public_landing",
                        side_effect=fake_open_login,
                    ) as mocked_open:
                        runner._ensure_logged_in(page, result)  # noqa: SLF001

            mocked_open.assert_called_once()
            mocked_print.assert_any_call(
                "[tax-portal][fuzzy] login required; waiting for successful login...",
                flush=True,
            )

    def test_ensure_logged_in_returns_authenticated_page_from_another_tab(self) -> None:
        class FakeLocator:
            def __init__(self, page: "FakePage") -> None:
                self.page = page

            def inner_text(self) -> str:
                return self.page.body_text

        class FakePage:
            def __init__(self, url: str, body_text: str) -> None:
                self.url = url
                self.body_text = body_text
                self.context = None

            def locator(self, selector: str) -> FakeLocator:
                return FakeLocator(self)

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            login_page = FakePage(
                "https://tpass.xiamen.chinatax.gov.cn:8443/#/login",
                "打开电子税务局APP扫一扫",
            )
            home_page = FakePage(
                "https://etax.xiamen.chinatax.gov.cn:8443/workbench/home",
                "首页 我要办税 我要查询",
            )
            context = SimpleNamespace(pages=[login_page, home_page])
            login_page.context = context
            home_page.context = context
            result = type("FakeResult", (), {"artifacts_dir": None, "store_key": "fuzzy"})()

            authenticated_page = runner._ensure_logged_in(login_page, result)  # noqa: SLF001

            self.assertIs(home_page, authenticated_page)

    def test_ensure_logged_in_ignores_authenticated_page_from_other_portal_area(self) -> None:
        class FakeLocator:
            def __init__(self, page: "FakePage") -> None:
                self.page = page

            def inner_text(self) -> str:
                return self.page.body_text

        class FakePage:
            def __init__(self, url: str, body_text: str) -> None:
                self.url = url
                self.body_text = body_text
                self.context = None

            def locator(self, selector: str) -> FakeLocator:
                return FakeLocator(self)

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
                portal_browser_backend="chrome_cdp",
                portal_login_timeout_minutes=1,
                portal_etax_app_username="demo-user",
                portal_etax_app_password="demo-pass",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            login_page = FakePage(
                "https://tpass.fujian.chinatax.gov.cn:8443/#/login",
                "打开电子税务局APP扫一扫",
            )
            other_area_home_page = FakePage(
                "https://etax.xiamen.chinatax.gov.cn:8443/workbench/home",
                "首页 我要办税 我要查询",
            )
            context = SimpleNamespace(pages=[login_page, other_area_home_page])
            login_page.context = context
            other_area_home_page.context = context
            result = type("FakeResult", (), {"artifacts_dir": None, "store_key": "fuzzy_qz", "portal_company_role": "legal_representative"})()
            store = StoreConfig(
                store_key="fuzzy_qz",
                store_name="FuzzyQZ",
                survey_id="1",
                output_xlsx_path=tmp_path / "output.xlsx",
                initial_last_processed_id=1,
                portal_enabled=True,
                portal_company_switch_name="泉州市鲤城区浮几餐饮店（个体工商户）（待确认）",
                portal_company_verify_name="泉州市鲤城区浮几餐饮店（个体工商户）",
                portal_area="fujian",
                portal_area_name="福建省",
            )

            monotonic_values = iter([0.0, 1.0, 2.0, 3.0])
            authenticated_page = object()

            with patch("app.portal_runner.monotonic", side_effect=lambda: next(monotonic_values)):
                with patch("app.portal_runner.sleep", lambda _: None):
                    with patch.object(runner, "_is_public_landing_page", return_value=False):
                        with patch.object(runner, "_page_requires_reauth", return_value=True):
                            with patch.object(runner, "_is_login_page", return_value=True):
                                with patch.object(runner, "_refresh_attached_authenticated_page", return_value=None):
                                    with patch.object(runner, "_attempt_local_app_login", return_value=True) as mocked_attempt:
                                        with patch.object(
                                            runner,
                                            "_confirmed_authenticated_page",
                                            side_effect=[None, None, None, authenticated_page],
                                        ):
                                            returned = runner._ensure_logged_in(login_page, result, store)  # noqa: SLF001

            self.assertIs(authenticated_page, returned)
            mocked_attempt.assert_called_once_with(login_page, result, store)

    def test_ensure_logged_in_adopts_new_same_area_tpass_page_before_local_scan(self) -> None:
        class FakePage:
            def __init__(self, url: str, body_text: str = "") -> None:
                self.url = url
                self.body_text = body_text
                self.context: object | None = None

            def locator(self, selector: str) -> object:
                return SimpleNamespace(inner_text=lambda: self.body_text)

        runner = object.__new__(TaxPortalRunner)
        runner.config = SimpleNamespace(
            portal_browser_backend="chrome_cdp",
            portal_login_timeout_minutes=1,
        )
        runner._observed_attached_pages = []  # noqa: SLF001
        runner._attached_cdp_connections = []  # noqa: SLF001
        runner._sync_playwright_factory = None  # noqa: SLF001
        loginb_page = FakePage("https://etax.fujian.chinatax.gov.cn:8443/loginb/")
        tpass_page = FakePage(
            "https://tpass.fujian.chinatax.gov.cn:8443/#/login?state=fujian",
            "打开电子税务局APP扫一扫",
        )
        context = SimpleNamespace(pages=[loginb_page, tpass_page])
        loginb_page.context = context
        tpass_page.context = context
        store = StoreConfig(
            store_key="fuzzy_qz",
            store_name="FuzzyQZ",
            survey_id="1",
            output_xlsx_path=Path("/tmp/fuzzy_qz.xlsx"),
            initial_last_processed_id=1,
            portal_enabled=True,
            portal_area="fujian",
        )
        result = SimpleNamespace(
            artifacts_dir=None,
            store_key="fuzzy_qz",
            portal_company_role="legal_representative",
        )
        authenticated_page = object()
        monotonic_values = iter([0.0, 1.0, 2.0, 3.0])

        with patch("app.portal_runner.monotonic", side_effect=lambda: next(monotonic_values)), patch(
            "app.portal_runner.sleep",
        ), patch.object(
            runner,
            "_confirmed_authenticated_page",
            side_effect=[None, None, None, authenticated_page],
        ), patch.object(
            runner,
            "_refresh_attached_authenticated_page",
            return_value=None,
        ), patch.object(
            runner,
            "_local_app_login_enabled",
            return_value=True,
        ), patch.object(
            runner,
            "_attempt_local_app_login",
            return_value=None,
        ) as mocked_attempt, patch.object(
            runner,
            "_cleanup_imported_login_qrs",
        ), patch.object(
            runner,
            "_log",
        ) as mocked_log:
            returned = runner._ensure_logged_in(loginb_page, result, store)  # noqa: SLF001

        self.assertIs(authenticated_page, returned)
        mocked_attempt.assert_called_once_with(tpass_page, result, store)
        self.assertIn(
            "adopting refreshed attached Chrome login page: " + tpass_page.url,
            [call.args[1] for call in mocked_log.call_args_list],
        )

    def test_blank_tpass_gateway_failure_detects_completed_empty_http_error_page(self) -> None:
        runner = object.__new__(TaxPortalRunner)
        target = {
            "type": "page",
            "url": (
                "https://tpass.fujian.chinatax.gov.cn:8443/api/v1.0/auth/oauth2/login"
                "?client_id=fujian"
            ),
        }
        store = SimpleNamespace(effective_portal_area=lambda: "fujian")
        snapshot = {
            "readyState": "complete",
            "title": target["url"],
            "bodyText": "",
            "navigationStatus": 412,
        }

        with patch.object(runner, "_raw_attached_targets", return_value=[target]), patch.object(
            runner,
            "_raw_cdp_evaluate",
            return_value=snapshot,
        ):
            failure = runner._blank_tpass_gateway_failure(store)  # noqa: SLF001

        self.assertEqual((target, snapshot), failure)

    def test_ensure_logged_in_restarts_dedicated_chrome_once_for_blank_gateway_page(self) -> None:
        runner = object.__new__(TaxPortalRunner)
        runner.config = SimpleNamespace(
            portal_browser_backend="chrome_cdp",
            portal_login_timeout_minutes=1,
        )
        store = SimpleNamespace(effective_portal_area=lambda: "fujian")
        result = SimpleNamespace(store_key="fuzzy_qz")
        original_page = object()
        restarted_page = object()
        authenticated_page = object()
        gateway_target = {
            "url": "https://tpass.fujian.chinatax.gov.cn:8443/api/v1.0/auth/oauth2/login"
        }
        gateway_snapshot = {"navigationStatus": 412}
        monotonic_values = iter([0.0, 1.0, 2.0])

        with patch("app.portal_runner.monotonic", side_effect=lambda: next(monotonic_values)), patch.object(
            runner,
            "_confirmed_authenticated_page",
            side_effect=[None, authenticated_page],
        ), patch.object(
            runner,
            "_blank_tpass_gateway_failure",
            side_effect=[(gateway_target, gateway_snapshot), None],
        ), patch.object(
            runner,
            "_restart_dedicated_chrome_after_gateway_error",
            return_value=restarted_page,
        ) as mocked_restart, patch.object(runner, "_log") as mocked_log:
            returned = runner._ensure_logged_in(original_page, result, store)  # noqa: SLF001

        self.assertIs(authenticated_page, returned)
        mocked_restart.assert_called_once_with(store)
        self.assertIn(
            "error: detected blank TPass OAuth API gateway challenge",
            [call.args[1].split(" url=")[0] for call in mocked_log.call_args_list],
        )

    def test_ensure_logged_in_does_not_restart_blank_gateway_page_twice(self) -> None:
        runner = object.__new__(TaxPortalRunner)
        runner.config = SimpleNamespace(
            portal_browser_backend="chrome_cdp",
            portal_login_timeout_minutes=1,
        )
        store = SimpleNamespace(effective_portal_area=lambda: "fujian")
        result = SimpleNamespace(store_key="fuzzy_qz")
        gateway_failure = (
            {"url": "https://tpass.fujian.chinatax.gov.cn:8443/api/v1.0/auth/oauth2/login"},
            {"navigationStatus": 412},
        )
        monotonic_values = iter([0.0, 1.0, 2.0])

        with patch("app.portal_runner.monotonic", side_effect=lambda: next(monotonic_values)), patch.object(
            runner,
            "_confirmed_authenticated_page",
            return_value=None,
        ), patch.object(
            runner,
            "_blank_tpass_gateway_failure",
            return_value=gateway_failure,
        ), patch.object(
            runner,
            "_restart_dedicated_chrome_after_gateway_error",
            return_value=object(),
        ) as mocked_restart, patch.object(runner, "_log"):
            with self.assertRaisesRegex(PortalRunnerError, "after one dedicated Chrome restart"):
                runner._ensure_logged_in(object(), result, store)  # noqa: SLF001

        mocked_restart.assert_called_once_with(store)

    def test_restart_dedicated_chrome_after_gateway_error_reuses_profile_and_store_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            runner = object.__new__(TaxPortalRunner)
            runner.config = SimpleNamespace(
                portal_chrome_cdp_user_data_dir=tmp_path / "cdp-profile",
                portal_chrome_executable_path=tmp_path / "Chrome",
                portal_action_timeout_ms=15000,
                portal_home_url_for_store=lambda store: (
                    "https://etax.fujian.chinatax.gov.cn:8443/loginb/"
                ),
            )
            runner._attached_cdp_url = "http://127.0.0.1:9222"  # noqa: SLF001
            runner._sync_playwright_factory = object()  # noqa: SLF001
            store = SimpleNamespace(store_key="fuzzy_qz")
            page = object()
            context = SimpleNamespace(
                pages=[page],
                set_default_timeout=lambda timeout: None,
            )
            browser = SimpleNamespace(contexts=[context])
            process = SimpleNamespace(pid=12345)
            endpoint_states = iter([True, False, False, True])
            monotonic_values = iter([0.0, 1.0, 2.0, 3.0, 4.0])

            with patch("app.portal_runner.monotonic", side_effect=lambda: next(monotonic_values)), patch(
                "app.portal_runner.sleep",
            ), patch.object(runner, "_stop_attached_playwright_transport"), patch(
                "app.portal_runner.subprocess.run",
                return_value=SimpleNamespace(returncode=0, stderr="", stdout=""),
            ) as mocked_run, patch(
                "app.portal_runner.subprocess.Popen",
                return_value=process,
            ) as mocked_popen, patch.object(
                runner,
                "_raw_cdp_endpoint_available",
                side_effect=lambda: next(endpoint_states),
            ), patch.object(
                runner,
                "_raw_attached_targets",
                return_value=[{"type": "page"}],
            ), patch.object(
                runner,
                "_restart_attached_cdp_transport",
                return_value=browser,
            ), patch.object(
                runner,
                "_find_attached_portal_page",
                return_value=page,
            ), patch.object(runner, "_log"):
                returned = runner._restart_dedicated_chrome_after_gateway_error(store)  # noqa: SLF001

            self.assertIs(page, returned)
            mocked_run.assert_called_once_with(
                [
                    "pkill",
                    "-f",
                    "--",
                    f"--user-data-dir={(tmp_path / 'cdp-profile').resolve()}",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            launch_args = mocked_popen.call_args.args[0]
            self.assertIn("--remote-debugging-port=9222", launch_args)
            self.assertIn(f"--user-data-dir={(tmp_path / 'cdp-profile').resolve()}", launch_args)
            self.assertEqual(
                "https://etax.fujian.chinatax.gov.cn:8443/loginb/",
                launch_args[-1],
            )

    def test_confirmed_authenticated_page_rechecks_load_redirect_to_login(self) -> None:
        class FakeLocator:
            def __init__(self, page: "FakePage") -> None:
                self.page = page

            def inner_text(self) -> str:
                return self.page.body_text

        class FakePage:
            def __init__(self) -> None:
                self.url = "https://etax.xiamen.chinatax.gov.cn:8443/loginb/"
                self.body_text = "首页 我要办税 我要查询"
                self.context = SimpleNamespace(pages=[self])

            def locator(self, selector: str) -> FakeLocator:
                return FakeLocator(self)

            def wait_for_load_state(self, state: str, timeout: int) -> None:
                if state == "load":
                    self.url = "https://tpass.xiamen.chinatax.gov.cn:8443/#/login"
                    self.body_text = "打开电子税务局APP扫一扫"

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)

            authenticated_page = runner._confirmed_authenticated_page(FakePage())  # noqa: SLF001

            self.assertIsNone(authenticated_page)

    def test_confirmed_authenticated_page_rejects_transient_authenticated_page_that_redirects_during_stability_window(
        self,
    ) -> None:
        class FakeLocator:
            def __init__(self, page: "FakePage") -> None:
                self.page = page

            def inner_text(self) -> str:
                return self.page.body_text

        class FakePage:
            def __init__(self) -> None:
                self.url = "https://etax.xiamen.chinatax.gov.cn:8443/loginb/"
                self.body_text = "首页 我要办税 我要查询"
                self.context = SimpleNamespace(pages=[self])
                self.wait_for_timeout_calls = 0

            def locator(self, selector: str) -> FakeLocator:
                return FakeLocator(self)

            def wait_for_load_state(self, state: str, timeout: int) -> None:
                return None

            def wait_for_timeout(self, timeout_ms: int) -> None:
                self.wait_for_timeout_calls += 1
                if self.wait_for_timeout_calls == 1:
                    self.url = "https://tpass.xiamen.chinatax.gov.cn:8443/#/login"
                    self.body_text = "打开电子税务局APP扫一扫"

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)

            monotonic_values = iter([0.0, 0.1])
            with patch("app.portal_runner.monotonic", side_effect=lambda: next(monotonic_values)):
                authenticated_page = runner._confirmed_authenticated_page(FakePage())  # noqa: SLF001

            self.assertIsNone(authenticated_page)

    def test_ensure_logged_in_returns_authenticated_shell_page_from_another_tab(self) -> None:
        class FakeLocator:
            def __init__(self, page: "FakePage") -> None:
                self.page = page

            def inner_text(self) -> str:
                return self.page.body_text

        class FakePage:
            def __init__(self, url: str, body_text: str) -> None:
                self.url = url
                self.body_text = body_text
                self.context = None

            def locator(self, selector: str) -> FakeLocator:
                return FakeLocator(self)

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            login_page = FakePage(
                "https://tpass.xiamen.chinatax.gov.cn:8443/#/login",
                "打开电子税务局APP扫一扫",
            )
            shell_page = FakePage(
                "https://etax.xiamen.chinatax.gov.cn:8443/workbench/home",
                "首页 我要查询 我的提醒",
            )
            context = SimpleNamespace(pages=[login_page, shell_page])
            login_page.context = context
            shell_page.context = context
            result = type("FakeResult", (), {"artifacts_dir": None, "store_key": "fuzzy"})()

            authenticated_page = runner._ensure_logged_in(login_page, result)  # noqa: SLF001

            self.assertIs(shell_page, authenticated_page)

    def test_ensure_logged_in_attempts_local_app_automation_once_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
                portal_login_timeout_minutes=1,
                portal_browser_backend="chrome_cdp",
                portal_etax_app_username="demo-user",
                portal_etax_app_password="demo-pass",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            result = SimpleNamespace(artifacts_dir=None, store_key="fuzzy", portal_company_role="legal_representative")
            page = SimpleNamespace(url="https://tpass.xiamen.chinatax.gov.cn:8443/#/login")
            home_page = object()

            monotonic_values = iter([0.0, 1.0, 2.0, 3.0])

            with patch("app.portal_runner.monotonic", side_effect=lambda: next(monotonic_values)):
                with patch("app.portal_runner.sleep", lambda _: None):
                    with patch.object(runner, "_confirmed_authenticated_page", side_effect=[None, None, None, home_page]):
                        with patch.object(runner, "_is_public_landing_page", return_value=False):
                            with patch.object(runner, "_page_requires_reauth", return_value=True):
                                with patch.object(runner, "_is_login_page", return_value=True):
                                    with patch.object(runner, "_refresh_attached_authenticated_page", return_value=None):
                                        with patch.object(runner, "_attempt_local_app_login", return_value=True) as mocked_attempt:
                                            authenticated_page = runner._ensure_logged_in(page, result)  # noqa: SLF001

            self.assertIs(home_page, authenticated_page)
            mocked_attempt.assert_called_once_with(page, result, None)

    def test_ensure_authenticated_home_page_normalizes_authenticated_shell_page(self) -> None:
        class FakeLocator:
            def __init__(self, page: "FakePage") -> None:
                self.page = page

            def inner_text(self) -> str:
                return self.page.body_text

        class FakePage:
            def __init__(self, url: str, body_text: str) -> None:
                self.url = url
                self.body_text = body_text

            def locator(self, selector: str) -> FakeLocator:
                return FakeLocator(self)

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            shell_page = FakePage(
                "https://etax.xiamen.chinatax.gov.cn:8443/workbench/home",
                "首页 我要查询 我的提醒",
            )
            result = SimpleNamespace(store_key="fuzzy")
            home_page = object()

            with patch.object(runner, "_navigate_with_reauth", return_value=home_page) as mocked_nav:
                normalized_page = runner._ensure_authenticated_home_page(shell_page, result)  # noqa: SLF001

            self.assertIs(home_page, normalized_page)
            mocked_nav.assert_called_once_with(
                shell_page,
                config.portal_home_url,
                result,
                store=None,
                expected_text="我要办税",
                step_name="open authenticated portal home",
            )

    def test_ensure_logged_in_does_not_refresh_qr_during_transient_post_scan_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
                portal_login_timeout_minutes=1,
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            result = type("FakeResult", (), {"artifacts_dir": None, "store_key": "fuzzy"})()
            page = object()
            home_page = object()

            monotonic_values = iter([0.0, 1.0, 2.0, 3.0])

            with patch("app.portal_runner.monotonic", side_effect=lambda: next(monotonic_values)):
                with patch("app.portal_runner.sleep", lambda _: None):
                    with patch.object(runner, "_confirmed_authenticated_page", side_effect=[None, None, home_page]):
                        with patch.object(runner, "_is_public_landing_page", side_effect=[True, False]):
                            with patch.object(runner, "_page_requires_reauth", return_value=True):
                                with patch.object(runner, "_open_login_from_public_landing") as mocked_open:
                                    with patch.object(runner, "_try_refresh_login_qr") as mocked_refresh:
                                        runner._ensure_logged_in(page, result)  # noqa: SLF001

            mocked_open.assert_called_once()
            mocked_refresh.assert_not_called()

    def test_ensure_logged_in_refreshes_qr_after_grace_period(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
                portal_login_timeout_minutes=1,
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            result = type("FakeResult", (), {"artifacts_dir": None, "store_key": "fuzzy"})()
            page = object()
            home_page = object()

            monotonic_values = iter([0.0, 1.0, 2.0, 3.0, 2.0 + QR_REFRESH_GRACE_SECONDS + 0.1, 8.0])

            with patch("app.portal_runner.monotonic", side_effect=lambda: next(monotonic_values)):
                with patch("app.portal_runner.sleep", lambda _: None):
                    with patch.object(runner, "_confirmed_authenticated_page", side_effect=[None, None, None, home_page]):
                        with patch.object(runner, "_is_public_landing_page", return_value=False):
                            with patch.object(runner, "_page_requires_reauth", return_value=True):
                                with patch.object(runner, "_try_refresh_login_qr") as mocked_refresh:
                                    runner._ensure_logged_in(page, result)  # noqa: SLF001

            mocked_refresh.assert_called_once_with(page)

    def test_ensure_logged_in_suppresses_oauth_restart_during_etax_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
                portal_login_timeout_minutes=1,
                portal_browser_backend="chrome_cdp",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            store = StoreConfig(
                store_key="fuzzy",
                store_name="Fuzzy",
                survey_id="1",
                output_xlsx_path=tmp_path / "output.xlsx",
                initial_last_processed_id=1,
                portal_enabled=True,
                portal_area="fujian",
            )
            result = SimpleNamespace(artifacts_dir=None, store_key="fuzzy")
            login_url = "https://tpass.fujian.chinatax.gov.cn:8443/#/login?state=original"
            page = SimpleNamespace(url=login_url)
            home_page = object()

            monotonic_values = iter(
                [
                    0.0,
                    0.5,
                    1.0,
                    1.5,
                    1.0 + QR_REFRESH_GRACE_SECONDS + 0.1,
                    12.0,
                ]
            )
            confirmed_pages = [None, None, None, None, None, home_page]

            with patch("app.portal_runner.monotonic", side_effect=lambda: next(monotonic_values)):
                with patch("app.portal_runner.sleep", lambda _: None):
                    with patch.object(
                        runner,
                        "_confirmed_authenticated_page",
                        side_effect=confirmed_pages,
                    ):
                        with patch.object(runner, "_refresh_attached_authenticated_page", return_value=None):
                            with patch.object(runner, "_is_public_landing_page", return_value=False):
                                with patch.object(runner, "_page_requires_reauth", return_value=True):
                                    with patch.object(runner, "_is_login_page", return_value=True):
                                        with patch.object(runner, "_local_app_login_enabled", return_value=False):
                                            with patch.object(
                                                runner,
                                                "_chrome_oauth_restart_readiness",
                                                return_value=(False, "etax-transition-visible"),
                                            ):
                                                with patch.object(runner, "_goto") as mocked_goto:
                                                    with patch.object(runner, "_log") as mocked_log:
                                                        authenticated_page = runner._ensure_logged_in(  # noqa: SLF001
                                                            page,
                                                            result,
                                                            store,
                                                        )

            self.assertIs(home_page, authenticated_page)
            mocked_goto.assert_not_called()
            self.assertIn(
                "suppressing automatic OAuth restart while waiting for login completion "
                "reason=etax-transition-visible",
                [call.args[1] for call in mocked_log.call_args_list],
            )

    def test_chrome_oauth_restart_requires_original_expired_qr(self) -> None:
        runner = object.__new__(TaxPortalRunner)
        store = SimpleNamespace(effective_portal_area=lambda: "fujian")
        page = object()
        original_url = (
            "https://tpass.fujian.chinatax.gov.cn:8443/#/login?state=original"
        )
        original_target = {
            "type": "page",
            "url": original_url,
        }

        with patch.object(
            runner,
            "_raw_attached_targets",
            return_value=[original_target],
        ), patch.object(
            runner,
            "_login_qr_requires_refresh",
            side_effect=[False, True],
        ):
            active_result = runner._chrome_oauth_restart_readiness(  # noqa: SLF001
                page,
                store,
                original_url,
            )
            expired_result = runner._chrome_oauth_restart_readiness(  # noqa: SLF001
                page,
                store,
                original_url,
            )

        self.assertEqual((False, "login-qr-still-active"), active_result)
        self.assertEqual((True, "original-login-qr-expired"), expired_result)

    def test_chrome_oauth_restart_is_blocked_by_etax_target(self) -> None:
        runner = object.__new__(TaxPortalRunner)
        store = SimpleNamespace(effective_portal_area=lambda: "fujian")
        page = object()
        original_url = (
            "https://tpass.fujian.chinatax.gov.cn:8443/#/login?state=original"
        )
        targets = [
            {"type": "page", "url": original_url},
            {
                "type": "page",
                "url": "https://etax.fujian.chinatax.gov.cn:8443/loginb/",
            },
        ]

        with patch.object(runner, "_raw_attached_targets", return_value=targets):
            readiness = runner._chrome_oauth_restart_readiness(  # noqa: SLF001
                page,
                store,
                original_url,
            )

        self.assertEqual((False, "etax-transition-visible"), readiness)

    def test_wait_for_batch_page_uses_official_dom_navigation(self) -> None:
        class FakePage:
            def __init__(self, url: str) -> None:
                self.url = url
                self.context: object | None = None
                self.default_timeout: int | None = None

            def set_default_timeout(self, timeout: int) -> None:
                self.default_timeout = timeout

        class FakeContext:
            def __init__(self, home_page: FakePage) -> None:
                self.pages = [home_page]

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            store = StoreConfig(
                store_key="fuzzy",
                store_name="Fuzzy",
                survey_id="1",
                output_xlsx_path=tmp_path / "output.xlsx",
                initial_last_processed_id=1,
                portal_enabled=True,
                portal_company_switch_name="厦门市思明区浮几创意餐厅",
                portal_company_verify_name="厦门市思明区浮几创意餐厅",
            )
            result = SimpleNamespace(workbook_sha256="abc123")
            home_page = FakePage("https://etax.xiamen.chinatax.gov.cn:8443/loginb/")
            invoice_page = FakePage("https://dppt.xiamen.chinatax.gov.cn:8443/invoice-business?ruuid=1")
            context = FakeContext(home_page)
            home_page.context = context
            invoice_page.context = context
            clicks: list[tuple[object, str, str]] = []

            def fake_dom_click(page: object, text: str, *, class_hint: str = "") -> None:
                clicks.append((page, text, class_hint))
                if text == "发票业务":
                    context.pages.append(invoice_page)
                elif text == "蓝字发票开具":
                    invoice_page.url = "https://dppt.xiamen.chinatax.gov.cn:8443/blue-invoice-makeout"
                elif text == "批量开票":
                    invoice_page.url = "https://dppt.xiamen.chinatax.gov.cn:8443/blue-invoice-makeout/invoice-batch"

            with patch.object(runner, "_update_store_step") as mocked_step:
                with patch.object(runner, "_dom_click_exact_text", side_effect=fake_dom_click):
                    with patch.object(runner, "_wait_for_invoice_business_page_ready") as mocked_wait_business:
                        with patch.object(runner, "_wait_for_blue_invoice_page_ready") as mocked_wait_blue:
                            with patch.object(runner, "_wait_for_batch_page_ready") as mocked_wait_batch:
                                with patch.object(runner, "_log"):
                                    page = runner._wait_for_batch_page(home_page, store, result)  # noqa: SLF001

            self.assertIs(invoice_page, page)
            mocked_step.assert_called_once()
            self.assertEqual(
                [
                    (home_page, "发票业务", "item-info"),
                    (invoice_page, "蓝字发票开具", "app_name"),
                    (invoice_page, "批量开票", ""),
                ],
                clicks,
            )
            mocked_wait_business.assert_called_once_with(invoice_page, store.store_key)
            mocked_wait_blue.assert_called_once_with(invoice_page, store.store_key)
            mocked_wait_batch.assert_called_once_with(invoice_page, store.store_key)
            self.assertEqual(config.portal_action_timeout_ms, invoice_page.default_timeout)

    def test_wait_for_batch_page_accepts_reused_invoice_business_tab(self) -> None:
        class FakePage:
            def __init__(self, url: str) -> None:
                self.url = url
                self.context: object | None = None

            def set_default_timeout(self, timeout: int) -> None:
                return None

        class FakeContext:
            def __init__(self, *pages: FakePage) -> None:
                self.pages = list(pages)

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            store = StoreConfig(
                store_key="fuzzy",
                store_name="Fuzzy",
                survey_id="1",
                output_xlsx_path=tmp_path / "output.xlsx",
                initial_last_processed_id=1,
                portal_enabled=True,
                portal_company_switch_name="厦门市思明区浮几创意餐厅",
                portal_company_verify_name="厦门市思明区浮几创意餐厅",
            )
            result = SimpleNamespace(workbook_sha256="abc123")
            home_page = FakePage("https://etax.xiamen.chinatax.gov.cn:8443/loginb/")
            reused_page = FakePage("about:blank")
            context = FakeContext(home_page, reused_page)
            home_page.context = context
            reused_page.context = context
            clicks: list[tuple[object, str]] = []

            def fake_dom_click(page: object, text: str, *, class_hint: str = "") -> None:
                clicks.append((page, text))
                if text == "发票业务":
                    reused_page.url = "https://dppt.xiamen.chinatax.gov.cn:8443/invoice-business?ruuid=1"
                elif text == "蓝字发票开具":
                    reused_page.url = "https://dppt.xiamen.chinatax.gov.cn:8443/blue-invoice-makeout"
                elif text == "批量开票":
                    reused_page.url = "https://dppt.xiamen.chinatax.gov.cn:8443/blue-invoice-makeout/invoice-batch"

            with patch.object(runner, "_update_store_step"):
                with patch.object(runner, "_dom_click_exact_text", side_effect=fake_dom_click):
                    with patch.object(runner, "_wait_for_invoice_business_page_ready"):
                        with patch.object(runner, "_wait_for_blue_invoice_page_ready"):
                            with patch.object(runner, "_wait_for_batch_page_ready"):
                                with patch.object(runner, "_log") as mocked_log:
                                    page = runner._wait_for_batch_page(home_page, store, result)  # noqa: SLF001

            self.assertIs(reused_page, page)
            self.assertEqual(
                [(home_page, "发票业务"), (reused_page, "蓝字发票开具"), (reused_page, "批量开票")],
                clicks,
            )
            self.assertIn(
                "invoice business page detected from official portal entry via reused page",
                [call.args[1] for call in mocked_log.call_args_list],
            )

    def test_wait_for_batch_page_uses_raw_readiness_gate_before_continuing(self) -> None:
        class FakePage:
            def __init__(self, url: str) -> None:
                self.url = url
                self.context: object | None = None

        class FakeContext:
            def __init__(self, home_page: FakePage) -> None:
                self.pages = [home_page]

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
                portal_browser_backend="chrome_cdp",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            runner._attached_cdp_url = "http://127.0.0.1:9222"  # noqa: SLF001
            runner._sync_playwright_factory = lambda: None  # noqa: SLF001
            store = StoreConfig(
                store_key="fuzzy",
                store_name="Fuzzy",
                survey_id="1",
                output_xlsx_path=tmp_path / "output.xlsx",
                initial_last_processed_id=1,
                portal_enabled=True,
                portal_company_switch_name="厦门市思明区浮几创意餐厅",
                portal_company_verify_name="厦门市思明区浮几创意餐厅",
            )
            result = SimpleNamespace(
                workbook_sha256="abc123",
                workbook_path=tmp_path / "output.xlsx",
                expected_count=2,
            )
            home_page = FakePage("https://etax.xiamen.chinatax.gov.cn:8443/loginb/")
            home_page.context = FakeContext(home_page)
            invoice_target = {
                "id": "dppt-1",
                "type": "page",
                "url": "https://dppt.xiamen.chinatax.gov.cn:8443/invoice-business?ruuid=1",
                "title": "发票业务-厦门市思明区浮几创意餐厅",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/dppt-1",
            }
            batch_target = {
                **invoice_target,
                "url": "https://dppt.xiamen.chinatax.gov.cn:8443/blue-invoice-makeout/invoice-batch",
            }

            with patch(
                "app.portal_runner.DPPT_INVOICE_BUSINESS_POST_READY_DELAY_SECONDS",
                0,
            ), patch.object(runner, "_close_raw_attached_dppt_targets", return_value=0), patch.object(
                runner,
                "_raw_attached_dppt_targets",
                side_effect=[[], [invoice_target], [invoice_target]],
            ), patch.object(
                runner,
                "_raw_cdp_invoice_business_readiness",
                side_effect=[
                    {"ready": False, "securityInitialized": False},
                    {
                        "ready": True,
                        "pageRendered": True,
                        "blueInvoiceClickable": True,
                        "navigationStatus": 200,
                        "securityPublicKeyStatus": 0,
                        "securityPermissionStatus": 200,
                    },
                ],
            ) as mocked_readiness, patch.object(
                runner,
                "_stop_attached_playwright_transport",
            ), patch.object(
                runner,
                "_raw_cdp_open_batch_page",
                return_value=batch_target,
            ) as mocked_open, patch.object(
                runner,
                "_raw_cdp_import_workbook",
            ) as mocked_import, patch.object(
                runner,
                "_update_store_step",
            ), patch.object(
                runner,
                "_dom_click_exact_text",
            ), patch.object(runner, "_log") as mocked_log:
                page = runner._wait_for_batch_page(home_page, store, result)  # noqa: SLF001

            self.assertEqual(batch_target, page.target)
            self.assertEqual(2, mocked_readiness.call_count)
            mocked_open.assert_called_once_with(invoice_target, store.store_key)
            mocked_import.assert_called_once_with(
                batch_target,
                store.store_key,
                result.workbook_path,
                result.expected_count,
            )
            self.assertIn(
                "invoice business readiness confirmed page_rendered=true "
                "blue_invoice_clickable=true invoice_business_http=200 "
                "security_public_key=preinitialized "
                "security_getYwqxbz_http=200; waiting 3s before continuing",
                [call.args[1] for call in mocked_log.call_args_list],
            )

    def test_raw_cdp_invoice_business_readiness_uses_security_request_status(self) -> None:
        runner = object.__new__(TaxPortalRunner)
        expected = {
            "ready": True,
            "pageRendered": True,
            "blueInvoiceClickable": True,
            "navigationStatus": 200,
            "securityPublicKeyStatus": 200,
            "securityPermissionStatus": 200,
        }
        with patch.object(runner, "_raw_cdp_evaluate", return_value=expected) as mocked_evaluate:
            snapshot = runner._raw_cdp_invoice_business_readiness({"id": "dppt-1"})  # noqa: SLF001

        self.assertEqual(expected, snapshot)
        expression = mocked_evaluate.call_args.args[1]
        self.assertIn("document.readyState === 'complete'", expression)
        self.assertIn(".page_app_list", expression)
        self.assertIn("蓝字发票开具", expression)
        self.assertIn("/szzhzz/cssSecurity/v1/getPublicKey", expression)
        self.assertIn("/szzhzz/swszzhCtr/v1/getYwqxbz", expression)
        self.assertIn("!publicKeyRequest.seen", expression)
        self.assertIn("permissionRequest.seen", expression)
        self.assertIn("navigationStatus >= 200", expression)

    def test_raw_cdp_submit_confirms_and_closes_result_dialog_before_returning(self) -> None:
        runner = object.__new__(TaxPortalRunner)
        target = {"id": "dppt-1"}
        result = SimpleNamespace(expected_count=1, submitted_count=0)
        result_text = "批量开具结果 开具成功发票1份 开具失败发票0份"
        body_texts = iter(
            [
                "本次勾选批量开具发票1份",
                result_text,
                "批量开票 共 1 条",
            ]
        )

        def fake_wait_until(predicate: object, **kwargs: object) -> None:
            self.assertTrue(predicate())

        with patch.object(runner, "_wait_until", side_effect=fake_wait_until), patch(
            "app.portal_runner.sleep",
        ) as mocked_sleep, patch.object(
            runner,
            "_raw_cdp_click_exact_text",
        ) as mocked_click_text, patch.object(
            runner,
            "_raw_cdp_body_text",
            side_effect=lambda target: next(body_texts),
        ), patch.object(
            runner,
            "_raw_cdp_click_dialog_button",
        ) as mocked_click_dialog, patch.object(
            runner,
            "_close_raw_cdp_target",
        ) as mocked_close_target, patch.object(
            runner,
            "_log",
        ) as mocked_log:
            details, success_count, failure_count, modal_text = runner._raw_cdp_submit_batch(  # noqa: SLF001
                target,
                "fuzzy",
                result,
            )

        self.assertEqual([], details)
        self.assertEqual(1, success_count)
        self.assertEqual(0, failure_count)
        self.assertEqual(result_text, modal_text)
        self.assertEqual(1, result.submitted_count)
        self.assertEqual(
            ["批量开具", "确定"],
            [call.args[1] for call in mocked_click_text.call_args_list],
        )
        self.assertEqual(
            [1.0, 1.0],
            [call.args[0] for call in mocked_sleep.call_args_list],
        )
        mocked_click_dialog.assert_called_once_with(target, "批量开具结果", "关闭")
        mocked_close_target.assert_called_once_with(target, "fuzzy")
        self.assertIn(
            "raw CDP portal issue result confirmed and closed",
            [call.args[1] for call in mocked_log.call_args_list],
        )

    def test_raw_cdp_select_all_uses_header_and_verifies_every_imported_row(self) -> None:
        runner = object.__new__(TaxPortalRunner)
        target = {"id": "dppt-1"}

        def fake_wait_until(predicate: object, **kwargs: object) -> None:
            self.assertTrue(predicate())

        with patch.object(runner, "_wait_until", side_effect=fake_wait_until), patch(
            "app.portal_runner.sleep",
        ) as mocked_sleep, patch.object(
            runner,
            "_raw_cdp_evaluate",
            side_effect=[2, {"found": True, "checked": True, "count": 1}, 2],
        ) as mocked_evaluate, patch.object(runner, "_log") as mocked_log:
            runner._raw_cdp_select_all_batch_rows(target, "fuzzy", 2)  # noqa: SLF001

        self.assertEqual([1.0], [call.args[0] for call in mocked_sleep.call_args_list])
        select_all_expression = mocked_evaluate.call_args_list[1].args[1]
        self.assertIn("th.t-table__cell-check", select_all_expression)
        self.assertIn("thead label.t-checkbox", select_all_expression)
        self.assertIn("headerLabel.click()", select_all_expression)
        self.assertIn(
            "raw CDP select-all verified rows=2",
            [call.args[1] for call in mocked_log.call_args_list],
        )

    def test_raw_cdp_dry_run_selects_all_then_closes_batch_tab(self) -> None:
        runner = object.__new__(TaxPortalRunner)
        runner.config = SimpleNamespace(
            portal_sync_from_server=False,
            portal_block_on_empty_amount=True,
            portal_browser_backend="chrome_cdp",
        )
        runner.submit = False
        target = {"id": "dppt-1"}
        events: list[str] = []
        home_page = SimpleNamespace(
            url="https://etax.xiamen.chinatax.gov.cn:8443/loginb/",
        )
        context = SimpleNamespace(pages=[home_page])
        home_page.context = context
        store = StoreConfig(
            store_key="fuzzy",
            store_name="Fuzzy",
            survey_id="1",
            output_xlsx_path=Path("/tmp/fuzzy.xlsx"),
            initial_last_processed_id=1,
            portal_enabled=True,
            portal_company_verify_name="厦门市思明区浮几创意餐厅",
        )
        rows = [object()]
        summary = SimpleNamespace(row_count=1, total_amount_including_tax=Decimal("123.45"))

        with patch("app.portal_runner.load_portal_issue_rows", return_value=rows), patch(
            "app.portal_runner.summarize_portal_issue_rows",
            return_value=summary,
        ), patch("app.portal_runner.sha256_file", return_value="abc123"), patch.object(
            runner,
            "_prepare_artifacts_dir",
            return_value=Path("/tmp/artifacts"),
        ), patch.object(runner, "_update_store_step"), patch.object(
            runner,
            "_find_attached_portal_page",
            return_value=home_page,
        ), patch.object(runner, "_is_home_page", return_value=True), patch.object(
            runner,
            "_ensure_logged_in",
            return_value=home_page,
        ), patch.object(
            runner,
            "_ensure_authenticated_home_page",
            return_value=home_page,
        ), patch.object(runner, "_ensure_company", return_value=home_page), patch.object(
            runner,
            "_wait_for_home_page_ready",
        ), patch.object(runner, "_wait_before_open_batch_page"), patch.object(
            runner,
            "_wait_for_batch_page",
            return_value=_RawBatchSession(target),
        ), patch.object(
            runner,
            "_raw_cdp_select_all_batch_rows",
            side_effect=lambda *_: events.append("select_all"),
        ), patch(
            "app.portal_runner.sleep",
            side_effect=lambda seconds: events.append(f"sleep:{seconds:.0f}s"),
        ), patch.object(
            runner,
            "_close_raw_cdp_target",
            side_effect=lambda *_: events.append("close_batch_tab"),
        ), patch.object(
            runner,
            "_finalize_result",
            side_effect=lambda result: result,
        ), patch.object(runner, "_log"):
            result = runner._run_store(context, home_page, store)  # noqa: SLF001

        self.assertEqual("validated", result.status)
        self.assertEqual(["select_all", "sleep:1s", "close_batch_tab"], events)

    def test_raw_cdp_dialog_button_waits_until_scoped_confirm_is_clickable(self) -> None:
        runner = object.__new__(TaxPortalRunner)
        target = {"id": "dppt-1"}

        def fake_wait_until(predicate: object, **kwargs: object) -> None:
            self.assertFalse(predicate())
            self.assertTrue(predicate())

        with patch.object(runner, "_wait_until", side_effect=fake_wait_until), patch.object(
            runner,
            "_raw_cdp_evaluate",
            side_effect=[
                {"clicked": False, "reason": "button-not-ready"},
                {"clicked": True, "tag": "BUTTON"},
            ],
        ) as mocked_evaluate:
            runner._raw_cdp_click_dialog_button(  # noqa: SLF001
                target,
                "批量开具结果",
                "确定",
            )

        self.assertEqual(2, mocked_evaluate.call_count)
        expression = mocked_evaluate.call_args.args[1]
        self.assertIn('[role="dialog"]', expression)
        self.assertIn("dialog.querySelectorAll", expression)
        self.assertIn("button-not-ready", expression)

    def test_close_raw_cdp_target_waits_until_batch_tab_disappears(self) -> None:
        class FakeResponse:
            def __init__(self, payload: bytes = b"") -> None:
                self.payload = payload

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return self.payload

        class FakeOpener:
            def __init__(self) -> None:
                self.urls: list[str] = []
                self.responses = iter([FakeResponse(), FakeResponse(b"[]")])

            def open(self, url: str, timeout: float) -> FakeResponse:
                self.urls.append(url)
                return next(self.responses)

        runner = object.__new__(TaxPortalRunner)
        runner._attached_cdp_url = "http://127.0.0.1:9222"  # noqa: SLF001
        opener = FakeOpener()

        def fake_wait_until(predicate: object, **kwargs: object) -> None:
            self.assertTrue(predicate())

        with patch("app.portal_runner.build_opener", return_value=opener), patch.object(
            runner,
            "_wait_until",
            side_effect=fake_wait_until,
        ), patch.object(runner, "_log") as mocked_log:
            runner._close_raw_cdp_target({"id": "dppt-1"}, "fuzzy")  # noqa: SLF001

        self.assertEqual(
            [
                "http://127.0.0.1:9222/json/close/dppt-1",
                "http://127.0.0.1:9222/json/list",
            ],
            opener.urls,
        )
        mocked_log.assert_called_once_with("fuzzy", "raw CDP batch issue tab closed")

    def test_wait_for_batch_page_refreshes_cdp_when_original_context_misses_new_target(self) -> None:
        class FakePage:
            def __init__(self, url: str) -> None:
                self.url = url
                self.context: object | None = None

            def set_default_timeout(self, timeout: int) -> None:
                return None

        class FakeContext:
            def __init__(self, home_page: FakePage) -> None:
                self.pages = [home_page]

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
                portal_browser_backend="chrome_cdp",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            runner._attached_cdp_url = "http://127.0.0.1:9222"  # noqa: SLF001
            store = StoreConfig(
                store_key="fuzzy",
                store_name="Fuzzy",
                survey_id="1",
                output_xlsx_path=tmp_path / "output.xlsx",
                initial_last_processed_id=1,
                portal_enabled=True,
                portal_company_switch_name="厦门市思明区浮几创意餐厅",
                portal_company_verify_name="厦门市思明区浮几创意餐厅",
            )
            result = SimpleNamespace(workbook_sha256="abc123")
            home_page = FakePage("https://etax.xiamen.chinatax.gov.cn:8443/loginb/")
            invoice_page = FakePage("https://dppt.xiamen.chinatax.gov.cn:8443/invoice-business?ruuid=1")
            context = FakeContext(home_page)
            home_page.context = context
            invoice_page.context = context

            with patch("app.portal_runner.DPPT_INVOICE_BUSINESS_POST_READY_DELAY_SECONDS", 0), patch(
                "app.portal_runner.DPPT_SENSITIVE_NAVIGATION_SETTLE_SECONDS",
                0,
            ):
                with patch.object(runner, "_update_store_step"):
                    with patch.object(runner, "_dom_click_exact_text"):
                        with patch.object(
                            runner,
                            "_raw_attached_dppt_targets",
                            side_effect=[
                                [],
                                [
                                    {
                                        "id": "dppt-1",
                                        "type": "page",
                                        "url": invoice_page.url,
                                        "title": "发票业务-厦门市思明区浮几创意餐厅",
                                    }
                                ],
                            ],
                        ):
                            with patch.object(
                                runner,
                                "_refresh_attached_invoice_business_target",
                                return_value=invoice_page,
                            ) as mocked_refresh:
                                with patch.object(runner, "_wait_for_invoice_business_page_ready"):
                                    with patch.object(runner, "_wait_for_blue_invoice_page_ready"):
                                        with patch.object(runner, "_wait_for_batch_page_ready"):
                                            with patch.object(runner, "_log"):
                                                page = runner._wait_for_batch_page(home_page, store, result)  # noqa: SLF001

            self.assertIs(invoice_page, page)
            mocked_refresh.assert_called_once_with(store)

    def test_wait_for_batch_page_retries_official_entry_after_handoff_http_400(self) -> None:
        class FakePage:
            def __init__(self, url: str, response_status: int = 0) -> None:
                self.url = url
                self.response_status = response_status
                self.context: object | None = None
                self.closed = False

            def evaluate(self, script: str) -> int:
                return self.response_status

            def set_default_timeout(self, timeout: int) -> None:
                return None

            def close(self) -> None:
                self.closed = True

        class FakeContext:
            def __init__(self, home_page: FakePage) -> None:
                self.pages = [home_page]

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
                portal_browser_backend="chrome_cdp",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            runner._attached_cdp_url = "http://127.0.0.1:9222"  # noqa: SLF001
            store = StoreConfig(
                store_key="fuzzy",
                store_name="Fuzzy",
                survey_id="1",
                output_xlsx_path=tmp_path / "output.xlsx",
                initial_last_processed_id=1,
                portal_enabled=True,
                portal_company_switch_name="厦门市思明区浮几创意餐厅",
                portal_company_verify_name="厦门市思明区浮几创意餐厅",
            )
            result = SimpleNamespace(workbook_sha256="abc123")
            home_page = FakePage("https://etax.xiamen.chinatax.gov.cn:8443/loginb/")
            failed_handoff = FakePage(
                "https://dppt.xiamen.chinatax.gov.cn:8443/szzhzz/spHandler?cdlj=invoice-business",
                response_status=400,
            )
            invoice_page = FakePage("https://dppt.xiamen.chinatax.gov.cn:8443/invoice-business?ruuid=1")
            context = FakeContext(home_page)
            home_page.context = context
            failed_handoff.context = context
            invoice_page.context = context

            with patch("app.portal_runner.DPPT_INVOICE_BUSINESS_POST_READY_DELAY_SECONDS", 0), patch(
                "app.portal_runner.DPPT_SENSITIVE_NAVIGATION_SETTLE_SECONDS",
                0,
            ):
                with patch.object(runner, "_update_store_step"):
                    with patch.object(runner, "_dom_click_exact_text") as mocked_click:
                        with patch.object(
                            runner,
                            "_raw_attached_dppt_targets",
                            side_effect=[
                                [],
                                [
                                    {
                                        "id": "handoff-1",
                                        "type": "page",
                                        "url": failed_handoff.url,
                                        "title": "",
                                    }
                                ],
                                [],
                                [
                                    {
                                        "id": "dppt-1",
                                        "type": "page",
                                        "url": invoice_page.url,
                                        "title": "发票业务-厦门市思明区浮几创意餐厅",
                                    }
                                ],
                            ],
                        ):
                            with patch.object(
                                runner,
                                "_refresh_attached_invoice_business_target",
                                side_effect=[failed_handoff, invoice_page],
                            ):
                                with patch.object(runner, "_wait_for_invoice_business_page_ready"):
                                    with patch.object(runner, "_wait_for_blue_invoice_page_ready"):
                                        with patch.object(runner, "_wait_for_batch_page_ready"):
                                            with patch.object(runner, "_log"):
                                                page = runner._wait_for_batch_page(home_page, store, result)  # noqa: SLF001

            self.assertIs(invoice_page, page)
            self.assertTrue(failed_handoff.closed)
            self.assertEqual(4, mocked_click.call_count)

    def test_reset_attached_dppt_site_data_preserves_home_and_closes_dppt_pages(self) -> None:
        class FakeSession:
            def __init__(self) -> None:
                self.calls: list[tuple[str, object]] = []
                self.detached = False

            def send(self, method: str, params: object) -> None:
                self.calls.append((method, params))

            def detach(self) -> None:
                self.detached = True

        class FakePage:
            def __init__(self, url: str) -> None:
                self.url = url
                self.closed = False
                self.context: object | None = None

            def close(self) -> None:
                self.closed = True

        class FakeContext:
            def __init__(self, pages: list[FakePage], session: FakeSession) -> None:
                self.pages = pages
                self.session = session

            def new_cdp_session(self, page: object) -> FakeSession:
                return self.session

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
                portal_browser_backend="chrome_cdp",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            store = StoreConfig(
                store_key="fuzzy",
                store_name="Fuzzy",
                survey_id="1",
                output_xlsx_path=tmp_path / "output.xlsx",
                initial_last_processed_id=1,
                portal_enabled=True,
                portal_company_switch_name="厦门市思明区浮几创意餐厅",
                portal_company_verify_name="厦门市思明区浮几创意餐厅",
            )
            session = FakeSession()
            home_page = FakePage("https://etax.xiamen.chinatax.gov.cn:8443/loginb/")
            dppt_page = FakePage("https://dppt.xiamen.chinatax.gov.cn:8443/invoice-business?ruuid=1")
            context = FakeContext([home_page, dppt_page], session)
            home_page.context = context
            dppt_page.context = context

            with patch.object(runner, "_refresh_attached_dppt_pages", return_value=[dppt_page]):
                reset = runner._reset_attached_dppt_site_data(home_page, store)  # noqa: SLF001

            self.assertTrue(reset)
            self.assertFalse(home_page.closed)
            self.assertTrue(dppt_page.closed)
            self.assertTrue(session.detached)
            self.assertEqual(
                [
                    (
                        "Storage.clearDataForOrigin",
                        {
                            "origin": "https://dppt.xiamen.chinatax.gov.cn:8443",
                            "storageTypes": "all",
                        },
                    )
                ],
                session.calls,
            )

    def test_dom_click_exact_text_uses_page_evaluate_instead_of_locator_click(self) -> None:
        class FakePage:
            def __init__(self) -> None:
                self.arguments: list[object] = []

            def wait_for_timeout(self, timeout: int) -> None:
                self.arguments.append(("wait", timeout))

            def evaluate(self, script: str, argument: object) -> object:
                self.arguments.append((script, argument))
                return {"clicked": True, "count": 1, "tag": "DIV", "className": "app_name__bold"}

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            page = FakePage()
            runner._dom_click_exact_text(page, "蓝字发票开具", class_hint="app_name")  # noqa: SLF001

            self.assertEqual(("wait", BROWSER_CLICK_DELAY_MS), page.arguments[0])
            self.assertEqual(
                {"text": "蓝字发票开具", "classHint": "app_name"},
                page.arguments[1][1],
            )

    def test_wait_for_text_uses_visible_body_text_instead_of_first_matching_locator(self) -> None:
        class FakePage:
            def __init__(self) -> None:
                self.arguments: tuple[object, ...] | None = None

            def wait_for_function(self, *args: object, **kwargs: object) -> None:
                self.arguments = (*args, kwargs)

            def get_by_text(self, text: str, exact: bool = False) -> object:
                raise AssertionError("first matching locator must not be used")

        page = FakePage()
        TaxPortalRunner._wait_for_text(page, "发票业务", timeout_ms=30000)  # noqa: SLF001

        self.assertIsNotNone(page.arguments)
        self.assertEqual({"arg": "发票业务", "timeout": 30000}, page.arguments[1])

    def test_run_store_waits_five_seconds_after_home_page_before_opening_batch_page(self) -> None:
        class FakeBatchPage:
            def __init__(self, events: list[str], label: str) -> None:
                self.events = events
                self.label = label

            def set_default_timeout(self, timeout: int) -> None:
                self.events.append(f"set_default_timeout:{self.label}:{timeout}")

            def close(self) -> None:
                self.events.append(f"close_batch_page:{self.label}")

        class FakeContext:
            def __init__(self, events: list[str]) -> None:
                self.events = events
                self.new_page_count = 0

            def new_page(self) -> FakeBatchPage:
                self.new_page_count += 1
                label = f"attempt{self.new_page_count}"
                self.events.append(f"new_page:{label}")
                return FakeBatchPage(self.events, label)

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            store = StoreConfig(
                store_key="fuzzy",
                store_name="Fuzzy",
                survey_id="1",
                output_xlsx_path=tmp_path / "output.xlsx",
                initial_last_processed_id=1,
                portal_enabled=True,
                portal_company_switch_name="厦门市思明区浮几创意餐厅",
                portal_company_verify_name="厦门市思明区浮几创意餐厅",
            )
            summary = SimpleNamespace(row_count=1, total_amount_including_tax=Decimal("123.45"))
            rows = [object()]
            home_page = object()
            events: list[str] = []
            context = FakeContext(events)

            def fake_wait_for_home_page_ready(page: object, current_store: StoreConfig) -> None:
                self.assertIs(home_page, page)
                self.assertIs(store, current_store)
                events.append("wait_for_home_page_ready")

            def fake_wait_for_batch_page(page: object, current_store: StoreConfig, current_result: object) -> object:
                self.assertIs(store, current_store)
                self.assertIsNotNone(current_result)
                events.append("batch_page_ready")
                return page

            with patch("app.portal_runner.load_portal_issue_rows", return_value=rows):
                with patch("app.portal_runner.summarize_portal_issue_rows", return_value=summary):
                    with patch("app.portal_runner.sha256_file", return_value="abc123"):
                        with patch("app.portal_runner.sleep", lambda _: events.append("sleep_before_batch")):
                            with patch.object(runner, "_goto"):
                                with patch.object(runner, "_ensure_logged_in", return_value=home_page):
                                    with patch.object(runner, "_ensure_company", return_value=home_page):
                                        with patch.object(runner, "_wait_for_home_page_ready", side_effect=fake_wait_for_home_page_ready):
                                            with patch.object(runner, "_install_network_diag"):
                                                with patch.object(runner, "_wait_for_batch_page", side_effect=fake_wait_for_batch_page):
                                                    with patch.object(runner, "_ensure_batch_page_clean"):
                                                        with patch.object(runner, "_import_workbook"):
                                                            with patch.object(runner, "_select_all_rows"):
                                                                with patch.object(
                                                                    runner,
                                                                    "_finalize_result",
                                                                    side_effect=lambda result: result,
                                                                ):
                                                                    with patch.object(runner, "_log"):
                                                                        result = runner._run_store(context, home_page, store)  # noqa: SLF001

            self.assertEqual("validated", result.status)
            self.assertEqual(0, context.new_page_count)
            self.assertEqual(1, events.count("wait_for_home_page_ready"))
            self.assertIn("sleep_before_batch", events)
            self.assertLess(events.index("wait_for_home_page_ready"), events.index("sleep_before_batch"))
            self.assertLess(events.index("sleep_before_batch"), events.index("batch_page_ready"))

    def test_run_store_skips_empty_workbook_before_opening_batch_page(self) -> None:
        class FakeContext:
            def __init__(self) -> None:
                self.new_page_count = 0

            def new_page(self) -> object:
                self.new_page_count += 1
                raise AssertionError("new_page should not be called for an empty workbook")

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            store = StoreConfig(
                store_key="fuzzy",
                store_name="Fuzzy",
                survey_id="1",
                output_xlsx_path=tmp_path / "output.xlsx",
                initial_last_processed_id=1,
                portal_enabled=True,
                portal_company_switch_name="厦门市思明区浮几创意餐厅",
                portal_company_verify_name="厦门市思明区浮几创意餐厅",
            )
            summary = SimpleNamespace(row_count=0, total_amount_including_tax=Decimal("0.00"))
            context = FakeContext()
            home_page = object()

            with patch("app.portal_runner.load_portal_issue_rows", return_value=[]):
                with patch("app.portal_runner.summarize_portal_issue_rows", return_value=summary):
                    with patch("app.portal_runner.sha256_file", return_value="abc123"):
                        with patch.object(runner, "_goto") as mocked_goto:
                            with patch.object(runner, "_ensure_logged_in") as mocked_ensure_logged_in:
                                with patch.object(runner, "_ensure_company") as mocked_ensure_company:
                                    with patch.object(runner, "_wait_for_home_page_ready") as mocked_wait_home_ready:
                                        with patch.object(runner, "_wait_before_open_batch_page") as mocked_wait_before_batch:
                                            with patch.object(runner, "_finalize_result", side_effect=lambda result: result):
                                                with patch.object(runner, "_log"):
                                                    result = runner._run_store(context, home_page, store)  # noqa: SLF001

            self.assertEqual("skipped", result.status)
            self.assertEqual("skip_empty_workbook", result.step)
            self.assertEqual(0, result.expected_count)
            self.assertEqual(0, context.new_page_count)
            mocked_goto.assert_not_called()
            mocked_ensure_logged_in.assert_not_called()
            mocked_ensure_company.assert_not_called()
            mocked_wait_home_ready.assert_not_called()
            mocked_wait_before_batch.assert_not_called()

    def test_select_company_switch_row_waits_for_query_results_before_clicking_switch(self) -> None:
        class FakeLocator:
            def __init__(self, events: list[str]) -> None:
                self.events = events

            def fill(self, value: str) -> None:
                self.events.append(f"fill:{value}")

            def click(self, timeout: int | None = None) -> None:
                if timeout is None:
                    self.events.append("click")
                else:
                    self.events.append(f"click:{timeout}")

            def filter(self, *, has_text: str) -> "FakeLocator":
                self.events.append(f"filter:{has_text}")
                return self

            @property
            def first(self) -> "FakeLocator":
                return self

            def get_by_role(self, role: str, name: str) -> "FakeLocator":
                self.events.append(f"nested_role:{role}:{name}")
                return self

        class FakePage:
            def __init__(self, events: list[str]) -> None:
                self.events = events

            def get_by_placeholder(self, text: str) -> FakeLocator:
                self.events.append(f"placeholder:{text}")
                return FakeLocator(self.events)

            def get_by_role(self, role: str, name: str) -> FakeLocator:
                self.events.append(f"role:{role}:{name}")
                return FakeLocator(self.events)

            def locator(self, selector: str) -> FakeLocator:
                self.events.append(f"locator:{selector}")
                return FakeLocator(self.events)

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            store = StoreConfig(
                store_key="fuzzy",
                store_name="Fuzzy",
                survey_id="1",
                output_xlsx_path=tmp_path / "output.xlsx",
                initial_last_processed_id=1,
                portal_enabled=True,
                portal_company_switch_name="厦门市思明区浮几创意餐厅",
                portal_company_verify_name="厦门市思明区浮几创意餐厅",
            )
            events: list[str] = []
            page = FakePage(events)

            def fake_wait_ready(current_page: object, store_key: str) -> None:
                self.assertIs(page, current_page)
                self.assertEqual("fuzzy", store_key)
                events.append("wait_query_ready")

            with patch.object(runner, "_wait_for_switch_query_results_ready", side_effect=fake_wait_ready):
                with patch.object(runner, "_log"):
                    selected = runner._select_company_switch_row(page, store)  # noqa: SLF001

            self.assertEqual("厦门市思明区浮几创意餐厅", selected)
            self.assertLess(events.index("wait_query_ready"), events.index("filter:厦门市思明区浮几创意餐厅"))

    def test_import_workbook_waits_for_import_page_to_settle_before_validating_rows(self) -> None:
        class FakeFileInput:
            def __init__(self, events: list[str]) -> None:
                self.events = events

            def count(self) -> int:
                return 1

            def set_input_files(self, value: str) -> None:
                self.events.append(f"set_input_files:{value}")

        class FakePage:
            def __init__(self, events: list[str]) -> None:
                self.events = events

            def locator(self, selector: str) -> FakeFileInput:
                self.events.append(f"locator:{selector}")
                return FakeFileInput(self.events)

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            workbook_path = tmp_path / "output.xlsx"
            summary = SimpleNamespace(row_count=1, total_amount_including_tax=Decimal("123.45"))
            rows = [
                SimpleNamespace(
                    buyer_name="示例公司",
                    amount_excluding_tax=Decimal("100.00"),
                    amount_including_tax=Decimal("123.45"),
                )
            ]
            success_prefix = "导入完成，共处理数据1条，处理成功1条"
            body_text = "示例公司 普通发票 " + success_prefix + " 共 1 条"
            events: list[str] = []
            page = FakePage(events)

            def fake_wait_ready(current_page: object, store_key: str, workbook_name: str, current_success_prefix: str) -> None:
                self.assertIs(page, current_page)
                self.assertEqual("fuzzy", store_key)
                self.assertEqual("output.xlsx", workbook_name)
                self.assertEqual(success_prefix, current_success_prefix)
                events.append("wait_import_ready")

            with patch.object(runner, "_wait_for_text"):
                with patch.object(runner, "_wait_until"):
                    with patch.object(runner, "_wait_for_batch_import_ready", side_effect=fake_wait_ready):
                        with patch.object(runner, "_log"):
                            runner._import_workbook(page, "fuzzy", workbook_path, rows, summary)  # noqa: SLF001

            self.assertIn("wait_import_ready", events)
            self.assertIn(f"set_input_files:{workbook_path}", events)

    def test_import_workbook_accepts_portal_transformed_amounts(self) -> None:
        class FakeFileInput:
            def count(self) -> int:
                return 1

            def set_input_files(self, value: str) -> None:
                self.last_value = value

        class FakePage:
            def locator(self, selector: str) -> FakeFileInput:
                return FakeFileInput()

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            workbook_path = tmp_path / "output.xlsx"
            summary = SimpleNamespace(row_count=2, total_amount_including_tax=Decimal("438.34"))
            rows = [
                SimpleNamespace(
                    buyer_name="洪玮颖",
                    amount_excluding_tax=Decimal("184.00"),
                    amount_including_tax=Decimal("185.84"),
                ),
                SimpleNamespace(
                    buyer_name="陈丹洁",
                    amount_excluding_tax=Decimal("250.00"),
                    amount_including_tax=Decimal("252.50"),
                ),
            ]
            page = FakePage()
            body_text = "导入完成，共处理数据2条，处理成功2条，发票价税合计共434元。"

            with patch.object(runner, "_wait_for_text"):
                with patch.object(runner, "_wait_until"):
                    with patch.object(runner, "_wait_for_batch_import_ready"):
                        with patch.object(runner, "_log"):
                            runner._import_workbook(page, "fuzzy", workbook_path, rows, summary)  # noqa: SLF001

    def test_import_workbook_surfaces_dppt_http_error_without_waiting_for_timeout(self) -> None:
        class FakeResponse:
            url = "https://dppt.xiamen.chinatax.gov.cn:8443/kpfw/excel/v1/importPlkj?QrkneIXh=value"
            status = 400
            headers = {"content-type": "text/html"}

        class FakeFileInput:
            def __init__(self, page: "FakePage") -> None:
                self.page = page

            def count(self) -> int:
                return 1

            def set_input_files(self, value: str) -> None:
                self.page.response_handler(FakeResponse())

        class FakePage:
            def __init__(self) -> None:
                self.response_handler = lambda response: None
                self.removed_handler: object | None = None

            def on(self, event: str, handler: object) -> None:
                self.assert_event(event)
                self.response_handler = handler

            def remove_listener(self, event: str, handler: object) -> None:
                self.assert_event(event)
                self.removed_handler = handler

            @staticmethod
            def assert_event(event: str) -> None:
                if event != "response":
                    raise AssertionError(f"unexpected event: {event}")

            def locator(self, selector: str) -> FakeFileInput:
                return FakeFileInput(self)

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            page = FakePage()
            summary = SimpleNamespace(row_count=1, total_amount_including_tax=Decimal("123.45"))

            with patch.object(runner, "_wait_for_text"):
                with patch.object(runner, "_log"):
                    with self.assertRaisesRegex(
                        PortalRunnerError,
                        "HTTP 400 content_type=text/html",
                    ):
                        runner._import_workbook(  # noqa: SLF001
                            page,
                            "fuzzy",
                            tmp_path / "output.xlsx",
                            [object()],
                            summary,
                        )

            self.assertIsNotNone(page.removed_handler)

    def test_open_submit_confirmation_waits_for_dialog_to_settle_before_confirm(self) -> None:
        class FakeConfirmButton:
            @property
            def last(self) -> "FakeConfirmButton":
                return self

            def is_visible(self) -> bool:
                return True

            def is_enabled(self) -> bool:
                return True

        class FakeActionButton:
            def __init__(self, events: list[str]) -> None:
                self.events = events

            def click(self) -> None:
                self.events.append("click_batch_submit")

        class FakePage:
            def __init__(self, events: list[str]) -> None:
                self.events = events

            def get_by_role(self, role: str, name: str) -> object:
                self.events.append(f"get_by_role:{role}:{name}")
                if name == "批量开具":
                    return FakeActionButton(self.events)
                return FakeConfirmButton()

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            events: list[str] = []
            page = FakePage(events)

            def fake_wait_until(predicate: object, *, timeout_seconds: float, message: str, interval_seconds: float = 0.5) -> None:
                self.assertTrue(predicate())
                events.append(f"wait_until:{message}")

            def fake_wait_ready(current_page: object, store_key: str, row_count: int, total_amount_including_tax: Decimal) -> None:
                self.assertIs(page, current_page)
                self.assertEqual("fuzzy", store_key)
                self.assertEqual(2, row_count)
                self.assertEqual(Decimal("123.45"), total_amount_including_tax)
                events.append("wait_submit_confirmation_ready")

            with patch.object(runner, "_wait_until", side_effect=fake_wait_until):
                with patch.object(runner, "_wait_for_submit_confirmation_ready", side_effect=fake_wait_ready):
                    runner._open_submit_confirmation(page, "fuzzy", 2, Decimal("123.45"))  # noqa: SLF001

            self.assertLess(
                events.index("wait_submit_confirmation_ready"),
                events.index("wait_until:enable submit confirmation button"),
            )

    def test_wait_for_result_modal_waits_for_dialog_to_settle_before_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            events: list[str] = []
            body_text = (
                "批量开具结果 开具成功发票1份 开具失败发票0份 "
                "1 1001 普通发票 12345678901234567890 123.45 demo@example.com 成功 - 共 1 条"
            )

            def fake_wait_until(predicate: object, *, timeout_seconds: float, message: str, interval_seconds: float = 0.5) -> None:
                self.assertTrue(predicate())
                events.append(f"wait_until:{message}")

            def fake_wait_ready(current_page: object, store_key: str) -> None:
                self.assertEqual("fuzzy", store_key)
                events.append("wait_result_modal_ready")

            body_read_count = 0

            def fake_body_text(_: object) -> str:
                nonlocal body_read_count
                body_read_count += 1
                if body_read_count == 1:
                    events.append("read_result_body_initial")
                else:
                    events.append("read_result_body_parse")
                return body_text

            with patch.object(runner, "_wait_until", side_effect=fake_wait_until):
                with patch.object(runner, "_wait_for_result_modal_ready", side_effect=fake_wait_ready):
                    with patch.object(runner, "_body_text", side_effect=fake_body_text):
                        details, success_count, failure_count, modal_text = runner._wait_for_result_modal(  # noqa: SLF001
                            object(),
                            "fuzzy",
                        )

            self.assertLess(events.index("wait_result_modal_ready"), events.index("read_result_body_parse"))
            self.assertEqual(1, success_count)
            self.assertEqual(0, failure_count)
            self.assertEqual(1, len(details))
            self.assertEqual(body_text, modal_text)

    def test_wait_for_result_modal_parses_failed_row_with_failure_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            body_text = (
                "批量开具结果 开具成功发票0份 开具失败发票1份 "
                "1 1 普通发票 - 218.16 3341663489@qq.com 失败 购买方纳税人识别号有误 共 1 条"
            )

            with patch.object(runner, "_wait_until", side_effect=lambda predicate, **_: self.assertTrue(predicate())):
                with patch.object(runner, "_wait_for_result_modal_ready"):
                    with patch.object(runner, "_body_text", return_value=body_text):
                        details, success_count, failure_count, modal_text = runner._wait_for_result_modal(  # noqa: SLF001
                            object(),
                            "fuzzy",
                        )

            self.assertEqual(0, success_count)
            self.assertEqual(1, failure_count)
            self.assertEqual(body_text, modal_text)
            self.assertEqual(1, len(details))
            self.assertEqual("1", details[0].invoice_serial)
            self.assertEqual("失败", details[0].status)
            self.assertIsNone(details[0].digital_invoice_number)
            self.assertEqual("3341663489@qq.com", details[0].buyer_email)
            self.assertEqual("购买方纳税人识别号有误", details[0].failure_reason)

    def test_reconcile_result_counts_prefers_complete_detail_rows(self) -> None:
        details = [
            PortalIssueDetail(
                invoice_serial="2",
                digital_invoice_number="26352000001889035201",
                buyer_email="first@example.com",
                status="成功",
                failure_reason=None,
            ),
            PortalIssueDetail(
                invoice_serial="1",
                digital_invoice_number="26352000001889158501",
                buyer_email="second@example.com",
                status="成功",
                failure_reason=None,
            ),
        ]

        counts = TaxPortalRunner._reconcile_result_counts_from_details(  # noqa: SLF001
            details,
            1,
            0,
            expected_count=2,
        )

        self.assertEqual((2, 0), counts)

    def test_capture_submit_result_artifacts_writes_screenshot_and_text(self) -> None:
        class FakePage:
            def screenshot(self, path: str, full_page: bool) -> None:
                self.captured_path = path
                self.full_page = full_page
                Path(path).write_text("fake image", encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            page = FakePage()
            artifacts_dir = tmp_path / "artifacts"

            runner._capture_submit_result_artifacts(  # noqa: SLF001
                page,
                artifacts_dir,
                "fuzzy",
                "批量开具结果\n开具成功发票0份\n开具失败发票1份",
            )

            self.assertTrue((artifacts_dir / "fuzzy-submit-result.png").exists())
            self.assertEqual(
                "批量开具结果\n开具成功发票0份\n开具失败发票1份",
                (artifacts_dir / "fuzzy-submit-result.txt").read_text(encoding="utf-8"),
            )

    def test_ensure_company_returns_when_company_appears_after_home_page_settles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            store = StoreConfig(
                store_key="fuzzy",
                store_name="Fuzzy",
                survey_id="1",
                output_xlsx_path=tmp_path / "output.xlsx",
                initial_last_processed_id=1,
                portal_enabled=True,
                portal_company_switch_name="厦门市思明区浮几创意餐厅",
                portal_company_verify_name="厦门市思明区浮几创意餐厅",
            )
            result = SimpleNamespace(workbook_sha256="abc123")
            page = object()

            with patch.object(runner, "_wait_for_home_page_shell_ready") as mocked_wait_home:
                with patch.object(runner, "_page_contains", return_value=False):
                    with patch.object(runner, "_wait_for_company_name", return_value=True) as mocked_wait_company:
                        with patch.object(runner, "_navigate_with_reauth") as mocked_nav:
                            with patch.object(runner, "_update_store_step") as mocked_step:
                                with patch.object(runner, "_log"):
                                    returned_page = runner._ensure_company(page, store, result)  # noqa: SLF001

            self.assertIs(page, returned_page)
            mocked_wait_home.assert_called_once_with(page, store.store_key)
            mocked_wait_company.assert_called_once_with(page, "厦门市思明区浮几创意餐厅", timeout_seconds=5.0)
            mocked_nav.assert_not_called()
            mocked_step.assert_not_called()

    def test_ensure_company_returns_home_when_switch_page_shows_company_already_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            store = StoreConfig(
                store_key="fuzzy",
                store_name="Fuzzy",
                survey_id="1",
                output_xlsx_path=tmp_path / "output.xlsx",
                initial_last_processed_id=1,
                portal_enabled=True,
                portal_company_switch_name="厦门市思明区浮几创意餐厅",
                portal_company_verify_name="厦门市思明区浮几创意餐厅",
            )
            result = SimpleNamespace(workbook_sha256="abc123")
            home_page = object()
            switch_page = object()

            with patch.object(runner, "_wait_for_home_page_shell_ready"):
                with patch.object(runner, "_page_contains", return_value=False):
                    with patch.object(runner, "_wait_for_company_name", return_value=False):
                        with patch.object(runner, "_wait_for_switch_page_ready") as mocked_wait_switch:
                            with patch.object(runner, "_switch_page_shows_active_company", return_value=True):
                                with patch.object(runner, "_select_company_switch_row") as mocked_select:
                                    with patch.object(
                                        runner,
                                        "_navigate_with_reauth",
                                        side_effect=[switch_page, home_page],
                                    ) as mocked_nav:
                                        with patch.object(runner, "_update_store_step") as mocked_step:
                                            with patch.object(runner, "_log"):
                                                returned_page = runner._ensure_company(home_page, store, result)  # noqa: SLF001

            self.assertIs(home_page, returned_page)
            mocked_wait_switch.assert_called_once_with(switch_page, store.store_key)
            mocked_select.assert_not_called()
            self.assertEqual(2, mocked_nav.call_count)
            mocked_nav.assert_any_call(
                home_page,
                config.portal_identity_switch_url,
                result,
                store=store,
                expected_text="企业办税",
                step_name="switch company",
            )
            mocked_nav.assert_any_call(
                switch_page,
                config.portal_home_url,
                result,
                store=store,
                expected_text="我要办税",
                step_name="return home with active company 厦门市思明区浮几创意餐厅",
            )
            mocked_step.assert_called_once()

    def test_ensure_company_waits_for_switch_dialogs_before_confirming(self) -> None:
        class FakeControl:
            def __init__(self, events: list[str], label: str) -> None:
                self.events = events
                self.label = label
                self.checked = False

            @property
            def last(self) -> "FakeControl":
                return self

            def click(self) -> None:
                self.events.append(f"click:{self.label}")
                if "radio:" in self.label:
                    self.checked = True

            def check(self, force: bool = False) -> None:
                self.events.append(f"check:{self.label}:{force}")
                self.checked = True

            def is_visible(self) -> bool:
                return True

            def is_enabled(self) -> bool:
                return True

            def is_checked(self) -> bool:
                return self.checked

        class FakePage:
            def __init__(self, events: list[str]) -> None:
                self.events = events
                self.controls: dict[str, FakeControl] = {}

            def get_by_role(self, role: str, name: str) -> FakeControl:
                self.events.append(f"get_by_role:{role}:{name}")
                key = f"{role}:{name}"
                if key not in self.controls:
                    self.controls[key] = FakeControl(self.events, key)
                return self.controls[key]

            def get_by_text(self, text: str, exact: bool = False) -> FakeControl:
                self.events.append(f"get_by_text:{text}:{exact}")
                return FakeControl(self.events, f"text:{text}")

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            store = StoreConfig(
                store_key="fuzzy",
                store_name="Fuzzy",
                survey_id="1",
                output_xlsx_path=tmp_path / "output.xlsx",
                initial_last_processed_id=1,
                portal_enabled=True,
                portal_company_switch_name="厦门市思明区浮几创意餐厅",
                portal_company_verify_name="厦门市思明区浮几创意餐厅",
            )
            result = SimpleNamespace(workbook_sha256="abc123")
            events: list[str] = []
            page = FakePage(events)

            def fake_wait_until(predicate: object, *, timeout_seconds: float, message: str, interval_seconds: float = 0.5) -> None:
                self.assertTrue(predicate())
                events.append(f"wait_until:{message}")

            with patch.object(runner, "_wait_for_home_page_shell_ready"):
                with patch.object(runner, "_page_contains", return_value=False):
                    with patch.object(runner, "_wait_for_company_name", return_value=False):
                        with patch.object(runner, "_wait_for_switch_page_ready"):
                            with patch.object(runner, "_switch_page_shows_active_company", return_value=False):
                                with patch.object(runner, "_select_company_switch_row", return_value="厦门市思明区浮几创意餐厅"):
                                    with patch.object(runner, "_wait_for_switch_confirmation_ready") as mocked_wait_confirm:
                                        with patch.object(runner, "_wait_for_role_selection_ready") as mocked_wait_role:
                                            with patch.object(runner, "_navigate_with_reauth", side_effect=[page, page]):
                                                with patch.object(runner, "_wait_until", side_effect=fake_wait_until):
                                                    with patch.object(runner, "_wait_for_company_switch_completion"):
                                                        with patch.object(runner, "_update_store_step"):
                                                            with patch.object(runner, "_log"):
                                                                returned_page = runner._ensure_company(page, store, result)  # noqa: SLF001

            self.assertIs(page, returned_page)
            mocked_wait_confirm.assert_called_once_with(page, store.store_key)
            mocked_wait_role.assert_called_once_with(page, store.store_key, "法定代表人")

    def test_ensure_company_waits_for_switch_completion_before_fallback_navigation(self) -> None:
        class FakeControl:
            def __init__(self) -> None:
                self.enabled = True

            @property
            def last(self) -> "FakeControl":
                return self

            def click(self) -> None:
                return None

            def is_enabled(self) -> bool:
                return self.enabled

            def is_visible(self) -> bool:
                return True

        class FakePage:
            def __init__(self) -> None:
                self.url = "https://tpass.xiamen.chinatax.gov.cn:8443/#/identitySwitch/enterprise"
                self.body_text = "身份类型选择 法定代表人 确定"

            def locator(self, selector: str) -> object:
                class FakeLocator:
                    def __init__(self, page: "FakePage") -> None:
                        self.page = page

                    def inner_text(self) -> str:
                        return self.page.body_text

                return FakeLocator(self)

            def get_by_role(self, role: str, name: str) -> FakeControl:
                return FakeControl()

            def get_by_text(self, text: str, exact: bool = False) -> FakeControl:
                return FakeControl()

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            store = StoreConfig(
                store_key="peanut",
                store_name="Peanut",
                survey_id="1",
                output_xlsx_path=tmp_path / "output.xlsx",
                initial_last_processed_id=1,
                portal_enabled=True,
                portal_company_switch_name="厦门市思明区花生创意餐厅（个体工商户）",
                portal_company_verify_name="厦门市思明区花生创意餐厅（个体工商户）",
            )
            result = SimpleNamespace(workbook_sha256="abc123")
            page = FakePage()

            def fake_wait_for_switch_completion(current_page: object, store_key: str, verify_name: str) -> None:
                self.assertIs(page, current_page)
                self.assertEqual("peanut", store_key)
                self.assertEqual("厦门市思明区花生创意餐厅（个体工商户）", verify_name)
                page.url = "https://etax.xiamen.chinatax.gov.cn:8443/loginb/"
                page.body_text = "首页 我要办税 厦门市思明区花生创意餐厅（个体工商户）"

            with patch.object(runner, "_wait_for_home_page_shell_ready"):
                with patch.object(runner, "_page_contains", side_effect=lambda current_page, text: text in current_page.body_text):
                    with patch.object(runner, "_wait_for_company_name", return_value=False):
                        with patch.object(runner, "_wait_for_switch_page_ready"):
                            with patch.object(runner, "_switch_page_shows_active_company", return_value=False):
                                with patch.object(runner, "_select_company_switch_row", return_value="厦门市思明区花生创意餐厅（个体工商户）"):
                                    with patch.object(runner, "_wait_for_switch_confirmation_ready"):
                                        with patch.object(runner, "_wait_for_role_selection_ready"):
                                            with patch.object(runner, "_wait_for_company_switch_completion", side_effect=fake_wait_for_switch_completion):
                                                with patch.object(runner, "_navigate_with_reauth", side_effect=[page]) as mocked_nav:
                                                    with patch.object(runner, "_update_store_step"):
                                                        with patch.object(runner, "_log"):
                                                            returned_page = runner._ensure_company(page, store, result)  # noqa: SLF001

            self.assertIs(page, returned_page)
            mocked_nav.assert_called_once()

    def test_ensure_company_reopens_target_area_home_when_portal_area_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
                portal_home_url="https://etax.{portal_area}.chinatax.gov.cn:8443/loginb/",
                portal_identity_switch_url=(
                    "https://tpass.{portal_area}.chinatax.gov.cn:8443/#/identitySwitch/enterprise"
                    "?client_id=y56b7aay5brf48f8aa7bf24dd54d775r"
                ),
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            store = StoreConfig(
                store_key="fuzzy_qz",
                store_name="FuzzyQZ",
                survey_id="27114382",
                output_xlsx_path=tmp_path / "output.xlsx",
                initial_last_processed_id=1,
                portal_enabled=True,
                portal_company_switch_name="泉州市鲤城区浮几餐饮店（个体工商户）（待确认）",
                portal_company_verify_name="泉州市鲤城区浮几餐饮店（个体工商户）",
                portal_area="quanzhou",
                portal_area_name="泉州",
            )
            result = SimpleNamespace(workbook_sha256="abc123")
            current_home_page = SimpleNamespace(url="https://etax.xiamen.chinatax.gov.cn:8443/loginb/")
            target_home_page = SimpleNamespace(url="https://etax.quanzhou.chinatax.gov.cn:8443/loginb/")
            switch_page = SimpleNamespace(url="https://tpass.quanzhou.chinatax.gov.cn:8443/#/identitySwitch/enterprise")
            final_home_page = SimpleNamespace(url="https://etax.quanzhou.chinatax.gov.cn:8443/loginb/")

            with patch.object(runner, "_wait_for_home_page_shell_ready"):
                with patch.object(runner, "_page_contains", return_value=False):
                    with patch.object(runner, "_wait_for_company_name", return_value=False):
                        with patch.object(runner, "_wait_for_switch_page_ready"):
                            with patch.object(runner, "_switch_page_shows_active_company", return_value=True):
                                with patch.object(
                                    runner,
                                    "_navigate_with_reauth",
                                    side_effect=[target_home_page, switch_page, final_home_page],
                                ) as mocked_nav:
                                    with patch.object(runner, "_update_store_step"):
                                        with patch.object(runner, "_log"):
                                            returned_page = runner._ensure_company(current_home_page, store, result)  # noqa: SLF001

            self.assertIs(final_home_page, returned_page)
            self.assertEqual(3, mocked_nav.call_count)
            mocked_nav.assert_any_call(
                current_home_page,
                "https://etax.quanzhou.chinatax.gov.cn:8443/loginb/",
                result,
                store=store,
                expected_text="我要办税",
                step_name="open 泉州 portal home",
            )
            mocked_nav.assert_any_call(
                target_home_page,
                "https://tpass.quanzhou.chinatax.gov.cn:8443/#/identitySwitch/enterprise?client_id=y56b7aay5brf48f8aa7bf24dd54d775r",
                result,
                store=store,
                expected_text="企业办税",
                step_name="switch company",
            )
            mocked_nav.assert_any_call(
                switch_page,
                "https://etax.quanzhou.chinatax.gov.cn:8443/loginb/",
                result,
                store=store,
                expected_text="我要办税",
                step_name="return home with active company 泉州市鲤城区浮几餐饮店（个体工商户）",
            )

    def test_wait_for_home_page_ready_waits_for_load_states_texts_and_network_idle(self) -> None:
        class FakePage:
            def __init__(self) -> None:
                self.load_states: list[tuple[str, int]] = []

            def wait_for_load_state(self, state: str, timeout: int) -> None:
                self.load_states.append((state, timeout))

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
                portal_action_timeout_ms=4321,
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            store = StoreConfig(
                store_key="fuzzy",
                store_name="Fuzzy",
                survey_id="1",
                output_xlsx_path=tmp_path / "output.xlsx",
                initial_last_processed_id=1,
                portal_enabled=True,
                portal_company_switch_name="厦门市思明区浮几创意餐厅",
                portal_company_verify_name="厦门市思明区浮几创意餐厅",
            )
            page = FakePage()

            with patch.object(runner, "_wait_for_text") as mocked_wait_text:
                with patch.object(runner, "_body_text", return_value="首页 我要办税 厦门市思明区浮几创意餐厅"):
                    with patch.object(runner, "_log"):
                        runner._wait_for_home_page_ready(page, store)  # noqa: SLF001

            self.assertEqual(
                [
                    ("domcontentloaded", 4321),
                    ("load", 4321),
                    ("networkidle", 4321),
                ],
                page.load_states,
            )
            self.assertEqual(
                [
                    ((page, "我要办税"), {"timeout_ms": 4321}),
                    ((page, "厦门市思明区浮几创意餐厅"), {"timeout_ms": 4321}),
                ],
                mocked_wait_text.call_args_list,
            )

    def test_wait_for_home_page_shell_ready_uses_minimum_60_second_timeout(self) -> None:
        class FakePage:
            def __init__(self) -> None:
                self.load_states: list[tuple[str, int]] = []

            def wait_for_load_state(self, state: str, timeout: int) -> None:
                self.load_states.append((state, timeout))

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
                portal_action_timeout_ms=30000,
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            page = FakePage()

            with patch.object(runner, "_wait_for_text") as mocked_wait_text:
                with patch.object(runner, "_body_text", return_value="首页 我要办税"):
                    with patch.object(runner, "_log"):
                        runner._wait_for_home_page_shell_ready(page, "peanut")  # noqa: SLF001

            self.assertEqual(
                [
                    ("domcontentloaded", 60000),
                    ("load", 60000),
                    ("networkidle", HOME_PAGE_NETWORKIDLE_GRACE_MS),
                ],
                page.load_states,
            )
            self.assertEqual(
                [
                    ((page, "我要办税"), {"timeout_ms": 60000}),
                ],
                mocked_wait_text.call_args_list,
            )

    def test_wait_for_home_page_ready_continues_when_network_stays_busy(self) -> None:
        class FakePage:
            def __init__(self) -> None:
                self.load_states: list[tuple[str, int]] = []

            def wait_for_load_state(self, state: str, timeout: int) -> None:
                self.load_states.append((state, timeout))
                if state == "networkidle":
                    raise RuntimeError("Timeout 4321ms exceeded.")

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
                portal_action_timeout_ms=4321,
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            store = StoreConfig(
                store_key="fuzzy",
                store_name="Fuzzy",
                survey_id="1",
                output_xlsx_path=tmp_path / "output.xlsx",
                initial_last_processed_id=1,
                portal_enabled=True,
                portal_company_switch_name="厦门市思明区浮几创意餐厅",
                portal_company_verify_name="厦门市思明区浮几创意餐厅",
            )
            page = FakePage()

            with patch.object(runner, "_wait_for_text"):
                with patch.object(runner, "_body_text", return_value="首页 我要办税 厦门市思明区浮几创意餐厅"):
                    with patch.object(runner, "_log") as mocked_log:
                        runner._wait_for_home_page_ready(page, store)  # noqa: SLF001

            self.assertEqual(
                [
                    ("domcontentloaded", 4321),
                    ("load", 4321),
                    ("networkidle", 4321),
                ],
                page.load_states,
            )
            self.assertTrue(
                any(
                    "authenticated home page network requests did not go idle within 4321ms" in call.args[1]
                    for call in mocked_log.call_args_list
                )
            )

    def test_wait_for_batch_page_ready_still_requires_networkidle(self) -> None:
        class FakePage:
            def __init__(self) -> None:
                self.load_states: list[tuple[str, int]] = []

            def wait_for_load_state(self, state: str, timeout: int) -> None:
                self.load_states.append((state, timeout))
                if state == "networkidle":
                    raise RuntimeError("Timeout 30000ms exceeded.")

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
                portal_action_timeout_ms=30000,
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            page = FakePage()

            with patch.object(runner, "_wait_for_text"):
                with patch.object(runner, "_body_text", return_value="批量开票"):
                    with self.assertRaises(PortalRunnerError):
                        runner._wait_for_batch_page_ready(page, "fuzzy")  # noqa: SLF001

    def test_run_store_waits_for_home_page_before_opening_batch_page(self) -> None:
        class FakeBatchPage:
            def __init__(self, events: list[str]) -> None:
                self.events = events

            def set_default_timeout(self, timeout: int) -> None:
                self.events.append(f"set_default_timeout:{timeout}")

            def close(self) -> None:
                self.events.append("close_batch_page")

        class FakeContext:
            def __init__(self, events: list[str], home_page: FakeHomePage) -> None:
                self.events = events
                self.pages = [home_page]

            def new_page(self) -> FakeBatchPage:
                self.events.append("new_page")
                return FakeBatchPage(self.events)

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            store = StoreConfig(
                store_key="fuzzy",
                store_name="Fuzzy",
                survey_id="1",
                output_xlsx_path=tmp_path / "output.xlsx",
                initial_last_processed_id=1,
                portal_enabled=True,
                portal_company_switch_name="厦门市思明区浮几创意餐厅",
                portal_company_verify_name="厦门市思明区浮几创意餐厅",
            )
            summary = SimpleNamespace(row_count=1, total_amount_including_tax=Decimal("123.45"))
            rows = [object()]
            home_page = object()
            events: list[str] = []

            def fake_wait_for_home_page_ready(page: object, current_store: StoreConfig) -> None:
                self.assertIs(home_page, page)
                self.assertIs(store, current_store)
                events.append("wait_for_home_page_ready")

            def fake_wait_before_open_batch_page(store_key: str) -> None:
                self.assertEqual("fuzzy", store_key)
                events.append("wait_before_open_batch_page")

            def fake_wait_for_batch_page(page: object, current_store: StoreConfig, current_result: object) -> object:
                self.assertIs(home_page, page)
                self.assertIs(store, current_store)
                self.assertIsNotNone(current_result)
                events.append("wait_for_batch_page")
                return page

            with patch("app.portal_runner.load_portal_issue_rows", return_value=rows):
                with patch("app.portal_runner.summarize_portal_issue_rows", return_value=summary):
                    with patch("app.portal_runner.sha256_file", return_value="abc123"):
                        with patch.object(runner, "_goto"):
                            with patch.object(runner, "_ensure_logged_in", return_value=home_page):
                                with patch.object(runner, "_ensure_company", return_value=home_page):
                                    with patch.object(
                                        runner,
                                        "_wait_for_home_page_ready",
                                        side_effect=fake_wait_for_home_page_ready,
                                    ):
                                        with patch.object(
                                            runner,
                                            "_wait_before_open_batch_page",
                                            side_effect=fake_wait_before_open_batch_page,
                                        ):
                                            with patch.object(runner, "_install_network_diag"):
                                                with patch.object(
                                                    runner,
                                                    "_wait_for_batch_page",
                                                    side_effect=fake_wait_for_batch_page,
                                                ):
                                                    with patch.object(runner, "_ensure_batch_page_clean"):
                                                        with patch.object(runner, "_import_workbook"):
                                                            with patch.object(runner, "_select_all_rows"):
                                                                with patch.object(
                                                                    runner,
                                                                    "_finalize_result",
                                                                    side_effect=lambda result: result,
                                                                ):
                                                                    with patch.object(runner, "_log"):
                                                                        result = runner._run_store(  # noqa: SLF001
                                                                            FakeContext(events, home_page),
                                                                            home_page,
                                                                            store,
                                                                        )

            self.assertEqual("validated", result.status)
            self.assertLess(
                events.index("wait_for_home_page_ready"),
                events.index("wait_before_open_batch_page"),
            )
            self.assertLess(
                events.index("wait_before_open_batch_page"),
                events.index("wait_for_batch_page"),
            )

    def test_run_store_reuses_existing_home_page_without_reopening_loginb_in_chrome_cdp_mode(self) -> None:
        class FakeBatchPage:
            def __init__(self, events: list[str]) -> None:
                self.events = events

            def set_default_timeout(self, timeout: int) -> None:
                self.events.append(f"set_default_timeout:{timeout}")

            def close(self) -> None:
                self.events.append("close_batch_page")

        class FakeHomePage:
            def __init__(self, events: list[str]) -> None:
                self.events = events
                self.url = "https://etax.xiamen.chinatax.gov.cn:8443/loginb/"

            def locator(self, selector: str) -> object:
                class FakeLocator:
                    def inner_text(self) -> str:
                        return "首页 我要办税 厦门市思明区浮几创意餐厅"

                return FakeLocator()

        class FakeContext:
            def __init__(self, events: list[str], home_page: FakeHomePage) -> None:
                self.events = events
                self.pages = [home_page]

            def new_page(self) -> FakeBatchPage:
                self.events.append("new_page")
                return FakeBatchPage(self.events)

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=None,
                portal_browser_backend="chrome_cdp",
                portal_chrome_cdp_url="http://127.0.0.1:9222",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            store = StoreConfig(
                store_key="fuzzy",
                store_name="Fuzzy",
                survey_id="1",
                output_xlsx_path=tmp_path / "output.xlsx",
                initial_last_processed_id=1,
                portal_enabled=True,
                portal_company_switch_name="厦门市思明区浮几创意餐厅",
                portal_company_verify_name="厦门市思明区浮几创意餐厅",
            )
            summary = SimpleNamespace(row_count=1, total_amount_including_tax=Decimal("123.45"))
            rows = [object()]
            events: list[str] = []
            home_page = FakeHomePage(events)

            with patch("app.portal_runner.load_portal_issue_rows", return_value=rows):
                with patch("app.portal_runner.summarize_portal_issue_rows", return_value=summary):
                    with patch("app.portal_runner.sha256_file", return_value="abc123"):
                        with patch.object(runner, "_goto") as mocked_goto:
                            with patch.object(runner, "_ensure_logged_in", return_value=home_page):
                                with patch.object(runner, "_ensure_company", return_value=home_page):
                                    with patch.object(runner, "_wait_for_home_page_ready"):
                                        with patch.object(runner, "_wait_before_open_batch_page"):
                                            with patch.object(runner, "_install_network_diag"):
                                                with patch.object(runner, "_wait_for_batch_page", side_effect=lambda page, *_: page):
                                                    with patch.object(runner, "_ensure_batch_page_clean"):
                                                        with patch.object(runner, "_import_workbook"):
                                                            with patch.object(runner, "_select_all_rows"):
                                                                with patch.object(
                                                                    runner,
                                                                    "_finalize_result",
                                                                    side_effect=lambda result: result,
                                                                ):
                                                                    with patch.object(runner, "_log"):
                                                                        result = runner._run_store(  # noqa: SLF001
                                                                            FakeContext(events, home_page),
                                                                            home_page,
                                                                            store,
                                                                        )

            self.assertEqual("validated", result.status)
            mocked_goto.assert_not_called()

    def test_run_store_waits_three_seconds_after_successful_submit(self) -> None:
        class FakeBatchPage:
            def __init__(self, events: list[str]) -> None:
                self.events = events

            def set_default_timeout(self, timeout: int) -> None:
                self.events.append(f"set_default_timeout:{timeout}")

            def close(self) -> None:
                self.events.append("close_batch_page")

        class FakeContext:
            def __init__(self, events: list[str]) -> None:
                self.events = events

            def new_page(self) -> FakeBatchPage:
                self.events.append("new_page")
                return FakeBatchPage(self.events)

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=True)
            store = StoreConfig(
                store_key="fuzzy",
                store_name="Fuzzy",
                survey_id="1",
                output_xlsx_path=tmp_path / "output.xlsx",
                initial_last_processed_id=1,
                portal_enabled=True,
                portal_company_switch_name="厦门市思明区浮几创意餐厅",
                portal_company_verify_name="厦门市思明区浮几创意餐厅",
            )
            summary = SimpleNamespace(row_count=1, total_amount_including_tax=Decimal("123.45"))
            rows = [object()]
            home_page = object()
            events: list[str] = []

            with patch("app.portal_runner.load_portal_issue_rows", return_value=rows):
                with patch("app.portal_runner.summarize_portal_issue_rows", return_value=summary):
                    with patch("app.portal_runner.sha256_file", return_value="abc123"):
                        with patch("app.portal_runner.sleep", lambda seconds: events.append(f"sleep:{seconds}")):
                            with patch.object(runner, "_goto"):
                                with patch.object(runner, "_ensure_logged_in", return_value=home_page):
                                    with patch.object(runner, "_ensure_company", return_value=home_page):
                                        with patch.object(runner, "_wait_for_home_page_ready"):
                                            with patch.object(runner, "_wait_before_open_batch_page"):
                                                with patch.object(runner, "_install_network_diag"):
                                                    with patch.object(runner, "_wait_for_batch_page", side_effect=lambda page, *_: page):
                                                        with patch.object(runner, "_ensure_batch_page_clean"):
                                                            with patch.object(runner, "_import_workbook"):
                                                                with patch.object(runner, "_select_all_rows"):
                                                                    with patch.object(runner, "_open_submit_confirmation"):
                                                                        with patch.object(runner, "_confirm_submit"):
                                                                            with patch.object(
                                                                                runner,
                                                                                "_wait_for_result_modal",
                                                                                return_value=([], 1, 0, "批量开具结果"),
                                                                            ):
                                                                                with patch.object(
                                                                                    runner,
                                                                                    "_finalize_result",
                                                                                    side_effect=lambda result: result,
                                                                                ):
                                                                                    with patch.object(runner, "_log"):
                                                                                        result = runner._run_store(  # noqa: SLF001
                                                                                            FakeContext(events),
                                                                                            home_page,
                                                                                            store,
                                                                                        )

            self.assertEqual("success", result.status)
            self.assertIn("sleep:3.0", events)

    def test_run_store_captures_submit_result_artifacts_when_portal_reports_failures(self) -> None:
        class FakeBatchPage:
            def __init__(self, events: list[str]) -> None:
                self.events = events

            def set_default_timeout(self, timeout: int) -> None:
                self.events.append(f"set_default_timeout:{timeout}")

            def close(self) -> None:
                self.events.append("close_batch_page")

        class FakeContext:
            def __init__(self, events: list[str]) -> None:
                self.events = events

            def new_page(self) -> FakeBatchPage:
                self.events.append("new_page")
                return FakeBatchPage(self.events)

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=True)
            store = StoreConfig(
                store_key="fuzzy",
                store_name="Fuzzy",
                survey_id="1",
                output_xlsx_path=tmp_path / "output.xlsx",
                initial_last_processed_id=1,
                portal_enabled=True,
                portal_company_switch_name="厦门市思明区浮几创意餐厅",
                portal_company_verify_name="厦门市思明区浮几创意餐厅",
            )
            summary = SimpleNamespace(row_count=1, total_amount_including_tax=Decimal("123.45"))
            rows = [object()]
            home_page = object()
            detail = PortalIssueDetail(
                invoice_serial="1",
                digital_invoice_number=None,
                buyer_email="demo@example.com",
                status="失败",
                failure_reason="购买方纳税人识别号有误",
            )
            events: list[str] = []

            with patch("app.portal_runner.load_portal_issue_rows", return_value=rows):
                with patch("app.portal_runner.summarize_portal_issue_rows", return_value=summary):
                    with patch("app.portal_runner.sha256_file", return_value="abc123"):
                        with patch.object(runner, "_goto"):
                            with patch.object(runner, "_ensure_logged_in", return_value=home_page):
                                with patch.object(runner, "_ensure_company", return_value=home_page):
                                    with patch.object(runner, "_wait_for_home_page_ready"):
                                        with patch.object(runner, "_wait_before_open_batch_page"):
                                            with patch.object(runner, "_install_network_diag"):
                                                with patch.object(runner, "_wait_for_batch_page", side_effect=lambda page, *_: page):
                                                    with patch.object(runner, "_ensure_batch_page_clean"):
                                                        with patch.object(runner, "_import_workbook"):
                                                            with patch.object(runner, "_select_all_rows"):
                                                                with patch.object(runner, "_open_submit_confirmation"):
                                                                    with patch.object(runner, "_confirm_submit"):
                                                                        with patch.object(
                                                                            runner,
                                                                            "_wait_for_result_modal",
                                                                            return_value=([detail], 0, 1, "批量开具结果 失败"),
                                                                        ):
                                                                            with patch.object(
                                                                                runner,
                                                                                "_capture_submit_result_artifacts",
                                                                            ) as mocked_capture:
                                                                                with patch.object(
                                                                                    runner,
                                                                                    "_finalize_result",
                                                                                    side_effect=lambda result: result,
                                                                                ):
                                                                                    with patch.object(runner, "_log"):
                                                                                        result = runner._run_store(  # noqa: SLF001
                                                                                            FakeContext(events),
                                                                                            home_page,
                                                                                            store,
                                                                                        )

            self.assertEqual("failed", result.status)
            mocked_capture.assert_called_once()

    def test_ensure_batch_page_clean_clears_existing_imported_rows(self) -> None:
        class FakeButton:
            def __init__(self, page: "FakePage", name: str) -> None:
                self.page = page
                self.name = name

            @property
            def last(self) -> "FakeButton":
                return self

            def click(self, timeout: int | None = None) -> None:
                if self.name == "清空导入":
                    self.page.body_text = "是否清空所有已导入内容？ 取消 确定"
                else:
                    self.page.body_text = "共 0 条"

        class FakePage:
            def __init__(self) -> None:
                self.body_text = "重新选择 共 2 条"

            def locator(self, selector: str) -> object:
                class FakeLocator:
                    def __init__(self, page: "FakePage") -> None:
                        self.page = page

                    def inner_text(self) -> str:
                        return self.page.body_text

                return FakeLocator(self)

            def get_by_role(self, role: str, name: str) -> FakeButton:
                return FakeButton(self, name)

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AppConfig(
                timezone="Asia/Shanghai",
                survey_cookie="cookie",
                survey_export_url="https://example.com/export",
                survey_export_method="POST",
                survey_export_body_template="",
                survey_export_download_url_path=None,
                survey_extra_headers={},
                default_attachment_question_id=None,
                openai_base_url="https://example.com/v1",
                openai_api_key="key",
                openai_model="model",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="user",
                smtp_password="pass",
                smtp_from="from@example.com",
                smtp_to=["to@example.com"],
                template_xlsx_path=tmp_path / "template.xlsx",
                state_db_path=tmp_path / "state.db",
                stores_config_path=tmp_path / "stores.yaml",
                backups_root=tmp_path / "backups",
                portal_user_data_dir=tmp_path / "profile",
            )
            runner = TaxPortalRunner(config, StateStore(tmp_path / "state.db"), submit=False)
            page = FakePage()

            with patch.object(runner, "_log"):
                runner._ensure_batch_page_clean(page, "fuzzy")  # noqa: SLF001

            self.assertEqual("共 0 条", page.body_text)

    def test_click_waits_one_second_before_click(self) -> None:
        class FakePage:
            def __init__(self, events: list[str]) -> None:
                self.events = events

            def wait_for_timeout(self, timeout_ms: int) -> None:
                self.events.append(f"wait:{timeout_ms}")

        class FakeTarget:
            def __init__(self, page: FakePage, events: list[str]) -> None:
                self.page = page
                self.events = events

            def click(self, timeout: int | None = None, force: bool = False) -> None:
                self.events.append(f"click:{timeout}:{force}")

        runner = TaxPortalRunner.__new__(TaxPortalRunner)
        events: list[str] = []
        page = FakePage(events)
        target = FakeTarget(page, events)

        runner._click(target, timeout=3000, force=True)  # noqa: SLF001

        self.assertEqual([f"wait:{BROWSER_CLICK_DELAY_MS}", "click:3000:True"], events)

    def test_check_waits_one_second_before_check(self) -> None:
        class FakePage:
            def __init__(self, events: list[str]) -> None:
                self.events = events

            def wait_for_timeout(self, timeout_ms: int) -> None:
                self.events.append(f"wait:{timeout_ms}")

        class FakeTarget:
            def __init__(self, page: FakePage, events: list[str]) -> None:
                self.page = page
                self.events = events

            def check(self, force: bool = False) -> None:
                self.events.append(f"check:{force}")

        runner = TaxPortalRunner.__new__(TaxPortalRunner)
        events: list[str] = []
        page = FakePage(events)
        target = FakeTarget(page, events)

        runner._check(target, force=True)  # noqa: SLF001

        self.assertEqual([f"wait:{BROWSER_CLICK_DELAY_MS}", "check:True"], events)

    def test_mouse_click_waits_one_second_before_click(self) -> None:
        class FakeMouse:
            def __init__(self, events: list[str]) -> None:
                self.events = events

            def click(self, x: int, y: int) -> None:
                self.events.append(f"mouse_click:{x}:{y}")

        class FakePage:
            def __init__(self, events: list[str]) -> None:
                self.events = events
                self.mouse = FakeMouse(events)

            def wait_for_timeout(self, timeout_ms: int) -> None:
                self.events.append(f"wait:{timeout_ms}")

        runner = TaxPortalRunner.__new__(TaxPortalRunner)
        events: list[str] = []
        page = FakePage(events)

        runner._mouse_click(page, 55, 515)  # noqa: SLF001

        self.assertEqual([f"wait:{BROWSER_CLICK_DELAY_MS}", "mouse_click:55:515"], events)

    def test_company_switch_candidates_fallback_to_verify_name(self) -> None:
        store = StoreConfig(
            store_key="peanut",
            store_name="Peanut",
            survey_id="1",
            output_xlsx_path=Path("/tmp/peanut.xlsx"),
            initial_last_processed_id=1,
            portal_enabled=True,
            portal_company_switch_name="厦门市思明区花生创意餐厅（个体工商户）（待确认）",
            portal_company_verify_name="厦门市思明区花生创意餐厅（个体工商户）",
        )

        self.assertEqual(
            [
                "厦门市思明区花生创意餐厅（个体工商户）（待确认）",
                "厦门市思明区花生创意餐厅（个体工商户）",
            ],
            TaxPortalRunner._company_switch_candidates(store),  # noqa: SLF001
        )
