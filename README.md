# AquaLedger Pro v5 — Multi-size edition

A runnable Python/FastAPI + SQLite water-plant management system. Includes a separate platform administrator console and tenant-isolated owner, manager and driver workspaces.

## Start here — Windows

1. Install Python 3.11 or newer and select **Add Python to PATH**.
2. Extract this ZIP to a normal folder; do not launch files from inside the ZIP.
3. Double-click `start_windows.bat`. The first launch installs dependencies and asks you to create a master administrator. Password input is hidden in the terminal.
4. Open **http://127.0.0.1:8000/admin** and use that master login.
5. Create a water plant and its owner account in the administrator console.
6. Open **http://127.0.0.1:8000/** and sign in using the plant owner account.
7. Open **Bottle catalog** to set prices and add sizes. Use **Bottle inventory → Stock adjustment** to enter starting full/empty bottles for each size.
8. Add customers, then record orders, production and deliveries. Keep the terminal open while using the app.

Master credentials work at `/admin`. Owner and driver credentials work at `/`. They are intentionally separate. No preset passwords or live customer data are included.

## What is new

- **12L, 14L and 19L** products created for every plant.
- **Custom bottle sizes**, including decimal capacities such as 1.5L or 5.5L, with a label, default refill price and low-stock threshold.
- Separate full, empty, damaged and customer-held bottle counts for each product.
- Size selection on delivery orders, recurring plans, production, stock adjustments and empty-bottle pickups.
- **Customer-specific bottle prices:** Customers → Bottle prices → select a size → save the agreed rate. The delivery form suggests this price. It remains editable for that transaction.
- A live delivery calculator shows litres, invoice total and excess collection/unpaid amount before saving.
- PDF, print and WhatsApp invoice drafts show the delivered capacity; product name and capacity are captured on each delivery so later catalogue edits do not rewrite invoice history.
- Reports include invoiced sales, bottle count and litres by product for the selected dates.
- Product-specific low-stock alerts and a dedicated Bottle Catalog screen.
- Customer ledger shows bottle balances per size and identifies the size of each delivery/pickup.
- Existing flexible payments, automatic invoice allocation, advance credits, routes, staff access, maintenance, quality records, exports and administrator management remain available.
- Docker Compose now actually passes `AQUA_ALLOWED_HOSTS` from `.env` into the container.
- Clearer login diagnostics for nonstandard 401 responses from an upstream authentication layer.
- Corrected aggregate bottle circulation to subtract standalone pickups; fixed the team-table column alignment.

## Bottle/pricing rules

- Preset prices start at **Rs 0** because your business rates are unknown. Set your prices before recording deliveries.
- The customer directory's base price and initial bottle balance apply to the default **19L** product. A saved customer-specific rate overrides its base price. Other sizes use the product price unless a customer rate exists.
- Initial full/empty/damaged stock starts at zero. Enter real quantities for each size; do not use stock adjustments as sales.
- One bottle size per order/delivery/invoice. For a mixed-size customer visit, create separate delivery records. Mixed-line invoices are not implemented in this release.
- Returns refer to the selected size. If historical bottles were never entered, excess physical returns are explicitly reconciled into that size's opening balance and recorded in the audit trail.
- Capacity is immutable after creation. Add a different product for a different capacity. Prices, labels and low-stock thresholds can be edited.
- A product cannot be archived while stock, customer-held bottles, pending orders or active recurring plans remain. The default 19L product stays active for backwards compatibility.
- Recurring orders keep their bottle size; completing an order with a different size is rejected.
- Money is stored in integer paisa; litres are used for capacity/reporting, not currency calculations.

## Linux/macOS

Run `bash start_linux_mac.sh` from a terminal. The script creates a virtual environment, installs dependencies, guides initial administrator setup and starts the local server.

Manual alternative:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python create_platform_admin.py
.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

## Docker

```bash
cp .env.example .env
docker compose up -d --build
docker compose exec aqua python create_platform_admin.py
```

Open `http://127.0.0.1:8000/admin`. The database persists in the host's `data/` directory. The port binds only to localhost.

`.env` is consumed by Docker Compose. For a manual Python launch, export environment variables in your shell; the application does not load `.env` itself.

If using an existing configured HTTPS reverse proxy/tunnel, add its **exact hostname** to `AQUA_ALLOWED_HOSTS` in `.env`, for example:

```text
AQUA_ALLOWED_HOSTS=127.0.0.1,localhost,water.example.com
```

Then apply it with `docker compose up -d --force-recreate`. A domain is not required for local use. Buying a domain or deleting/recreating the database is not a remedy for an unexplained authentication failure.

This ZIP does not create an Oracle server, domain or tunnel. Local files cannot restore an already deleted server's database unless you kept a backup. An upstream Cloudflare authentication failure must be diagnosed in the tunnel/browser logs; this version does not bypass that protection or claim to fix a third-party session bug.

## Upgrade from v4.1

1. Stop the previous app/container.
2. Preserve a complete copy of the old `data/` folder, including `aqualedger.sqlite3` and `.session_secret`. Retain any environment-provided `AQUA_SECRET` separately if used.
3. Extract v5 into a separate folder. Copy the old `data/` folder into it and transfer only the needed configuration. Do not overwrite your only backup.
4. Start v5. It takes a SQLite snapshot into `data/backups/pre_v5_*.sqlite3` before the first size migration, adds new tables/columns, and assigns old stock and transactions to 19L.
5. Verify customer balances, stock, accounts and invoices before entering new transactions.

Migration is additive and rerunnable. For rollback, stop v5 and restore the complete pre-upgrade data copy with the old application. Do not simply run old code against a database that has received new multi-size transactions.

## Backup, exports and recovery

- `backup_database.py` / `backup_windows.bat` create a database backup; read the script's output for the destination.
- Preserve the session secret or environment-provided secret separately for full recovery.
- Settings → Download JSON exports plant records, including products, product prices and opening bottle balances. It intentionally excludes password hashes. It is not an automatic restore file.
- `recover_admin_windows.bat` or `python recover_platform_admin.py` resets the master account locally.
- Never commit `data/`, `.env`, credentials or database backups to GitHub. `.gitignore` and `.dockerignore` are included.

## Tests

From this project folder:

```bash
python -m pytest -q
```

See `VALIDATION.md` for the checks performed on this delivery. New tests cover stock separation, custom sizes/prices, recurrence, tenant permissions, invoice history and migration. No tests use your live server.

## Scope

This is a complete local/self-hosted project, not a managed hosting subscription. It retains SQLite and single-server operation; it does not provide offline sync, automatic WhatsApp sending, payment-gateway integration, mixed-line invoices, or automated restore. TDS/pH logs record measurements and do not certify water safety. Production deployment still requires access control, HTTPS and independent backups appropriate to your business.
