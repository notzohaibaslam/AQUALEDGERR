#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
if ! .venv/bin/python -c "import main,sys; c=main.db(); found=c.execute('SELECT 1 FROM platform_admins LIMIT 1').fetchone(); c.close(); sys.exit(0 if found else 2)"; then
  .venv/bin/python create_platform_admin.py
fi
printf 'Administrator: http://127.0.0.1:8000/admin\nPlant workspace: http://127.0.0.1:8000/\n'
.venv/bin/python run_local.py
