from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.main import command_tax_lookup_test
from app.models import AppConfig, TaxLookupResult
from app.state import StateStore
from app.tax_lookup import AlapiTaxLookupProvider, TaxLookupClient


def build_config(tmp_path: Path, **overrides: object) -> AppConfig:
    config = AppConfig(
        timezone="Asia/Shanghai",
        survey_cookie="cookie=value",
        survey_export_url="https://wj.qq.com/api/answer_exports/generate",
        survey_export_method="POST",
        survey_export_body_template='{"survey_id":"{{survey_id}}"}',
        survey_export_download_url_path=None,
        survey_extra_headers={},
        default_attachment_question_id="q-1",
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
        openai_ssl_verify=True,
        openai_ca_bundle_path=None,
        survey_ssl_verify=True,
        survey_ca_bundle_path=None,
        tax_lookup_ssl_verify=True,
        tax_lookup_ca_bundle_path=None,
        tax_lookup_provider="disabled",
        tax_lookup_alapi_token=None,
        tax_lookup_url_template=None,
        tax_lookup_extra_headers={},
        tax_lookup_value_path=None,
        tax_lookup_timeout_seconds=30,
        tax_lookup_cache_negative_ttl_hours=24,
    )
    return replace(config, **overrides)


class StubTaxLookupProvider:
    name = "stub"

    def __init__(self, result: TaxLookupResult) -> None:
        self.result = result
        self.calls = 0

    def lookup(self, company_name: str) -> TaxLookupResult:
        self.calls += 1
        return self.result


class StubTaxLookupClient(TaxLookupClient):
    def __init__(self, config: AppConfig, state_store: StateStore, provider: StubTaxLookupProvider) -> None:
        self._provider_override = provider
        super().__init__(config, state_store)

    def _build_provider(self) -> StubTaxLookupProvider:
        return self._provider_override


class AlapiTaxLookupProviderTests(unittest.TestCase):
    def test_alapi_provider_accepts_exact_match_after_normalization(self) -> None:
        payload = {
            "success": True,
            "data": {
                "items": [
                    {
                        "name": "深圳易思商务咨询有限公司厦门分公司",
                        "credit_no": "AB12345678",
                    }
                ]
            },
        }
        provider = AlapiTaxLookupProvider(
            token="token",
            timeout_seconds=30,
            request_json=lambda url, method, headers, body: payload,
        )

        result = provider.lookup("深圳易思商务咨询有限公司 厦门分公司")

        self.assertEqual("success", result.status)
        self.assertEqual("AB12345678", result.tax_id)
        self.assertEqual("深圳易思商务咨询有限公司厦门分公司", result.matched_name)

    def test_alapi_provider_rejects_non_exact_match(self) -> None:
        payload = {
            "success": True,
            "data": {
                "items": [
                    {
                        "name": "深圳易思商务咨询有限公司",
                        "credit_no": "AB12345678",
                    }
                ]
            },
        }
        provider = AlapiTaxLookupProvider(
            token="token",
            timeout_seconds=30,
            request_json=lambda url, method, headers, body: payload,
        )

        result = provider.lookup("深圳易思商务咨询有限公司厦门分公司")

        self.assertEqual("no_exact_match", result.status)
        self.assertIsNone(result.tax_id)
        self.assertEqual(1, result.candidate_count)


class TaxLookupClientCacheTests(unittest.TestCase):
    def test_positive_lookup_is_reused_from_sqlite_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = build_config(tmp_path, tax_lookup_provider="alapi")
            first_provider = StubTaxLookupProvider(
                TaxLookupResult(
                    provider="stub",
                    status="success",
                    tax_id="AB12345678",
                    matched_name="测试公司",
                    candidate_count=1,
                    raw_response_json='{"hit":true}',
                )
            )
            state_store = StateStore(config.state_db_path)
            first_client = StubTaxLookupClient(config, state_store, first_provider)

            first_result = first_client.lookup("测试公司")

            self.assertFalse(first_result.from_cache)
            self.assertEqual(1, first_provider.calls)

            second_provider = StubTaxLookupProvider(
                TaxLookupResult(
                    provider="stub",
                    status="success",
                    tax_id="ZZ99999999",
                    matched_name="测试公司",
                    candidate_count=1,
                    raw_response_json='{"should_not_call":true}',
                )
            )
            second_client = StubTaxLookupClient(config, state_store, second_provider)

            second_result = second_client.lookup("测试公司")

            self.assertTrue(second_result.from_cache)
            self.assertEqual("AB12345678", second_result.tax_id)
            self.assertEqual(0, second_provider.calls)

    def test_negative_cache_expires_immediately_when_ttl_is_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = build_config(
                tmp_path,
                tax_lookup_provider="alapi",
                tax_lookup_cache_negative_ttl_hours=0,
            )
            state_store = StateStore(config.state_db_path)
            first_provider = StubTaxLookupProvider(
                TaxLookupResult(
                    provider="stub",
                    status="no_result",
                    tax_id=None,
                    matched_name=None,
                    candidate_count=0,
                    raw_response_json='{"items":[]}',
                )
            )
            first_client = StubTaxLookupClient(config, state_store, first_provider)
            first_client.lookup("测试公司")
            self.assertEqual(1, first_provider.calls)

            second_provider = StubTaxLookupProvider(
                TaxLookupResult(
                    provider="stub",
                    status="no_result",
                    tax_id=None,
                    matched_name=None,
                    candidate_count=0,
                    raw_response_json='{"items":[]}',
                )
            )
            second_client = StubTaxLookupClient(config, state_store, second_provider)
            second_client.lookup("测试公司")

            self.assertEqual(1, second_provider.calls)


class TaxLookupCliTests(unittest.TestCase):
    def test_tax_lookup_test_command_prints_structured_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = build_config(tmp_path, tax_lookup_provider="alapi")

            class FakeTaxLookupClient:
                def __init__(self, _config: AppConfig, _state_store: StateStore) -> None:
                    self.config = _config

                def lookup(self, company_name: str) -> TaxLookupResult:
                    return TaxLookupResult(
                        provider="alapi",
                        status="success",
                        tax_id="AB12345678",
                        matched_name=company_name,
                        candidate_count=1,
                        message="ok",
                    )

            stdout = io.StringIO()
            with patch("app.main.load_app_config", return_value=config), patch(
                "app.main.TaxLookupClient",
                FakeTaxLookupClient,
            ), redirect_stdout(stdout):
                exit_code = command_tax_lookup_test(tmp_path / ".env", "测试公司")

            self.assertEqual(0, exit_code)
            payload = json.loads(stdout.getvalue())
            self.assertEqual("alapi", payload["provider"])
            self.assertEqual("success", payload["status"])
            self.assertEqual("AB12345678", payload["tax_id"])
