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
    portal_priority: 10
    portal_company_switch_name: 测试门店（个体工商户）（待确认）
    portal_company_verify_name: 测试门店（个体工商户）
    portal_company_role: legal_representative
""".strip(),
                encoding="utf-8",
            )
            stores = load_store_configs(stores_path)
            self.assertEqual(1, len(stores))
            self.assertEqual((tmp_path / "output" / "test.xlsx").resolve(), stores[0].output_xlsx_path)
            self.assertEqual("tencent_survey", stores[0].effective_data_source())
            self.assertEqual("q-1", stores[0].effective_attachment_question_id("fallback"))
            self.assertTrue(stores[0].portal_enabled)
            self.assertEqual(10, stores[0].portal_priority)
            self.assertEqual("测试门店（个体工商户）（待确认）", stores[0].effective_portal_company_switch_name())
            self.assertEqual("测试门店（个体工商户）", stores[0].effective_portal_company_verify_name())
            self.assertEqual("legal_representative", stores[0].portal_company_role)

    def test_load_store_configs_supports_invoice_submit_source_without_survey_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            stores_path = tmp_path / "stores.yaml"
            stores_path.write_text(
                """
stores:
  - store_key: peanut_local
    store_name: Peanut
    data_source: invoice-submit
    invoice_submit_store_key: peanut
    output_xlsx_path: ./output/peanut.xlsx
    initial_last_processed_id: 0
    enabled: true
""".strip(),
                encoding="utf-8",
            )

            stores = load_store_configs(stores_path)

            self.assertEqual(1, len(stores))
            self.assertEqual("invoice_submit", stores[0].effective_data_source())
            self.assertEqual("peanut", stores[0].effective_invoice_submit_store_key())
            self.assertEqual("invoice-submit:peanut", stores[0].effective_source_identifier())

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

    def test_portal_area_fields_and_dynamic_portal_urls_are_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, patch.dict(os.environ, {}, clear=True):
            tmp_path = Path(tmp_dir)
            stores_path = tmp_path / "stores.yaml"
            env_path = tmp_path / ".env"
            stores_path.write_text(
                """
stores:
  - store_key: fuzzy_qz
    store_name: FuzzyQZ
    survey_id: "27114382"
    output_xlsx_path: ./output/fuzzy_qz.xlsx
    initial_last_processed_id: 0
    enabled: true
    portal_enabled: true
    portal_priority: 30
    portal_area: quanzhou
    portal_area_name: 泉州
    portal_company_switch_name: 泉州市鲤城区浮几餐饮店（个体工商户）（待确认）
    portal_company_verify_name: 泉州市鲤城区浮几餐饮店（个体工商户）
    portal_company_role: legal_representative
""".strip(),
                encoding="utf-8",
            )
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
                        "TAX_PORTAL_HOME_URL=https://etax.{portal_area}.chinatax.gov.cn:8443/loginb/",
                        "TAX_PORTAL_IDENTITY_SWITCH_URL=https://tpass.{portal_area}.chinatax.gov.cn:8443/#/identitySwitch/enterprise?client_id=y56b7aay5brf48f8aa7bf24dd54d775r",
                        "TAX_PORTAL_BATCH_ISSUE_URL=https://dppt.{portal_area}.chinatax.gov.cn:8443/blue-invoice-makeout/invoice-batch",
                    ]
                ),
                encoding="utf-8",
            )

            stores = load_store_configs(stores_path)
            config = load_app_config(env_path)

            self.assertEqual("quanzhou", stores[0].effective_portal_area())
            self.assertEqual("泉州", stores[0].effective_portal_area_name())
            self.assertEqual(
                "https://etax.quanzhou.chinatax.gov.cn:8443/loginb/",
                config.portal_home_url_for_store(stores[0]),
            )
            self.assertEqual(
                "https://tpass.quanzhou.chinatax.gov.cn:8443/#/identitySwitch/enterprise?client_id=y56b7aay5brf48f8aa7bf24dd54d775r",
                config.portal_identity_switch_url_for_store(stores[0]),
            )
            self.assertEqual(
                "https://dppt.quanzhou.chinatax.gov.cn:8443/blue-invoice-makeout/invoice-batch",
                config.portal_batch_issue_url_for_store(stores[0]),
            )

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
            self.assertFalse(config.portal_disable_proxy)
            self.assertFalse(config.portal_headless)
            self.assertTrue(str(config.portal_artifacts_dir).endswith("data/tax-portal-artifacts"))
            self.assertTrue(config.portal_sync_from_server)
            self.assertEqual("root@example.com", config.portal_sync_remote_host)
            self.assertEqual("/srv/output", config.portal_sync_remote_output_dir)

    def test_load_app_config_allows_invoice_submit_only_without_tencent_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, patch.dict(os.environ, {}, clear=True):
            tmp_path = Path(tmp_dir)
            stores_path = tmp_path / "stores.yaml"
            env_path = tmp_path / ".env"
            stores_path.write_text(
                """
stores:
  - store_key: peanut_local
    store_name: Peanut
    data_source: invoice_submit
    invoice_submit_store_key: peanut
    output_xlsx_path: ./output/peanut.xlsx
    initial_last_processed_id: 0
    enabled: true
""".strip(),
                encoding="utf-8",
            )
            env_path.write_text(
                "\n".join(
                    [
                        "TZ=Asia/Shanghai",
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
                        f"INVOICE_SUBMIT_DB_PATH={tmp_path / 'invoice-submit.db'}",
                    ]
                ),
                encoding="utf-8",
            )

            config = load_app_config(env_path)

            self.assertEqual("", config.survey_cookie)
            self.assertEqual(
                Path(os.path.realpath(tmp_path / "invoice-submit.db")),
                Path(os.path.realpath(str(config.invoice_submit_db_path))),
            )

    def test_load_app_config_parses_portal_disable_proxy(self) -> None:
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
                        "TAX_PORTAL_DISABLE_PROXY=true",
                    ]
                ),
                encoding="utf-8",
            )

            config = load_app_config(env_path)

            self.assertTrue(config.portal_disable_proxy)

    def test_load_app_config_parses_local_etax_app_credentials(self) -> None:
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
                        "TAX_PORTAL_ETAX_APP_USERNAME=demo-user",
                        "TAX_PORTAL_ETAX_APP_PASSWORD=demo-pass",
                        f"TAX_PORTAL_ETAX_APP_PATH={tmp_path / '电子税务局.app'}",
                    ]
                ),
                encoding="utf-8",
            )

            config = load_app_config(env_path)

            self.assertEqual("demo-user", config.portal_etax_app_username)
            self.assertEqual("demo-pass", config.portal_etax_app_password)
            self.assertEqual((tmp_path / "电子税务局.app").resolve(), config.portal_etax_app_path)

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
