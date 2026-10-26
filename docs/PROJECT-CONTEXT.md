# Project context and accepted scope

Updated: 2026-10-26 (Asia/Karachi). Repository: https://github.com/hanzlahsfc/Appointment-booking.git

This document preserves the decisions from the owner/orchestrator conversation. Read it before continuing implementation. It supersedes conflicting older plans, but is not a claim that unverified features work.

## Product and user journey

Build an assisted Greece visa appointment booking tool for applicants in Pakistan, initially National D employment/seasonal categories and Islamabad/Lahore. Confirm actual portal category names; old labels such as Premiere Launch must not be treated as official visa categories.

The operator starts the application on their Windows laptop with one command. Clients install nothing and visit a Vercel dashboard. Each enters an operator-issued random access code (no username/password registration), sees only their assigned online host/session, and opens their own isolated remote browser. The client signs into the visa portal and solves CAPTCHA manually. They set matching preferences and start monitoring. The backend attempts reservation when a matching slot appears, parks the browser, and signals the client dashboard. The client completes the form and final submission manually. Confirmation must be verified and recorded as booked to prevent another reservation.

The ALARM MUST SOUND ON THE CLIENT DASHBOARD, not the server laptop. Provide Enable Sound, Test Sound, acknowledgement, persistent visual alert, and restoration of unresolved alerts after reconnect. Browser background-tab suspension means sound cannot be guaranteed; do not claim otherwise.

## Accepted architecture

- Vercel: client-facing dashboard.
- Operator laptop: persistent Python/FastAPI backend, browser processing and monitoring, databases/session state.
- Authenticated HTTPS/WSS connection through a secure tunnel, planned Cloudflare Tunnel.
- A separate private workspace, browser profile, data and access permissions for every client.
- Never attach to the operator's personal Chrome or expose their Google account, passwords, tabs, desktop, files, shell, or raw CDP endpoint.
- Remote browser input is constrained and authenticated. Manual control and automation must be coordinated so they cannot race.
- Operator provisions/revokes clients by CLI for now.
- Admin dashboard, server GUI, VPS migration, and residential proxy deployment are FUTURE scope.
- Keep configuration/storage portable for later VPS migration; current host is a ThinkPad X13, 10th-gen i5, 16 GB RAM. Capacity is unmeasured: start with one client and test two-client isolation/load.
- The operator's machine must stay awake, online and running the app. No Devin or other coding agent is required during normal operation.

## Scope history: preserve alternatives

The original Python/Playwright monitor was hardened first. A standalone extension was then built on extension-only (47a6db2), with extension-standalone/, tests-ext/, and a ZIP artifact. That remains an alternative, NOT the active client architecture. Do not resume the extension-only rewrite or require a client extension for the hosted workflow.

Active branch at this handoff: hosted-client. Verified local tip before this documentation change: 8d6bb9a. Prior hosted commits include a94529a, aec9166, 1f41c2e, 4651737, 3256068. Earlier Python/runbook history includes f1e3b49, c7ce748, 3c2c2db, 2002d21, 223725a. Inspect Git rather than assuming every branch contains every fix.

## Reported implementation status

Devin/SWE2 is the sole implementation agent. Command Code/DeepSeek exhausted usage; its bridge work was subsequently incorporated. The owner relays prompts and reports. Codex acts as orchestrator/reviewer with limited usage. Preserve unfinished work and avoid attributing authorship without evidence.

Devin reports 144 Python tests pass, plus a successful end-to-end demo against tests/fake_portal: access code -> scoped session -> remote browser -> manual mock login -> detection -> assisted reserve -> client handoff -> manual completion -> verified booked status. These results are agent-reported, not independently rerun by this context-writing pass.

Reported implemented:
- 96-bit access codes stored as PBKDF2 hashes; code rotation revokes sessions; rate-limited code login exchanges for client-scoped Bearer sessions.
- Per-client persistent Chromium, CDP screencast and restricted input forwarding.
- Navigation guards and isolated records/streams; anonymous WebSocket rejection.
- Mock-portal assisted flow and restart-safe unresolved holds.
- Client-side Web Audio alarm and reconnect restoration.
- Confirmation-marker verified manual completion, including dashboard completion action.
- Single-flight monitoring and status/latency/effective-interval metrics.
- Static dashboard scaffold, cross-origin configuration, startup script and tunnel example.

A running demo is NOT real GVCW success. Earlier reports of alarm fired referred to host audio; the latest implementation reportedly moved it to client audio. Do not preserve demo passwords/codes or session tokens in documentation.

## Safety and performance requirements

