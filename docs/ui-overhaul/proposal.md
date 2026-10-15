# Runway UI/UX overhaul proposals
Date: 13 October 2026. Status: design review; application implementation has not started.

## Open the proposals

Open [index.html](index.html) in a browser. Both proposals work directly from disk; no npm installation or running backend is needed.

- [A — Streamlit refinement](current-stack.html)
- [B — React workspace](react-stack.html)
- [A desktop](current-desktop.png), [A mobile](current-mobile.png)
- [B desktop](react-desktop.png), [B mobile](react-mobile.png)
- [B approval review](react-approval.png)

These are interactive HTML/CSS/JavaScript design prototypes. A is a feasibility study for a Streamlit implementation. B demonstrates a proposed React interaction design; it is not a React implementation or a proof that the API migration is complete. Shared prototype code deliberately avoids installing either stack.

The forecast is copied from the recorded fictional demo in `docs/execution_logs/demo_run.md`, rather than queried from the live database. Approval states and event sequences are illustrative and local to the page. Refreshing resets them. Uploaded files are not read, emails are not sent, and no bank, database, model provider or application endpoint is called.

## Design brief

The primary users are a small supplier's Owner and external Accountant. Their main decision is whether a cash shortfall needs action, and whether the proposed action is justified. The design should make the consequence, amount, date, recipient, approval requirement and source evidence readable before the user acts.

The request is to overhaul the experience, use real production code as inspiration, consider a different frontend stack, and show both proposals before changing application code. Assumption: preserve all existing capabilities and security boundaries; do not add a new financial product or rewrite the finance engine.

Both proposals cover Today, Cash forecast, Approvals, Documents and Audit trail, with Company data, Payees, Demo bank and Test inbox grouped under management. They include sample approval/rejection, a cash scenario, source tracing, document flag inspection, and first-run/loading/error previews.

### Shared UX changes

1. Start at Today for an existing company; send an empty company to the import flow.
2. Bring the forecast, Monday brief and proposed actions together. The next action has a descriptive name rather than a generic tool label.
3. Display baseline, scenario preview and executed results as distinct states. A preview never silently changes a payment.
4. Put the exact amount, date and approvals beside the decision. Owner approval of financing remains pending for Accountant.
5. Show document flags where they block a payment. Clearing a flag, verifying a recipient and executing a payment remain separate operations.
6. Keep technical request payloads and tool traces expandable, with plain-language reasons shown first.
7. Separate demo clock/reset/test inbox from normal financial work. Show the simulated date and fictional company clearly.
8. Preserve important controls at narrow widths, including agent pause in the labelled Menu.

### Visual direction

A keeps Canary's dark slate character, but uses stronger text contrast and fewer decorative status effects. Its base tokens are canvas #101820, surface #18232e, text #f0f4f8, muted #adbdcc, action #245fba, and evidence accent #9bc8ff.

B uses a white working surface on blue-grey canvas, with a navy navigation frame. Its base tokens are canvas #f2f5f9, surface #ffffff, text #142b43, muted #516479, action/evidence #1857a1, and risk #a62b2b. The lighter surface is intended for sustained reading of ledger evidence; this is a proposed preference, not a framework limitation.

Both use Segoe UI/system fallback to match the Windows environment, avoid remote font dependencies and keep tabular amounts readable. Status always has a text label. Red means a cash gap or blocked action; amber means a human decision is waiting; green means a recorded or allowed state. There is no pulsing/glowing decoration. Motion is limited to direct interaction feedback. Design dials: ENERGY 2 / RHYTHM 2 / MOTION 1.

The framework does not dictate these palettes. A could be light and B could be dark.

## What the current code actually does

The installed Streamlit package is 1.65.0. `pyproject.toml` declares Streamlit, FastAPI, SQLModel, Pandas and other Python dependencies; there is no Runway JavaScript frontend.

