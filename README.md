# Appointment Booker

Hosted appointment monitoring and assisted reservation for Greece visa applicants in Pakistan.

Clients use an access-code dashboard and their own isolated portal browser. The operator runs the Python backend on a local computer. Clients complete portal login/CAPTCHA and final booking submission manually. Booking is disabled by default.

## Start locally on Windows

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
python scripts/run_local_monitor.py --issue-code
```

Open http://127.0.0.1:8081 and enter the private access code printed in the terminal. Subsequent starts can omit `--issue-code`; code rotation revokes existing dashboard sessions.

Real monitoring requires verified portal selectors. Portal reachability and actual reservation behavior remain unverified; simulated tests are not proof of real appointment success. The dashboard is not yet publicly deployed.

## Documentation

- [Operator and agent startup guide](docs/AGENT-STARTUP.md): commands, another website, selectors, troubleshooting, deployment.
- [Project context](docs/PROJECT-CONTEXT.md): accepted scope and decisions.
- [Current handoff](HANDOFF.md): implemented fixes and remaining blockers.
- [Hosted architecture](docs/HOSTED.md): browsers, authentication and client isolation.

## Repository layout

| Path | Purpose |
| --- | --- |
| `app/hosted/` | Hosted backend, client accounts, browser sessions and monitoring |
| `client-dashboard/` | Client dashboard; prepared for Vercel hosting |
| `app/` | Shared monitoring, booking, storage and portal drivers |
| `scripts/` | Local launch and mock demonstration tools |
| `tests/` | Python unit, socket and mock-portal integration tests |
| `deploy/` | Deployment examples; not an active public deployment |
| `extension-standalone/`, `tests-ext/` | Preserved extension-only alternative |
| `extension/`, `main.py`, `recon.py` | Original bridge/crawler/recon tooling |
| `docs/archive/` | Historical instructions; superseded where they conflict |
| `docs/images/` | Historical screenshots |

Client databases, browser profiles, credentials, recon captures, downloaded binaries and generated packages stay local and are ignored by Git. The preserved extension ZIP is under `artifacts/` on the operator machine.

## Verification

```powershell
python -m pytest tests -q
node --check client-dashboard/app.js
node --test tests-ext/test_core.js
```

Latest backend verification: 147 tests passed on 2026-10-26. Real GVCW booking has not been verified.
