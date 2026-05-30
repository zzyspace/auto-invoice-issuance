from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from app.config import load_app_config, load_store_configs
from app.excel_writer import InvoiceExcelWriter
from app.mailer import SummaryMailer
from app.service import BatchProcessor, Services
from app.state import StateStore
from app.survey_client import TencentSurveyClient
from app.tax_lookup import TaxLookupClient
from app.utils import next_daily_run
from app.vision_client import OpenAICompatibleVisionClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Tencent survey invoice automation")
    parser.add_argument(
        "command",
        choices=(
            "run-once",
            "schedule",
            "smoke-test",
            "tax-lookup-test",
            "portal-sync",
            "portal-open-chrome-cdp",
            "portal-issue-dry-run",
            "portal-issue-run",
        ),
    )
    parser.add_argument("--env-file", default=".env", help="Path to .env file")
    parser.add_argument("--company-name", help="Company name used by tax-lookup-test")
    parser.add_argument(
        "--store-key",
        action="append",
        dest="store_keys",
        help="Limit portal issue commands to one or more store_key values",
    )
    parser.add_argument(
        "--skip-sync",
        action="store_true",
        help="For portal issue commands, skip syncing workbooks from the server and use local output files directly.",
    )
    return parser


def build_processor(env_file: Path) -> tuple[BatchProcessor, object, list[object]]:
    config = load_app_config(env_file)
    stores = load_store_configs(config.stores_config_path)
    state_store = StateStore(config.state_db_path)
    services = Services(
        survey_client=TencentSurveyClient(config),
        vision_client=OpenAICompatibleVisionClient(
            config.openai_base_url,
            config.openai_api_key,
            config.openai_model,
            timeout_seconds=config.openai_timeout_seconds,
            ssl_verify=config.openai_ssl_verify,
            ca_bundle_path=config.openai_ca_bundle_path,
        ),
        tax_lookup_client=TaxLookupClient(config, state_store),
        excel_writer=InvoiceExcelWriter(config.template_xlsx_path, config.backups_root),
        state_store=state_store,
        mailer=SummaryMailer(
            config.smtp_host,
            config.smtp_port,
            config.smtp_username,
            config.smtp_password,
            config.smtp_from,
            config.smtp_to,
        ),
    )
    return BatchProcessor(services), config, stores


def command_run_once(env_file: Path) -> int:
    processor, _, stores = build_processor(env_file)
    processor.run(stores)
    return 0


def command_schedule(env_file: Path) -> int:
    processor, config, stores = build_processor(env_file)
    timezone = ZoneInfo(config.timezone)
    while True:
        now = datetime.now(timezone)
        next_run = next_daily_run(now, config.run_hour, config.run_minute)
        sleep_seconds = max((next_run - now).total_seconds(), 1)
        time.sleep(sleep_seconds)
        processor.run(stores)


def command_smoke_test(env_file: Path) -> int:
    processor, _, _ = build_processor(env_file)
    processor.services.vision_client.smoke_test()
    return 0


def command_tax_lookup_test(env_file: Path, company_name: str | None) -> int:
    if not company_name or not company_name.strip():
        raise ValueError("--company-name is required for tax-lookup-test")
    config = load_app_config(env_file)
    state_store = StateStore(config.state_db_path)
    client = TaxLookupClient(config, state_store)
    result = client.lookup(company_name.strip())
    print(
        json.dumps(
            {
                "provider": result.provider,
                "status": result.status,
                "tax_id": result.tax_id,
                "matched_name": result.matched_name,
                "candidate_count": result.candidate_count,
                "from_cache": result.from_cache,
                "message": result.message,
            },
            ensure_ascii=False,
        )
    )
    return 0


def _select_portal_stores(stores: list[object], requested_store_keys: list[str] | None) -> list[object]:
    requested = {item.strip() for item in (requested_store_keys or []) if item and item.strip()}
    selected: list[tuple[int, int, object]] = []
    for index, store in enumerate(stores):
        if requested and store.store_key not in requested:
            continue
        if store.portal_enabled:
            selected.append((int(store.portal_priority), index, store))
    if requested:
        missing = requested.difference({store.store_key for _, _, store in selected})
        if missing:
            raise ValueError(
                "Requested store_key values are not portal-enabled or not configured: "
                + ", ".join(sorted(missing))
            )
    if not selected:
        raise ValueError("No portal-enabled stores selected. Set portal_enabled=true in stores.yaml.")
    selected.sort(key=lambda item: (item[0], item[1]))
    return [store for _, _, store in selected]


def _resolve_portal_issue_config(config: object, skip_sync: bool) -> object:
    if not skip_sync:
        return config
    return replace(config, portal_sync_from_server=False)


def command_portal_sync(env_file: Path, store_keys: list[str] | None) -> int:
    from app.portal_sync import PortalWorkbookSyncer

    config = load_app_config(env_file)
    if not config.portal_sync_from_server:
        raise ValueError(
            "portal-sync requires TAX_PORTAL_SYNC_FROM_SERVER=true so the remote source is configured."
        )
    stores = load_store_configs(config.stores_config_path)
    selected_stores = _select_portal_stores(stores, store_keys)
    syncer = PortalWorkbookSyncer(config)
    synced_paths: list[str] = []
    for store in selected_stores:
        syncer.sync_store_workbook(store)
        synced_paths.append(str(store.output_xlsx_path))
    print(json.dumps({"synced": synced_paths}, ensure_ascii=False, indent=2))
    return 0


