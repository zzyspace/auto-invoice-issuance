from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import load_app_config
from app.main import _resolve_portal_issue_config, _select_portal_stores, build_parser, command_portal_sync


class MainCommandTests(unittest.TestCase):
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
