"""Create the first platform admin from a local terminal, never over a public endpoint."""
import getpass
import sqlite3
import sys
import main


def main_cli():
    with main.db() as c:
        existing=c.execute('SELECT COUNT(*) FROM platform_admins').fetchone()[0]
    if existing:
        print('A platform administrator already exists. This tool will not create another.')
        print('To reset a lost password, use the local recovery procedure in the README.')
        return 0
    print('AquaLedger Pro v5 | Set up YOUR master administrator account')
    print('This login is separate from all water-plant owner and staff accounts.\n')
    name=input('Your name: ').strip()
    username=main.clean_username(input('Master login ID: '))
    password=getpass.getpass('Master password (12+ characters recommended): ')
    confirm=getpass.getpass('Repeat password: ')
    if not 2<=len(name)<=80:
        print('Name must be 2–80 characters.');return 1
    if password != confirm:
        print('Passwords do not match.');return 1
    if len(password)<12:
        print('Use a strong master password of at least 12 characters.');return 1
    try:
        with main.transaction() as c:
            if c.execute('SELECT 1 FROM platform_admins LIMIT 1').fetchone():
                print('Already configured.');return 1
            c.execute('''INSERT INTO platform_admins(name,username,password_hash,created_at)
                         VALUES(?,?,?,?)''',(name,username,main.hash_pw(password),main.now()))
    except sqlite3.IntegrityError:
        print('Master login ID already in use.');return 1
    print('\nPlatform admin created. Start the app and visit http://127.0.0.1:8000/admin')
    return 0


if __name__=='__main__':
    try:sys.exit(main_cli())
    except (KeyboardInterrupt,EOFError):
        print('\nCanceled.');sys.exit(1)
