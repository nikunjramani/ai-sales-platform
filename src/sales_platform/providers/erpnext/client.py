"""A thin client for the Frappe REST API that ERPNext serves; the only place with HTTP calls to
ERPNext (https://docs.frappe.io/framework/user/en/api/rest).

- Authentication: the header `Authorization: token <api key>:<api secret>` of an API user.
- Lists: GET /api/resource/<DocType>?fields=[..]&filters=[..]&limit_start=&limit_page_length=
- Creating: POST /api/resource/<DocType> with the document as JSON.

Every failure becomes an ErpError with our normalised code (ports/erp.py):
401 -> unauthenticated; 403 -> forbidden (Frappe also answers 403 for a wrong key);
404 -> not_found; 409 -> conflict (duplicate); 417 or other 4xx -> invalid_request (Frappe's
ValidationError is 417); 429 -> rate_limited; 5xx and connection failures -> temporary for
reads. For writes, a timeout or 5xx after the request was sent may mean the document exists,
so it is unknown_outcome (never retried blindly).
"""

import json
from collections.abc import Iterator, Mapping, Sequence
from typing import Any
from urllib.parse import quote

import httpx

from sales_platform.catalogue.ports.erp import ErpError, ErpErrorCode

TIMEOUT = httpx.Timeout(20.0, connect=5.0)
PAGE = 500
MAX_ROWS = 20_000  # a list longer than this is refused rather than half-read
CHUNK = 100  # names per "in" filter, so URLs stay short


def _detail(response: httpx.Response) -> str:
    """Frappe's short error text (exc_type and the first server message), never a traceback."""
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    kind = str(body.get("exc_type") or "")
    message = ""
    raw = body.get("_server_messages")
    if isinstance(raw, str):
        try:
            first = json.loads(raw)[0]
            message = str(json.loads(first).get("message", "")) if isinstance(first, str) else ""
        except (ValueError, IndexError, AttributeError, TypeError):
            message = ""
    text = ": ".join(part for part in (kind, message) if part) or f"HTTP {response.status_code}"
    return text[:300]


def error_for(response: httpx.Response, *, write: bool) -> ErpError:
    status = response.status_code
    detail = _detail(response)
    if status == 401:
        return ErpError(ErpErrorCode.UNAUTHENTICATED, f"ERPNext refused the API key ({detail})")
    if status == 403:
        return ErpError(ErpErrorCode.FORBIDDEN, f"ERPNext refused access ({detail})")
    if status == 404:
        return ErpError(ErpErrorCode.NOT_FOUND, detail)
    if status == 409:
        return ErpError(ErpErrorCode.CONFLICT, detail)
    if status == 429:
        return ErpError(ErpErrorCode.RATE_LIMITED, "ERPNext asked us to slow down")
    if status >= 500:
        if write:
            return ErpError(
                ErpErrorCode.UNKNOWN_OUTCOME, f"ERPNext failed while writing ({detail})"
            )
        return ErpError(ErpErrorCode.TEMPORARY_UNAVAILABLE, f"ERPNext is unavailable ({detail})")
    return ErpError(ErpErrorCode.INVALID_REQUEST, f"ERPNext refused the request ({detail})")


