import { Link, useParams } from "react-router";

import { useCurrentTenant } from "@/features/tenant";
import { money } from "@/shared/lib/format";
import { QueryState } from "@/shared/ui/QueryState";

import { useOrder } from "../../hooks/useOrders";
import { OrderActions } from "../components/OrderActions";
import { OrderStatusBadge } from "../components/OrderStatusBadge";
import { OrderTimeline } from "../components/OrderTimeline";

/** One order: its lines (copied from the accepted version), actions and timeline. */
export function OrderDetailPage() {
  const { orderId = "" } = useParams();
  const query = useOrder(orderId);
  const { can } = useCurrentTenant();
  const order = query.data;
  return (
    <article>
      <Link className="link" to="/orders">
        ← All orders
      </Link>
      <QueryState isLoading={query.isLoading} error={query.error}>
        {order && (
          <>
            <header className="title-row">
              <h1>{order.order_number}</h1>
              <OrderStatusBadge status={order.status} />
            </header>
            <p className="muted">
              From <Link to={`/quotations/${order.quotation_id}`}>{order.quotation_number}</Link>{" "}
              version {order.version}
            </p>
            {order.hold_reason && <p className="notice">On hold: {order.hold_reason}</p>}
            <section className="card">
              <h2>Lines</h2>
              <table>
                <thead>
                  <tr>
                    <th>SKU</th>
                    <th>Description</th>
                    <th>Quantity</th>
                    <th>Unit price</th>
                    <th>Total</th>
                  </tr>
                </thead>
                <tbody>
                  {order.lines.map((line) => (
                    <tr key={line.line_index}>
                      <td>{line.sku}</td>
                      <td>{line.description}</td>
                      <td>
                        {line.quantity} {line.unit}
                      </td>
                      <td>{money(line.unit_price, order.currency)}</td>
                      <td>{money(line.line_total, order.currency)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p>
                Subtotal {money(order.subtotal, order.currency)} · Tax {money(order.tax, order.currency)} ·{" "}
                <strong>Total {money(order.total, order.currency)}</strong>
              </p>
            </section>
            {can("orders:manage") && <OrderActions order={order} />}
            <OrderTimeline events={order.events} />
          </>
        )}
      </QueryState>
    </article>
  );
}
