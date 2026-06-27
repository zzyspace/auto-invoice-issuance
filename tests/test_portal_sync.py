from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.models import AppConfig, StoreConfig
from app.portal_sync import PortalWorkbookSyncError, PortalWorkbookSyncer


def build_config(tmp_path: Path, *, sync_from_server: bool = True) -> AppConfig:
    return AppConfig(
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
        portal_artifacts_dir=tmp_path / "artifacts",
        portal_sync_from_server=sync_from_server,
        portal_sync_remote_host="root@example.com",
        portal_sync_remote_output_dir="/srv/output",
        portal_sync_ssh_key_path=tmp_path / "id_ed25519",
        portal_sync_ssh_port=2222,
        portal_sync_connect_timeout_seconds=8,
        portal_sync_strict_host_key_checking=False,
        portal_sync_batch_mode=True,
    )


class PortalSyncTests(unittest.TestCase):
    def test_build_command_uses_remote_output_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = build_config(tmp_path)
            syncer = PortalWorkbookSyncer(config)

            command = syncer._build_command(  # noqa: SLF001 - intentional unit coverage
                "scp",
                "root@example.com:/srv/output/fuzzy_invoice.xlsx",
                tmp_path / "output" / "fuzzy_invoice.xlsx.downloading",
            )

            self.assertEqual(
                [
                    "scp",
                    "-P",
                    "2222",
                    "-i",
                    str(tmp_path / "id_ed25519"),
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    "ConnectTimeout=8",
                    "root@example.com:/srv/output/fuzzy_invoice.xlsx",
                    str(tmp_path / "output" / "fuzzy_invoice.xlsx.downloading"),
                ],
                command,
            )

    def test_sync_store_workbook_is_noop_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = build_config(tmp_path, sync_from_server=False)
            syncer = PortalWorkbookSyncer(config)
            store = StoreConfig(
                store_key="fuzzy",
                store_name="Fuzzy",
                survey_id="1",
                output_xlsx_path=tmp_path / "output" / "fuzzy_invoice.xlsx",
                initial_last_processed_id=1,
            )

            with patch("app.portal_sync.subprocess.run") as mocked_run:
                syncer.sync_store_workbook(store)
            mocked_run.assert_not_called()

    def test_sync_store_workbook_requires_scp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = build_config(tmp_path)
            syncer = PortalWorkbookSyncer(config)
            store = StoreConfig(
                store_key="fuzzy",
                store_name="Fuzzy",
                survey_id="1",
                output_xlsx_path=tmp_path / "output" / "fuzzy_invoice.xlsx",
                initial_last_processed_id=1,
            )

            with patch("app.portal_sync.shutil.which", return_value=None):
                with self.assertRaisesRegex(PortalWorkbookSyncError, "scp"):
                    syncer.sync_store_workbook(store)

    def test_sync_store_workbook_backs_up_existing_local_workbook_before_replace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = build_config(tmp_path)
            syncer = PortalWorkbookSyncer(config)
            output_path = tmp_path / "output" / "fuzzy_invoice.xlsx"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text("old workbook", encoding="utf-8")
            store = StoreConfig(
                store_key="fuzzy",
                store_name="Fuzzy",
                survey_id="1",
                output_xlsx_path=output_path,
                initial_last_processed_id=1,
            )

            def fake_run(command: list[str], **_: object) -> None:
                Path(command[-1]).write_text("new workbook", encoding="utf-8")

            with patch("app.portal_sync.shutil.which", return_value="scp"):
                with patch("app.portal_sync.subprocess.run", side_effect=fake_run):
                    syncer.sync_store_workbook(store)

            backups = list((tmp_path / "backups" / "sync" / "fuzzy").glob("*.xlsx"))
            self.assertEqual(1, len(backups))
            self.assertEqual("old workbook", backups[0].read_text(encoding="utf-8"))
            self.assertEqual("new workbook", output_path.read_text(encoding="utf-8"))
