# Quotation PDFs and delivery (M5)
**Status:** M5 is complete: verified locally (unit, PostgreSQL integration, migration round trips and a live run) and in [CI run 39903715921](https://github.com/nikunjramani/ai-sales-quotation-order-processing/actions/runs/39903715921). Email approval and email delivery were deferred by the user on 2026-10-23: managers approve in the portal ([ADR 0015](../adr/0015-quotation-documents-and-delivery.md)).

**Business purpose:** once a manager approves a quotation, the customer gets exactly that approved version as a PDF, and staff can see whether and when it reached them. Nothing unapproved ever reaches a customer.

## Terms in simple language
- **Approved document:** the PDF of one approved version. It is rendered once, stored privately and never changed. Its SHA-256 fingerprint is recorded, so anyone can check that a file is the one that was approved and sent.
- **Preview:** a PDF of any version for staff, marked "PREVIEW - NOT AN OFFER" on every page. It is never stored or sent.
- **Outbox / delivery:** a database row that says "send version N of this quotation to this customer through this channel", written together with the job that does it. Its status records what actually happened.
- **Pull channel / push channel:** with a pull channel the customer fetches the file (our API). With a push channel we send it (email or chat, later).
- **Outcome unknown:** a provider may have received the document even though we got no answer, e.g. after a timeout. We then do not send it again automatically, because the customer could get two copies; a person checks.

## Source files
- **Domain:** [domain/documents.py](../../src/sales_platform/quotations/domain/documents.py) holds statuses, `DocumentSource`, `delivery_recipient`, document IDs/keys and errors.
- **Ports:** [ports/documents.py](../../src/sales_platform/quotations/ports/documents.py) defines `QuotationRenderer`, `DeliveryStore` and `DocumentReader`. Since phase 1 a delivery is carried out by one outbox message (`deliver_quotation` prepares the PDF and queues it, `send_message` hands it to the enquiry's connection) and mirrors its status; see [connections](connections.md).
- **Application:**
  - [application/delivery.py](../../src/sales_platform/quotations/application/delivery.py) (`QuotationDeliveryHandler`, the `deliver_quotation` job)
  - [application/documents.py](../../src/sales_platform/quotations/application/documents.py) (`QuotationDocumentService`: previews, approved files, customer downloads)
- **Adapters:**
  - [adapters/documents/pdf.py](../../src/sales_platform/quotations/adapters/documents/pdf.py) (ReportLab renderer)
  - [adapters/providers/api/](../../src/sales_platform/providers/api/__init__.py) (API provider: pull)
  - [adapters/storage/local.py](../../src/sales_platform/platform/adapters/storage/local.py) (now also `documents/` keys)
- **Workflow:** the `approved` step in [workflows/quotation.py](../../src/sales_platform/quotations/workflows/quotation.py) queues delivery.
- **Persistence:** [persistence/documents.py](../../src/sales_platform/quotations/persistence/documents.py), [0006_documents.py](../../migrations/versions/0001_baseline.sql).
- **API:**
  - [api/portal.py](../../src/sales_platform/quotations/api/portal.py) (preview, approved PDF, delivery status)
  - [api/enquiries.py](../../src/sales_platform/customer_channels/api/enquiries.py) (customer view and PDF)
  - [api/pdf.py](../../src/sales_platform/quotations/api/pdf.py) (download headers)
- **Portal:** `DocumentPanel` and `DeliveryList` in `portal/src/features/quotations/ui/components/`, `deliveryService.ts` and `useQuotationDocuments.ts`.

## Flow
1. A manager approves (portal or a linked channel). The approval workflow resumes and reaches `approved`.
2. **approved step:**
   - Check that this version is the quotation's approved, current version.
   - Insert one `quotation_deliveries` row (status `pending`) for the enquiry's channel and sender, and queue `deliver_quotation`, in one transaction.
   - End the run. A repeat inserts nothing new.
3. **deliver_quotation job:**
   1. Finished already? Do nothing.
   2. Left in `sending` by a crashed worker? Mark it `unknown`.
   3. Get the document. Reuse the stored one, re-checking its hash; otherwise check again that an approve decision exists for exactly this version and content hash, render, store, and insert the `quotation_documents` row.
   4. Compare-and-set `pending` -> `sending`.
   5. Call the channel adapter, and record `available`/`sent`, `failed` (with reason), `unknown`, or back to `pending` for a retry.
4. **API channel:**
   - The delivery becomes `available`.
   - The client calls `GET /api/v1/enquiries/{id}/quotation` (JSON with the lines and totals as approved, plus a download path) and `GET .../quotation.pdf`.
   - The first download time is recorded and shown in the portal.

## API
| Route | Who | Notes |
|---|---|---|
| `GET /api/v1/tenants/{t}/quotations/{id}/versions/{v}/preview.pdf` | `quotations:view` | Any version, marked as preview; 404 unknown version; 422 if the content cannot be rendered |
| `GET /api/v1/tenants/{t}/quotations/{id}/document.pdf` | `quotations:view` | The stored approved PDF; 404 until rendered; 500 if the stored file fails its hash check (never served) |
| `GET /api/v1/tenants/{t}/quotations/{id}/delivery` | `quotations:view` | `{document, deliveries[]}` with status, recipient, errors, delivered and first-download times |
| `GET /api/v1/enquiries/{id}/quotation` | API key with `enquiries:view` | The issued quotation for that enquiry; 404 until delivered (also for other tenants and unknown IDs) |
| `GET /api/v1/enquiries/{id}/quotation.pdf` | API key with `enquiries:view` | The PDF; records the first download |

PDF responses are always downloads, with `Content-Disposition: attachment`, `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`.

## Tenant and auth boundary
- Both tables are under forced tenant RLS and every query filters by tenant. Other tenants get 404 everywhere.
- **Worker:** may insert documents and update only the delivery progress columns.
- **API:** may update only `first_downloaded_at`.
- `quotation_documents` is append-only, enforced by grants plus the `reject_change()` trigger, even for the owner.
- Customers see only delivered (approved) quotations, and never internal evidence (price list IDs, stock checks).

## Failures and recovery
| Situation | Result |
|---|---|
| Characters the PDF fonts cannot show (e.g. Devanagari, CJK, emoji) | `failed`: "... contains characters the PDF fonts cannot show". Nothing is replaced silently |
| Stored file missing or changed | `failed` (worker); 500 for downloads; never served |
| No adapter for the enquiry's channel | `failed`: "No delivery channel is configured for '...'" |
| Provider rejects | `failed` with the provider's reason |
| Timeout after sending, or a crash while sending | `unknown`; never resent automatically |
| Transient error | back to `pending`, retried with backoff; `failed` after the last attempt |
| Duplicate or replayed job | no effect (statuses are final; compare-and-set) |

## Configuration
No new settings. PDFs live under `SALES_STORAGE_DIR` (`tenants/<t>/documents/`).

## Tests and example
- **Unit:**
  - `tests/unit/domain/test_documents.py`
  - `tests/unit/adapters/documents/test_pdf.py`
  - `tests/unit/application/test_delivery.py`
  - `tests/unit/application/test_documents.py`
  - the API tests in `tests/unit/api/`
- **Integration:** the M5 section of `tests/integration/workflows/test_quotation.py`.
- **Portal:** `portal/tests/features/quotations/services/deliveryService.test.ts` and `.../ui/components/DocumentPanel.test.tsx`.
- **Try it:** see [setup](../development/setup.md#quotation-pdfs-and-delivery-m5).

## Known limitations
- Built-in PDF fonts cover Western European text only; a Unicode font is needed before real customer data.
- No resend or "mark as delivered" action for `failed`/`unknown` deliveries yet.
- No quote validity date on the PDF yet (M6 offer lifecycle).
- Only the API pull channel exists. Email and Telegram delivery come with those providers ([adding providers](../development/adding-providers.md)).
- Local disk storage only; production object storage is decided at deployment (M8).

Related ADRs: [0015](../adr/0015-quotation-documents-and-delivery.md), [0013](../adr/0013-portal-identity-and-approval-core.md), [0004](../adr/0004-durable-execution.md), [0007](../adr/0007-channel-independent-approval.md).
