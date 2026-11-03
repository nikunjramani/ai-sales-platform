// Orders (phase 7, ADR 0024): what an accepted quotation becomes. Decimals are strings.

export type OrderStatus =
  | "placed"
  | "confirmed"
  | "in_progress"
  | "shipped"
  | "delivered"
  | "cancelled"
  | "on_hold";

export interface OrderLine {
  line_index: number;
  sku: string;
  description: string;
  quantity: string;
  unit: string;
  unit_price: string;
  line_total: string;
}

export interface Order {
  order_id: string;
  order_number: string;
  quotation_id: string;
  quotation_number: string;
  version: number;
  status: OrderStatus;
  hold_reason: string | null;
  currency: string;
  subtotal: string;
  tax: string;
  total: string;
  lines: OrderLine[];
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface OrderEvent {
  event_id: string;
  type: string;
  from_status: OrderStatus | null;
  to_status: OrderStatus | null;
  source: "system" | "staff";
  actor: string;
  detail: Record<string, unknown>;
  occurred_at: string;
}

export interface OrderDetail extends Order {
  events: OrderEvent[];
}

export type OrderCreation = "staff" | "automatic";

export interface OrderSettings {
  order_creation: OrderCreation;
}

export interface OrderListFilter {
  status?: OrderStatus;
}
