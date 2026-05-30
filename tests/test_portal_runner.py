from __future__ import annotations

import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.models import AppConfig, StoreConfig
from app.portal_runner import QR_REFRESH_GRACE_SECONDS, TaxPortalRunner
from app.state import StateStore


class PortalRunnerUrlTests(unittest.TestCase):
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

            monotonic_values = iter([0.0, 1.0, 2.0, 3.0])

            with patch("app.portal_runner.monotonic", side_effect=lambda: next(monotonic_values)):
                with patch("app.portal_runner.sleep", lambda _: None):
                    with patch.object(runner, "_is_home_page", side_effect=[False, False, True]):
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

            monotonic_values = iter(
                [0.0, 1.0, 2.0, 2.0 + QR_REFRESH_GRACE_SECONDS + 0.1, 9.0, 10.0]
            )

            with patch("app.portal_runner.monotonic", side_effect=lambda: next(monotonic_values)):
                with patch("app.portal_runner.sleep", lambda _: None):
                    with patch.object(runner, "_is_home_page", side_effect=[False, False, False, True]):
                        with patch.object(runner, "_is_public_landing_page", return_value=False):
                            with patch.object(runner, "_page_requires_reauth", return_value=True):
                                with patch.object(runner, "_try_refresh_login_qr") as mocked_refresh:
                                    runner._ensure_logged_in(page, result)  # noqa: SLF001

            mocked_refresh.assert_called_once_with(page)

    def test_wait_for_batch_page_uses_batch_issue_text_as_expected_text(self) -> None:
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

            with patch.object(runner, "_update_store_step") as mocked_step:
                with patch.object(runner, "_navigate_with_reauth", return_value=page) as mocked_nav:
                    with patch.object(runner, "_wait_for_batch_page_ready_with_recovery") as mocked_wait_ready:
                        with patch.object(runner, "_log"):
                            runner._wait_for_batch_page(page, store, result)  # noqa: SLF001

            mocked_step.assert_called_once()
            mocked_nav.assert_called_once_with(
                page,
                config.portal_batch_issue_url,
                result,
                expected_text=None,
                step_name="open batch issue page",
            )
            mocked_wait_ready.assert_called_once_with(page, store.store_key)

    def test_wait_for_batch_page_ready_with_recovery_refreshes_after_session_invalid_prompt(self) -> None:
        class FakePage:
            def __init__(self) -> None:
                self.body_text = "系统检测到电票平台会话已失效或电局账号已退出，需刷新重新操作"
                self.reload_count = 0

            def locator(self, selector: str) -> object:
                class FakeLocator:
                    def __init__(self, page: "FakePage") -> None:
                        self.page = page

                    def inner_text(self) -> str:
                        return self.page.body_text

                return FakeLocator(self)

            def reload(self, wait_until: str) -> None:
                self.reload_count += 1
                self.last_wait_until = wait_until
                self.body_text = "批量开票 页面已恢复"

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

            with patch.object(runner, "_wait_for_batch_page_ready") as mocked_wait_ready:
                with patch.object(runner, "_log"):
                    runner._wait_for_batch_page_ready_with_recovery(page, "fuzzy")  # noqa: SLF001

            self.assertEqual(1, page.reload_count)
            self.assertEqual("domcontentloaded", page.last_wait_until)
            mocked_wait_ready.assert_called_once_with(page, "fuzzy")

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
                                                    with patch.object(runner, "_assert_batch_page_clean"):
                                                        with patch.object(runner, "_import_workbook"):
                                                            with patch.object(
                                                                runner,
                                                                "_finalize_result",
                                                                side_effect=lambda result: result,
                                                            ):
                                                                with patch.object(runner, "_log"):
                                                                    result = runner._run_store(context, home_page, store)  # noqa: SLF001

            self.assertEqual("validated", result.status)
            self.assertEqual(1, context.new_page_count)
            self.assertEqual(1, events.count("wait_for_home_page_ready"))
            self.assertIn("sleep_before_batch", events)
            self.assertLess(events.index("wait_for_home_page_ready"), events.index("sleep_before_batch"))
            self.assertLess(events.index("sleep_before_batch"), events.index("new_page:attempt1"))

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
        class FakeClickable:
            def __init__(self, events: list[str]) -> None:
                self.events = events

            def click(self) -> None:
                self.events.append("click_select_file")

        class FakeChooserValue:
            def __init__(self, events: list[str]) -> None:
                self.events = events

            def set_files(self, value: str) -> None:
                self.events.append(f"set_files:{value}")

        class FakeChooserContext:
            def __init__(self, events: list[str]) -> None:
                self.events = events
                self.value = FakeChooserValue(events)

            def __enter__(self) -> "FakeChooserContext":
                self.events.append("enter_file_chooser")
                return self

            def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
                self.events.append("exit_file_chooser")
                return None

        class FakePage:
            def __init__(self, events: list[str]) -> None:
                self.events = events

            def expect_file_chooser(self) -> FakeChooserContext:
                return FakeChooserContext(self.events)

            def get_by_text(self, text: str, exact: bool = False) -> FakeClickable:
                self.events.append(f"get_by_text:{text}:{exact}")
                return FakeClickable(self.events)

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
            body_text = "示例公司 100.00 123.45 123.45 " + success_prefix
            events: list[str] = []
            page = FakePage(events)

            def fake_wait_ready(current_page: object, store_key: str, workbook_name: str, current_success_prefix: str) -> None:
                self.assertIs(page, current_page)
                self.assertEqual("fuzzy", store_key)
                self.assertEqual("output.xlsx", workbook_name)
                self.assertEqual(success_prefix, current_success_prefix)
                events.append("wait_import_ready")

            def fake_body_text(_: object) -> str:
                events.append("read_body_text")
                return body_text

            with patch.object(runner, "_wait_for_text"):
                with patch.object(runner, "_wait_until"):
                    with patch.object(runner, "_wait_for_batch_import_ready", side_effect=fake_wait_ready):
                        with patch.object(runner, "_body_text", side_effect=fake_body_text):
                            with patch.object(runner, "_log"):
                                runner._import_workbook(page, "fuzzy", workbook_path, rows, summary)  # noqa: SLF001

            self.assertLess(events.index("wait_import_ready"), events.index("read_body_text"))

    def test_open_submit_confirmation_waits_for_dialog_to_settle_before_confirm(self) -> None:
        class FakeConfirmButton:
            @property
            def last(self) -> "FakeConfirmButton":
                return self

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
            expected_amount_text = "价税合计123.45元"
            body_text = f"本次勾选批量开具发票2份 {expected_amount_text}"
            events: list[str] = []
            page = FakePage(events)

            def fake_wait_until(predicate: object, *, timeout_seconds: float, message: str, interval_seconds: float = 0.5) -> None:
                self.assertTrue(predicate())
                events.append(f"wait_until:{message}")

            def fake_wait_ready(current_page: object, store_key: str, expected_count_text: str, current_amount_text: str) -> None:
                self.assertIs(page, current_page)
                self.assertEqual("fuzzy", store_key)
                self.assertEqual("本次勾选批量开具发票2份", expected_count_text)
                self.assertEqual(expected_amount_text, current_amount_text)
                events.append("wait_submit_confirmation_ready")

            with patch.object(runner, "_body_text", return_value=body_text):
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
                        details, success_count, failure_count = runner._wait_for_result_modal(object(), "fuzzy")  # noqa: SLF001

            self.assertLess(events.index("wait_result_modal_ready"), events.index("read_result_body_parse"))
            self.assertEqual(1, success_count)
            self.assertEqual(0, failure_count)
            self.assertEqual(1, len(details))

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
                expected_text="企业办税",
                step_name="switch company",
            )
            mocked_nav.assert_any_call(
                switch_page,
                config.portal_home_url,
                result,
                expected_text="我要办税",
                step_name="return home with active company 厦门市思明区浮几创意餐厅",
            )
            mocked_step.assert_called_once()

    def test_ensure_company_waits_for_switch_dialogs_before_confirming(self) -> None:
        class FakeControl:
            def __init__(self, events: list[str], label: str) -> None:
                self.events = events
                self.label = label

            @property
            def last(self) -> "FakeControl":
                return self

            def click(self) -> None:
                self.events.append(f"click:{self.label}")

            def check(self, force: bool = False) -> None:
                self.events.append(f"check:{self.label}:{force}")

            def is_enabled(self) -> bool:
                return True

        class FakePage:
            def __init__(self, events: list[str]) -> None:
                self.events = events

            def get_by_role(self, role: str, name: str) -> FakeControl:
                self.events.append(f"get_by_role:{role}:{name}")
                return FakeControl(self.events, f"{role}:{name}")

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
                                                    with patch.object(runner, "_update_store_step"):
                                                        with patch.object(runner, "_log"):
                                                            returned_page = runner._ensure_company(page, store, result)  # noqa: SLF001

            self.assertIs(page, returned_page)
            mocked_wait_confirm.assert_called_once_with(page, store.store_key)
            mocked_wait_role.assert_called_once_with(page, store.store_key, "法定代表人")

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

    def test_run_store_waits_for_home_page_before_opening_batch_page(self) -> None:
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
                                                    side_effect=lambda page, *_: page,
                                                ):
                                                    with patch.object(runner, "_assert_batch_page_clean"):
                                                        with patch.object(runner, "_import_workbook"):
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

            self.assertEqual("validated", result.status)
            self.assertLess(
                events.index("wait_for_home_page_ready"),
                events.index("wait_before_open_batch_page"),
            )
            self.assertLess(
                events.index("wait_before_open_batch_page"),
                events.index("new_page"),
            )

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
