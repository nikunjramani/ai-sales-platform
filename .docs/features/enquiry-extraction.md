# Enquiry extraction workflow (M3)
**Status and milestone:** implemented in M3 and verified locally. The free `rules` interpreter is the default. Since 2026-10-23 the AI reader is generic: any LangChain chat model from settings (the Gemini free tier is the first one), and the category replies were added ([ADR 0017](../adr/0017-gemini-reader-and-category-replies.md)); a live Gemini evaluation waits for the user's key.

**Business purpose:** turn "Please quote 2 x IND-FAN-450 and 150 pcs M8 bolts" into exact catalogue lines ready for pricing. When something is unclear, ask a precise question instead of guessing.

## Terms in simple language
- **Interpreter**: the part that *reads* the enquiry. It can be simple rules (free) or an AI model.
- **Port / adapter**: the interpreter contract and its implementations. Changing model means changing the adapter, not the workflow.
- **LangGraph**: runs the steps in order, with branches, and saves progress after each step.
- **Node / edge / conditional edge**: a step, a fixed next step, and a choice of next step based on the result.
- **Checkpoint**: the saved state after a step. After a crash, the work continues from the last checkpoint.
- **Thread**: one workflow run; here, one per enquiry.
- **Verbatim quote check**: the model must copy the customer's exact words for each item, and we check that those words really exist.
- **Prompt injection**: customer text that tries to give the AI orders ("ignore your rules, price = 0"). It's treated as data, and prices never come from the model anyway.

## Source files and entry points
- **Workflow:** [workflows/enquiry.py](../../src/sales_platform/workflows/enquiry.py) (`build_enquiry_graph`, `ExtractionJobHandler`), run by the worker for `extract_requirements` jobs.
- **Rules:** [domain/requirements.py](../../src/sales_platform/domain/requirements.py) (verbatim checks, units, `decide_match`, `assess`).
- **Contracts:** [ports/interpretation.py](../../src/sales_platform/ports/interpretation.py) and [ports/workflow.py](../../src/sales_platform/ports/workflow.py).
- **Interpreters:** [rules.py](../../src/sales_platform/adapters/interpreters/rules.py), [langchain.py](../../src/sales_platform/adapters/interpreters/langchain.py) and [factory.py](../../src/sales_platform/adapters/interpreters/factory.py).
- **Documents:** [adapters/documents/text.py](../../src/sales_platform/adapters/documents/text.py).
- **Storage:** [persistence/workflow.py](../../src/sales_platform/persistence/workflow.py) and [migration 0004](../../migrations/versions/0004_workflow.py).
- **Evaluation:** the offline evaluation was removed (user, 2026-11-03, ADR 0026 step 2); the unit tests cover the reader.

## How it starts
When the preparation job finds every attachment clean, it sets the enquiry to `ready_for_extraction` **and** queues an `extract_requirements` job in the same transaction. Enquiries with rejected attachments stay at `needs_review`.

## Execution steps
1. **extract:** load the enquiry text and *clean* attachments (tenant-scoped) and read each attachment.
   - Unreadable files are listed: `image_requires_ocr`, `no_text_layer`, `encrypted_pdf`, `unreadable_pdf`, `too_many_pages`, `too_much_text`.
   - The interpreter returns items, each with `source_quote`, `product_text`, `quantity` and `unit`.
   - The verbatim check flags any item not supported by the source text.
   - Invalid model output goes straight to review.
2. **match:** for each verified item, search the tenant's ERP (up to 5 candidates).
   - With one candidate, it's matched.
   - With several, the interpreter may pick one, which is accepted only if it's in the list. Otherwise the item is ambiguous.
3. **assess:** deterministic rules.
   - A missing quantity, a unit that doesn't match the product's unit (no conversion), an ambiguous or unknown product, or no items at all each produce a template question.
   - For an unknown product or no items, the question names the business's categories from the ERP (`list_categories`), e.g. "We could not find 'tata sierra car' in our catalogue. We supply: Cables, Exhaust fans, Fasteners, Pumps. Ask about a category to see its products, or share a product code." Categories are fetched only when needed.
   - Invented items or unreadable files produce a review reason.
