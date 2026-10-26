# Validation — v5.0.0

Checks performed on 26 October 2026 in an isolated test environment:

- `python -m pytest -q`: **32 passed**. One upstream TestClient/httpx deprecation warning; no failing or skipped tests.
- JavaScript syntax checks: `node --check static/app.js` and `node --check static/admin.js`: passed.
- DOM integration against a running FastAPI instance using jsdom: 17 workspace views loaded without page errors; custom 5L product creation, customer-specific 12L price, delivery price lookup, live litres/total calculator, delivery save, customer ledger and size sales report passed.
- Python module compilation passed.
- Archive checked for data, secrets, caches and virtual environments before delivery.

Backend coverage includes existing tenant isolation, roles, accounts, invoice/payment allocation, advances and returns, plus new size-specific inventory, custom capacity validation, customer prices, immutable invoice snapshots, recurring size propagation, driver restrictions and legacy migration. The old test that depended on an unavailable absolute v2 checkout was replaced with a self-contained v2-style schema fixture.

Limitations: a full Chromium visual/layout run was attempted but the browser binary download failed in the execution environment. DOM integration does not validate CSS layout. Windows batch files and Docker Compose were inspected but not executed on Windows or a Docker host. No test ran against a live Oracle/Cloudflare deployment. These checks are not an independent security audit.
