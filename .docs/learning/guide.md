# Learning guide
Learning must explain the real architecture; it must not weaken release requirements.

| Term | Plain explanation | Project use |
|---|---|---|
| Tenant | One business using the application | Keep different businesses separate |
| Adapter | Translator between our format and another system | Add an ERP or messaging channel |
| Approval service | One place that checks and records a manager's decision | Portal and chat use identical business rules |
| Identity binding | Verified link between a staff member and a messaging account | Recognize an authorized manager on Telegram |
| Counteroffer | Customer asks for different price or terms | Future negotiation creates a revised quotation |
| Port | Contract the translator must satisfy | All inventory adapters offer defined lookups |
| LangChain | Model/tool integration building blocks | Ask a model for validated enquiry fields |
| LangGraph | Controller for steps, branches and saved progress | Pause quotation processing for approval |
| State | Information carried through a workflow | Requirements, quote ID and pending action |
| Checkpoint | Saved workflow progress | Resume after restart |
| Idempotency | Repeating an operation has the same business effect | Retried acceptance does not create another order |
| Inbox | Durable record of incoming events | Detect replayed provider events |
| Outbox | Durable record of intended outgoing work | Send only after the business change commits |
| Lease | Temporary ownership of a job | Recover work if a worker dies |
| Reconciliation | Check what actually happened elsewhere | Find whether an ERP order succeeded after timeout |
| ADR | Written architecture decision and reasons | Let a future contributor understand the design |
| Source of truth | System whose value is authoritative | ERP supplies current stock and prices |
| RAG | Retrieve information before model reasoning | Possible future policy/manual lookup |
| Tenant context | Trusted note: this person, this business, these roles | Built only after checking stored membership |
| Role / permission | A job title and the actions it allows | A viewer can look, a sales operator can draft, an approver can approve |
| Row-level security (RLS) | Database filter that hides other businesses' rows | Even a buggy query cannot read another tenant |
| Decimal | Exact base-10 number; floats are approximate | Money never drifts by fractions of a paisa |
| Rounding mode | Tie-break rule for an exact half | half_up: 0.125 -> 0.13; half_even: 0.125 -> 0.12 |
| Protocol | Python's way to describe a port: methods an adapter must have | Simulated and real ERPs both satisfy ErpReader |
| Contract test | One suite every adapter must pass | A new ERP proves it omits unknown SKUs and respects tenants |
| Idempotency key | Caller-chosen ID meaning "this exact request" | A retried POST returns the first enquiry, not a second one |
| 202 Accepted | "Safely stored; work continues in the background" | Sent only after the database commit |
| Lease / fencing token | Temporary job ownership, and proof you still own it | A crashed worker's job is retried; its late result is ignored |
| SKIP LOCKED | Database option to skip rows another worker holds | Several workers never take the same job |
| Backoff with jitter | Wait longer after each failure, randomly within a limit | Retries do not overwhelm a recovering service |
| Quarantine | Holding area until a file is checked | Unsafe uploads never reach extraction |
| API key | A password for a system, tied to one business and role | The website form submits enquiries for one tenant only |
| Node / edge | A workflow step, and the arrow to the next step | extract -> match -> assess |
| Conditional edge | Choose the next step from the result | assess -> ready, clarify or review |
| Thread | One saved run of a workflow | One per enquiry, stored in workflow_runs |
| Verbatim check | The model must copy the customer's exact words; we verify them | Invented items are caught and sent to a person |
| Structured output | The model answers in a fixed JSON shape, checked strictly | Extra fields like a price make the answer invalid |
| Prompt injection | Customer text trying to give the AI orders | Treated as data; prices never come from the model |
| Evaluation dataset | Fixed test enquiries with expected answers | Compare free rules and any AI model fairly |
| Immutable version | A quotation snapshot that never changes; edits create a new one | The manager approves exactly version 2, not "the quote" |
| Content hash | A fingerprint of the content | A decision on an old page fails because the hash no longer matches |
| Interrupt / resume | A workflow pauses for a person and continues later from its checkpoint | Waiting days for approval survives worker restarts |
| Session cookie + CSRF token | A sign-in cookie scripts cannot read, plus a header only our page knows | Another website cannot approve on your behalf |
| Row lock | The database lets one transaction at a time change a row | Two managers clicking at once produce one decision |
| Channel binding | Proof that a messaging account belongs to a manager | A forwarded approval message does not work for someone else |
| Webhook signature | The provider signs each callback with a shared secret | A faked or altered event is rejected before reading it |
| Deterministic rendering | The same input always produces byte-identical output | A retried PDF job stores exactly the same file, with the same fingerprint |
| Compare-and-set | Change a value only if it still has the expected old value | Only one worker can move a delivery from pending to sending |
| Pull vs push delivery | The customer fetches the document, or we send it to them | API clients download their PDF; email (later) sends it |
| Unknown outcome | The provider may or may not have received it (e.g. a timeout) | Recorded as unknown and not resent, so the customer never gets duplicates |
| Offer validity | How long a quotation can be accepted | 7 days by default, printed on the PDF; afterwards it expires |
| Re-check at acceptance | Price again before saying yes | A changed price refuses the acceptance and a manager approves a new version |
| Delayed job | A job that may only run from a given time | The expiry job runs at "valid until" |

M1 trace: run `uv run python scripts/demo_quotation.py`. Every price, unit and stock number comes from the ERP; the calculator only does arithmetic and checks rules. No AI is involved yet.
M3 trace: submit "Please quote 50 exhaust fans" and "Please quote 2 x IND-FAN-450" through the API and run the worker - one asks which fan size, the other is ready for pricing.
M4 trace: follow [approvals and portal](../development/setup.md#approvals-and-portal-m4). Prices come from the calculator and the ERP; the person decides; the workflow only waits and continues.
M6 trace: follow [customer acceptance](../development/setup.md#customer-acceptance-m6). Accept as the customer's system, then try to accept again or after withdrawal, and watch the portal and the manager's simulated phone.
M5 trace: follow [quotation PDFs and delivery](../development/setup.md#quotation-pdfs-and-delivery-m5). Approve in the portal, run the worker, then fetch the PDF as the customer's system and compare its total and fingerprint with the portal.
M2 trace: follow [enquiry intake in the setup guide](../development/setup.md#enquiry-intake-m2) - submit twice with the same Idempotency-Key, run the worker, and watch the status and the files move from quarantine to clean.

Teaching sequence: business scenario -> term -> minimal example -> production implementation -> trace inputs/outputs -> test failure -> explain tradeoffs.
For each milestone, show an execution trace with synthetic data. Make clear which values AI interprets and which facts tools retrieve.
