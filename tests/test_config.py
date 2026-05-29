from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import load_app_config, load_store_configs


class StoreConfigTests(unittest.TestCase):
    def test_load_store_configs_resolves_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            stores_path = tmp_path / "stores.yaml"
            stores_path.write_text(
                """
stores:
  - store_key: test_store
    store_name: 测试门店
    survey_id: "1001"
    output_xlsx_path: ./output/test.xlsx
    initial_last_processed_id: 307
    attachment_question_id: q-1
    enabled: true
    portal_enabled: true
    portal_company_switch_name: 测试门店（个体工商户）（待确认）
    portal_company_verify_name: 测试门店（个体工商户）
    portal_company_role: legal_representative
""".strip(),
                encoding="utf-8",
            )
            stores = load_store_configs(stores_path)
            self.assertEqual(1, len(stores))
            self.assertEqual((tmp_path / "output" / "test.xlsx").resolve(), stores[0].output_xlsx_path)
            self.assertEqual("q-1", stores[0].effective_attachment_question_id("fallback"))
            self.assertTrue(stores[0].portal_enabled)
            self.assertEqual("测试门店（个体工商户）（待确认）", stores[0].effective_portal_company_switch_name())
            self.assertEqual("测试门店（个体工商户）", stores[0].effective_portal_company_verify_name())
            self.assertEqual("legal_representative", stores[0].portal_company_role)

    def test_store_uses_default_question_id_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            stores_path = tmp_path / "stores.yaml"
            stores_path.write_text(
                """
stores:
  - store_key: test_store
    store_name: 测试门店
    survey_id: "1001"
    output_xlsx_path: ./output/test.xlsx
    initial_last_processed_id: 307
    enabled: true
""".strip(),
                encoding="utf-8",
            )
            stores = load_store_configs(stores_path)
            self.assertEqual("fallback", stores[0].effective_attachment_question_id("fallback"))

    def test_load_app_config_prefers_explicit_tax_lookup_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, patch.dict(os.environ, {}, clear=True):
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
                        "TAX_LOOKUP_PROVIDER=legacy_template",
                        "TAX_LOOKUP_ALAPI_TOKEN=demo_token",
                        "TAX_LOOKUP_URL_TEMPLATE=https://example.com/company?name={company_name}",
                        "TAX_PORTAL_SYNC_FROM_SERVER=true",
                        "TAX_PORTAL_REMOTE_HOST=root@example.com",
                        "TAX_PORTAL_REMOTE_OUTPUT_DIR=/srv/output",
                    ]
                ),
                encoding="utf-8",
            )

            config = load_app_config(env_path)

            self.assertEqual("legacy_template", config.tax_lookup_provider)
            self.assertEqual("chrome", config.portal_browser_channel)
            self.assertFalse(config.portal_headless)
            self.assertTrue(str(config.portal_artifacts_dir).endswith("data/tax-portal-artifacts"))
            self.assertTrue(config.portal_sync_from_server)
            self.assertEqual("root@example.com", config.portal_sync_remote_host)
            self.assertEqual("/srv/output", config.portal_sync_remote_output_dir)

    def test_load_app_config_auto_selects_alapi_when_token_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, patch.dict(os.environ, {}, clear=True):
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
                        "TAX_LOOKUP_ALAPI_TOKEN=demo_token",
                    ]
                ),
                encoding="utf-8",
            )

            config = load_app_config(env_path)

            self.assertEqual("alapi", config.tax_lookup_provider)

    def test_load_app_config_auto_selects_legacy_template_when_only_template_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, patch.dict(os.environ, {}, clear=True):
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
                        "TAX_LOOKUP_URL_TEMPLATE=https://example.com/company?name={company_name}",
                    ]
                ),
                encoding="utf-8",
            )

            config = load_app_config(env_path)

            self.assertEqual("legacy_template", config.tax_lookup_provider)
