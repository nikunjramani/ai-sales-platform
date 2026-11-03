# 0012: Provider-independent enquiry extraction with LangGraph and verbatim checks
Date: 2026-10-22
Extended by [0021](0021-customers-conversations-and-website-chat.md): The interpreter port takes a transcript of every customer message (phase 4, ADR 0021).
Status: accepted and implemented in M3 (commit 52cccd7, [CI run 39747434193](https://github.com/nikunjramani/ai-sales-quotation-order-processing/actions/runs/39747434193) passed)

## Context
M3 turns enquiry text and attachments into structured requirements. The user wants a free model now and the freedom to switch to any provider later. Models can misread, invent facts or follow injected instructions, so their output must never set prices, stock or permissions.

## Decision
- **One contract for models:** the workflow depends only on the `EnquiryInterpreter` port, which has two methods: `extract` and `choose_product`.
  - `rules`: a free, offline, deterministic interpreter and the default.
  - `ollama`: a free local model through a LangChain adapter.
  - **Adding Claude, OpenAI or another provider:** add one LangChain chat model in [factory.py](../../src/sales_platform/enquiries/adapters/interpreters/factory.py) and one setting value.
- **Portable model output:** the LangChain adapter asks for JSON and validates it with strict Pydantic schemas (extra fields forbidden). This works with any chat model, with or without native tool calling.
  - Customer content is wrapped in tags declared as data, and attempts to close the tag are neutralised.
  - Invalid output becomes `InterpretationRejected` and goes to a person. Connection errors are retried.
- **Anti-invention checks (deterministic):** every item must carry a verbatim `source_quote` that exists in the customer's text and contains the product words and quantity. Items that fail send the enquiry to `needs_review` and are never quoted.
- **Product matching through the ERP:** the ERP's search supplies the candidates. The model may only choose among them, and an answer outside the list is ignored.
- **Fixed rules for the next step:**
  - **Units:** a fixed alias table, with no conversions. A bare count is accepted only for products sold per `each`.
  - **Outcome:** `ready_for_quotation`, `needs_clarification` (with template questions, never model-written) or `needs_review` (invented items, invalid output, or attachments that cannot be read).
- **Workflow:** LangGraph runs extract, match, assess, then one of ready, clarify or review.
  - Each step is checkpointed in PostgreSQL, so a crash resumes at the failed step without asking the model again.
  - State holds IDs and small summaries; document text is not checkpointed.
  - A `workflow_runs` table (with tenant RLS) maps each enquiry to its thread.
- **Checkpoint tables:** LangGraph's checkpoint schema is copied and frozen in migration 0004. Only the worker role may access these tables, which have no tenant column, and always through a tenant-scoped run row. A test fails if a library upgrade changes the schema.
- **Documents:** PDF text via pypdf, bounded to 20 pages and 50,000 characters. Images and PDFs without a text layer are reported as unsupported, which sends the enquiry to review. There is no OCR.
- **Evaluation:** evals/enquiry_extraction.jsonl (removed 2026-11-03, ADR 0026) is run through the real graph by `scripts/evaluate_extraction.py` for any provider. The rules interpreter must pass it in unit tests.

## Alternatives
Native tool-calling structured output (not portable to every model); letting the model match products freely or write customer questions; storing document text in checkpoints; running LangGraph's `setup()` at runtime; a paid model now; OCR now.

## Consequences
- Switching providers is a configuration and adapter change, measured with the same dataset.
- The rules interpreter is simple: unusual phrasings become clarification questions rather than errors.
- Small local models on CPU are slow. That's acceptable because the worker runs in the background.
- Requested delivery and location are kept as the customer's words, never as promises.
- Pricing is not triggered automatically; drafting quotations belongs to M4.

## Verification
Unit, workflow, evaluation and PostgreSQL integration tests are listed in [testing](../development/testing.md). The live run with uvicorn and the worker is recorded in the [handoff](../handoff.md). Ollama has not been evaluated live, because it isn't installed on this workstation.
