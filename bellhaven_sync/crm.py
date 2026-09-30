"""Thin client for the CRM sandbox API."""
from __future__ import annotations

import time
from typing import Any

import requests

from . import config


class CRMError(RuntimeError):
    pass


class CRMClient:
    def __init__(self, base: str | None = None, token: str | None = None, session: requests.Session | None = None):
        self.base = (base or config.CRM_BASE).rstrip("/")
        token = token or config.CRM_TOKEN
        if not token:
            raise CRMError("No CRM token. Set BH_CRM_TOKEN.")
        self.s = session or requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})

    # -- low level ---------------------------------------------------------
    def _req(self, method: str, path: str, **kw) -> Any:
        url = self.base + path
        for attempt in range(4):
            r = self.s.request(method, url, timeout=config.REQUEST_TIMEOUT, **kw)
            if r.status_code in (429, 502, 503, 504) and attempt < 3:
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code >= 400:
                raise CRMError(f"{method} {path} -> {r.status_code}: {r.text[:500]}")
            return r.json() if r.content else None
        raise CRMError(f"{method} {path} failed after retries")

    def _paged(self, path: str, params: dict | None = None) -> list[dict]:
        params = dict(params or {})
        params.setdefault("page_size", 100)
        out, page = [], 1
        while True:
            params["page"] = page
            body = self._req("GET", path, params=params)
            data = body.get("data", [])
            out.extend(data)
            total = body.get("total", len(out))
            if not data or len(out) >= total:
                return out
            page += 1

    # -- accounts ----------------------------------------------------------
    def list_accounts(self, **filters) -> list[dict]:
        return self._paged("/accounts", filters)

    def get_account(self, account_id: str) -> dict:
        return self._req("GET", f"/accounts/{account_id}")

    def update_account(self, account_id: str, fields: dict) -> dict:
        return self._req("PATCH", f"/accounts/{account_id}", json=fields)

    def create_account(self, fields: dict) -> dict:
        return self._req("POST", "/accounts", json=fields)

    # -- contacts ----------------------------------------------------------
    def list_contacts(self, **filters) -> list[dict]:
        return self._paged("/contacts", filters)

    def update_contact(self, contact_id: str, fields: dict) -> dict:
        return self._req("PATCH", f"/contacts/{contact_id}", json=fields)
