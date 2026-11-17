import { describe, expect, it } from "vitest";

import type { Order } from "@/features/orders/model/types";
import { moveLabel, needsReason, nextStatuses } from "@/features/orders/services/orderService";

const ORDER: Order = {
  order_id: "o1",
  order_number: "O-1A2B3C4D",
  quotation_id: "q1",
  quotation_number: "Q-9F8E7D6C",
  version: 1,
  status: "placed",
  hold_reason: null,
  currency: "INR",
  subtotal: "8500.00",
  tax: "1530.00",
  total: "10030.00",
  lines: [],
  created_by: "sales@example.com",
  created_at: "2026-10-29T10:00:00Z",
  updated_at: "2026-10-29T10:00:00Z",
};

describe("orderService", () => {
  it("offers an order the server's next steps and nothing after delivery", () => {
    expect(nextStatuses(ORDER)).toEqual(["confirmed", "on_hold", "cancelled"]);
    expect(nextStatuses({ ...ORDER, status: "shipped" })).toEqual(["delivered"]);
    expect(nextStatuses({ ...ORDER, status: "delivered" })).toEqual([]);
    expect(moveLabel({ ...ORDER, status: "on_hold" }, "placed")).toBe("Release as Placed");
  });

  it("asks for a reason to cancel or hold", () => {
    expect(needsReason("cancelled")).toBe(true);
    expect(needsReason("on_hold")).toBe(true);
    expect(needsReason("confirmed")).toBe(false);
  });
});
