import { type FormEvent, useState } from "react";

import { ErrorMessage } from "@/shared/ui/QueryState";

import { useOrderActions } from "../../hooks/useOrders";
import type { Order, OrderStatus } from "../../model/types";
import { moveLabel, needsReason, nextStatuses } from "../../services/orderService";

/** Moves staff may make (orders:manage). */
export function OrderActions({ order }: { order: Order }) {
  const { move } = useOrderActions(order.order_id);
  const [target, setTarget] = useState<OrderStatus | null>(null);
  const [reason, setReason] = useState("");
  const choices = nextStatuses(order);
  if (choices.length === 0) return null;
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!target) return;
    move.mutate(
      { status: target, reason: reason.trim() || null },
      {
        onSuccess: () => {
          setTarget(null);
          setReason("");
        },
      },
    );
  };
  return (
    <form className="card" onSubmit={submit}>
      <h2>Next step</h2>
      <div className="row-actions">
        {choices.map((choice) => (
          <button
            key={choice}
            type="button"
            className={choice === target ? "primary" : choice === "cancelled" ? "danger" : "secondary"}
            onClick={() => setTarget(choice)}
          >
            {moveLabel(order, choice)}
          </button>
        ))}
      </div>
      {target && (
        <>
          {needsReason(target) && (
            <label>
              Reason (kept in the timeline, not sent to the customer)
              <input
                value={reason}
                required
                maxLength={500}
                onChange={(event) => setReason(event.target.value)}
              />
            </label>
          )}
          <button className="primary" type="submit" disabled={move.isPending}>
            Confirm: {moveLabel(order, target)}
          </button>
        </>
      )}
      <ErrorMessage message={move.error?.message} />
    </form>
  );
}