| Evidence | Consequence for the redesign |
| --- | --- |
| `ui/app.py:45`, `sidebar():824`, `PAGES:787` | Nine peer sections and sidebar radio navigation. Group daily work and management; use page URLs. |
| `main():832`, `status_banner():793` | A full cash summary is rendered above every page. Use a full summary on Today, compact risk context elsewhere. |
| `page_chat():315`, `render_turn():305` | Brief and chat are separate from Forecast; tool trace starts expanded. Bring the brief beside the plan and collapse execution detail by default. |
| `page_forecast():379` | Source tracing and what-if logic already exist. Reorganise these capabilities rather than recalculate them in the browser. |
| `page_approvals():484`, `ui/render.py:describe_approval` | Human-readable request summaries already exist. Reuse them, with clearer state and action labels. |
| `page_documents():539`, `page_payees():611` | Document review and recipient verification are distinct. Preserve this separation. |
| `page_onboarding():202`, `reset_demo():108` | Import/reset replace company data and the audit log. Preserve explicit confirmation and describe the affected data. |
| `ui/auth.py` | Role, stable approver ID and lockouts are tied to Streamlit state. This is the main authentication coupling to extract for B. |
| `bank/app.py` | FastAPI serves the mock bank only. It does not expose forecasts, agent conversations, approval workflow, documents or audit views. |
| `ui/style.css`, `.streamlit/config.toml` | Already use Canary-inspired colours. The overhaul must improve task flow, not merely recolour. |

These are design findings from focused code reading, not a whole-repository security audit. Documentation was checked against the implementation where relevant: for example, onboarding already supports the optional paid-invoice and scheduled-payment history files, despite older architecture prose saying otherwise.

## A — Streamlit refinement

A sidebar groups five daily destinations and four management destinations. Today has an immediate shortfall explanation, a chart using the recorded forecast, the proposed action list, and a document warning. Review expands into an inline panel with exact terms and required approvals. This keeps the UI close to the existing Python execution model.

Implementation map:

- Use `st.Page` / `st.navigation` for grouped navigation and URL-addressable views.
- Reuse `build_forecast`, `explain_figure`, `Agent`, `describe_approval`, `executor.approve/reject`, `auth`, `safe_text`, `money` and `pack_for`.
- Compose Today from the current brief, chart and proposal data. Use native containers, columns, dataframes, forms and expanders for interactions.
- Use the theme configuration for supported colours and typography. Restrict custom CSS to intentional presentation rather than brittle selectors into Streamlit internals.
- Use native forms/dialogs for confirmations and native file upload/download widgets for imports and PDFs.
- Keep bank operations, auth/lockout behavior, source tracing and audit calls in Python.

Streamlit 1.65 also supports right/left modal drawers through `st.dialog(position="right")`; that capability was confirmed in the installed source. A can use a drawer if preferred. React is not necessary just to add that pattern. Native widget spacing, sidebar collapse and responsive behavior will differ from the HTML study; exact pixel parity is not promised.

Estimated effort: **3–5 focused engineering days** for the described reorganisation, all nine views, responsive review and regression validation. This assumes existing financial behavior is preserved. This is an engineering estimate, not a measured delivery guarantee.

Advantages: shortest route, existing auth and execution code remain integrated, no new application API or frontend build pipeline.
Limits: rerun/session model, less control over cross-view client state and responsive composition, and more constraints when making highly customised tables/interactions.

## B — React + TypeScript + Vite, with Python retained

B uses a compact top navigation frame, a light evidence workspace, and a contextual approval drawer. The approval queue stays visible behind review; the drawer becomes a full-width screen on a phone. Financial detail, request terms and waiting roles travel together.

**Node.js is a runtime, not the UI framework.** Here React is the UI library, TypeScript checks frontend code, Vite builds the frontend, and Node runs development/build tools. Vite produces static files; Node is not required as a production application server for this arrangement.

Proposed shape:

