import type { Order, OrderStatus } from "../model/types";

export const STATUS_LABELS: Record<OrderStatus, string> = {
  placed: "Placed",
  confirmed: "Confirmed",
  in_progress: "In progress",
  shipped: "Shipped",
  delivered: "Delivered",
  cancelled: "Cancelled",
  on_hold: "On hold",
};

// The same moves the server allows (domain/orders.py LOCAL_TRANSITIONS).
const LOCAL_MOVES: Record<OrderStatus, OrderStatus[]> = {
  placed: ["confirmed", "on_hold", "cancelled"],
  confirmed: ["in_progress", "shipped", "on_hold", "cancelled"],
  in_progress: ["shipped", "on_hold", "cancelled"],
  shipped: ["delivered"],
  on_hold: ["placed", "confirmed", "in_progress", "cancelled"],
  delivered: [],
  cancelled: [],
};

/** The next statuses staff may choose. */
export function nextStatuses(order: Order): OrderStatus[] {
  return LOCAL_MOVES[order.status];
}

/** Cancelling and holding need a reason (the server checks it too). */
export function needsReason(target: OrderStatus): boolean {
  return target === "cancelled" || target === "on_hold";
}

export function moveLabel(order: Order, target: OrderStatus): string {
  if (order.status === "on_hold" && target !== "cancelled") return `Release as ${STATUS_LABELS[target]}`;
  return `Mark ${STATUS_LABELS[target].toLowerCase()}`;
}

const EVENT_LABELS: Record<string, string> = {
  created: "Order created",
  status_changed: "Status changed",
  held: "Held: stock is short",
};

export function eventLabel(type: string): string {
  return EVENT_LABELS[type] ?? type;
}
