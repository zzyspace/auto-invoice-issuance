from __future__ import annotations

import json
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, urlencode
from urllib.request import Request, urlopen

from app.models import AppConfig, StoreConfig
from app.utils import find_first_string_by_keys, get_value_by_path, render_template


class TencentSurveyClient:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.base_headers = {
            "Cookie": config.survey_cookie,
            "User-Agent": "Mozilla/5.0",
            "Accept": "*/*",
        }
        self.base_headers.update(config.survey_extra_headers)

    def export_csv(self, store: StoreConfig) -> str:
        context = {"survey_id": store.survey_id, "store_key": store.store_key, "store_name": store.store_name}
        url = render_template(self.config.survey_export_url, context)
        body = render_template(self.config.survey_export_body_template, context)
        headers = dict(self.base_headers)
        method = self.config.survey_export_method.upper()
        payload: Optional[bytes] = None
        if method == "GET" and body:
            params = urlencode(dict(parse_qsl(body, keep_blank_values=True)))
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{params}"
        elif method == "POST":
            payload = body.encode("utf-8")
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
        response_bytes, response_headers = self._request(url, method, headers, payload)
        content_type = response_headers.get("Content-Type", "")
        if "csv" in content_type.lower() or response_bytes.startswith("\ufeff".encode("utf-8")):
            return response_bytes.decode("utf-8-sig")
        text = response_bytes.decode("utf-8")
        if "编号," in text or "编号\t" in text:
            return text
        payload_json = json.loads(text)
        download_url = self._extract_export_download_url(payload_json)
        if not download_url:
            raise ValueError("Export response did not contain CSV content or a downloadable URL.")
        csv_bytes, _ = self._request(download_url, "GET", headers, None)
        return csv_bytes.decode("utf-8-sig")

    def download_attachment(self, store: StoreConfig, file_name: str) -> bytes:
        question_id = store.effective_attachment_question_id(self.config.default_attachment_question_id)
        url = (
            "https://wj.qq.com/api/files/download"
            f"?survey_id={store.survey_id}&question_id={question_id}&file_name={file_name}"
        )
        response_bytes, _ = self._request(url, "GET", self.base_headers, None)
        return response_bytes

    def _extract_export_download_url(self, payload: object) -> Optional[str]:
        configured = get_value_by_path(payload, self.config.survey_export_download_url_path)
        if isinstance(configured, str) and configured.strip():
            return configured.strip()
        return find_first_string_by_keys(
            payload,
            ("download_url", "downloadUrl", "url", "file_url", "fileUrl"),
        )

    def _request(
        self,
        url: str,
        method: str,
        headers: dict[str, str],
        payload: Optional[bytes],
    ) -> tuple[bytes, dict[str, str]]:
        request = Request(url=url, method=method, headers=headers, data=payload)
        with urlopen(request, timeout=self.config.survey_timeout_seconds) as response:
            return response.read(), dict(response.headers)

