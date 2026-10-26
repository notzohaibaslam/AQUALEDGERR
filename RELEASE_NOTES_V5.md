# v5.0.0 — Bottle catalogue and size-aware operations

This release extends v4.1 with tenant-scoped products, separate stock, customer-specific rates, capacity snapshots on invoices, per-size circulation and sales reports. Default products: 12L, 14L and 19L. Custom capacities support decimal litres.

Database migration backs up the database before introducing product tables and tags historical operational records as 19L. Existing accounts and payment allocation logic remain intact. No live data or preconfigured passwords ship in the archive.

Additional corrections: standalone pickups reduce the aggregate customer circulation count; staff table columns align; Compose passes the allowed-host configuration; errors from upstream authentication no longer masquerade as a normal bad-password response.

See README.md for migration, pricing defaults, archive constraints and limitations.
