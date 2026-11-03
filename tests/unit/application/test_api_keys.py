from typing import Any
from uuid import UUID

import pytest

from sales_platform.application.api_keys import (
    ApiKeyAuthenticator,
    AuthenticationFailed,
    generate_api_key,
    parse_token,
)
from sales_platform.domain.tenancy import Role
from sales_platform.ports.api_keys import ApiKeyRecord


def register(reader: Any, tenant: UUID, *, revoked: bool = False) -> str:
    key = generate_api_key()
    reader.records[key.key_id] = ApiKeyRecord(
        key.key_id, tenant, Role.INTAKE_SERVICE, key.secret_sha256, revoked
    )
    return key.token


def test_valid_key_yields_tenant_context(api_key_reader: Any, industrial_tenant: UUID) -> None:
    token = register(api_key_reader, industrial_tenant)
    context = ApiKeyAuthenticator(api_key_reader).authenticate(token)
    assert context.tenant_id == industrial_tenant
    assert context.roles == {Role.INTAKE_SERVICE}
    assert context.subject.startswith("api-key:")


def test_token_format_and_secret_not_stored(api_key_reader: Any) -> None:
    key = generate_api_key()
    key_id, secret = parse_token(key.token)
    assert key_id == key.key_id
    assert secret not in key.secret_sha256 and len(key.secret_sha256) == 64


@pytest.mark.parametrize("mutate", ["wrong_secret", "revoked", "unknown", "malformed"])
def test_every_failure_looks_the_same(
    api_key_reader: Any, industrial_tenant: UUID, mutate: str
) -> None:
    token = register(api_key_reader, industrial_tenant, revoked=mutate == "revoked")
    if mutate == "wrong_secret":
        token = token[:-4] + "AAAA"
    elif mutate == "unknown":
        api_key_reader.records.clear()
    elif mutate == "malformed":
        token = "Bearer something-else"
    with pytest.raises(AuthenticationFailed, match="^Invalid credentials$"):
        ApiKeyAuthenticator(api_key_reader).authenticate(token)
