from __future__ import annotations

import json
from typing import Optional
from urllib.parse import quote
from urllib.request import Request, urlopen

from app.models import AppConfig
from app.utils import find_first_string_by_keys, get_value_by_path, normalize_tax_id


class TaxLookupClient:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def lookup(self, company_name: str) -> Optional[str]:
        if not self.config.tax_lookup_url_template:
            return None
        url = self.config.tax_lookup_url_template.format(
            company_name=quote(company_name),
            company_name_raw=company_name,
        )
        request = Request(url=url, method="GET", headers=self.config.tax_lookup_extra_headers)
        with urlopen(request, timeout=self.config.survey_timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        configured = get_value_by_path(payload, self.config.tax_lookup_value_path)
        if isinstance(configured, str):
            return normalize_tax_id(configured)
        discovered = find_first_string_by_keys(
            payload,
            (
                "tax_id",
                "taxId",
                "credit_code",
                "creditCode",
                "credit_code_full",
                "tax_no",
                "taxNo",
                "business_no",
                "businessNo",
            ),
        )
        if discovered:
            return normalize_tax_id(discovered)
        return None

