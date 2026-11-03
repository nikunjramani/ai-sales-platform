import type { FormEvent } from "react";
import { Link } from "react-router";

import { useCurrentTenant } from "@/features/tenant";
import { money } from "@/shared/lib/format";
import { ErrorMessage } from "@/shared/ui/QueryState";

import { useCreateOrder, useQuotationOrder } from "../../hooks/useOrders";
import { OrderStatusBadge } from "./OrderStatusBadge";

/**
 * On an accepted quotation (exported for the quotations feature): its order, or "Create order"
 * for staff with orders:manage (also for quotations accepted before orders existed, Q14).
 */
export function QuotationOrderPanel({ quotationId, accepted }: { quotationId: string; accepted: boolean }) {
  const { can } = useCurrentTenant();
  const order = useQuotationOrder(quotationId);
  const create = useCreateOrder(quotationId);
  if (!can("orders:view") || order.isLoading) return null;
  if (order.data) {
    return (
      <section className="card">
        <h2>Order</h2>
        <p>
          <Link className="link" to={`/orders/${order.data.order_id}`}>
            {order.data.order_number}
          </Link>{" "}
          <OrderStatusBadge status={order.data.status} />{" "}
          <span className="muted small">{money(order.data.total, order.data.currency)}</span>
        </p>
      </section>
    );
  }
  if (!accepted || !can("orders:manage")) return null;
  const submit = (event: FormEvent) => {
    event.preventDefault();
    create.mutate();
  };
  return (
    <form className="card" onSubmit={submit}>
      <h2>Order</h2>
      <p className="muted">The customer accepted this quotation. No order exists yet.</p>
      <button className="primary" type="submit" disabled={create.isPending}>
        Create order
      </button>
      <ErrorMessage message={create.error?.message} />
    </form>
  );
}
