from __future__ import annotations

import sqlite3
import tempfile
import unittest
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
                with patch.object(runner, "_navigate_with_reauth") as mocked_nav:
                    with patch.object(runner, "_log"):
                        runner._wait_for_batch_page(page, store, result)  # noqa: SLF001

            mocked_step.assert_called_once()
            mocked_nav.assert_called_once_with(
                page,
                config.portal_batch_issue_url,
                result,
                expected_text="批量开票",
                step_name="open batch issue page",
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
