# 0015: Approved quotation PDFs and an honest delivery outbox; email approval deferred
Date: 2026-10-23
Status: accepted; implemented in M5 (commit 6f951ec, [CI run 39903715921](https://github.com/nikunjramani/ai-sales-platform/actions/runs/39903715921) passed)
Changes the M5 plan in the [roadmap](../plan/roadmap.md); builds on [0004](0004-durable-execution.md), [0007](0007-channel-independent-approval.md) and [0013](0013-portal-identity-and-approval-core.md)
Extended by [0018](0018-connector-framework.md) (phase 1): `DeliveryChannel` is replaced by the outbox; a delivery mirrors the status of the message that carries it (same honesty rules).

## Context
The original M5 planned PDFs, delivery tracking, a real email sandbox and manager approval by email reply. On 2026-10-23 the user decided:
- **Email approval is out of scope for now.** Managers approve in the portal, which already works.
- Any future email approval or messaging provider must be **easy to add**.
- PDFs use the **existing storage decision**: local disk behind the `ObjectStorage` port, with production object storage chosen at deployment (M8).

The only customer channel today is the intake API. Enquiries arrive from other systems with an API key.

## Decision
**Documents**
- Only an approved version gets a customer document. The `approved` workflow step queues delivery only if the version is still the quotation's approved, current version. Rendering checks again that an approve decision exists for exactly that version and content hash.
- **Renderer:** ReportLab 5.0.1 (BSD licence, pure Python, no system libraries on Windows or CI), behind the `QuotationRenderer` port.
  - **Deterministic:** invariant mode gives byte-identical output for the same version.
  - **Faithful:** amounts are printed as stored, with no recalculation.
  - **Escaped:** customer text is escaped before ReportLab's markup parser sees it.
  - **No guessing:** characters outside Windows-1252 raise `UnrenderableContent` instead of being dropped or replaced.
  - **Customer-facing:** no internal price list IDs or stock evidence.
- **Storage:**
  - The PDF is stored once under `tenants/<t>/documents/<id>`. The ID is derived from the version and the PDF hash, so a different render never overwrites a stored file.
  - It is recorded in the append-only `quotation_documents` table with its SHA-256, size, renderer version and source content hash.
  - Every read re-checks the hash; a mismatch is never served.
- **Previews:** staff can render any version on request. Each page is marked "PREVIEW - NOT AN OFFER". Previews are never stored or sent.

**Delivery outbox**
- `quotation_deliveries` has one row per (approved version, channel, recipient). It is written in the same transaction as its `deliver_quotation` job.
- **Recipient:** always whoever sent the enquiry, never an address found in message text.
  - API enquiries: the submitting API connection.
  - Push channels: the provider's verified sender address.
- **Statuses** say only what is known: `pending`, `sending`, `available` (pull), `sent` (push), `failed` (with reason), `unknown`.
  - `pending` -> `sending` is a compare-and-set, so only one worker hands a document over.
  - A crash while `sending`, or an uncertain provider response, becomes `unknown`. It is never resent automatically, so the customer never gets duplicates.
  - Problems that retrying cannot fix are recorded as `failed` with a short reason shown in the portal: no adapter for the channel, unrenderable content, a missing or changed file, or a rejection.
  - Transient errors return the delivery to `pending` for the job retry. The last attempt records `failed`.
- **API channel (pull):** delivery makes the document available. The client fetches `GET /api/v1/enquiries/{id}/quotation(.pdf)` with its API key. Drafts, pending and rejected versions are never visible there. The first download is recorded.

**Extension points (for email and others)**
- **Customer delivery:** one `DeliveryChannel` adapter per channel, registered in the worker.
- **Manager approval by email:**
  - an `ApprovalNotifier` adapter;
  - an inbound route that verifies the provider signature and the trusted sender evidence;
  - the existing `ChannelActionService` and `ApprovalService`.

  No application code changes. The steps are in [adding channels](../development/adding-channels.md).

## Alternatives
- **WeasyPrint (HTML to PDF):** needs GTK/Pango system libraries, which are hard to install on Windows and the corporate VM.
- **fpdf2:** LGPL, and fewer table features.
- **Storing PDF bytes in PostgreSQL:** bloats backups; object storage is the agreed pattern.
- **Rendering inside the approval transaction:** slow, and a render failure would block the decision.
- **Automatic resend after a timeout:** risks duplicate quotations reaching customers.
- **Replacing unsupported characters:** silently changes commercial documents.
- **Building email approval now:** deferred by the user. It needs a provider account and the email verification decision (open decisions).

## Consequences
- Migration `0006_documents`:
  - **Worker:** inserts documents and updates only the delivery progress columns.
  - **API:** reads both tables and may update only `first_downloaded_at`.
  - Downgrading leaves PDF files on disk, unreachable.
- Non-Western-European names or descriptions fail delivery visibly until a Unicode font is embedded (open decisions).
- `failed` and `unknown` deliveries have no resend action yet. A person must investigate (open decisions).
- Email manager approval and email delivery move to M7 with the other real providers. The direct-reply approval requirement still stands.

## Verification
- **Unit:** domain, renderer, delivery handler, document service and API routes, over in-memory fakes.
- **PostgreSQL integration:**
  - render, store and offer to the API client;
  - rejected quotations never rendered;
  - append-only documents and column-limited delivery grants;
  - tenant isolation.
- **Migrations:** the full suite twice around a downgrade to empty; a 0006 -> 0005 -> 0006 round trip with M5 data present.
- **Live run:** the real API and worker processes, with the PDF checked against the approved total.
