# Release and operations runbook
Status: planned checklist, not an executable deployment guide yet.

## Before pilot
1. Select hosting/region and record ownership, commercial policy sign-off, integration access and budget.
2. Pin dependencies; CI runs lint/types/tests, migration checks and secret/dependency checks.
3. Configure production identity, scoped credentials, HTTPS, storage, retention and tenant onboarding.
4. Test database restoration and private document recovery; agree recovery time/data-loss targets.
5. Run sandbox end-to-end, tenant isolation and fault-recovery suites.
6. Establish dashboards: intake failures, queue age, paused approvals, provider errors, unknown outcomes, order duplicates, workflow failures and per-tenant model cost.
7. Set measured latency/load targets and alert thresholds; assign an incident responder.
8. Confirm supported connectors and known limitations in release notes.

## Rollout
Create tagged build -> apply backward-compatible migrations -> deploy staging -> smoke test -> authorize production activation -> pilot tenant rollout -> monitor -> expand.
No public send/order traffic is enabled merely by installing a release.
Do not roll back schema destructively; use expand/contract migrations, compatible application rollback or documented forward repair.
Preserve old workflow compatibility until in-flight runs complete or are migrated.

## Operational scenarios
| Symptom | Operator action |
|---|---|
| Provider outage | Pause affected connector dispatch; preserve intents; retry after recovery |
| Unknown ERP write outcome | Search by stable reference; reconcile; do not create a second order (see [order needs attention](#order-needs-attention)) |
| Stuck approval | Check assignment, expiry, notification and durable resume job |
| Worker crash | Confirm lease recovery and operation reconciliation |
| Tenant leakage suspicion | Restrict affected access, preserve audit evidence, rotate impacted credentials, investigate scope |
| Bad model/prompt release | Disable affected model path, route to review, restore evaluated configuration |
| Database incident | Stop external dispatch, restore and reconcile remote effects since restore point before resuming |

At implementation, add actual commands, service names, dashboard links, backup schedules, escalation contacts, and tested restore results.
