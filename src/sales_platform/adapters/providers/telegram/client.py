"""A thin Telegram Bot API client (https://core.telegram.org/bots/api), the only place with
HTTP calls to Telegram. Outcomes are mapped to what the outbox needs:
- TelegramRefused: Telegram answered and refused for good (4xx except 429);
- TelegramBusy: 429 "too many requests" (retry after the given seconds);
- TelegramUncertain: the request may have arrived (timeout or 5xx after sending);
- any other exception (DNS, connection refused): nothing was sent, safe to retry.
"""

import json
from collections.abc import Mapping
from typing import Any

import httpx

API = "https://api.telegram.org"
TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class TelegramRefused(Exception):
    def __init__(self, code: int, description: str) -> None:
        super().__init__(description)
        self.code = code
        self.description = description


class TelegramBusy(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__(f"Telegram asked to wait {retry_after} s")
        self.retry_after = retry_after


class TelegramUncertain(Exception):
    """The request may have been carried out (never repeated automatically)."""


class TelegramClient:
    def __init__(self, token: str, http: httpx.Client | None = None) -> None:
        self._token = token
        self._http = http or httpx.Client(timeout=TIMEOUT)

    def __repr__(self) -> str:
        return "TelegramClient(<token hidden>)"

    def call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        files: Mapping[str, tuple[str, bytes, str]] | None = None,
    ) -> Any:
        url = f"{API}/bot{self._token}/{method}"
        try:
            if files:
                data = {
                    name: value if isinstance(value, str) else json.dumps(value)
                    for name, value in (params or {}).items()
                }
                response = self._http.post(url, data=data, files=dict(files))
            else:
                response = self._http.post(url, json=dict(params or {}))
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError) as error:
            raise TelegramUncertain(f"{method}: {type(error).__name__}") from None
        return self._result(method, response)

    def download(self, file_path: str, max_bytes: int) -> bytes:
        """A file from getFile; more than max_bytes raises ValueError (stops reading)."""
        url = f"{API}/file/bot{self._token}/{file_path}"
        data = bytearray()
        with self._http.stream("GET", url) as response:
            if response.status_code != 200:
                raise ValueError("The file could not be downloaded")
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise ValueError("The file is too large")
        return bytes(data)

    @staticmethod
    def _result(method: str, response: httpx.Response) -> Any:
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code == 200 and body.get("ok"):
            return body.get("result")
        description = str(body.get("description") or f"HTTP {response.status_code}")
        if response.status_code == 429:
            retry = body.get("parameters", {}).get("retry_after", 5)
            raise TelegramBusy(int(retry))
        if response.status_code >= 500:
            raise TelegramUncertain(f"{method}: {description}")
        raise TelegramRefused(response.status_code, description)