class FrappeClient:
    def __init__(
        self, base_url: str, api_key: str, api_secret: str, http: httpx.Client | None = None
    ) -> None:
        self._base = base_url.rstrip("/")
        self._auth = f"token {api_key}:{api_secret}"
        self._http = http or httpx.Client(timeout=TIMEOUT)

    def __repr__(self) -> str:
        return f"FrappeClient({self._base!r}, <key hidden>)"

    # --- reads -----------------------------------------------------------------------------
    def logged_user(self) -> str:
        body = self._get("/api/method/frappe.auth.get_logged_user", {})
        return str(body.get("message", ""))

    def get_list(
        self,
        doctype: str,
        fields: Sequence[str],
        filters: Sequence[Sequence[Any]] = (),
        order_by: str = "name asc",
    ) -> list[dict[str, Any]]:
        """Every matching row, page by page (refused above MAX_ROWS)."""
        rows: list[dict[str, Any]] = []
        start = 0
        while True:
            body = self._get(
                f"/api/resource/{quote(doctype)}",
                {
                    "fields": json.dumps(list(fields)),
                    "filters": json.dumps([list(item) for item in filters]),
                    "order_by": order_by,
                    "limit_start": str(start),
                    "limit_page_length": str(PAGE),
                },
            )
            page = body.get("data")
            if not isinstance(page, list):
                raise ErpError(ErpErrorCode.TEMPORARY_UNAVAILABLE, "ERPNext sent no list")
            rows.extend(item for item in page if isinstance(item, dict))
            if len(rows) > MAX_ROWS:
                raise ErpError(
                    ErpErrorCode.INVALID_REQUEST, f"More than {MAX_ROWS:,} {doctype} rows"
                )
            if len(page) < PAGE:
                return rows
            start += PAGE

    def get_list_in(
        self,
        doctype: str,
        fields: Sequence[str],
        field_name: str,
        values: Sequence[str],
        filters: Sequence[Sequence[Any]] = (),
    ) -> list[dict[str, Any]]:
        """get_list with `field_name in values`, in chunks."""
        rows: list[dict[str, Any]] = []
        for chunk in _chunks(list(dict.fromkeys(values)), CHUNK):
            rows.extend(self.get_list(doctype, fields, [*filters, [field_name, "in", chunk]]))
        return rows

    def get_doc(self, doctype: str, name: str) -> dict[str, Any]:
        body = self._get(f"/api/resource/{quote(doctype)}/{quote(name, safe='')}", {})
        data = body.get("data")
        if not isinstance(data, dict):
            raise ErpError(ErpErrorCode.TEMPORARY_UNAVAILABLE, "ERPNext sent no document")
        return data

    # --- writes ----------------------------------------------------------------------------
    def insert(self, doctype: str, document: Mapping[str, Any]) -> dict[str, Any]:
        """Create one document. A failure after sending is unknown_outcome."""
        try:
            response = self._http.post(
                f"{self._base}/api/resource/{quote(doctype)}",
                json=dict(document),
                headers=self._headers(),
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as error:
            # Never reached ERPNext: nothing was created, safe to try again later.
            raise ErpError(
                ErpErrorCode.TEMPORARY_UNAVAILABLE, f"ERPNext unreachable ({type(error).__name__})"
            ) from None
        except httpx.HTTPError as error:
            raise ErpError(
                ErpErrorCode.UNKNOWN_OUTCOME,
                f"No answer from ERPNext after sending ({type(error).__name__})",
            ) from None
        if response.status_code != 200:
            raise error_for(response, write=True)
        try:
            data = response.json().get("data")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise ErpError(ErpErrorCode.UNKNOWN_OUTCOME, "ERPNext's answer could not be read")
        return data

    # --- internals -------------------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {"Authorization": self._auth, "Accept": "application/json"}

    def _get(self, path: str, params: Mapping[str, str]) -> dict[str, Any]:
        try:
            response = self._http.get(f"{self._base}{path}", params=params, headers=self._headers())
        except httpx.HTTPError as error:
            raise ErpError(
                ErpErrorCode.TEMPORARY_UNAVAILABLE, f"ERPNext unreachable ({type(error).__name__})"
            ) from None
        if response.status_code != 200:
            raise error_for(response, write=False)
        try:
            body = response.json()
        except ValueError:
            raise ErpError(
                ErpErrorCode.TEMPORARY_UNAVAILABLE, "ERPNext's answer could not be read"
            ) from None
        if not isinstance(body, dict):
            raise ErpError(ErpErrorCode.TEMPORARY_UNAVAILABLE, "ERPNext's answer could not be read")
        return body


def _chunks(values: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]