Booking off by default, explicit per-client/profile opt-in, dry-run available. Do not submit the final application automatically in this release. No real reservation without explicit operator authorization for a supervised test.

Reserve calls can time out after succeeding. Treat ambiguous outcomes as potentially held, persist them, stop navigation/polling, and require human resolution. Unresolved holds and stale in-flight attempts must block navigation across restarts. Resolution must distinguish booked versus abandoned/verified not reserved. Verify recovery commands target the hosted client's actual database; do not blindly use legacy main.py commands.

Validate category/location and parsed dates. Unparseable rows or failed reads must not be classified as successful empty availability. Deduplicate missing-time slots, deactivate absent slots only on verified successful polls, and retry failed notifications.

The five-minute legacy interval is unsuitable for the intended release-window test. The owner asked for 1 ms checking; explain that this is not a viable request cadence and do NOT implement 1,000 requests/sec. Prefer reacting to portal updates, otherwise configurable single-flight availability polling without full navigation where feasible. Never overlap/queue polls; back off on throttling/challenges/errors. Measure check delay, response time, parsing, detection-to-reserve dispatch, and reservation response separately. Mock millisecond timings do not establish real portal performance. Real behavior is unverified.

## Real-portal evidence and important distinctions

No real GVCW reservation has been verified. Latest agent report: plain HTTP reachability returned an Incapsula shell. This does NOT independently prove that the isolated Chromium browser on the laptop is blocked. Test that browser manually first, with booking disabled. If also blocked, a working network is needed before authentic recon.

Official visa entry point: https://pk-gr.gvcworld.eu/en/schedule-appointment
Its booking button was followed during research to https://pk-gr-services.gvcworld.eu/login/appointments/en (403 to the research browser). The same domain also serves attestation, so do not infer the wrong workflow from the domain alone.

Official announcement: https://pk-gr.gvcworld.eu/en/news/appointments-for-the-month-of-november
It states November slots release gradually on 26 October 2026, 11:00-17:00 Pakistan time. The owner already knew this window. It is not a recurring release schedule. Weekly Monday-Friday/Thursday-Friday statements on official pages describe submission days, not slot release days.

GVCW's official scheduling page says it is intended for individuals/families rather than professional use and warns excess use can lead to slot cancellation. Clarify permitted operation before commercial rollout. Do not promise guaranteed appointments or bypass portal controls.

Research found SlotWing advertising Pakistan-to-Greece alerts and autofill, explicitly excluding booking/holding. JetVisa's broad country listings do not prove this exact route. No independently verified reliable automatic-reservation success rate was established. Competitor marketing is not proof.

## Remaining work, in priority order

1. Test actual isolated-browser reachability and manual login, booking disabled.
2. Observe authentic picker/availability flow and configure real selectors. Verify detected category/location/dates against the visible page; do not invent selectors or reserve semantics.
3. Verify manual-control locking, correct hosted recovery commands, and multi-client isolation against current code. Older HANDOFF limitations include clients added during serve needing restart and control checks only between steps; recheck rather than assuming fixed.
4. One supervised real claim when explicitly authorized and available: establish whether reserve holds a slot and for how long; verify client alarm and manual completion.
5. Deploy dashboard to Vercel and configure secure tunnel/backend origin/CORS/auth. Not deployed as of latest report.
6. Test from a separate device/network: code login, browser interaction, isolation, reconnect/offline state, alarm behavior, and host load.

Do not spend time on layout polish or future architecture before proving the booking path. Do not mark real integration complete based on mock tests. Keep development autonomous within scope; ask only for necessary credentials/access or consequential live actions.

## Repository navigation and startup

Read docs/HOSTED.md, docs/REAL-CLAIM-CHECKLIST.md, docs/LIVE-VALIDATION.md and HANDOFF.md alongside this document. Older documents contain stale commands/claims; verify against app/hosted/__main__.py.

Main areas: app/hosted/{auth,store,manager,monitor,server}.py; client-dashboard/; scripts/start_hosted.cmd; tests/test_hosted*; deploy/.

Host start: python -m app.hosted serve (or scripts\\start_hosted.cmd).
Client CLI: python -m app.hosted add-client --help; issue-code; list-clients.
Tests reported: python -m pytest tests -q.
Dashboard backend origin is configured by window.AB_API_BASE in client-dashboard/config.js. Keep secrets out of frontend configuration.

Never commit data/, browser profiles, cookies, access codes, applicant details, local databases, raw sensitive network captures, or .env. The extension ZIP currently exists as an untracked artifact; do not include it in this context commit.
