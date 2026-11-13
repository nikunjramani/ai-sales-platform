# Current handoff
Updated: 2026-11-03. Earlier phase-by-phase notes (M0-M6, phases 1-7, ADR 0025/0026 steps) are
in git history; ADRs and feature docs hold the decisions.

## Current state
- **Built and merged to main:** M0-M6 (intake, extraction, pricing, approval, PDF delivery,
  customer acceptance), phases 1-7 (accounts and onboarding, our own product list, customers and
  website chat, one-click providers with Telegram and approval by message, local orders), the
  friendly portal (ADR 0025) and the module restructure (ADR 0026, PRs #13-#18).
- **Branch `refactor/simplify` (not merged):** the backend review follow-up.
  - ERP integration removed ([ADR 0027](adr/0027-remove-erp-integration.md)): ERPNext, catalogue
    sync, the ERP order writer and the simulated ERP. Demo businesses get seeded demo products
    (`seed.DEMO_CATALOGUES`); orders are local only. The baseline migration was edited in place
    (unreleased; user: no new migrations). The worker only reads the product tables.
  - One app-level error handler (`platform/api/errors.py`) for denied permission (403), bad value
    (422) and database/storage outage (503); module routers map only their own errors.
  - Four store ports with one SQL implementation and no fake removed (`InvitationStore`,
    `ContactReader`, `ChatStore`, `ReplyStore`); the architecture test lets application code use
    its own module's persistence (user, 2026-11-03).
  - `catalogue/ports/erp.py` merged into `catalogue/ports/sources.py`.
  - Finished per-phase implementation plans and the ERPNext feature doc deleted (git history).
  - After pulling: `uv run python scripts/recreate_local_database.py` (baseline changed), then
    restart the API and the worker.
- **Working rules (AGENTS.md):** never push to `main`; branch and PR only, with the user's
  permission each time; ask before assuming (clarify-before-building); tests stay in `tests/`.
- The user's `.vscode/settings.json` and `.env` stay untracked.

## Known gaps (tracked)
- Dev login only; OIDC is needed before any non-development deployment.
- No approval deadlines, escalation, delegation or multi-level policy.
- PDFs use built-in fonts (Western European text only), no letterhead or terms.
- Enquiry exceptions cannot be edited or answered in the portal; clarification questions are not sent.
- Email, WhatsApp, SMS, Instagram and Messenger are not built (only Telegram, website chat and the API).
- No OCR, no malware scanner, local storage only, no dead-job view.
- Orders: no PO number, address, delivery date, reservation or payments.
- Windows only: `test_concurrent_duplicates_create_exactly_one_enquiry` can fail when two uploads
  race in `LocalObjectStorage._path` (the resolved-path check); passes on rerun.

## Next task
- Review and merge `refactor/simplify` (with the user's permission).
- Choose the next milestone with the user (clarify-before-building). Ideas: follow-up messages
  ([plan/conversation-follow-ups.md](plan/conversation-follow-ups.md)), open decisions
  ([plan/open-decisions.md](plan/open-decisions.md)), whether an ERP is wanted (ADR 0027).
- Update this handoff in every change; never mark work complete without evidence.
