from __future__ import annotations

import json
import re
import ssl
import shutil
from copy import copy
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Optional

ORGANIZATION_KEYWORDS = (
    "公司",
    "有限",
    "集团",
    "事务所",
    "分公司",
    "工作室",
    "中心",
    "工厂",
    "厂",
    "店",
    "个体工商户",
    "门店",
    "商行",
    "超市",
    "药房",
    "酒店",
    "餐厅",
    "俱乐部",
    "协会",
    "学校",
    "医院",
)
ENTERPRISE_TAX_ID_PATTERN = re.compile(r"^(?:\d{15}|[0-9A-Z]{18})$")


def parse_json_object(raw: str, default: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    text = raw.strip()
    if not text:
        return default or {}
    return json.loads(text)


def render_template(template: str, context: dict[str, Any]) -> str:
    if not template:
        return ""

    class SafeDict(dict[str, Any]):
        def __missing__(self, key: str) -> str:
            return "{" + key + "}"

    return template.format_map(SafeDict(context))


def render_token_template(template: str, context: dict[str, Any]) -> str:
    rendered = template
    for key, value in context.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", str(value))
    return rendered


def ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def backup_existing_file(path: Path, backup_dir: Path) -> Optional[Path]:
    if not path.exists():
        return None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = path.suffix
    backup_path = backup_dir / f"{timestamp}{suffix}"
    index = 1
    while backup_path.exists():
        backup_path = backup_dir / f"{timestamp}_{index}{suffix}"
        index += 1
    ensure_parent_dir(backup_path)
    shutil.copy2(path, backup_path)
    return backup_path


def build_ssl_context(verify: bool = True, ca_bundle_path: Optional[Path] = None) -> ssl.SSLContext:
    if not verify:
        return ssl._create_unverified_context()
    if ca_bundle_path:
        return ssl.create_default_context(cafile=str(ca_bundle_path))
    return ssl.create_default_context()


def is_ascii_alphanumeric(value: str) -> bool:
    return bool(value) and value.isascii() and value.isalnum()


def normalize_tax_id(value: str) -> Optional[str]:
    raw = (value or "").strip()
    if not raw:
        return None
    cleaned = re.sub(r"\s+", "", raw)
    normalized = cleaned.upper()
    if is_ascii_alphanumeric(normalized):
        return normalized
    return None


def looks_like_valid_enterprise_tax_id(value: str) -> bool:
    normalized = normalize_tax_id(value)
    if not normalized:
        return False
    return bool(ENTERPRISE_TAX_ID_PATTERN.fullmatch(normalized))


def normalize_company_name(value: str) -> str:
    cleaned = re.sub(r"\s+", "", value or "")
    if not cleaned:
        return ""
    normalized = (
        cleaned.replace("（", "(")
        .replace("）", ")")
        .replace("【", "[")
        .replace("】", "]")
    )
    return normalized.lower()


def extract_attachment_download_url(raw_value: str) -> Optional[str]:
    raw = (raw_value or "").strip()
    if not raw:
        return None
    match = re.match(r'=Hyperlink\("([^"]+)"[，,]\s*"[^"]*"\)', raw, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    return None


def looks_like_natural_person(title: str) -> bool:
    cleaned = re.sub(r"\s+", "", title or "")
    if not cleaned:
        return False
    if any(keyword in cleaned for keyword in ORGANIZATION_KEYWORDS):
        return False
    return bool(re.fullmatch(r"[A-Za-z\u4e00-\u9fff]{2,5}", cleaned))


def format_decimal_text(value: Optional[Decimal]) -> Optional[str]:
    if value is None:
        return None
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return str(normalized.quantize(Decimal("1")))
    return format(normalized, "f").rstrip("0").rstrip(".")


def parse_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def extract_json_block(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        raise ValueError("Empty model response.")
    if stripped.startswith("{") and stripped.endswith("}"):
        return json.loads(stripped)
    match = re.search(r"\{.*\}", stripped, re.DOTALL)
    if not match:
        raise ValueError(f"Could not find JSON object in model response: {stripped}")
    return json.loads(match.group(0))


def find_first_string_by_keys(payload: Any, candidate_keys: Iterable[str]) -> Optional[str]:
    if isinstance(payload, dict):
        for key in candidate_keys:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in payload.values():
            found = find_first_string_by_keys(value, candidate_keys)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = find_first_string_by_keys(item, candidate_keys)
            if found:
                return found
    return None


def get_value_by_path(payload: Any, path: Optional[str]) -> Any:
    if not path:
        return None
    current = payload
    for segment in path.split("."):
        if isinstance(current, dict):
            current = current.get(segment)
        else:
            return None
    return current


def next_daily_run(now: datetime, hour: int, minute: int) -> datetime:
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate = candidate + timedelta(days=1)
    return candidate


def default_export_range(now: Optional[datetime] = None) -> tuple[str, str]:
    current = now or datetime.now()
    from_point = current - timedelta(days=365)
    from_datetime = from_point.strftime("%Y-%m-%d 00:00:00")
    to_datetime = current.strftime("%Y-%m-%d 23:59:59")
    return from_datetime, to_datetime


def clone_row_style(sheet: Any, template_row: int, target_row: int, max_column: int) -> None:
    for column in range(1, max_column + 1):
        source = sheet.cell(template_row, column)
        target = sheet.cell(target_row, column)
        if source.has_style:
            target._style = copy(source._style)
        if source.number_format:
            target.number_format = source.number_format
        if source.font:
            target.font = copy(source.font)
        if source.fill:
            target.fill = copy(source.fill)
        if source.border:
            target.border = copy(source.border)
        if source.alignment:
            target.alignment = copy(source.alignment)
        if source.protection:
            target.protection = copy(source.protection)


@dataclass(frozen=True)
class BackupResult:
    backup_path: Optional[Path]
    output_path: Path
