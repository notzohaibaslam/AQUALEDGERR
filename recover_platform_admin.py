"""Local, physical-file-access recovery for the sole platform administrator."""
import getpass
import sys
import main


def run():
    with main.db() as c:
        admins=c.execute('SELECT id,username FROM platform_admins').fetchall()
    if len(admins)!=1:
        print('Expected exactly one master administrator. No changes made.');return 1
    admin=admins[0]
    print('LOCAL recovery utility. Only use on your own trusted server.')
    username=input('Enter your exact existing master Login ID: ').strip().lower()
    if username != admin['username']:
        print('Login ID did not match.');return 1
    password=getpass.getpass('New master password (minimum 12 characters): ')
    confirm=getpass.getpass('Repeat new password: ')
    if password != confirm or len(password)<12:
        print('Password mismatch or too short.');return 1
    with main.transaction() as c:
        c.execute('''UPDATE platform_admins SET password_hash=?,session_version=session_version+1 WHERE id=?''',
                  (main.hash_pw(password),admin['id']))
        main.platform_audit(c,{'id':admin['id']},'local_recovery')
    print('Master password changed. Existing platform sessions were revoked.')
    return 0


if __name__=='__main__':
    try:sys.exit(run())
    except (KeyboardInterrupt,EOFError):
        print('\nCanceled.');sys.exit(1)
