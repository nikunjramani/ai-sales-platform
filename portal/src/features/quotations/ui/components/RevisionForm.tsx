import { ErrorMessage } from "@/shared/ui/QueryState";

import { useRevisionForm } from "../../hooks/useRevisionForm";
import type { QuotationDetail } from "../../model/types";

/**
 * A sales operator changes products or quantities; the server re-prices from the product list.
 * Prices cannot be typed in: they always come from the product list and the business's pricing policy.
 */
export function RevisionForm({ detail }: { detail: QuotationDetail }) {
  const form = useRevisionForm(detail);
  return (
    <form className="card" aria-label="Revision" onSubmit={form.submit}>
      <h2>Prepare a revision</h2>
      <p className="muted">Change products or quantities. Prices are recalculated from your product list.</p>
      {form.lines.map((line, index) => (
        <div className="line-edit" key={index}>
          <input
            aria-label={`SKU ${index + 1}`}
            placeholder="SKU"
            value={line.sku}
            onChange={(event) => form.updateLine(index, "sku", event.target.value)}
          />
          <input
            aria-label={`Quantity ${index + 1}`}
            inputMode="decimal"
            value={line.quantity}
            onChange={(event) => form.updateLine(index, "quantity", event.target.value)}
          />
          <input
            aria-label={`Unit ${index + 1}`}
            value={line.unit}
            onChange={(event) => form.updateLine(index, "unit", event.target.value)}
          />
          <button
            type="button"
            className="secondary"
            aria-label={`Remove line ${index + 1}`}
            onClick={() => form.removeLine(index)}
          >
            ×
          </button>
        </div>
      ))}
      <button type="button" className="secondary" onClick={form.addLine}>
        Add line
      </button>
      <label htmlFor="revision-reason">What changed and why</label>
      <textarea
        id="revision-reason"
        rows={2}
        maxLength={1000}
        value={form.reason}
        onChange={(event) => form.setReason(event.target.value)}
      />
      <ErrorMessage message={form.error} />
      {form.issues.length > 0 && (
        <ul className="issues">
          {form.issues.map((issue) => (
            <li key={`${issue.line_index}-${issue.code}`}>
              {issue.sku}: {issue.message}
            </li>
          ))}
        </ul>
      )}
      <button type="submit" className="primary" disabled={form.busy}>
        Submit revision for approval
      </button>
    </form>
  );
}
