from __future__ import annotations

import json
import time
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, urlencode
from urllib.request import Request, urlopen

from app.models import AppConfig, StoreConfig
from app.utils import (
    build_ssl_context,
    default_export_range,
    find_first_string_by_keys,
    get_value_by_path,
    render_template,
    render_token_template,
)

EXPORT_CHECK_URL = "https://wj.qq.com/api/files/export_check"
EXPORT_POLL_INTERVAL_SECONDS = 2
EXPORT_POLL_MAX_ATTEMPTS = 30


class TencentSurveyClient:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.ssl_context = build_ssl_context(config.survey_ssl_verify, config.survey_ca_bundle_path)
        self.base_headers = {
            "Cookie": config.survey_cookie,
            "User-Agent": "Mozilla/5.0",
            "Accept": "*/*",
        }
        self.base_headers.update(config.survey_extra_headers)

    def export_csv(self, store: StoreConfig) -> str:
        from_datetime, to_datetime = default_export_range()
        context = {
            "survey_id": store.survey_id,
            "store_key": store.store_key,
            "store_name": store.store_name,
            "from_datetime": from_datetime,
            "to_datetime": to_datetime,
        }
        url = render_template(self.config.survey_export_url, context)
        body = self._build_export_body(context)
        headers = dict(self.base_headers)
        method = self.config.survey_export_method.upper()
        payload: Optional[bytes] = None
        if method == "GET" and body:
            params = urlencode(dict(parse_qsl(body, keep_blank_values=True)))
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{params}"
        elif method == "POST":
            payload = body.encode("utf-8")
            if body.lstrip().startswith("{"):
                headers.setdefault("Content-Type", "application/json")
                headers.setdefault("Accept", "application/json, text/plain, */*")
            else:
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
            job_id = get_value_by_path(payload_json, "data.id")
            if job_id is None:
                raise ValueError("Export response did not contain CSV content, download URL, or job id.")
            checked_payload = self._poll_export_job(int(job_id), headers)
            download_url = self._extract_export_download_url(checked_payload)
        if not download_url:
            raise ValueError("Export flow did not return a downloadable URL.")
        csv_bytes, _ = self._request(download_url, "GET", headers, None)
        return self._decode_csv_bytes(csv_bytes)

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
            ("cos_download_url", "download_url", "downloadUrl", "url", "file_url", "fileUrl"),
        )

    def _build_export_body(self, context: dict[str, str]) -> str:
        template = self.config.survey_export_body_template.strip()
        if template:
            if "{{" in template and "}}" in template:
                return render_token_template(template, context)
            return render_template(template, context)
        return json.dumps(
            {
                "survey_id": context["survey_id"],
                "type": "excel",
                "ip_unique": False,
                "query": {},
                "location": {},
                "duration": {},
                "sort": "ended_at",
                "order": "desc",
                "from": context["from_datetime"],
                "to": context["to_datetime"],
                "channels": [],
                "respondent_nickname": "",
                "status": "valid",
                "q": "",
                "contact_group_ids": [],
                "query_datetime": {},
                "query_text": {},
                "custom_args": [],
                "is_hide_sensitive_data": False,
            },
            ensure_ascii=False,
        )

    def _poll_export_job(self, job_id: int, headers: dict[str, str]) -> dict[str, object]:
        last_payload: Optional[dict[str, object]] = None
        for _ in range(EXPORT_POLL_MAX_ATTEMPTS):
            timestamp = int(time.time() * 1000)
            url = f"{EXPORT_CHECK_URL}?_={timestamp}&job_id={job_id}"
            response_bytes, _ = self._request(url, "GET", headers, None)
            payload = json.loads(response_bytes.decode("utf-8"))
            last_payload = payload
            status = str(get_value_by_path(payload, "data.status_info") or "")
            if status == "Done":
                return payload
            if status in {"Fail", "Error"}:
                raise ValueError(f"Export job {job_id} failed with status '{status}'.")
            time.sleep(EXPORT_POLL_INTERVAL_SECONDS)
        raise TimeoutError(f"Export job {job_id} did not complete after polling.")

    @staticmethod
    def _decode_csv_bytes(csv_bytes: bytes) -> str:
        if zipfile.is_zipfile(BytesIO(csv_bytes)):
            with zipfile.ZipFile(BytesIO(csv_bytes)) as archive:
                csv_members = [name for name in archive.namelist() if name.lower().endswith(".csv")]
                if not csv_members:
                    raise ValueError("Export archive did not contain a CSV file.")
                with archive.open(csv_members[0]) as member:
                    return member.read().decode("utf-8-sig")
        return csv_bytes.decode("utf-8-sig")

    def _request(
        self,
        url: str,
        method: str,
        headers: dict[str, str],
        payload: Optional[bytes],
    ) -> tuple[bytes, dict[str, str]]:
        request = Request(url=url, method=method, headers=headers, data=payload)
        with urlopen(request, timeout=self.config.survey_timeout_seconds, context=self.ssl_context) as response:
            return response.read(), dict(response.headers)
