from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from app.models import AppConfig, StoreConfig
from app.utils import parse_json_object


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            continue
        os.environ.setdefault(key.strip(), value.strip())


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def _optional_path(name: str, default: Optional[str] = None) -> Optional[Path]:
    raw = os.environ.get(name, default)
    if raw is None or not str(raw).strip():
        return None
    return Path(raw).expanduser().resolve()


def _parse_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"false", "0", "no", "off"}


def _parse_scalar(value: str) -> Any:
    stripped = value.strip()
    if not stripped:
        return ""
    if (stripped.startswith('"') and stripped.endswith('"')) or (
        stripped.startswith("'") and stripped.endswith("'")
    ):
        return stripped[1:-1]
    lowered = stripped.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "none"}:
        return None
    if stripped.lstrip("-").isdigit():
        return int(stripped)
    return stripped


def load_store_configs(path: Path) -> list[StoreConfig]:
    content = path.read_text(encoding="utf-8")
    stores = _parse_stores_yaml(content)
    base_dir = path.parent
    configs: list[StoreConfig] = []
    for item in stores:
        if not isinstance(item, dict):
            raise ValueError(f"Invalid store item in {path}: {item!r}")
        output_path = Path(str(item["output_xlsx_path"])).expanduser()
        if not output_path.is_absolute():
            output_path = (base_dir / output_path).resolve()
        configs.append(
            StoreConfig(
                store_key=str(item["store_key"]),
                store_name=str(item["store_name"]),
                survey_id=str(item["survey_id"]),
                output_xlsx_path=output_path,
                initial_last_processed_id=int(item["initial_last_processed_id"]),
                enabled=_parse_bool(item.get("enabled", True)),
                attachment_question_id=str(item["attachment_question_id"])
                if item.get("attachment_question_id")
                else None,
            )
        )
    if not configs:
        raise ValueError(f"No stores configured in {path}")
    return configs


def _parse_stores_yaml(content: str) -> list[dict[str, Any]]:
    stores: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None
    in_stores_block = False
    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not raw_line.startswith(" ") and stripped.endswith(":"):
            in_stores_block = stripped[:-1] == "stores"
            continue
        if not in_stores_block and stripped.startswith("- "):
            in_stores_block = True
        if not in_stores_block:
            continue
        if stripped.startswith("- "):
            if current:
                stores.append(current)
            current = {}
            remainder = stripped[2:].strip()
            if remainder:
                key, separator, value = remainder.partition(":")
                if not separator:
                    raise ValueError(f"Invalid store entry line: {raw_line}")
                current[key.strip()] = _parse_scalar(value)
            continue
        if current is None:
            raise ValueError(f"Found store property before list item: {raw_line}")
        key, separator, value = stripped.partition(":")
        if not separator:
            raise ValueError(f"Invalid YAML line: {raw_line}")
        current[key.strip()] = _parse_scalar(value)
    if current:
        stores.append(current)
    return stores


def load_app_config(env_path: Optional[Path] = None) -> AppConfig:
    if env_path:
        load_env_file(env_path)
    base_dir = env_path.parent.resolve() if env_path else Path.cwd()

    def resolve_path(raw: str) -> Path:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = (base_dir / path).resolve()
        return path

    config = AppConfig(
        timezone=os.environ.get("TZ", "Asia/Shanghai"),
        survey_cookie=_require_env("TENCENT_SURVEY_COOKIE"),
        survey_export_url=_require_env("TENCENT_SURVEY_EXPORT_URL"),
        survey_export_method=_require_env("TENCENT_SURVEY_EXPORT_METHOD").upper(),
        survey_export_body_template=os.environ.get("TENCENT_SURVEY_EXPORT_BODY_TEMPLATE", ""),
        survey_export_download_url_path=os.environ.get("TENCENT_SURVEY_EXPORT_DOWNLOAD_URL_PATH") or None,
        survey_extra_headers=parse_json_object(
            os.environ.get("TENCENT_SURVEY_EXTRA_HEADERS_JSON", "{}"), default={}
        ),
        default_attachment_question_id=os.environ.get("TENCENT_SURVEY_DEFAULT_UPLOAD_QUESTION_ID") or None,
        openai_base_url=_require_env("OPENAI_BASE_URL"),
        openai_api_key=_require_env("OPENAI_API_KEY"),
        openai_model=os.environ.get("OPENAI_MODEL", "qwen3.5-flash"),
        smtp_host=_require_env("SMTP_HOST"),
        smtp_port=int(os.environ.get("SMTP_PORT", "587")),
        smtp_username=_require_env("SMTP_USERNAME"),
        smtp_password=_require_env("SMTP_PASSWORD"),
        smtp_from=_require_env("SMTP_FROM"),
        smtp_to=[item.strip() for item in _require_env("SMTP_TO").split(",") if item.strip()],
        template_xlsx_path=resolve_path(_require_env("TEMPLATE_XLSX_PATH")),
        state_db_path=resolve_path(_require_env("STATE_DB_PATH")),
        stores_config_path=resolve_path(_require_env("STORES_CONFIG_PATH")),
        backups_root=(
            _optional_path("BACKUPS_ROOT")
            or base_dir.joinpath("backups").resolve()
        ),
        run_hour=int(os.environ.get("RUN_HOUR", "22")),
        run_minute=int(os.environ.get("RUN_MINUTE", "30")),
        openai_timeout_seconds=int(os.environ.get("OPENAI_TIMEOUT_SECONDS", "60")),
        survey_timeout_seconds=int(os.environ.get("SURVEY_TIMEOUT_SECONDS", "60")),
        tax_lookup_url_template=os.environ.get("TAX_LOOKUP_URL_TEMPLATE") or None,
        tax_lookup_extra_headers=parse_json_object(
            os.environ.get("TAX_LOOKUP_EXTRA_HEADERS_JSON", "{}"), default={}
        ),
        tax_lookup_value_path=os.environ.get("TAX_LOOKUP_VALUE_PATH") or None,
    )
    return config
