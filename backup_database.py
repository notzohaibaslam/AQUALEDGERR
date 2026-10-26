"""Create a consistent, validated, portable AquaLedger Pro database backup.

Run `python backup_database.py` any time; SQLite's online backup works during use.
Keep backups OFF the computer as well. ZIP is not encrypted, so keep it private.
"""
import json
import os
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from main import DB_PATH, DATA, SECRET_FILE


def make_backup():
    if not DB_PATH.is_file():
        raise SystemExit('No database exists. Start AquaLedger first.')
    backup_dir=DATA/'backups'
    backup_dir.mkdir(exist_ok=True,parents=True)
    name='AquaLedger_Pro_backup_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'.zip'
    archive=backup_dir/name
    with tempfile.TemporaryDirectory(prefix='aqua_backup_') as d:
        snapshot=Path(d)/'aqualedger.sqlite3'
        with sqlite3.connect(DB_PATH) as source,sqlite3.connect(snapshot) as dest:
            source.backup(dest)
        with sqlite3.connect(snapshot) as conn:
            result=conn.execute('PRAGMA integrity_check').fetchone()[0]
            if result!='ok':
                raise RuntimeError('Backup integrity check failed: '+result)
            customers=conn.execute('SELECT COUNT(*) FROM customers').fetchone()[0]
        manifest={'format':'AquaLedger Pro SQLite backup','created_at':datetime.now().isoformat(),
                  'customers':customers,'integrity_check':result,
                  'notes':'Stop server before restoring a DB file; keep this ZIP private.'}
        with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
            z.write(snapshot,arcname='aqualedger.sqlite3')
            z.writestr('backup_info.json',json.dumps(manifest,indent=2))
            if SECRET_FILE.is_file():
                z.write(SECRET_FILE,arcname='.session_secret')
            elif os.getenv('AQUA_SECRET'):
                z.writestr('SECRET_NOTICE.txt',
                    'AQUA_SECRET was configured as an environment variable; retain that secret securely outside this ZIP.\n')
    print('Verified backup created: '+str(archive))
    print('Keep another copy on a different device or secured backup storage.')
    return archive

if __name__=='__main__':
    make_backup()
