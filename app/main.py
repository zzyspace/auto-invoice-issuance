from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
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
    parser.add_argument("command", choices=("run-once", "schedule", "smoke-test", "tax-lookup-test"))
    parser.add_argument("--env-file", default=".env", help="Path to .env file")
    parser.add_argument("--company-name", help="Company name used by tax-lookup-test")
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
    return command_tax_lookup_test(env_file, args.company_name)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
