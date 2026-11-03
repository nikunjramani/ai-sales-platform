"""The product list API on PostgreSQL: real sessions and CSRF, two businesses, paging,
permissions, read-only sources and the setup checklist step."""

from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from sales_platform.api.app import create_app
from sales_platform.api.auth import CSRF_HEADER
from sales_platform.config import Settings
from sales_platform.persistence.seed import INDUSTRIAL_TENANT, LOCAL_TENANT

pytestmark = pytest.mark.integration


@pytest.fixture
def app(integration_settings: Settings, runtime_engine: Engine) -> Iterator[TestClient]:
    with TestClient(create_app(integration_settings)) as client:
        yield client


def login(client: TestClient, subject: str) -> dict[str, str]:
    response = client.post("/api/v1/auth/dev-login", json={"subject": subject})
    assert response.status_code == 200, response.text
    return {CSRF_HEADER: response.json()["csrf_token"]}


def base(tenant: UUID = LOCAL_TENANT) -> str:
    return f"/api/v1/tenants/{tenant}/catalogue"


def test_sales_operator_manages_products_prices_and_stock(app: TestClient) -> None:
    headers = login(app, "sales@local.example")
    summary = app.get(f"{base()}/summary").json()
    assert summary["source"] == {
        "provider_key": "local_catalogue",
        "display_name": "Our product list",
        "editable": True,
    }
    assert (summary["active_products"], summary["priced_products"]) == (5, 4)
    assert summary["units"] == ["box", "each", "kg", "metre"] and summary["currency"] == "INR"

    category = app.post(f"{base()}/categories", json={"name": "Valves"}, headers=headers)
    assert category.status_code == 201, category.text
    body = {
        "sku": "VALVE-1",
        "name": "Ball valve 1 inch",
        "unit": "each",
        "category_id": category.json()["category_id"],
    }
    assert app.post(f"{base()}/products", json=body).status_code == 403  # no CSRF header
    created = app.post(f"{base()}/products", json=body, headers=headers)
    assert created.status_code == 201, created.text
    product = created.json()
    assert (product["minimum_quantity"], product["current_price"], product["row_version"]) == (
        "1",
        None,
        1,
    )
    taken = app.post(f"{base()}/products", json={**body, "sku": "valve-1"}, headers=headers)
    assert taken.status_code == 409 and taken.json()["detail"]["code"] == "sku_taken"
    bad = app.post(
        f"{base()}/products", json={**body, "sku": "V2", "unit": "litre"}, headers=headers
    )
    assert bad.status_code == 422 and bad.json()["detail"]["field"] == "unit"

    path = f"{base()}/products/{product['product_id']}"
    command = str(uuid4())
    price = {"unit_price": "1250", "command_id": command}
    first = app.post(f"{path}/prices", json=price, headers=headers)
    again = app.post(f"{path}/prices", json=price, headers=headers)
    assert (first.status_code, again.status_code) == (201, 200)
    assert first.json()["unit_price"] == "1250.00" and first.json()["revision"] == 1
    zero = app.post(
        f"{path}/prices", json={"unit_price": "0", "command_id": str(uuid4())}, headers=headers
    )
    assert zero.status_code == 422 and zero.json()["detail"]["field"] == "unit_price"
    stock = app.post(
        f"{path}/stock", json={"quantity": "40", "command_id": str(uuid4())}, headers=headers
    )
    assert stock.status_code == 201 and stock.json()["quantity"] == "40"

    detail = app.get(path).json()
    assert detail["current_price"]["unit_price"] == "1250.00"
    assert detail["stock"]["quantity"] == "40" and detail["stock"]["stale"] is False
    locked = app.patch(path, json={"expected_version": 1, "unit": "box"}, headers=headers)
    assert locked.status_code == 422 and locked.json()["detail"]["field"] == "unit"
    renamed = app.patch(
        path, json={"expected_version": 1, "name": "Ball valve 25 mm"}, headers=headers
    )
    assert renamed.status_code == 200 and renamed.json()["row_version"] == 2
    stale = app.patch(path, json={"expected_version": 1, "name": "x"}, headers=headers)
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "version_conflict"
    referenced = app.delete(path, headers=headers)
    assert (
        referenced.status_code == 409
        and referenced.json()["detail"]["code"] == "product_referenced"
    )
    archived = app.post(f"{path}/archive", json={"expected_version": 2}, headers=headers)
    assert archived.status_code == 200 and archived.json()["archived_at"] is not None
    history = app.get(f"{path}/prices").json()
    assert [entry["state"] for entry in history] == ["current"]
    changes = [c["action"] for c in app.get(f"{path}/changes").json()]
    assert changes == ["archived", "updated", "created"]


def test_paging_and_search(app: TestClient) -> None:
    login(app, "admin@local.example")
    first = app.get(f"{base()}/products", params={"limit": 2}).json()
    assert [p["sku"] for p in first["items"]] == ["IND-BOLT-M8", "IND-CABLE-2C"]
    second = app.get(f"{base()}/products", params={"limit": 2, "after": first["next_after"]}).json()
    assert [p["sku"] for p in second["items"]] == ["IND-FAN-450", "IND-FAN-600"]
    found = app.get(f"{base()}/products", params={"q": "zinc"}).json()["items"]
    assert [p["sku"] for p in found] == ["IND-BOLT-M8"]
    assert found[0]["current_price"]["unit_price"] == "2.375"


def test_other_businesses_and_read_only_sources(app: TestClient) -> None:
    headers = login(app, "admin@industrial.example")
    assert app.get(f"{base()}/summary").status_code == 404  # not a member of the local demo
    local_product = UUID(int=1)
    assert app.get(f"{base(INDUSTRIAL_TENANT)}/products/{local_product}").status_code == 404
    summary = app.get(f"{base(INDUSTRIAL_TENANT)}/summary").json()
    assert summary["source"]["editable"] is False
    refused = app.post(f"{base(INDUSTRIAL_TENANT)}/categories", json={"name": "X"}, headers=headers)
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "source_not_editable"
    viewer = login(app, "viewer@industrial.example")
    assert app.get(f"{base(INDUSTRIAL_TENANT)}/products").status_code == 200
    assert (
        app.post(
            f"{base(INDUSTRIAL_TENANT)}/categories", json={"name": "X"}, headers=viewer
        ).status_code
        == 403
    )


def test_checklist_product_step_follows_the_catalogue(app: TestClient) -> None:
    login(app, "admin@local.example")
    items = {i["key"]: i for i in app.get(f"/api/v1/tenants/{LOCAL_TENANT}/setup").json()["items"]}
    assert items["product_source"]["done"] is True and items["product_source"]["required"] is False