```text
Browser: React UI
    |
    | same-origin /api requests, authenticated server session
    v
Runway FastAPI application layer (new)
    |
    +-- existing forecast / policy / agent / audit / importer modules
    +-- server-held human and executor bank credentials
    +-- existing BankClient -> mock bank
    +-- existing SQLite database and PDF/outbox generation

Vite dist files -> served at the same origin by deployment/static hosting
```

The Python modules are already separated from the UI in many places. The hard work is exposing their application workflows safely and replacing Streamlit-specific session state, not porting the forecasting arithmetic.

### Required migration reach

| Area | Keep | Add or change |
| --- | --- | --- |
| Financial engine | Forecast, financing costs, integer cents, country packs | Typed read response models and display strings; no independent browser financial calculations |
| Agent | Orchestrator, policy checks, tools, pseudonymization | Server-side conversation/session lifecycle, busy/error states and brief/chat endpoints |
| Approvals | Exact stored arguments, policy recheck, independent approvers, idempotency | Authenticated list/detail/approve/reject API; server-derived role and identity; refresh status after ambiguous outcomes |
| Sign-in | PIN comparison and lockout behavior for demo parity | Streamlit-independent session adapter; secure server-issued session identity; browser never supplies authoritative roles/approver IDs |
| Documents | Safe ingestion, scanner, linkage, flag-clearing authorization | Multipart upload, list/detail/clear endpoints, bounded inputs and safe text responses |
| Imports | Importer validation and replacement confirmation | Preview/commit contract, four optional history inputs, and controlled reset flow |
| Payees / simulator | Existing human-only operations and audit attribution | Server-side authorised routes; never ship human bank tokens in frontend config |
| Outputs | Existing test email and PDF generation | Safe output-list/download routes by application ID, not arbitrary filesystem paths |
| Audit | Hash chain and verification code | Read/verify routes and contextual timeline view |
| Web state | Existing domain statuses | Router, query cache/invalidation and local view state; keep permissions enforced by the server |
| Delivery | Existing Python tests and scripts | Frontend build, API/session contract tests, browser interaction tests, deployment instructions |

Start with explicit application endpoints, not a generic browser-exposed tool executor. Read endpoints would cover company context, forecast/scenario/source explanation, proposals, documents, outputs and audit. Mutation endpoints would cover brief/chat, approvals/rejection, flag clearing, import/reset, verified human payee work and simulator actions.

Keep sessions same-origin with HttpOnly cookies and appropriate CSRF protection for mutations. Preserve lockout and sign-out semantics. A migrated demo still uses demo PINs; real SSO/MFA/user provisioning is a separate requirement. Do not copy Canary's browser token approach blindly.

For a first version, normal HTTP requests and query refresh are sufficient. Do not add streaming, WebSockets, a queue broker or a second Node backend unless a measured requirement needs them. Do not cache a completed approval as authoritative while server state may have changed.

Estimated effort: **12–20 focused engineering days** for full parity with the nine current views and demo authentication:

- application API/session extraction: 3–5 days;
- frontend workflows and visual implementation: 5–8 days;
- imports, outputs and management integration: 1–2 days;
- end-to-end/security regression and deployment: 3–5 days.

A Today/Forecast/Approvals slice is roughly 5–8 days, but is not a complete migration. Real identity/SSO, multi-company tenancy, hosting operations, penetration testing and a broader production hardening program are excluded from these estimates.

### Other choices

| Choice | Assessment |
| --- | --- |
| React + Vite + existing Python | Recommended migration path. Matches Canary's separation and avoids rewriting tested domain logic. |
| Next.js + existing Python | Viable, but adds server-rendering/server-function and deployment choices. This authenticated workspace has no established SEO or SSR requirement. |
| Full Node backend rewrite | High effort and risk, with little UI benefit. Would port database integration, forecast/cost rounding, policy, audit locking, imports, PDFs, bank integration and Python tests. Roughly 30–50+ engineering days before equivalent confidence, with low estimate confidence. |
| Keep Streamlit | Best fit for the competition timeline and a focused redesign. Can still deliver a substantial UX improvement. |

## Production code and Canary references

