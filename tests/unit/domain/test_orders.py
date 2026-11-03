from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from sales_platform.domain.orders import (
    FINAL_STATUSES,
    LOCAL_TRANSITIONS,
    Fulfilment,
    Order,
    OrderLine,
    OrderNotAllowed,
    OrderQuery,
    OrderStatus,
    check_local_move,
    clean_reason,
    erp_move_allowed,
    erp_status,
    external_reference,
    order_lines,
    order_number,
    order_status_command,
    stock_shortfall,
)
from sales_platform.ports.erp import ErpOrderState

NOW = datetime(2026, 10, 29, tzinfo=UTC)
ORDER_ID = UUID("1a2b3c4d-0000-4000-8000-000000000001")


def _line(sku: str, quantity: str) -> OrderLine:
    return OrderLine(0, sku, sku, Decimal(quantity), "each", Decimal("10"), Decimal("10"))


def _order(status: OrderStatus, fulfilment: Fulfilment = Fulfilment.LOCAL) -> Order:
    return Order(
        ORDER_ID, uuid4(), "O-1A2B3C4D", uuid4(), 1, "0" * 64, uuid4(), uuid4(), None, status,
        fulfilment, None, None, None, "INR", Decimal(10), Decimal(1), Decimal(11), (), "t", NOW,
        NOW,
    )  # fmt: skip


def test_numbers_and_references_come_from_the_order_id() -> None:
    assert order_number(ORDER_ID) == "O-1A2B3C4D"
    assert external_reference(ORDER_ID) == f"QO-{ORDER_ID.hex}"


def test_lines_are_copied_exactly_from_the_accepted_version() -> None:
    content = {
        "lines": [
            {
                "line_index": 0,
                "sku": "PUMP-100",
                "description": "Pump",
                "quantity": "2.500",
                "unit": "each",
                "unit_price": "4250.00",
                "line_total": "10625.00",
            }
        ]
    }
    [line] = order_lines(content)
    assert (str(line.quantity), str(line.unit_price), str(line.line_total)) == (
        "2.500",
        "4250.00",
        "10625.00",
    )
    with pytest.raises(OrderNotAllowed):
        order_lines({"lines": []})


def test_shortfall_counts_known_stock_only_and_adds_repeated_skus() -> None:
    lines = [_line("A", "3"), _line("A", "2"), _line("B", "1"), _line("C", "9")]
    available = {"A": Decimal("4"), "B": Decimal("1"), "C": None}
    assert stock_shortfall(lines, available) == ("A",)  # C unknown, D absent: not short
    assert stock_shortfall([_line("D", "1")], available) == ()


def test_local_moves_follow_the_table_and_end_at_final_statuses() -> None:
    check_local_move(_order(OrderStatus.PLACED), OrderStatus.CONFIRMED)
    check_local_move(_order(OrderStatus.CONFIRMED), OrderStatus.CANCELLED)
    with pytest.raises(OrderNotAllowed):
        check_local_move(_order(OrderStatus.SHIPPED), OrderStatus.CANCELLED)  # Q7: before ship
    with pytest.raises(OrderNotAllowed):
        check_local_move(_order(OrderStatus.PLACED), OrderStatus.DELIVERED)
    for status in FINAL_STATUSES:
        assert LOCAL_TRANSITIONS[status] == frozenset()
    assert set(LOCAL_TRANSITIONS) == set(OrderStatus)


def test_erp_orders_are_never_moved_by_staff() -> None:
    with pytest.raises(OrderNotAllowed, match="ERP"):
        check_local_move(_order(OrderStatus.PLACED, Fulfilment.ERP), OrderStatus.CONFIRMED)


def test_erp_states_map_to_our_statuses_and_final_ones_never_reopen() -> None:
    assert {erp_status(state.value) for state in ErpOrderState} <= set(OrderStatus)
    assert erp_status("draft") is OrderStatus.PLACED
    assert erp_move_allowed(OrderStatus.PLACED, OrderStatus.CONFIRMED)
    assert not erp_move_allowed(OrderStatus.CONFIRMED, OrderStatus.CONFIRMED)
    assert not erp_move_allowed(OrderStatus.CANCELLED, OrderStatus.CONFIRMED)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Where is my order?", OrderQuery()),
        ("  TRACK   my order ", OrderQuery()),
        ("orders", OrderQuery()),
        ("o-1a2b3c4d", OrderQuery("O-1A2B3C4D")),
        ("status O-1A2B3C4D", OrderQuery("O-1A2B3C4D")),
        ("I want to order 5 pumps", None),
        ("order", None),
        ("Q-1A2B3C4D", None),
        ("where is my order and also quote 3 fans", None),
    ],
)
def test_only_fixed_phrases_and_order_numbers_ask_for_orders(
    text: str, expected: OrderQuery | None
) -> None:
    assert order_status_command(text) == expected


def test_reasons_are_single_line_and_bounded() -> None:
    assert clean_reason("  out \n of stock ", required=True) == "out of stock"
    assert clean_reason(None, required=False) is None
    with pytest.raises(ValueError):
        clean_reason(" ", required=True)
    with pytest.raises(ValueError):
        clean_reason("x" * 501, required=False)
