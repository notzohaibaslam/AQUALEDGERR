# AquaLedger Pro v4.1 — Flexible Payments and Bottle Returns

## What changed

- You can collect any valid, non-negative amount while recording a delivery, even when it is larger than the new invoice.
- New payments are recorded as **one cash receipt**. A `payment_allocations` ledger connects receipts to individual invoices without duplicating cash received.
- When cash is collected against an invoice, the selected invoice is paid first, then older unpaid invoices (by delivery date). Any remaining value stays on the customer's overall ledger as payment credit. Opening balances are handled by the overall account ledger.
- Existing advance receipts are applied automatically to future delivery invoices; this does **not** create a second payment receipt.
- The Invoice Center now permits collecting an additional payment even if a specific invoice is already marked as paid.
- New **Empty bottle pickup** action records customers returning bottles without a new water delivery, and updates plant empty stock and their bottle balance.
- When a customer returns more empty bottles than were previously attributed to them, the software records an auditable historical opening-bottle correction instead of rejecting the physical return. Operators should confirm such corrections are accurate.
- Scheduled orders can be fulfilled for a different actual quantity. The original requested quantity remains in the order record; the actual delivered quantity appears on the delivery invoice.
- PDF and WhatsApp invoice views distinguish **cash received during delivery** from **amount allocated to this invoice**.
- Admin-only provisioning, tenant separation, password requirements and user/driver permissions from v4 remain intact.

## Example

A customer orders 5 bottles at Rs 120 each (invoice Rs 600), but pays Rs 3,500. Recording the delivery succeeds:

- Rs 600 applies to that invoice.
- Any existing older unpaid invoices are settled using the rest.
- Any remaining overpayment contributes to the customer's advance credit or settles historical opening dues.
- Total cash collected is Rs 3,500 **once**, regardless of how many invoices receive an allocation.

## Existing v4 database upgrades

**Do not delete v4 and do not put the v4.1 ZIP over your existing installation.**

1. Close the old command window; stop the old server first.
2. Back up your old `AquaLedger_Pro_v4/data` directory, including `.session_secret` and database files. Run `backup_windows.bat` and copy the backup ZIP outside the application folder.
3. Extract v4.1 into a separate directory (`AquaLedger_Pro_v4_1`).
4. Copy your old entire **data** folder into the new `AquaLedger_Pro_v4_1/data` folder. Do not copy `.venv`.
5. Double-click `start_windows.bat` from the new folder.
6. The app automatically creates any missing v4.1 database tables, takes a pre-migration snapshot if old payment records exist, and runs its one-time allocation conversion.
7. Verify your master admin at `http://127.0.0.1:8000/admin` and your existing plant owners at `http://127.0.0.1:8000/`.
8. Compare customer balances, invoice paid statuses, bottle counts, and payment totals against the old application.
9. Keep your original v4 directory untouched as a rollback copy.

**Important:** Never run v4 and v4.1 simultaneously against one live database. If you roll back, use the untouched pre-upgrade database/backup rather than pointing the old version at a modified v4.1 database.

## Integrity protections that remain

- You cannot dispatch more full bottles than exist in plant stock or produce more full bottles than the recorded empty stock.
- Discounts cannot make an invoice negative.
- Negative cash receipts are not accepted; corrections and refunds require a proper future dedicated workflow.
- Only authorized users can access or change their own plant's records, and drivers remain restricted to authorized tasks.
- Recorded payment amounts must be valid finite numeric values. Practical upper bounds remain to prevent corrupt or abusive entries.

## Limitations

This is a locally testable SaaS **prototype**, not a live internet-hosted service. Credit cannot currently be refunded automatically, disputed payment reversals need a dedicated workflow, and the system is not audited for public hosting. Always review your payment allocation and customer ledgers before using them for business decisions. SQLite is currently configured for a single application instance. Full cloud deployment requires additional production security and backup work.