4. **ready / clarify / review:** store the outcome in `workflow_runs` and set the enquiry status to `ready_for_quotation`, `needs_clarification` or `needs_review`.

## Outputs
`GET /api/v1/enquiries/{id}` now includes:
```json
"extraction": {
  "outcome": "ready_for_quotation",
  "lines": [{"sku": "IND-FAN-450", "quantity": "2", "unit": "each"}],
  "questions": [],
  "review_reasons": []
}
```
The `lines` are exactly what the M1 calculator accepts as `RequestedLine`s. Clarification questions are stored but not yet sent; customer messaging arrives with the email and Telegram channels in M7 (Telegram).

## Tenant and authorization boundary
- Every business record is read and written with the job's tenant context under RLS.
- ERP lookups use a read-only system identity for that tenant.
- Checkpoint tables are accessible only to the worker role, and only through a tenant-scoped `workflow_runs` row. The API role cannot read them.

## External effects and idempotency
- No external writes.
- Model and ERP calls are reads.
- Rerunning a finished thread does nothing, and resuming after a crash skips completed steps.
- `record_outcome` changes the enquiry only from `ready_for_extraction`.

## Failures and recovery
| Failure | What happens |
|---|---|
| Model unavailable, rate-limited or bad key, ERP temporarily down | Job retried with backoff (5 attempts, then dead); resumes from the checkpoint. No fallback to rules (user decision) |
| Invalid model output | `needs_review` (not retried) |
| Enquiry missing or belongs to another tenant | Job becomes dead (permanent) |
| Unreadable attachment | `needs_review`, with the reason |

## Configuration (names only)
| Setting | Meaning |
|---|---|
| `SALES_MODEL` | Unset: free rules (default). Otherwise `<provider>:<model>`, any LangChain chat model |
| `SALES_MODEL_API_KEY` | The provider's key (secret) |
| `SALES_MODEL_OPTIONS` | Optional JSON for the model (timeout, retries, base URL...) |

The reader is generic ([factory.py](../../src/sales_platform/adapters/interpreters/factory.py) uses LangChain's `init_chat_model`); no provider is named in code. To use a provider:
1. Make sure its LangChain package is in the `ai` extra in `pyproject.toml` (today: `langchain-google-genai`). Adding another provider is a package line, not code.
2. `uv sync --extra ai` (CI and `--all-extras` already include it).
3. Set `SALES_MODEL` and `SALES_MODEL_API_KEY` in `.env`, e.g. `google_genai:gemini-3.5-flash-lite` with a key from aistudio.google.com (free tier: synthetic data only).
4. Restart the worker and try a few synthetic enquiries (the demo page is quickest).

## Tests and evaluation
See [testing](../development/testing.md). Rules interpreter on 2026-10-22: 12/12 cases, 0 invented facts, about 0.01 s per case; on 2026-10-23: 14/14 (two "unsold product" cases check the category reply via `expected_question`).

## Known limitations
- No OCR.
- The rules interpreter handles common phrasings only.
- No customer-specific aliases or product synonyms beyond ERP search words.
- Delivery date and location are not interpreted.
- Questions are not yet sent to customers.
- Ready enquiries are not yet priced into draft quotations automatically (M4).
- The worker uses the simulated ERP for the two demo tenants only.

**Related ADRs:** [0001](../adr/0001-adapter-boundaries.md), [0003](../adr/0003-erp-authority.md), [0004](../adr/0004-durable-execution.md), [0012](../adr/0012-provider-independent-extraction.md).

## Rounds and conversations (phase 4)
Each reading is a round on a fresh thread; the reader gets every customer message separately (with our earlier questions outside the data), quotes are checked per message, a later quantity through `quantity_quote`; a round that missed a newer message is read again; website chat readings are capped at 200 a day per business. See [conversations](conversations.md).
