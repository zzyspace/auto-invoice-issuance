from __future__ import annotations

import json
from dataclasses import replace
from typing import Callable, Optional, Protocol
from urllib.parse import quote
from urllib.request import Request, urlopen

from app.models import AppConfig, TaxLookupResult
from app.state import StateStore
from app.utils import (
    build_ssl_context,
    find_first_string_by_keys,
    get_value_by_path,
    normalize_company_name,
    normalize_tax_id,
)

ALAPI_SIMPLE_SEARCH_URL = "https://v3.alapi.cn/api/enterprise/simple_search"
KNOWN_TAX_ID_KEYS = (
    "tax_id",
    "taxId",
    "credit_code",
    "creditCode",
    "credit_code_full",
    "credit_no",
    "creditNo",
    "tax_no",
    "taxNo",
    "taxNumber",
    "business_no",
    "businessNo",
)


class TaxLookupProvider(Protocol):
    name: str

    def lookup(self, company_name: str) -> TaxLookupResult:
        ...


class AlapiTaxLookupProvider:
    name = "alapi"

    def __init__(
        self,
        token: str,
        timeout_seconds: int,
        request_json: Callable[[str, str, dict[str, str], Optional[bytes]], object],
    ) -> None:
        if not token.strip():
            raise ValueError("TAX_LOOKUP_ALAPI_TOKEN is required when TAX_LOOKUP_PROVIDER=alapi.")
        self.token = token.strip()
        self.timeout_seconds = timeout_seconds
        self.request_json = request_json

    def lookup(self, company_name: str) -> TaxLookupResult:
        payload = self.request_json(
            ALAPI_SIMPLE_SEARCH_URL,
            "POST",
            {
                "Content-Type": "application/json",
                "token": self.token,
            },
            json.dumps({"keyword": company_name, "skip": "0"}, ensure_ascii=False).encode("utf-8"),
        )
        if not isinstance(payload, dict):
            raise RuntimeError("ALAPI returned a non-object JSON payload.")
        if payload.get("success") is False:
            raise RuntimeError(str(payload.get("message") or "ALAPI request failed."))

        raw_response_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        items = payload.get("data", {}).get("items", [])
        if not isinstance(items, list):
            items = []
        if not items:
            return TaxLookupResult(
                provider=self.name,
                status="no_result",
                tax_id=None,
                matched_name=None,
                candidate_count=0,
                raw_response_json=raw_response_json,
                message="未返回候选企业。",
            )

        normalized_input = normalize_company_name(company_name)
        exact_matches: list[tuple[str, str]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            candidate_name = str(item.get("name") or "").strip()
            if normalize_company_name(candidate_name) != normalized_input:
                continue
            tax_id = normalize_tax_id(
                str(
                    item.get("credit_no")
                    or item.get("creditNo")
                    or item.get("taxNumber")
                    or item.get("tax_no")
                    or ""
                )
            )
            if not tax_id:
                continue
            exact_matches.append((candidate_name, tax_id))

        if not exact_matches:
            return TaxLookupResult(
                provider=self.name,
                status="no_exact_match",
                tax_id=None,
                matched_name=None,
                candidate_count=len(items),
                raw_response_json=raw_response_json,
                message="候选企业未命中标准化后的精确公司名。",
            )

        unique_tax_ids = {tax_id for _, tax_id in exact_matches}
        if len(unique_tax_ids) != 1:
            return TaxLookupResult(
                provider=self.name,
                status="no_exact_match",
                tax_id=None,
                matched_name=None,
                candidate_count=len(items),
                raw_response_json=raw_response_json,
                message="精确公司名命中了多个不同税号。",
            )

        matched_name, tax_id = exact_matches[0]
        return TaxLookupResult(
            provider=self.name,
            status="success",
            tax_id=tax_id,
            matched_name=matched_name,
            candidate_count=len(items),
            raw_response_json=raw_response_json,
            message="命中精确公司名。",
        )


class LegacyTemplateTaxLookupProvider:
    name = "legacy_template"

    def __init__(
        self,
        url_template: str,
        extra_headers: dict[str, str],
        value_path: Optional[str],
        request_json: Callable[[str, str, dict[str, str], Optional[bytes]], object],
    ) -> None:
        if not url_template.strip():
            raise ValueError(
                "TAX_LOOKUP_URL_TEMPLATE is required when TAX_LOOKUP_PROVIDER=legacy_template."
            )
        self.url_template = url_template
        self.extra_headers = extra_headers
        self.value_path = value_path
        self.request_json = request_json

    def lookup(self, company_name: str) -> TaxLookupResult:
        url = self.url_template.format(
            company_name=quote(company_name),
            company_name_raw=company_name,
        )
        payload = self.request_json(url, "GET", self.extra_headers, None)
        if not isinstance(payload, dict):
            raise RuntimeError("Legacy tax lookup returned a non-object JSON payload.")

        raw_response_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        configured = get_value_by_path(payload, self.value_path)
        if isinstance(configured, str):
            tax_id = normalize_tax_id(configured)
            if tax_id:
                return TaxLookupResult(
                    provider=self.name,
                    status="success",
                    tax_id=tax_id,
                    matched_name=None,
                    raw_response_json=raw_response_json,
                    message="命中配置的税号字段。",
                )

        discovered = find_first_string_by_keys(payload, KNOWN_TAX_ID_KEYS)
        if discovered:
            tax_id = normalize_tax_id(discovered)
            if tax_id:
                return TaxLookupResult(
                    provider=self.name,
                    status="success",
                    tax_id=tax_id,
                    matched_name=None,
                    raw_response_json=raw_response_json,
                    message="命中默认税号字段。",
                )

        return TaxLookupResult(
            provider=self.name,
            status="no_result",
            tax_id=None,
            matched_name=None,
            raw_response_json=raw_response_json,
            message="未找到可用税号字段。",
        )


class TaxLookupClient:
    def __init__(self, config: AppConfig, state_store: StateStore) -> None:
        self.config = config
        self.state_store = state_store
        self.ssl_context = build_ssl_context(config.tax_lookup_ssl_verify, config.tax_lookup_ca_bundle_path)
        self.provider = self._build_provider()
        self._memory_cache: dict[tuple[str, str], TaxLookupResult] = {}

    @property
    def provider_name(self) -> str:
        return self.provider.name if self.provider else self.config.tax_lookup_provider

    @property
    def is_enabled(self) -> bool:
        return self.provider is not None

    def lookup(self, company_name: str) -> TaxLookupResult:
        normalized_company_name = normalize_company_name(company_name)
        if not normalized_company_name:
            return TaxLookupResult(
                provider=self.provider_name,
                status="no_result",
                tax_id=None,
                matched_name=None,
                message="公司名为空。",
            )
        if self.provider is None:
            return TaxLookupResult(
                provider="disabled",
                status="disabled",
                tax_id=None,
                matched_name=None,
                message="未启用税号查询。",
            )

        memory_key = (self.provider.name, normalized_company_name)
        memoized = self._memory_cache.get(memory_key)
        if memoized is not None:
            return replace(memoized, from_cache=True)

        cached_entry = self.state_store.get_tax_lookup_cache(
            self.provider.name,
            normalized_company_name,
            self.config.tax_lookup_cache_negative_ttl_hours,
        )
        if cached_entry is not None:
            result = TaxLookupResult(
                provider=cached_entry.provider,
                status=cached_entry.lookup_status,
                tax_id=cached_entry.tax_id,
                matched_name=cached_entry.matched_name,
                candidate_count=cached_entry.candidate_count,
                from_cache=True,
                raw_response_json=cached_entry.raw_response_json,
                message="命中本地缓存。",
            )
            self._memory_cache[memory_key] = replace(result, from_cache=False)
            return result

        result = self.provider.lookup(company_name)
        if result.status in {"success", "no_result", "no_exact_match"}:
            self.state_store.upsert_tax_lookup_cache(
                self.provider.name,
                normalized_company_name,
                result,
            )
            self._memory_cache[memory_key] = replace(result, from_cache=False)
        return result

    def _build_provider(self) -> Optional[TaxLookupProvider]:
        provider_name = self.config.tax_lookup_provider
        if provider_name == "disabled":
            return None
        if provider_name == "alapi":
            return AlapiTaxLookupProvider(
                token=self.config.tax_lookup_alapi_token or "",
                timeout_seconds=self.config.tax_lookup_timeout_seconds,
                request_json=self._request_json,
            )
        if provider_name == "legacy_template":
            return LegacyTemplateTaxLookupProvider(
                url_template=self.config.tax_lookup_url_template or "",
                extra_headers=self.config.tax_lookup_extra_headers,
                value_path=self.config.tax_lookup_value_path,
                request_json=self._request_json,
            )
        raise ValueError(f"Unsupported TAX_LOOKUP_PROVIDER: {provider_name}")

    def _request_json(
        self,
        url: str,
        method: str,
        headers: dict[str, str],
        payload: Optional[bytes],
    ) -> object:
        request = Request(url=url, method=method, headers=headers, data=payload)
        with urlopen(
            request,
            timeout=self.config.tax_lookup_timeout_seconds,
            context=self.ssl_context,
        ) as response:
            return json.loads(response.read().decode("utf-8"))
