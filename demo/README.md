# Two-window demo
A simple page for demos, kept outside the main code on purpose (no portal structure, no tests).
Development only; it refuses to start when `SALES_ENVIRONMENT=production`.

- **Left, website chat (phase 4):** the demo business's real hosted chat page (`/chat/<handle>`)
  with the real widget: start a chat with a name and an email or phone, ask, answer our
  questions, and Accept or Decline the quotation card in the chat. Try "We need 5 boxes of
  2-core cable", then "10 metres". Build the widget first: `npm run build` in `portal/`.
- **Middle, customer system:** type an enquiry and press Send. A reply with the same reference
  continues the open enquiry (shown once, with every message). The reply shows what was understood, any
  clarification questions, and, once approved, the quotation with **Download PDF**, **Accept**
  and **Decline**.
- **Right, manager's phone:** the approval message (lines, totals, stock) with **Approve**,
  **Reject** and **Request revision**. Notices such as "the customer accepted" appear here too.

## Run
```powershell
docker compose up -d --wait
uv run python demo/run_demo.py      # migrations + seed, API (8000), worker, demo page (8001)
```
Open http://127.0.0.1:8001. Ctrl+C stops everything. The full portal still works for details.

**VS Code, one click:** Run and Debug -> **Demo (DB + API + worker + demo page)**. It stops old
processes, starts the database, migrates and seeds, then starts the three processes and opens the
page. The stop button ends all of them (the task "dev: stop all" also frees port 8001).

## How it works
`server.py` is a small FastAPI app that calls the real API:
- Customer: creates one intake API key for the demo business on first use and keeps it in
  `var/demo/state.json` (git-ignored); the browser never sees it.
- Website chat: enables the demo business's chat once (like the portal's Enable button) with
  this page (`http://127.0.0.1:8001`) as an allowed website, and keeps its address in the state
  file. `run_demo.py` starts the API with `SALES_PORTAL_DIST_DIR=portal/dist`, so it serves
  `/chat/widget.js`.
- Manager: links the account `demo-manager-phone` to `manager@industrial.example` (dev sign-in,
  linking code, signed enroll event), reads review messages from the simulated channel outbox
  and sends decisions as signed events.

**Clear windows** forgets the shown enquiries and messages (records stay in the database) and
creates a new key and link next time, e.g. after resetting the database.
"Request revision" needs a sales operator to change the quotation in the portal
(`sales@industrial.example`); the new version then arrives on the phone again.
