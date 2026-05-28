from __future__ import annotations

import base64
import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional
from urllib.request import Request, urlopen

from app.models import AmountExtraction
from app.utils import build_ssl_context, extract_json_block, parse_decimal

SMOKE_TEST_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAwAAAAMCAQAAAD8fJRsAAAAEElEQVR42mNkYGD4z0AEYBxVSFUAAN0AARt1ik0AAAAASUVORK5CYII="
)


class OpenAICompatibleVisionClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: int = 60,
        ssl_verify: bool = True,
        ca_bundle_path: Optional[Path] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.ssl_context = build_ssl_context(ssl_verify, ca_bundle_path)

    def smoke_test(self) -> None:
        self._chat_completion(SMOKE_TEST_PNG_BASE64, "test.png", "请返回JSON: {\"ok\": true}")

    def extract_total_amount(self, image_bytes: bytes, file_name: str) -> AmountExtraction:
        image_base64 = base64.b64encode(image_bytes).decode("ascii")
        prompt = (
            "你是开票金额识别助手。请识别图片中的所有实际支付金额，"
            "如果有多条付款记录，请求和得到总金额。"
            "只返回 JSON，不要输出额外文字，格式为："
            "{\"total_amount\": \"123.45\", \"confidence\": \"0.92\", \"notes\": \"简短说明\"}。"
            "如果无法确定金额，则 total_amount 设为 null，并在 notes 说明原因。"
        )
        raw_text = self._chat_completion(image_base64, file_name, prompt)
        payload = extract_json_block(raw_text)
        total_amount = parse_decimal(payload.get("total_amount"))
        confidence = parse_decimal(payload.get("confidence"))
        notes = str(payload.get("notes") or "")
        return AmountExtraction(
            total_amount=total_amount,
            confidence=confidence,
            notes=notes,
            raw_response=raw_text,
        )

    def _chat_completion(self, image_base64: str, file_name: str, prompt: str) -> str:
        endpoint = self.base_url
        if not endpoint.endswith("/chat/completions"):
            endpoint = f"{endpoint}/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;name={file_name};base64,{image_base64}"
                            },
                        },
                    ],
                }
            ],
            "temperature": 0,
        }
        request = Request(
            url=endpoint,
            method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        with urlopen(request, timeout=self.timeout_seconds, context=self.ssl_context) as response:
            body = json.loads(response.read().decode("utf-8"))
        choices = body.get("choices") or []
        if not choices:
            raise ValueError(f"Vision API returned no choices: {body}")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    texts.append(str(item.get("text") or ""))
            if texts:
                return "\n".join(texts)
        raise ValueError(f"Unsupported vision response payload: {body}")
