import { dateTime } from "@/shared/lib/format";

import type { OrderEvent } from "../../model/types";
import { eventLabel, STATUS_LABELS } from "../../services/orderService";

/** Every recorded step of the order, oldest first (append-only on the server). */
export function OrderTimeline({ events }: { events: OrderEvent[] }) {
  return (
    <section className="card">
      <h2>Timeline</h2>
      <ol className="history">
        {events.map((event) => (
          <li key={event.event_id}>
            <strong>{eventLabel(event.type)}</strong>
            {event.to_status && <> → {STATUS_LABELS[event.to_status]}</>}{" "}
            <span className="muted small">
              {dateTime(event.occurred_at)} · {event.actor}
            </span>
            {typeof event.detail.reason === "string" && (
              <div className="quote">{event.detail.reason}</div>
            )}
            {typeof event.detail.error === "string" && (
              <div className="muted small">{event.detail.error}</div>
            )}
          </li>
        ))}
      </ol>
    </section>
  );
}