def _portal_chrome_cdp_ready(cdp_url: str, timeout_seconds: float = 2.0) -> bool:
    version_url = cdp_url.rstrip("/") + "/json/version"
    try:
        with urlopen(version_url, timeout=timeout_seconds) as response:
            return response.status == 200
    except (URLError, OSError):
        return False


def _wait_for_portal_chrome_cdp(cdp_url: str, timeout_seconds: float = 30.0) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if _portal_chrome_cdp_ready(cdp_url):
            return True
        time.sleep(0.5)
    return False


def _resolve_chrome_cdp_port(cdp_url: str) -> int:
    raw = cdp_url.rsplit(":", 1)[-1].strip().rstrip("/")
    if not raw.isdigit():
        raise ValueError(f"Unsupported TAX_PORTAL_CHROME_CDP_URL for Chrome launch: {cdp_url}")
    return int(raw)


def _resolve_portal_chrome_executable(config: object) -> str:
    if config.portal_chrome_executable_path is not None:
        return str(config.portal_chrome_executable_path)
    candidates = (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
    )
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    raise ValueError(
        "Could not locate a Chrome executable. Set TAX_PORTAL_CHROME_EXECUTABLE_PATH explicitly."
    )


def command_portal_open_chrome_cdp(env_file: Path) -> int:
    config = load_app_config(env_file)
    cdp_url = config.portal_chrome_cdp_url or "http://127.0.0.1:9222"
    if _portal_chrome_cdp_ready(cdp_url):
        print(
            json.dumps(
                {
                    "status": "ready",
                    "cdp_url": cdp_url,
                    "user_data_dir": str(config.portal_chrome_cdp_user_data_dir),
                    "launched": False,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    chrome_executable = _resolve_portal_chrome_executable(config)
    cdp_port = _resolve_chrome_cdp_port(cdp_url)
    user_data_dir = config.portal_chrome_cdp_user_data_dir
    if user_data_dir is None:
        raise ValueError("TAX_PORTAL_CHROME_CDP_USER_DATA_DIR is required to launch a dedicated Chrome instance.")
    user_data_dir.mkdir(parents=True, exist_ok=True)
    log_path = user_data_dir / "chrome-cdp.log"
    with log_path.open("ab") as log_file:
        process = subprocess.Popen(
            [
                chrome_executable,
                f"--remote-debugging-port={cdp_port}",
                f"--user-data-dir={user_data_dir}",
                "--no-first-run",
                "--new-window",
                config.portal_home_url,
            ],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    if not _wait_for_portal_chrome_cdp(cdp_url, timeout_seconds=30.0):
        raise RuntimeError(
            f"Chrome launched but CDP did not become ready at {cdp_url}. Check {log_path}."
        )
    print(
        json.dumps(
            {
                "status": "ready",
                "cdp_url": cdp_url,
                "user_data_dir": str(user_data_dir),
                "chrome_executable": chrome_executable,
                "pid": process.pid,
                "log_path": str(log_path),
                "launched": True,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def command_portal_issue(
    env_file: Path,
    store_keys: list[str] | None,
    submit: bool,
    skip_sync: bool,
) -> int:
    from app.portal_runner import TaxPortalRunner

    config = load_app_config(env_file)
    config = _resolve_portal_issue_config(config, skip_sync)
    stores = load_store_configs(config.stores_config_path)
    state_store = StateStore(config.state_db_path)
    selected_stores = _select_portal_stores(stores, store_keys)
    runner = TaxPortalRunner(config, state_store, submit=submit)
    results = runner.run(selected_stores)
    print(
        json.dumps(
            [
                {
                    "store_key": result.store_key,
                    "company_verify_name": result.company_verify_name,
                    "mode": result.mode,
                    "status": result.status,
                    "step": result.step,
                    "expected_count": result.expected_count,
                    "submitted_count": result.submitted_count,
                    "success_count": result.success_count,
                    "failure_count": result.failure_count,
                    "skip_sync": skip_sync,
                    "error": result.error,
                    "artifacts_dir": str(result.artifacts_dir) if result.artifacts_dir else None,
                }
                for result in results
            ],
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if all(result.status in {"validated", "success"} for result in results) else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    env_file = Path(args.env_file).expanduser().resolve()
    if args.command == "run-once":
        return command_run_once(env_file)
    if args.command == "schedule":
        return command_schedule(env_file)
    if args.command == "smoke-test":
        return command_smoke_test(env_file)
    if args.command == "portal-sync":
        return command_portal_sync(env_file, args.store_keys)
    if args.command == "portal-open-chrome-cdp":
        return command_portal_open_chrome_cdp(env_file)
    if args.command == "portal-issue-dry-run":
        return command_portal_issue(env_file, args.store_keys, submit=False, skip_sync=args.skip_sync)
    if args.command == "portal-issue-run":
        return command_portal_issue(env_file, args.store_keys, submit=True, skip_sync=args.skip_sync)
    return command_tax_lookup_test(env_file, args.company_name)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
