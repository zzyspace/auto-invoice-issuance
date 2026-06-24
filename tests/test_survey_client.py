from __future__ import annotations

import io
import json
import unittest
import zipfile
from pathlib import Path

from app.models import AppConfig, StoreConfig
from app.survey_client import TencentSurveyClient


class StubSurveyClient(TencentSurveyClient):
    def __init__(self, config: AppConfig, responses: list[tuple[bytes, dict[str, str]]]) -> None:
        super().__init__(config)
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def _request(
        self,
        url: str,
        method: str,
        headers: dict[str, str],
        payload: bytes | None,
    ) -> tuple[bytes, dict[str, str]]:
        self.calls.append((url, method, headers, payload))
        if not self.responses:
            raise AssertionError("No more stub responses configured.")
        return self.responses.pop(0)


def build_config() -> AppConfig:
    return AppConfig(
        timezone="Asia/Shanghai",
        survey_cookie="cookie=value",
        survey_export_url="https://wj.qq.com/api/answer_exports/generate",
        survey_export_method="POST",
        survey_export_body_template='{"survey_id":"{{survey_id}}","from":"{{from_datetime}}","to":"{{to_datetime}}"}',
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
        template_xlsx_path=Path("/tmp/template.xlsx"),
        state_db_path=Path("/tmp/state.db"),
        stores_config_path=Path("/tmp/stores.yaml"),
        backups_root=Path("/tmp/backups"),
        openai_ssl_verify=True,
        openai_ca_bundle_path=None,
        survey_ssl_verify=True,
        survey_ca_bundle_path=None,
        tax_lookup_ssl_verify=True,
        tax_lookup_ca_bundle_path=None,
    )


class SurveyClientTests(unittest.TestCase):
    def test_export_csv_polls_job_and_extracts_csv_from_zip(self) -> None:
        csv_text = "编号,1.发票抬头\n308,张三\n"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("demo.csv", csv_text.encode("utf-8-sig"))

        responses = [
            (
                json.dumps(
                    {
                        "status": 1,
                        "data": {"id": 11046713, "status_info": "Ready", "result": "[]"},
                    }
                ).encode("utf-8"),
                {"Content-Type": "application/json"},
            ),
            (
                json.dumps(
                    {
                        "status": 1,
                        "data": {
                            "id": 11046713,
                            "status_info": "Done",
                            "progress": 100,
                            "result": {
                                "cos_download_url": "https://download.example.com/export.csv.zip",
                            },
                        },
                    }
                ).encode("utf-8"),
                {"Content-Type": "application/json"},
            ),
            (buffer.getvalue(), {"Content-Type": "application/zip"}),
        ]
        client = StubSurveyClient(build_config(), responses)
        store = StoreConfig(
            store_key="store_a",
            store_name="门店A",
            survey_id="22512014",
            output_xlsx_path=Path("/tmp/output.xlsx"),
            initial_last_processed_id=307,
        )

        result = client.export_csv(store)

        self.assertEqual(csv_text, result)
        self.assertEqual(3, len(client.calls))
        generate_url, generate_method, generate_headers, generate_payload = client.calls[0]
        self.assertEqual("https://wj.qq.com/api/answer_exports/generate", generate_url)
        self.assertEqual("POST", generate_method)
        self.assertEqual("application/json", generate_headers["Content-Type"])
        self.assertIn('"survey_id":"22512014"', generate_payload.decode("utf-8"))
        self.assertIn("/api/files/export_check", client.calls[1][0])
        self.assertEqual("https://download.example.com/export.csv.zip", client.calls[2][0])

    def test_export_csv_reports_login_page_when_cookie_expired(self) -> None:
        responses = [
            (
                (
                    "<!DOCTYPE html><html lang=\"zh-cn\"><head>"
                    "<title>登录 - 腾讯问卷</title></head><body>请登录</body></html>"
                ).encode("utf-8"),
                {"Content-Type": "text/html; charset=utf-8"},
            )
        ]
        client = StubSurveyClient(build_config(), responses)
        store = StoreConfig(
            store_key="store_a",
            store_name="门店A",
            survey_id="22512014",
            output_xlsx_path=Path("/tmp/output.xlsx"),
            initial_last_processed_id=307,
        )

        with self.assertRaisesRegex(
            ValueError,
            "TENCENT_SURVEY_COOKIE may have expired or been signed out",
        ):
            client.export_csv(store)

    def test_export_csv_reports_login_page_during_poll(self) -> None:
        responses = [
            (
                json.dumps(
                    {
                        "status": 1,
                        "data": {"id": 11046713, "status_info": "Ready", "result": "[]"},
                    }
                ).encode("utf-8"),
                {"Content-Type": "application/json"},
            ),
            (
                (
                    "<!DOCTYPE html><html lang=\"zh-cn\"><head>"
                    "<title>登录 - 腾讯问卷</title></head><body>请登录</body></html>"
                ).encode("utf-8"),
                {"Content-Type": "text/html; charset=utf-8"},
            ),
        ]
        client = StubSurveyClient(build_config(), responses)
        store = StoreConfig(
            store_key="store_a",
            store_name="门店A",
            survey_id="22512014",
            output_xlsx_path=Path("/tmp/output.xlsx"),
            initial_last_processed_id=307,
        )

        with self.assertRaisesRegex(
            ValueError,
            "TENCENT_SURVEY_COOKIE may have expired or been signed out",
        ):
            client.export_csv(store)