I studied these as concrete patterns, without copying a product wholesale or treating a framework as evidence of production readiness:

- Canary `RPISoftware/web/src/App.tsx`: shared authenticated shell, explicit routes and role-gated destinations. Transfer stable navigation and server-backed role separation.
- Canary `components/Topbar.tsx`, `components/StatusPill.tsx` and `styles.css`: persistent identity, semantic status labels and a small colour system. Transfer hierarchy and status meaning; avoid emergency flashing for a finance workspace.
- Canary `api/client.ts`, `api/hooks.ts` and `views/ManagerView.tsx`: separation of API access and views, mutations with explicit states, actionable summaries. Reuse the structure, not the safety-domain vocabulary.
- [shadcn/ui Sheet implementation](https://github.com/shadcn-ui/ui/blob/main/apps/v4/registry/new-york-v4/ui/sheet.tsx): typed composition over a dialog primitive, separate overlay/content/title/close pieces and an accessible close label. This informs B's contextual review, rather than a hand-built production focus trap.
- [shadcn/ui Data Table implementation guide](https://ui.shadcn.com/docs/components/base/data-table): columns, sorting/filtering/selection and data-specific composition. Use table behavior only where Runway needs it, rather than installing a dashboard kit.
- [Actual Budget's feature-error isolation work](https://github.com/actualbudget/actual/issues/7391): concrete production failure reports motivated view-local failure states and retry while preserving navigation. The linked issue was reviewed; Actual's component source could not be retrieved, so it is not represented as source code I inspected.
- [Streamlit navigation](https://docs.streamlit.io/develop/api-reference/navigation/st.navigation), [dialogs/drawers](https://docs.streamlit.io/develop/api-reference/execution-flow/st.dialog) and [theming](https://docs.streamlit.io/develop/concepts/configuration/theming): verified current native capabilities.
- [Node.js introduction](https://nodejs.org/learn/getting-started/introduction-to-nodejs) and [Vite production build](https://vite.dev/guide/build): runtime/tooling distinction and static build output. [Vite deployment](https://vite.dev/guide/static-deploy) explicitly separates local preview from production serving.

Canary code was read in place and not changed. No third-party code or dependencies were vendored into the application.

## Recommendation and next implementation boundary

Choose **A for the current competition submission**, given the README's 20 October deadline and the additional API work B requires. A should be a task-flow overhaul rather than another colour pass.

Choose **B for the longer-term product** if custom interaction, a maintained frontend component system and future UI expansion justify a separate API/frontend contract. Migrate in stages: establish session/approval contracts; ship read-only Today/Forecast; add approvals and agent work; then imports, documents and management. Keep Streamlit available until parity is validated, with cutover controlled per session to avoid competing workflows.

Before implementation, select a proposal and review its visual direction. The design files do not authorise a backend rewrite or remove any existing safeguard. No application source, dependency files, settings or database contents were changed for this deliverable.

## Acceptance for the eventual implementation

- All nine existing capabilities remain reachable; current role restrictions and lockout behavior remain.
- Forecast figures trace to source rows; scenario previews cannot mutate payments.
- An Owner cannot supply both roles on a two-role approval. Role switching does not reset stable approver identity.
- Double-click, timeout/retry and concurrent approvals cannot execute twice; policy is checked again against current state.
- A flagged document still blocks all linked payments; clearing and recipient verification remain separate.
- Agent pause is available on phone and desktop, is audited, and makes higher-tier actions fail closed.
- Import/reset explicitly confirm data and audit-log replacement. Downloads cannot expose arbitrary server files.
- Model/document text stays inert and cannot introduce links, images or tracking pixels.
- Page loading, first run, empty queue, permission denied, failure and lost-response states are clear.
- Run the full Python suite, relevant API/security tests and browser workflow checks before claiming implementation complete. Headless/emulated checks do not replace a short owner/accountant walkthrough on actual devices.

See [validation.md](validation.md) for the checks performed on these prototypes.
