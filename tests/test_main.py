from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.config import load_app_config
from app.models import PortalIssueDetail
from app.main import (
    _portal_chrome_cdp_ready,
    _resolve_portal_issue_config,
    _select_portal_stores,
    _terminate_portal_chrome_instance,
    build_parser,
    command_portal_issue,
    command_portal_open_chrome_cdp,
    command_portal_sync,
)


class MainCommandTests(unittest.TestCase):
    def test_portal_chrome_cdp_ready_requires_page_target(self) -> None:
        version_response = MagicMock()
        version_response.__enter__.return_value.status = 200

        targets_response = MagicMock()
        targets_response.__enter__.return_value.status = 200
        targets_response.__enter__.return_value.read.return_value = b'[]'

        with patch("app.main.urlopen", side_effect=[version_response, targets_response]):
            self.assertFalse(_portal_chrome_cdp_ready("http://127.0.0.1:9222"))

    def test_portal_chrome_cdp_ready_accepts_page_target(self) -> None:
        version_response = MagicMock()
        version_response.__enter__.return_value.status = 200

        targets_response = MagicMock()
        targets_response.__enter__.return_value.status = 200
        targets_response.__enter__.return_value.read.return_value = (
            b'[{"id":"1","type":"page","url":"https://example.com"}]'
        )

        with patch("app.main.urlopen", side_effect=[version_response, targets_response]):
            self.assertTrue(_portal_chrome_cdp_ready("http://127.0.0.1:9222"))

    def test_parser_accepts_skip_sync_flag(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["portal-issue-dry-run", "--env-file", ".env", "--store-key", "fuzzy", "--skip-sync"]
        )

        self.assertEqual("portal-issue-dry-run", args.command)
        self.assertEqual(["fuzzy"], args.store_keys)
        self.assertTrue(args.skip_sync)

    def test_parser_accepts_portal_sync_command(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["portal-sync", "--env-file", ".env", "--store-key", "fuzzy"])

        self.assertEqual("portal-sync", args.command)
        self.assertEqual(["fuzzy"], args.store_keys)

    def test_parser_accepts_portal_open_chrome_cdp_command(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["portal-open-chrome-cdp", "--env-file", ".env"])

        self.assertEqual("portal-open-chrome-cdp", args.command)

    def test_resolve_portal_issue_config_disables_sync_only_for_current_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            env_path = tmp_path / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "TZ=Asia/Shanghai",
                        "TENCENT_SURVEY_COOKIE=cookie=value",
                        "TENCENT_SURVEY_EXPORT_URL=https://wj.qq.com/api/answer_exports/generate",
                        "TENCENT_SURVEY_EXPORT_METHOD=POST",
                        "OPENAI_BASE_URL=https://example.com/v1",
                        "OPENAI_API_KEY=key",
                        "SMTP_HOST=smtp.example.com",
                        "SMTP_USERNAME=user",
                        "SMTP_PASSWORD=pass",
                        "SMTP_FROM=from@example.com",
                        "SMTP_TO=to@example.com",
                        "TEMPLATE_XLSX_PATH=./template.xlsx",
                        "STATE_DB_PATH=./state.db",
                        "STORES_CONFIG_PATH=./stores.yaml",
                        "TAX_PORTAL_SYNC_FROM_SERVER=true",
                        "TAX_PORTAL_REMOTE_HOST=root@example.com",
                        "TAX_PORTAL_REMOTE_OUTPUT_DIR=/srv/output",
                    ]
                ),
                encoding="utf-8",
            )

            config = load_app_config(env_path)
            updated = _resolve_portal_issue_config(config, skip_sync=True)

            self.assertTrue(config.portal_sync_from_server)
            self.assertFalse(updated.portal_sync_from_server)
            self.assertEqual(config.portal_sync_remote_output_dir, updated.portal_sync_remote_output_dir)

    def test_command_portal_sync_calls_syncer_for_selected_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            env_path = tmp_path / ".env"
            stores_path = tmp_path / "stores.yaml"
            env_path.write_text(
                "\n".join(
                    [
                        "TZ=Asia/Shanghai",
                        "TENCENT_SURVEY_COOKIE=cookie=value",
                        "TENCENT_SURVEY_EXPORT_URL=https://wj.qq.com/api/answer_exports/generate",
                        "TENCENT_SURVEY_EXPORT_METHOD=POST",
                        "OPENAI_BASE_URL=https://example.com/v1",
                        "OPENAI_API_KEY=key",
                        "SMTP_HOST=smtp.example.com",
                        "SMTP_USERNAME=user",
                        "SMTP_PASSWORD=pass",
                        "SMTP_FROM=from@example.com",
                        "SMTP_TO=to@example.com",
                        "TEMPLATE_XLSX_PATH=./template.xlsx",
                        "STATE_DB_PATH=./state.db",
                        f"STORES_CONFIG_PATH={stores_path}",
                        "TAX_PORTAL_SYNC_FROM_SERVER=true",
                        "TAX_PORTAL_REMOTE_HOST=root@example.com",
                        "TAX_PORTAL_REMOTE_OUTPUT_DIR=/srv/output",
                    ]
                ),
                encoding="utf-8",
            )
            stores_path.write_text(
                """
stores:
  - store_key: fuzzy
    store_name: Fuzzy
    survey_id: "1001"
    output_xlsx_path: ./output/fuzzy.xlsx
    initial_last_processed_id: 1
    portal_enabled: true
    portal_priority: 10
    portal_company_switch_name: Fuzzy
    portal_company_verify_name: Fuzzy
    portal_company_role: legal_representative
""".strip(),
                encoding="utf-8",
            )

            with patch("app.portal_sync.PortalWorkbookSyncer.sync_store_workbook") as mocked_sync:
                exit_code = command_portal_sync(env_path, ["fuzzy"])

            self.assertEqual(0, exit_code)
            mocked_sync.assert_called_once()

    def test_command_portal_open_chrome_cdp_launches_dedicated_browser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            env_path = tmp_path / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "TZ=Asia/Shanghai",
                        "TENCENT_SURVEY_COOKIE=cookie=value",
                        "TENCENT_SURVEY_EXPORT_URL=https://wj.qq.com/api/answer_exports/generate",
                        "TENCENT_SURVEY_EXPORT_METHOD=POST",
                        "OPENAI_BASE_URL=https://example.com/v1",
                        "OPENAI_API_KEY=key",
                        "SMTP_HOST=smtp.example.com",
                        "SMTP_USERNAME=user",
                        "SMTP_PASSWORD=pass",
                        "SMTP_FROM=from@example.com",
                        "SMTP_TO=to@example.com",
                        "TEMPLATE_XLSX_PATH=./template.xlsx",
                        "STATE_DB_PATH=./state.db",
                        "STORES_CONFIG_PATH=./stores.yaml",
                        "TAX_PORTAL_CHROME_CDP_URL=http://127.0.0.1:9555",
                        f"TAX_PORTAL_CHROME_CDP_USER_DATA_DIR={tmp_path / 'cdp-profile'}",
                        f"TAX_PORTAL_CHROME_EXECUTABLE_PATH={tmp_path / 'Chrome'}",
                    ]
                ),
                encoding="utf-8",
            )
            (tmp_path / "Chrome").write_text("", encoding="utf-8")

            fake_process = MagicMock(pid=12345)
            with patch("app.main._portal_chrome_cdp_ready", return_value=False):
                with patch("app.main._wait_for_portal_chrome_cdp", return_value=True):
                    with patch("subprocess.Popen", return_value=fake_process) as mocked_popen:
                        with patch("builtins.print") as mocked_print:
                            exit_code = command_portal_open_chrome_cdp(env_path)

            self.assertEqual(0, exit_code)
            mocked_popen.assert_called_once()
            printed = mocked_print.call_args.args[0]
            payload = json.loads(printed)
            self.assertEqual("ready", payload["status"])
            self.assertEqual("http://127.0.0.1:9555", payload["cdp_url"])
            self.assertTrue(payload["launched"])

    def test_command_portal_open_chrome_cdp_skips_launch_when_page_target_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            env_path = tmp_path / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "TZ=Asia/Shanghai",
                        "TENCENT_SURVEY_COOKIE=cookie=value",
                        "TENCENT_SURVEY_EXPORT_URL=https://wj.qq.com/api/answer_exports/generate",
                        "TENCENT_SURVEY_EXPORT_METHOD=POST",
                        "OPENAI_BASE_URL=https://example.com/v1",
                        "OPENAI_API_KEY=key",
                        "SMTP_HOST=smtp.example.com",
                        "SMTP_USERNAME=user",
                        "SMTP_PASSWORD=pass",
                        "SMTP_FROM=from@example.com",
                        "SMTP_TO=to@example.com",
                        "TEMPLATE_XLSX_PATH=./template.xlsx",
                        "STATE_DB_PATH=./state.db",
                        "STORES_CONFIG_PATH=./stores.yaml",
                        "TAX_PORTAL_CHROME_CDP_URL=http://127.0.0.1:9555",
                        f"TAX_PORTAL_CHROME_CDP_USER_DATA_DIR={tmp_path / 'cdp-profile'}",
                    ]
                ),
                encoding="utf-8",
            )

            with patch("app.main._portal_chrome_cdp_ready", return_value=True):
                with patch("subprocess.Popen") as mocked_popen:
                    with patch("builtins.print") as mocked_print:
                        exit_code = command_portal_open_chrome_cdp(env_path)

            self.assertEqual(0, exit_code)
            mocked_popen.assert_not_called()
            payload = json.loads(mocked_print.call_args.args[0])
            self.assertFalse(payload["launched"])

    def test_command_portal_issue_treats_skipped_store_as_success(self) -> None:
        fake_config = MagicMock()
        fake_config.stores_config_path = Path("/tmp/stores.yaml")
        fake_config.state_db_path = Path("/tmp/state.db")
        selected_store = MagicMock()
        skipped_result = MagicMock(
            store_key="fuzzy",
            company_verify_name="Fuzzy",
            mode="dry_run",
            status="skipped",
            step="skip_empty_workbook",
            expected_count=0,
            submitted_count=0,
            success_count=0,
            failure_count=0,
            details=(),
            error=None,
            artifacts_dir=None,
        )

        with patch("app.main.load_app_config", return_value=fake_config):
            with patch("app.main.load_store_configs", return_value=[selected_store]):
                with patch("app.main._select_portal_stores", return_value=[selected_store]):
                    with patch("app.main.StateStore"):
                        with patch("app.portal_runner.TaxPortalRunner") as mocked_runner_cls:
                            mocked_runner_cls.return_value.run.return_value = [skipped_result]
                            with patch("builtins.print"):
                                exit_code = command_portal_issue(
                                    Path("/tmp/.env"),
                                    store_keys=["fuzzy"],
                                    submit=False,
                                    skip_sync=False,
                                )

        self.assertEqual(0, exit_code)

    def test_command_portal_issue_closes_local_apps_after_submit_without_failures(self) -> None:
        fake_config = MagicMock()
        fake_config.stores_config_path = Path("/tmp/stores.yaml")
        fake_config.state_db_path = Path("/tmp/state.db")
        selected_store = MagicMock()
        success_result = MagicMock(
            store_key="fuzzy",
            company_verify_name="Fuzzy",
            mode="submit",
            status="success",
            step="submit_result",
            expected_count=1,
            submitted_count=1,
            success_count=1,
            failure_count=0,
            details=(),
            error=None,
            artifacts_dir=Path("/tmp/artifacts"),
        )
        skipped_result = MagicMock(
            store_key="peanut",
            company_verify_name="Peanut",
            mode="submit",
            status="skipped",
            step="skip_empty_workbook",
            expected_count=0,
            submitted_count=0,
            success_count=0,
            failure_count=0,
            details=(),
            error=None,
            artifacts_dir=Path("/tmp/artifacts-2"),
        )

        with patch("app.main.load_app_config", return_value=fake_config):
            with patch("app.main.load_store_configs", return_value=[selected_store]):
                with patch("app.main._select_portal_stores", return_value=[selected_store]):
                    with patch("app.main.StateStore"):
                        with patch("app.main._close_successful_portal_run_apps") as mocked_cleanup:
                            with patch("app.portal_runner.TaxPortalRunner") as mocked_runner_cls:
                                mocked_runner_cls.return_value.run.return_value = [success_result, skipped_result]
                                with patch("builtins.print"):
                                    exit_code = command_portal_issue(
                                        Path("/tmp/.env"),
                                        store_keys=["fuzzy"],
                                        submit=True,
                                        skip_sync=False,
                                    )

        self.assertEqual(0, exit_code)
        mocked_cleanup.assert_called_once_with(fake_config)

    def test_terminate_portal_chrome_instance_passes_pattern_after_separator(self) -> None:
        completed = MagicMock(returncode=1, stderr="", stdout="")
        user_data_dir = Path("/tmp/tax-portal-chrome-cdp").resolve()

        with patch("app.main.subprocess.run", return_value=completed) as mocked_run:
            _terminate_portal_chrome_instance(user_data_dir)

        mocked_run.assert_called_once_with(
            ["pkill", "-f", "--", f"--user-data-dir={user_data_dir}"],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_command_portal_issue_includes_failed_details_in_json_output(self) -> None:
        fake_config = MagicMock()
        fake_config.stores_config_path = Path("/tmp/stores.yaml")
        fake_config.state_db_path = Path("/tmp/state.db")
        selected_store = MagicMock()
        failed_result = MagicMock(
            store_key="peanut",
            company_verify_name="Peanut",
            mode="submit",
            status="failed",
            step="submit_result",
            expected_count=1,
            submitted_count=1,
            success_count=0,
            failure_count=1,
            details=(
                PortalIssueDetail(
                    invoice_serial="1",
                    digital_invoice_number=None,
                    buyer_email="demo@example.com",
                    status="失败",
                    failure_reason="购买方纳税人识别号有误",
                ),
            ),
            error=None,
            artifacts_dir=Path("/tmp/artifacts"),
        )

        with patch("app.main.load_app_config", return_value=fake_config):
            with patch("app.main.load_store_configs", return_value=[selected_store]):
                with patch("app.main._select_portal_stores", return_value=[selected_store]):
                    with patch("app.main.StateStore"):
                        with patch("app.main._close_successful_portal_run_apps") as mocked_cleanup:
                            with patch("app.portal_runner.TaxPortalRunner") as mocked_runner_cls:
                                mocked_runner_cls.return_value.run.return_value = [failed_result]
                                with patch("builtins.print") as mocked_print:
                                    exit_code = command_portal_issue(
                                        Path("/tmp/.env"),
                                        store_keys=["peanut"],
                                        submit=True,
                                        skip_sync=False,
                                    )

        self.assertEqual(1, exit_code)
        mocked_cleanup.assert_not_called()
        payload = json.loads(mocked_print.call_args.args[0])
        self.assertEqual(
            [
                {
                    "invoice_serial": "1",
                    "digital_invoice_number": None,
                    "buyer_email": "demo@example.com",
                    "status": "失败",
                    "failure_reason": "购买方纳税人识别号有误",
                }
            ],
            payload[0]["failed_details"],
        )

    def test_select_portal_stores_orders_by_priority(self) -> None:
        class FakeStore:
            def __init__(self, store_key: str, portal_priority: int, portal_enabled: bool = True) -> None:
                self.store_key = store_key
                self.portal_priority = portal_priority
                self.portal_enabled = portal_enabled

        stores = [
            FakeStore("peanut", 20),
            FakeStore("fuzzy", 10),
            FakeStore("disabled", 0, portal_enabled=False),
        ]

        selected = _select_portal_stores(stores, requested_store_keys=None)

        self.assertEqual(["fuzzy", "peanut"], [store.store_key for store in selected])
