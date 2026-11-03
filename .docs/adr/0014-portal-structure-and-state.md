# 0014: Portal structure by feature and layer, with TanStack Query and Zustand
Date: 2026-10-23
Status: accepted; implemented (commit 21f0ba5, [CI run 39900219005](https://github.com/nikunjramani/ai-sales-platform/actions/runs/39900219005) portal job passed)

## Context
The first M4 portal kept UI, data fetching and rules together in a few large components, with hand-written routing and ad-hoc state. The user asked for:
- separate UI, logic, repository and service code;
- a feature/page-wise structure that makes new screens easy to add;
- proper state management.

The portal will grow to many screens: orders, delivery, settings and negotiation.

## Decision
- **Structure:** feature folders (`src/features/<name>/`) with layers `model`, `api` (repository), `services` (pure rules), `store`, `hooks`, `ui/pages`, `ui/components`, plus `routes.tsx` and a public `index.ts`. `src/shared/` holds feature-free code (the only HTTP client, helpers, generic UI). `src/app/` only wires providers, routes and layout. See [portal architecture](../development/portal-architecture.md).
- **Server state:** TanStack Query 5.104.1. It provides caching, loading and error states, invalidation after mutations, and no automatic retry of mutations or 4xx responses.
- **Client state:** Zustand 5.0.15. It holds state shared across screens; for now only the selected business, persisted in localStorage with an in-memory fallback.
- **Routing:** React Router 8.4.0 with hash URLs. The portal stays plain static files, both under the API's `/portal/` and on future static hosting.
- **Enforcement:** an automated architecture test checks layer imports, feature boundaries (index-only), that `fetch` appears only in `shared/api`, and that `src/` contains no test files. Tests mirror `src/` in `portal/tests/`.

## Alternatives
- **Redux Toolkit / RTK Query:** heavier, more boilerplate for this size.
- **Context only:** re-render and caching problems as screens grow.
- **Layer-first folders** (all components together, all hooks together): features get scattered.
- **Browser history URLs:** need server fallback routes.
- **ESLint boundary plugins:** an extra toolchain; the test does the same with no dependency.

## Consequences
- The bundle grew from 74 kB to 116 kB gzip, because of the router and query libraries.
- Every new screen follows the documented steps. Cross-feature use goes through public indexes.
- Server data must not be copied into local or Zustand state.

## Verification
`npm run typecheck`, `npm test` (27 tests, including architecture rules shown to reject a UI-to-repository import and a deep cross-feature import) and `npm run build`.
