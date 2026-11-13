import { ClipboardCheck, Truck } from "lucide-react";
import { Link, useSearchParams } from "react-router";

import { useCurrentTenant } from "@/features/tenant";
import { dateTime, money } from "@/shared/lib/format";
import { FilterTabs } from "@/shared/ui/FilterTabs";
import { PageHeader } from "@/shared/ui/PageHeader";
import { QueryState } from "@/shared/ui/QueryState";

import { useOrderSettings, useOrders } from "../../hooks/useOrders";
import type { OrderListFilter, OrderStatus } from "../../model/types";
import { OrderStatusBadge } from "../components/OrderStatusBadge";

const TABS = [
  { id: "all", label: "All" },
  { id: "on_hold", label: "On hold" },
  { id: "placed", label: "Placed" },
  { id: "confirmed", label: "Confirmed" },
  { id: "shipped", label: "Shipped" },
  { id: "delivered", label: "Delivered" },
] as const;

function filterFor(tab: string): OrderListFilter {
  if (tab === "all") return {};
  return { status: tab as OrderStatus };
}

/** Every order, newest first (refreshes every 15 seconds). */
export function OrderListPage() {
  // The tab lives in the URL (?filter=on_hold) like the other lists, so Home can link to it.
  const [params, setParams] = useSearchParams();
  const tab = TABS.find((item) => item.id === params.get("filter"))?.id ?? "all";
  const orders = useOrders(filterFor(tab));
  const { can } = useCurrentTenant();
  return (
    <section>
      <PageHeader
        icon={Truck}
        title="Orders"
        hint="When a customer says yes to a quote, it becomes an order. Follow each order here until it is delivered."
      />
      {can("tenant:manage") && <CreationSetting />}
      <FilterTabs tabs={TABS} selected={tab} onSelect={(id) => setParams({ filter: id })} />
      <QueryState
        isLoading={orders.isLoading}
        error={orders.error}
        isEmpty={orders.data?.length === 0}
        emptyText="No orders here yet."
      >
        <ul className="list">
          {orders.data?.map((order) => (
            <li key={order.order_id}>
              <Link className="row" to={`/orders/${order.order_id}`}>
                <span>
                  <strong>{order.order_number}</strong>{" "}
                  <span className="muted small">from {order.quotation_number}</span>
                  <br />
                  {money(order.total, order.currency)}
                  <br />
                  <span className="muted small">{dateTime(order.created_at)}</span>
                </span>
                <OrderStatusBadge status={order.status} />
              </Link>
            </li>
          ))}
        </ul>
      </QueryState>
    </section>
  );
}

/** Q1: orders are created by staff (default) or automatically on acceptance. */
function CreationSetting() {
  const { query, save } = useOrderSettings();
  if (!query.data) return null;
  return (
    <label className="small setting">
      <ClipboardCheck size={18} aria-hidden="true" />
      <input
        type="checkbox"
        checked={query.data.order_creation === "automatic"}
        disabled={save.isPending}
        onChange={(event) => save.mutate(event.target.checked ? "automatic" : "staff")}
      />{" "}
      Create the order automatically when a customer accepts a quotation (otherwise staff press
      "Create order")
    </label>
  );
}
