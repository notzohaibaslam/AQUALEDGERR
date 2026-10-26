"""AquaLedger Pro v5.0: platform-admin provisioned multi-tenant RO plant operations API.
Starter product. Production deployments require TLS, backups, monitoring and a security review.
"""
import base64
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import shutil
import time
from contextlib import contextmanager
from datetime import datetime, date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Depends, Header, Query, Response, Request
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor
from reportlab.lib.utils import simpleSplit
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field

BASE = Path(__file__).resolve().parent
DATA = Path(os.getenv('AQUA_DATA_DIR', BASE / 'data'))
DATA.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA / 'aqualedger.sqlite3'
SECRET_FILE = DATA / '.session_secret'
if os.getenv('AQUA_SECRET'):
    SECRET = os.getenv('AQUA_SECRET').encode()
else:
    if not SECRET_FILE.exists():
        fd = os.open(str(SECRET_FILE), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(secrets.token_hex(32))
    SECRET = SECRET_FILE.read_text().strip().encode()
TZ = ZoneInfo('Asia/Karachi')

def allocate_receipt(c, plant_id, customer_id, payment_id):
    '''Apply money received once; allocations never change cash or ledger totals.
    Target the invoice selected at collection, then oldest outstanding invoices.
    Unapplied remainder is customer advance credit.
    Must run in a BEGIN IMMEDIATE transaction.
    '''
    payment=c.execute('SELECT id,amount_paisa,delivery_id FROM payments WHERE id=? AND plant_id=? AND customer_id=?',
                      (payment_id,plant_id,customer_id)).fetchone()
    if not payment:raise ValueError('Missing payment')
    used=c.execute('SELECT COALESCE(SUM(amount_paisa),0) FROM payment_allocations WHERE payment_id=?',
                   (payment_id,)).fetchone()[0]
    left=payment['amount_paisa']-used
    if left<=0:return
    target=payment['delivery_id'] if payment['delivery_id'] is not None else -1
    invoices=c.execute('''SELECT d.id,d.total_paisa,
        COALESCE((SELECT SUM(a.amount_paisa) FROM payment_allocations a WHERE a.delivery_id=d.id),0) allocated
        FROM deliveries d WHERE d.plant_id=? AND d.customer_id=?
        ORDER BY CASE WHEN d.id=? THEN 0 ELSE 1 END, d.delivered_at, d.id''',
        (plant_id,customer_id,target)).fetchall()
    for invoice in invoices:
        due=invoice['total_paisa']-invoice['allocated']
        if due<=0:continue
        amount=min(left,due)
        c.execute('''INSERT INTO payment_allocations(payment_id,delivery_id,amount_paisa) VALUES(?,?,?)
            ON CONFLICT(payment_id,delivery_id) DO UPDATE SET amount_paisa=amount_paisa+excluded.amount_paisa''',
            (payment_id,invoice['id'],amount))
        left-=amount
        if left==0:break


def reconcile_customer(c, plant_id, customer_id):
    '''Use prior unallocated receipts on new invoices, without recording cash twice.'''
    receipts=c.execute('''SELECT p.id FROM payments p WHERE p.plant_id=? AND p.customer_id=?
        AND p.amount_paisa>(SELECT COALESCE(SUM(a.amount_paisa),0)
                             FROM payment_allocations a WHERE a.payment_id=p.id)
        ORDER BY p.id''',(plant_id,customer_id)).fetchall()
    for receipt in receipts:
        allocate_receipt(c,plant_id,customer_id,receipt['id'])


def migrate_payment_allocations():
    '''One-time, idempotent conversion of previous v4 payment attribution.'''
    with db() as c:
        c.execute('CREATE TABLE IF NOT EXISTS schema_migrations (key TEXT PRIMARY KEY)')
        migrated=c.execute("SELECT 1 FROM schema_migrations WHERE key='payment_allocation_v41'").fetchone()
        payment_count=c.execute('SELECT COUNT(*) FROM payments').fetchone()[0]
    if migrated:
        return
    if payment_count:
        # Take a consistent snapshot of the ORIGINAL ledger before migrating it.
        # The user's own full data-folder backup is still strongly recommended.
        backup_dir=DB_PATH.parent/'backups'
        backup_dir.mkdir(exist_ok=True,parents=True)
        timestamp=datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        target=backup_dir/f'pre_v41_payment_migration_{timestamp}.sqlite3'
        with sqlite3.connect(DB_PATH) as live, sqlite3.connect(target) as snap:
            live.backup(snap)
        with sqlite3.connect(target) as saved:
            if saved.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                raise RuntimeError('Pre-migration database backup failed integrity check')
    with transaction() as c:
        # Guard against re-running if another application process completed migration.
        if c.execute("SELECT 1 FROM schema_migrations WHERE key='payment_allocation_v41'").fetchone():
            return
        receipts=c.execute('SELECT id,plant_id,customer_id FROM payments ORDER BY id').fetchall()
        for r in receipts:
            allocate_receipt(c,r['plant_id'],r['customer_id'],r['id'])
        c.execute("INSERT INTO schema_migrations(key) VALUES('payment_allocation_v41')")


def today():
    return datetime.now(TZ).date().isoformat()

def now():
    return datetime.now(TZ).isoformat(timespec='seconds')

def money(value):
    try:
        d = Decimal(str(value))
        if not d.is_finite() or d < 0 or d > 100000000:
            raise ValueError()
        return int((d * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, TypeError):
        raise HTTPException(422, 'Invalid amount')

def rs(paisa):
    return round((paisa or 0) / 100, 2)

def db():
    conn = sqlite3.connect(str(DB_PATH), isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA busy_timeout=15000')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA wal_autocheckpoint=1000')
    return conn

@contextmanager
def transaction():
    conn = db()
    try:
        conn.execute('BEGIN IMMEDIATE')
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

SCHEMA = '''
CREATE TABLE IF NOT EXISTS plants (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT DEFAULT '', address TEXT DEFAULT '',
 full_stock INTEGER NOT NULL DEFAULT 0 CHECK(full_stock>=0),
 empty_stock INTEGER NOT NULL DEFAULT 0 CHECK(empty_stock>=0),
 damaged_stock INTEGER NOT NULL DEFAULT 0 CHECK(damaged_stock>=0), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY, plant_id INTEGER NOT NULL REFERENCES plants(id),
 name TEXT NOT NULL, username TEXT NOT NULL UNIQUE COLLATE NOCASE, email TEXT UNIQUE COLLATE NOCASE,
 password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('owner','manager','driver')),
 active INTEGER NOT NULL DEFAULT 1, session_version INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS customers (
 id INTEGER PRIMARY KEY, plant_id INTEGER NOT NULL REFERENCES plants(id), name TEXT NOT NULL,
 phone TEXT DEFAULT '', area TEXT DEFAULT '', address TEXT DEFAULT '',
 price_paisa INTEGER NOT NULL DEFAULT 0, opening_balance_paisa INTEGER NOT NULL DEFAULT 0,
 opening_bottles INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS routes (
 id INTEGER PRIMARY KEY, plant_id INTEGER NOT NULL REFERENCES plants(id),
 name TEXT NOT NULL, driver_id INTEGER REFERENCES users(id));
CREATE TABLE IF NOT EXISTS subscriptions (
 id INTEGER PRIMARY KEY, plant_id INTEGER NOT NULL REFERENCES plants(id),
 customer_id INTEGER NOT NULL REFERENCES customers(id), qty INTEGER NOT NULL CHECK(qty>0),
 weekdays TEXT NOT NULL, driver_id INTEGER REFERENCES users(id), route_id INTEGER REFERENCES routes(id),
 active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS orders (
 id INTEGER PRIMARY KEY, plant_id INTEGER NOT NULL REFERENCES plants(id),
 customer_id INTEGER NOT NULL REFERENCES customers(id), qty INTEGER NOT NULL CHECK(qty>0),
 requested_date TEXT NOT NULL, driver_id INTEGER REFERENCES users(id), route_id INTEGER REFERENCES routes(id),
 subscription_id INTEGER REFERENCES subscriptions(id), status TEXT NOT NULL DEFAULT 'pending',
 note TEXT DEFAULT '', delivery_id INTEGER, created_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS uq_subscription_date ON orders(plant_id,subscription_id,requested_date) WHERE subscription_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS deliveries (
 id INTEGER PRIMARY KEY, plant_id INTEGER NOT NULL REFERENCES plants(id),
 customer_id INTEGER NOT NULL REFERENCES customers(id), order_id INTEGER REFERENCES orders(id),
 qty INTEGER NOT NULL CHECK(qty>0), empties INTEGER NOT NULL CHECK(empties>=0),
 unit_price_paisa INTEGER NOT NULL, discount_paisa INTEGER NOT NULL DEFAULT 0,
 total_paisa INTEGER NOT NULL, driver_id INTEGER REFERENCES users(id), route_id INTEGER REFERENCES routes(id),
 delivered_at TEXT NOT NULL, notes TEXT DEFAULT '', created_by INTEGER REFERENCES users(id));
CREATE TABLE IF NOT EXISTS bottle_returns (
 id INTEGER PRIMARY KEY, plant_id INTEGER NOT NULL REFERENCES plants(id),
 customer_id INTEGER NOT NULL REFERENCES customers(id),
 qty INTEGER NOT NULL CHECK(qty>0), returned_at TEXT NOT NULL,
 notes TEXT DEFAULT '', created_by INTEGER REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_bottle_returns_customer ON bottle_returns(plant_id,customer_id,returned_at);
CREATE TABLE IF NOT EXISTS payments (
 id INTEGER PRIMARY KEY, plant_id INTEGER NOT NULL REFERENCES plants(id),
 customer_id INTEGER NOT NULL REFERENCES customers(id), delivery_id INTEGER REFERENCES deliveries(id),
 amount_paisa INTEGER NOT NULL CHECK(amount_paisa>0),
 method TEXT NOT NULL, paid_at TEXT NOT NULL, notes TEXT DEFAULT '', created_by INTEGER REFERENCES users(id));
CREATE TABLE IF NOT EXISTS production (
 id INTEGER PRIMARY KEY, plant_id INTEGER NOT NULL REFERENCES plants(id),
 qty INTEGER NOT NULL CHECK(qty>0), tds REAL, ph REAL, produced_at TEXT NOT NULL,
 notes TEXT DEFAULT '', created_by INTEGER REFERENCES users(id));
CREATE TABLE IF NOT EXISTS stock_movements (
 id INTEGER PRIMARY KEY, plant_id INTEGER NOT NULL REFERENCES plants(id),
 kind TEXT NOT NULL, qty INTEGER NOT NULL, reason TEXT NOT NULL,
 created_at TEXT NOT NULL, created_by INTEGER REFERENCES users(id));
CREATE TABLE IF NOT EXISTS expenses (
 id INTEGER PRIMARY KEY, plant_id INTEGER NOT NULL REFERENCES plants(id),
 category TEXT NOT NULL, amount_paisa INTEGER NOT NULL CHECK(amount_paisa>0),
 incurred_at TEXT NOT NULL, notes TEXT DEFAULT '', created_by INTEGER REFERENCES users(id));
CREATE TABLE IF NOT EXISTS quality (
 id INTEGER PRIMARY KEY, plant_id INTEGER NOT NULL REFERENCES plants(id),
 tds REAL NOT NULL, ph REAL NOT NULL, sample_point TEXT NOT NULL,
 tested_at TEXT NOT NULL, notes TEXT DEFAULT '', created_by INTEGER REFERENCES users(id));
CREATE TABLE IF NOT EXISTS maintenance (
 id INTEGER PRIMARY KEY, plant_id INTEGER NOT NULL REFERENCES plants(id),
 equipment TEXT NOT NULL, due_date TEXT NOT NULL, completed_at TEXT,
 notes TEXT DEFAULT '', created_by INTEGER REFERENCES users(id));
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY, plant_id INTEGER NOT NULL REFERENCES plants(id),
 user_id INTEGER REFERENCES users(id), action TEXT NOT NULL, entity TEXT NOT NULL,
 entity_id INTEGER, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_cust ON customers(plant_id);
CREATE INDEX IF NOT EXISTS idx_delivery ON deliveries(plant_id,delivered_at);
CREATE INDEX IF NOT EXISTS idx_delivery_customer ON deliveries(plant_id,customer_id);
CREATE INDEX IF NOT EXISTS idx_payment ON payments(plant_id,paid_at);
CREATE INDEX IF NOT EXISTS idx_payment_customer ON payments(plant_id,customer_id);
CREATE INDEX IF NOT EXISTS idx_order ON orders(plant_id,requested_date,status);
CREATE INDEX IF NOT EXISTS idx_order_driver ON orders(plant_id,driver_id,requested_date,status);
CREATE INDEX IF NOT EXISTS idx_customer_name ON customers(plant_id,name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_delivery_customer_date ON deliveries(plant_id,customer_id,delivered_at DESC);
CREATE TABLE IF NOT EXISTS payment_allocations (
 id INTEGER PRIMARY KEY,
 payment_id INTEGER NOT NULL REFERENCES payments(id) ON DELETE CASCADE,
 delivery_id INTEGER NOT NULL REFERENCES deliveries(id) ON DELETE CASCADE,
 amount_paisa INTEGER NOT NULL CHECK(amount_paisa>0),
 UNIQUE(payment_id,delivery_id)
);
CREATE INDEX IF NOT EXISTS idx_allocation_payment ON payment_allocations(payment_id);
CREATE INDEX IF NOT EXISTS idx_allocation_invoice ON payment_allocations(delivery_id);
CREATE INDEX IF NOT EXISTS idx_payment_delivery ON payments(plant_id,delivery_id);
CREATE INDEX IF NOT EXISTS idx_expense_date ON expenses(plant_id,incurred_at);
CREATE INDEX IF NOT EXISTS idx_quality_date ON quality(plant_id,tested_at DESC);
CREATE INDEX IF NOT EXISTS idx_maint_due ON maintenance(plant_id,completed_at,due_date);
CREATE INDEX IF NOT EXISTS idx_audit_recent ON audit(plant_id,id DESC);
CREATE TABLE IF NOT EXISTS auth_attempts(
 key TEXT PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0,
 window_start INTEGER NOT NULL, locked_until INTEGER NOT NULL DEFAULT 0);

'''
with db() as c:
    c.execute('PRAGMA journal_mode=WAL')
    c.executescript(SCHEMA)

def upgrade_legacy_users():
    # v2 has email NOT NULL and no username. Rebuild inside a single transaction;
    # SQLite's backup API preserves a recoverable copy before the first migration.
    with db() as con:
        columns={r['name'] for r in con.execute('PRAGMA table_info(users)')}
    if 'username' in columns:
        return
    backup_dir=DATA/'backups';backup_dir.mkdir(parents=True,exist_ok=True)
    target=backup_dir/('pre_v3_migration_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'.sqlite3')
    with sqlite3.connect(DB_PATH) as live, sqlite3.connect(target) as snap:
        live.backup(snap)
    with sqlite3.connect(DB_PATH, isolation_level=None, timeout=30) as con:
        con.execute('PRAGMA foreign_keys=OFF')
        try:
            con.execute('BEGIN IMMEDIATE')
            con.execute('''CREATE TABLE users_new(
                id INTEGER PRIMARY KEY, plant_id INTEGER NOT NULL REFERENCES plants(id),
                name TEXT NOT NULL, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                email TEXT UNIQUE COLLATE NOCASE, password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('owner','manager','driver')),
                active INTEGER NOT NULL DEFAULT 1, session_version INTEGER NOT NULL DEFAULT 0)''')
            con.execute('''INSERT INTO users_new(id,plant_id,name,username,email,password_hash,role,active)
                SELECT id,plant_id,name,'user'||id,email,password_hash,role,active FROM users''')
            con.execute('DROP TABLE users')
            con.execute('ALTER TABLE users_new RENAME TO users')
            problems=con.execute('PRAGMA foreign_key_check').fetchall()
            if problems:raise RuntimeError('Foreign key check failed during migration')
            con.execute('COMMIT')
        except Exception:
            con.execute('ROLLBACK');raise
        finally:
            con.execute('PRAGMA foreign_keys=ON')

upgrade_legacy_users()

# v4 additive migration: does not rebuild or clear existing operational tables.
with db() as c:
    plant_columns={r['name'] for r in c.execute('PRAGMA table_info(plants)')}
    if 'active' not in plant_columns:
        c.execute('ALTER TABLE plants ADD COLUMN active INTEGER NOT NULL DEFAULT 1')
    c.executescript("""
    CREATE TABLE IF NOT EXISTS platform_admins (
      id INTEGER PRIMARY KEY, name TEXT NOT NULL,
      username TEXT NOT NULL UNIQUE COLLATE NOCASE,
      password_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
      session_version INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS platform_audit (
      id INTEGER PRIMARY KEY,
      admin_id INTEGER REFERENCES platform_admins(id),
      action TEXT NOT NULL, plant_id INTEGER REFERENCES plants(id),
      subject_id INTEGER, created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_platform_audit ON platform_audit(id DESC);
    CREATE INDEX IF NOT EXISTS idx_users_plant_role ON users(plant_id,role);
    """)


def encode_token(uid, plant_id, version=0):
    payload = {'kind':'tenant','u': uid, 'p': plant_id, 'v': version, 'exp': int(time.time()) + 60 * 60 * 24}
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(',', ':')).encode()).decode().rstrip('=')
    sig = hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()
    return body + '.' + sig

def decode_token(tok):
    try:
        body, sig = tok.split('.', 1)
        expected = hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise ValueError()
        payload = json.loads(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)))
        if not isinstance(payload, dict) or payload.get('exp',0) < time.time():
            raise ValueError()
        return payload
    except Exception:
        raise HTTPException(401, 'Session expired or invalid')

def hash_pw(password):
    if len(password) < 8:
        raise HTTPException(422, 'Password must be at least 8 characters')
    if len(password) > 200:
        raise HTTPException(422, 'Password is too long')
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 260000)
    return salt.hex() + ':' + digest.hex()

def check_pw(password, password_hash):
    try:
        salt, expected = password_hash.split(':')
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 260000)
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False

def auth(authorization: Optional[str] = Header(default=None)):
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(401, 'Please log in')
    token = decode_token(authorization[7:])
    if token.get('kind','tenant') != 'tenant':
        raise HTTPException(403, 'This endpoint requires a plant account')
    with db() as c:
        u = c.execute('''SELECT u.id,u.plant_id,u.name,u.username,u.email,u.role,u.active,u.session_version
             FROM users u JOIN plants p ON p.id=u.plant_id
             WHERE u.id=? AND u.plant_id=? AND p.active=1''',
                      (token['u'], token['p'])).fetchone()
    if not u or not u['active'] or token.get('v')!=u['session_version']:
        raise HTTPException(401, 'Session expired or account unavailable')
    return dict(u)

def require(u, *roles):
    if u['role'] not in roles:
        raise HTTPException(403, 'Insufficient permissions')

def owned(c, table, item_id, plant_id):
    row = c.execute(f'SELECT * FROM {table} WHERE id=?' if table == 'plants' else f'SELECT * FROM {table} WHERE id=? AND plant_id=?', (item_id,) if table == 'plants' else (item_id,plant_id)).fetchone()
    if row is None:
        raise HTTPException(404, f'{table[:-1].capitalize()} not found')
    return row

def valid_date(s):
    try:
        return date.fromisoformat(s).isoformat()
    except (ValueError, TypeError):
        raise HTTPException(422, 'Date must be YYYY-MM-DD')

def audit(c, u, action, entity, eid):
    c.execute('INSERT INTO audit(plant_id,user_id,action,entity,entity_id,created_at) VALUES(?,?,?,?,?,?)',
              (u['plant_id'],u['id'],action,entity,eid,now()))

def customer_stats(c, pid, id=None):
    sql = """SELECT c.*,
       (c.opening_balance_paisa+COALESCE(d.total,0)-COALESCE(p.total,0)) AS balance_paisa,
       (c.opening_bottles+COALESCE(d.bottles,0)-COALESCE(br.qty,0)) AS bottles_out
       FROM customers c
       LEFT JOIN (SELECT customer_id,SUM(total_paisa) total,SUM(qty-empties) bottles
           FROM deliveries WHERE plant_id=? GROUP BY customer_id) d ON d.customer_id=c.id
       LEFT JOIN (SELECT customer_id,SUM(amount_paisa) total
           FROM payments WHERE plant_id=? GROUP BY customer_id) p ON p.customer_id=c.id
       LEFT JOIN (SELECT customer_id,SUM(qty) qty FROM bottle_returns WHERE plant_id=? GROUP BY customer_id) br ON br.customer_id=c.id
       WHERE c.plant_id=?"""
    params=[pid,pid,pid,pid]
    if id is not None:
        sql+=' AND c.id=?';params.append(id)
    sql+=' ORDER BY c.name COLLATE NOCASE'
    return [dict(r) for r in c.execute(sql,params).fetchall()]

def readable_customer(row):
    row=dict(row)
    row['price']=rs(row.pop('price_paisa'))
    row['balance']=rs(row.pop('balance_paisa'))
    row['opening_balance']=rs(row.pop('opening_balance_paisa'))
    return row

class Signup(BaseModel):
    plant: str = Field(min_length=2,max_length=100)
    name: str = Field(min_length=2,max_length=80)
    username: str = Field(min_length=3,max_length=40)
    email: Optional[str] = Field(default=None,max_length=160)
    password: str = Field(min_length=8,max_length=200)

class Login(BaseModel):
    identifier: Optional[str] = None
    email: Optional[str] = None  # v2 login compatibility
    password: str

migrate_payment_allocations()

class PlantUpdate(BaseModel):
    name: str = Field(min_length=2,max_length=100)
    phone: str = Field(default='',max_length=40)
    address: str = Field(default='',max_length=240)

class CustomerIn(BaseModel):
    name: str = Field(min_length=2,max_length=100)
    phone: str = Field(default='',max_length=40)
    area: str = Field(default='',max_length=80)
    address: str = Field(default='',max_length=240)
    price: float = Field(ge=0,le=1000000)
    opening_balance: float = Field(default=0,ge=0,le=100000000)
    opening_bottles: int = Field(default=0,ge=0,le=100000)

class RouteIn(BaseModel):
    name: str = Field(min_length=2,max_length=80)
    driver_id: Optional[int] = None

class StaffIn(BaseModel):
    name: str = Field(min_length=2,max_length=80)
    username: str = Field(min_length=3,max_length=40)
    email: Optional[str] = Field(default=None,max_length=160)
    password: str = Field(min_length=8,max_length=200)
    role: str

class OrderIn(BaseModel):
    product_id: Optional[int] = Field(default=None,gt=0)
    customer_id: int
    qty: int = Field(gt=0,le=100000)
    requested_date: str
    driver_id: Optional[int] = None
    route_id: Optional[int] = None
    note: str = Field(default='',max_length=600)

class SubscriptionIn(BaseModel):
    product_id: Optional[int] = Field(default=None,gt=0)
    customer_id: int
    qty: int = Field(gt=0,le=10000)
    weekdays: list[int] = Field(min_length=1,max_length=7)
    driver_id: Optional[int] = None
    route_id: Optional[int] = None

class DeliveryIn(BaseModel):
    product_id: Optional[int] = Field(default=None,gt=0)
    customer_id: int
    qty: int = Field(gt=0,le=100000)
    empties: int = Field(ge=0,le=100000)
    unit_price: float = Field(ge=0,le=1000000)
    discount: float = Field(default=0,ge=0,le=100000000)
    paid_now: float = Field(default=0,ge=0,le=100000000)
    method: str = Field(default='Cash',max_length=40)
    delivered_at: str
    driver_id: Optional[int] = None
    route_id: Optional[int] = None
    order_id: Optional[int] = None
    notes: str = Field(default='',max_length=600)

class BottleReturnIn(BaseModel):
    product_id: Optional[int] = Field(default=None,gt=0)
    customer_id: int
    qty: int = Field(gt=0,le=100000)
    returned_at: str
    notes: str = Field(default='',max_length=600)

class PaymentIn(BaseModel):
    customer_id: int
    delivery_id: Optional[int] = None
    amount: float = Field(gt=0,le=100000000)
    method: str = Field(default='Cash',max_length=40)
    paid_at: str
    notes: str = Field(default='',max_length=600)

class ProductionIn(BaseModel):
    product_id: Optional[int] = Field(default=None,gt=0)
    qty: int = Field(gt=0,le=100000)
    produced_at: str
    tds: Optional[float] = Field(default=None,ge=0,le=100000)
    ph: Optional[float] = Field(default=None,ge=0,le=14)
    notes: str = Field(default='',max_length=600)

class StockIn(BaseModel):
    product_id: Optional[int] = Field(default=None,gt=0)
    kind: str
    qty: int = Field(ge=-100000,le=100000)
    reason: str = Field(min_length=3,max_length=300)

class ExpenseIn(BaseModel):
    category: str = Field(min_length=2,max_length=60)
    amount: float = Field(gt=0,le=100000000)
    incurred_at: str
    notes: str = Field(default='',max_length=500)

class QualityIn(BaseModel):
    tds: float = Field(ge=0,le=100000)
    ph: float = Field(ge=0,le=14)
    sample_point: str = Field(min_length=2,max_length=120)
    tested_at: str
    notes: str = Field(default='',max_length=500)

class MaintenanceIn(BaseModel):
    equipment: str = Field(min_length=2,max_length=120)
    due_date: str
    notes: str = Field(default='',max_length=500)

def clean_email(value):
    value=(value or '').strip().lower()
    if not value:return None
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',value):
        raise HTTPException(422,'Invalid email address')
    return value

def clean_username(value):
    value=(value or '').strip().lower()
    if not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{2,39}',value):
        raise HTTPException(422,'Login ID must be 3-40 characters (letters, numbers, dot, underscore or hyphen)')
    return value

def throttle_key(identity):
    return hmac.new(SECRET,identity.encode('utf-8'),hashlib.sha256).hexdigest()

def assert_login_allowed(identity):
    key=throttle_key('login:'+identity)
    with db() as c:
        row=c.execute('SELECT * FROM auth_attempts WHERE key=?',(key,)).fetchone()
    if row and row['locked_until']>int(time.time()):
        raise HTTPException(429,'Too many failed attempts. Try again in 15 minutes.')

def track_login(identity,success):
    key=throttle_key('login:'+identity);t=int(time.time())
    with transaction() as c:
        if success:
            c.execute('DELETE FROM auth_attempts WHERE key=?',(key,))
            return
        row=c.execute('SELECT failures,window_start FROM auth_attempts WHERE key=?',(key,)).fetchone()
        count=(row['failures']+1) if row and t-row['window_start']<900 else 1
        start=row['window_start'] if row and t-row['window_start']<900 else t
        c.execute('''INSERT INTO auth_attempts(key,failures,window_start,locked_until) VALUES(?,?,?,?)
            ON CONFLICT(key) DO UPDATE SET failures=excluded.failures,window_start=excluded.window_start,
                locked_until=excluded.locked_until''',(key,count,start,t+900 if count>=5 else 0))
        # Bounded cleanup; old records cannot accumulate forever.
        if secrets.randbelow(64)==0:
            c.execute('DELETE FROM auth_attempts WHERE window_start<?',(t-86400,))


# v5: tenant-scoped bottle catalogue and additive migration.
PRODUCT_TABLES = ('orders','subscriptions','deliveries','bottle_returns','production','stock_movements')

def ensure_products(c,pid):
    if c.execute('SELECT 1 FROM bottle_products WHERE plant_id=?',(pid,)).fetchone():
        return
    plant=c.execute('SELECT * FROM plants WHERE id=?',(pid,)).fetchone()
    for litres in (19,12,14):
        c.execute("""INSERT INTO bottle_products(plant_id,name,litres,price_paisa,full_stock,empty_stock,damaged_stock)
          VALUES(?,?,?,?,?,?,?)""",(pid,f'{litres}L Bottle',litres,0,
          plant['full_stock'] if litres==19 else 0,plant['empty_stock'] if litres==19 else 0,
          plant['damaged_stock'] if litres==19 else 0))

def product_for(c,pid,product_id=None,active=True):
    ensure_products(c,pid)
    row=c.execute('SELECT * FROM bottle_products WHERE plant_id=? AND '+('id=?' if product_id is not None else 'litres=19 ORDER BY id LIMIT 1'),
                  (pid,product_id) if product_id is not None else (pid,)).fetchone()
    if row is None:raise HTTPException(404,'Bottle size not found in this plant')
    if active and not row['active']:raise HTTPException(409,'This bottle size is archived')
    return row

def sync_stock(c,pid):
    c.execute("""UPDATE plants SET
      full_stock=(SELECT COALESCE(SUM(full_stock),0) FROM bottle_products WHERE plant_id=?),
      empty_stock=(SELECT COALESCE(SUM(empty_stock),0) FROM bottle_products WHERE plant_id=?),
      damaged_stock=(SELECT COALESCE(SUM(damaged_stock),0) FROM bottle_products WHERE plant_id=?) WHERE id=?""",(pid,pid,pid,pid))

def bottle_balance(c,pid,cid,product_id):
    opening=c.execute('SELECT opening FROM customer_bottles WHERE plant_id=? AND customer_id=? AND product_id=?',(pid,cid,product_id)).fetchone()
    sent=c.execute('SELECT COALESCE(SUM(qty-empties),0) FROM deliveries WHERE plant_id=? AND customer_id=? AND product_id=?',(pid,cid,product_id)).fetchone()[0]
    back=c.execute('SELECT COALESCE(SUM(qty),0) FROM bottle_returns WHERE plant_id=? AND customer_id=? AND product_id=?',(pid,cid,product_id)).fetchone()[0]
    return (opening[0] if opening else 0)+sent-back

def reconcile_bottles(c,u,cid,product_id,correction):
    c.execute("""INSERT INTO customer_bottles(plant_id,customer_id,product_id,opening) VALUES(?,?,?,?)
      ON CONFLICT(customer_id,product_id) DO UPDATE SET opening=opening+excluded.opening""",(u['plant_id'],cid,product_id,correction))
    c.execute('UPDATE customers SET opening_bottles=opening_bottles+? WHERE id=? AND plant_id=?',(correction,cid,u['plant_id']))
    audit(c,u,'reconcile_untracked_return_'+str(correction),'customers',cid)

def migrate_v5():
    with db() as c:
        migrated=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bottle_products'").fetchone()
    if not migrated:
        directory=DATA/'backups';directory.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(DB_PATH) as source,sqlite3.connect(directory/('pre_v5_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'.sqlite3')) as target:
            source.backup(target)
    with transaction() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS bottle_products(
          id INTEGER PRIMARY KEY,plant_id INTEGER NOT NULL REFERENCES plants(id),
          name TEXT NOT NULL COLLATE NOCASE,litres REAL NOT NULL CHECK(litres>0),
          price_paisa INTEGER NOT NULL DEFAULT 0 CHECK(price_paisa>=0),
          full_stock INTEGER NOT NULL DEFAULT 0 CHECK(full_stock>=0),
          empty_stock INTEGER NOT NULL DEFAULT 0 CHECK(empty_stock>=0),
          damaged_stock INTEGER NOT NULL DEFAULT 0 CHECK(damaged_stock>=0),
          low_stock INTEGER NOT NULL DEFAULT 20 CHECK(low_stock>=0),active INTEGER NOT NULL DEFAULT 1,
          UNIQUE(plant_id,name))""")
        c.execute("""CREATE TABLE IF NOT EXISTS customer_bottles(
          plant_id INTEGER NOT NULL REFERENCES plants(id),customer_id INTEGER NOT NULL REFERENCES customers(id),
          product_id INTEGER NOT NULL REFERENCES bottle_products(id),opening INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(customer_id,product_id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS customer_prices(
          plant_id INTEGER NOT NULL REFERENCES plants(id),customer_id INTEGER NOT NULL REFERENCES customers(id),
          product_id INTEGER NOT NULL REFERENCES bottle_products(id),price_paisa INTEGER NOT NULL CHECK(price_paisa>=0),
          PRIMARY KEY(customer_id,product_id))""")
        for table in PRODUCT_TABLES:
            cols={r['name'] for r in c.execute(f'PRAGMA table_info({table})')}
            if 'product_id' not in cols:c.execute(f'ALTER TABLE {table} ADD COLUMN product_id INTEGER REFERENCES bottle_products(id)')
            c.execute(f'CREATE INDEX IF NOT EXISTS idx_{table}_product ON {table}(plant_id,product_id)')
        cols={r['name'] for r in c.execute('PRAGMA table_info(deliveries)')}
        if 'product_name' not in cols:
            c.execute("ALTER TABLE deliveries ADD COLUMN product_name TEXT NOT NULL DEFAULT '19L Bottle'")
            c.execute('ALTER TABLE deliveries ADD COLUMN litres REAL NOT NULL DEFAULT 19')
        for plant in c.execute('SELECT id FROM plants').fetchall():
            pid=plant['id'];pr=product_for(c,pid,active=False)
            for table in PRODUCT_TABLES:c.execute(f'UPDATE {table} SET product_id=? WHERE plant_id=? AND product_id IS NULL',(pr['id'],pid))
            if not migrated:
                c.execute('INSERT OR IGNORE INTO customer_bottles SELECT plant_id,id,?,opening_bottles FROM customers WHERE plant_id=?',(pr['id'],pid))


def platform_token(admin):
    # Separate token namespace: never accepted by plant APIs.
    payload={'kind':'platform','u':admin['id'],'v':admin['session_version'],
             'exp':int(time.time())+60*60*8}
    body=base64.urlsafe_b64encode(json.dumps(payload,separators=(',',':')).encode()).decode().rstrip('=')
    signature=hmac.new(SECRET,body.encode(),hashlib.sha256).hexdigest()
    return body+'.'+signature


def platform_auth(authorization:Optional[str]=Header(default=None)):
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(401,'Platform administrator login required')
    payload=decode_token(authorization[7:])
    if payload.get('kind')!='platform':
        raise HTTPException(403,'Platform administrator login required')
    with db() as c:
        row=c.execute('SELECT id,name,username,active,session_version FROM platform_admins WHERE id=?',
                      (payload.get('u'),)).fetchone()
    if not row or not row['active'] or row['session_version'] != payload.get('v'):
        raise HTTPException(401,'Administrator session expired')
    return dict(row)


def platform_audit(c, admin, action, plant_id=None, subject_id=None):
    c.execute('INSERT INTO platform_audit(admin_id,action,plant_id,subject_id,created_at) VALUES(?,?,?,?,?)',
              (admin['id'],action,plant_id,subject_id,now()))


class PlatformLogin(BaseModel):
    username:str=Field(min_length=3,max_length=160)
    password:str=Field(min_length=1,max_length=200)


class ProvisionPlant(BaseModel):
    plant:str=Field(min_length=2,max_length=100)
    name:str=Field(min_length=2,max_length=80)
    username:str=Field(min_length=3,max_length=40)
    email:Optional[str]=Field(default=None,max_length=160)
    password:str=Field(min_length=8,max_length=200)
    phone:str=Field(default='',max_length=40)
    address:str=Field(default='',max_length=240)


class TogglePlant(BaseModel):
    active:bool


class AdminResetPassword(BaseModel):
    password:str=Field(min_length=8,max_length=200)


class AdminChangePassword(BaseModel):
    current_password:str
    new_password:str=Field(min_length=8,max_length=200)


app = FastAPI(title='AquaLedger Pro', version='5.0.0',
    docs_url='/api/docs' if os.getenv('AQUA_ENABLE_DOCS')=='1' else None,
    openapi_url='/api/openapi.json' if os.getenv('AQUA_ENABLE_DOCS')=='1' else None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=[x.strip() for x in
    os.getenv('AQUA_ALLOWED_HOSTS','127.0.0.1,localhost,testserver').split(',') if x.strip()])

@app.middleware('http')
async def security_headers(request:Request, call_next):
    try:
        n=int(request.headers.get('content-length','0'))
    except ValueError:
        n=0
    if n>1024*1024:
        return JSONResponse({'detail':'Request too large'},status_code=413)
    response=await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['X-Frame-Options']='DENY'
    response.headers['Referrer-Policy']='no-referrer'
    response.headers['Permissions-Policy']='camera=(), microphone=(), geolocation=()'
    response.headers['Content-Security-Policy']=("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control']='no-store'
    return response

@app.post('/api/platform/login')
def platform_login(item:PlatformLogin):
    identity=clean_username(item.username)
    assert_login_allowed('platform:'+identity)
    with db() as c:
        admin=c.execute('SELECT * FROM platform_admins WHERE username=?',(identity,)).fetchone()
    valid=bool(admin and admin['active'] and check_pw(item.password,admin['password_hash']))
    track_login('platform:'+identity,valid)
    if not valid:raise HTTPException(401,'Invalid administrator credentials')
    return {'token':platform_token(admin),'name':admin['name'],'role':'platform_admin'}


@app.get('/api/platform/me')
def platform_me(admin=Depends(platform_auth)):
    return admin


@app.get('/api/platform/plants')
def platform_plants(admin=Depends(platform_auth)):
    # Account summaries, not unrestricted access to plant customer ledgers.
    with db() as c:
        rows=c.execute('''SELECT p.id,p.name,p.phone,p.address,p.active,p.created_at,
            u.id owner_id,u.name owner_name,u.username owner_username,u.email owner_email,u.active owner_active,
            (SELECT COUNT(*) FROM customers x WHERE x.plant_id=p.id) customer_count,
            (SELECT COUNT(*) FROM users x WHERE x.plant_id=p.id) user_count,
            (SELECT COUNT(*) FROM deliveries x WHERE x.plant_id=p.id) delivery_count
            FROM plants p LEFT JOIN users u ON u.id=(
              SELECT x.id FROM users x WHERE x.plant_id=p.id AND x.role='owner' ORDER BY x.id LIMIT 1
            ) ORDER BY p.id DESC''').fetchall()
    return [dict(r) for r in rows]


@app.post('/api/platform/plants',status_code=201)
def platform_provision(item:ProvisionPlant,admin=Depends(platform_auth)):
    username=clean_username(item.username);email=clean_email(item.email)
    with transaction() as c:
        # An all-or-nothing transaction prevents orphaned plants if a Login ID is taken.
        if c.execute('SELECT 1 FROM users WHERE username=?',(username,)).fetchone():
            raise HTTPException(409,'Login ID already in use')
        if email and c.execute('SELECT 1 FROM users WHERE email=?',(email,)).fetchone():
            raise HTTPException(409,'Email already in use')
        pid=c.execute('''INSERT INTO plants(name,phone,address,created_at)
                      VALUES(?,?,?,?)''',(item.plant.strip(),item.phone.strip(),item.address.strip(),now())).lastrowid
        uid=c.execute('''INSERT INTO users(plant_id,name,username,email,password_hash,role)
                      VALUES(?,?,?,?,?,?)''',(pid,item.name.strip(),username,email,hash_pw(item.password),'owner')).lastrowid
        ensure_products(c,pid)
        platform_audit(c,admin,'provision_plant',pid,uid)
    return {'id':pid,'owner_id':uid,'name':item.plant.strip(),'owner_username':username}


@app.put('/api/platform/plants/{pid}/status')
def platform_status(pid:int,item:TogglePlant,admin=Depends(platform_auth)):
    with transaction() as c:
        plant=c.execute('SELECT id,active FROM plants WHERE id=?',(pid,)).fetchone()
        if not plant:raise HTTPException(404,'Plant not found')
        c.execute('UPDATE plants SET active=? WHERE id=?',(1 if item.active else 0,pid))
        if not item.active:
            # Suspension also revokes sessions, including after later reactivation.
            c.execute('UPDATE users SET session_version=session_version+1 WHERE plant_id=?',(pid,))
        platform_audit(c,admin,'activate_plant' if item.active else 'suspend_plant',pid)
    return {'id':pid,'active':item.active}


@app.put('/api/platform/plants/{pid}/owner-password')
def platform_reset_owner(pid:int,item:AdminResetPassword,admin=Depends(platform_auth)):
    with transaction() as c:
        owner=c.execute("SELECT id FROM users WHERE plant_id=? AND role='owner' ORDER BY id LIMIT 1",(pid,)).fetchone()
        if not owner:raise HTTPException(404,'Plant owner not found')
        c.execute('''UPDATE users SET password_hash=?,session_version=session_version+1
                     WHERE id=?''',(hash_pw(item.password),owner['id']))
        platform_audit(c,admin,'reset_owner_password',pid,owner['id'])
    return {'ok':True}


@app.post('/api/platform/change-password')
def platform_change_password(item:AdminChangePassword,admin=Depends(platform_auth)):
    with transaction() as c:
        current=c.execute('SELECT password_hash FROM platform_admins WHERE id=?',(admin['id'],)).fetchone()
        if not check_pw(item.current_password,current['password_hash']):
            raise HTTPException(403,'Current password is incorrect')
        c.execute('''UPDATE platform_admins SET password_hash=?,session_version=session_version+1 WHERE id=?''',
                  (hash_pw(item.new_password),admin['id']))
        platform_audit(c,admin,'change_platform_password')
    return {'ok':True}


@app.get('/api/platform/audit')
def platform_audit_log(admin=Depends(platform_auth)):
    with db() as c:
        return [dict(r) for r in c.execute('''SELECT a.id,a.action,a.plant_id,a.created_at,
             p.name plant_name, u.username by_admin FROM platform_audit a
             LEFT JOIN plants p ON p.id=a.plant_id
             LEFT JOIN platform_admins u ON u.id=a.admin_id ORDER BY a.id DESC LIMIT 100''')]


@app.get('/api/public/config')
def public_config():
    # Customer provisioning is exclusively controlled by the platform administrator.
    return {'registration_open':False, 'platform_managed':True}

@app.post('/api/auth/register')
def register_disabled():
    # Never expose a public first-plant bootstrap path, even on empty databases.
    raise HTTPException(403, 'Public plant signup is disabled. Contact your AquaLedger administrator.')

@app.post('/api/auth/login')
def login(item:Login):
    identity=(item.identifier or item.email or '').strip().lower()
    if not identity or len(identity)>160 or len(item.password)>200:
        raise HTTPException(401,'Invalid login ID or password')
    assert_login_allowed(identity)
    with db() as c:
        row=c.execute('''SELECT u.* FROM users u JOIN plants p ON p.id=u.plant_id
                   WHERE (u.username=? OR u.email=?) AND p.active=1''',(identity,identity)).fetchone()
    valid=bool(row and row['active'] and check_pw(item.password,row['password_hash']))
    track_login(identity,valid)
    if not valid:
        raise HTTPException(401,'Invalid login ID or password')
    return {'token':encode_token(row['id'],row['plant_id'],row['session_version']),
            'name':row['name'],'role':row['role']}

@app.get('/api/me')
def me(u=Depends(auth)):
    return u

@app.get('/api/settings')
def settings(u=Depends(auth)):
    with db() as c:
        row=owned(c,'plants',u['plant_id'],u['plant_id'])
        return dict(row)

@app.put('/api/settings')
def update_settings(item:PlantUpdate,u=Depends(auth)):
    require(u,'owner')
    with transaction() as c:
        c.execute('UPDATE plants SET name=?,phone=?,address=? WHERE id=?',
              (item.name.strip(),item.phone.strip(),item.address.strip(),u['plant_id']))
        audit(c,u,'update','plants',u['plant_id'])
    return {'ok':True}

@app.get('/api/staff')
def list_staff(u=Depends(auth)):
    require(u,'owner','manager')
    with db() as c:
        return [dict(r) for r in c.execute('SELECT id,name,username,email,role,active FROM users WHERE plant_id=? ORDER BY role,name',(u['plant_id'],))]

@app.post('/api/staff')
def add_staff(item:StaffIn,u=Depends(auth)):
    require(u,'owner')
    if item.role not in ('driver','manager'):
        raise HTTPException(422,'Select driver or manager')
    username=clean_username(item.username);email=clean_email(item.email)
    with transaction() as c:
        try:
            uid=c.execute('INSERT INTO users(plant_id,name,username,email,password_hash,role) VALUES(?,?,?,?,?,?)',
             (u['plant_id'],item.name.strip(),username,email,hash_pw(item.password),item.role)).lastrowid
        except sqlite3.IntegrityError:
            raise HTTPException(409,'Login ID or email already registered')
        audit(c,u,'create','users',uid)
    return {'id':uid}

@app.get('/api/customers')
def list_customers(q:str='',u=Depends(auth)):
    with db() as c:
        if u['role']=='driver':
            rows=[dict(r) for r in c.execute('''SELECT DISTINCT c.id,c.name,c.phone,c.area,c.address,c.price_paisa
                FROM customers c WHERE c.plant_id=? AND (EXISTS
                (SELECT 1 FROM orders o WHERE o.plant_id=c.plant_id AND o.customer_id=c.id AND o.driver_id=?)
                OR EXISTS (SELECT 1 FROM deliveries d WHERE d.plant_id=c.plant_id AND d.customer_id=c.id AND d.driver_id=?))
                ORDER BY c.name LIMIT 2000''',(u['plant_id'],u['id'],u['id']))]
            rows=[dict(r,price=rs(r['price_paisa']),balance=0,opening_balance=0,bottles_out=0) for r in rows]
        else:
            rows=[readable_customer(r) for r in customer_stats(c,u['plant_id'])]
    if q:
        q=q.lower()
        rows=[r for r in rows if q in (r['name']+' '+r['phone']+' '+r['area']).lower()]
    return rows

@app.post('/api/customers')
def add_customer(item:CustomerIn,u=Depends(auth)):
    require(u,'owner','manager')
    with transaction() as c:
        cid=c.execute('''INSERT INTO customers(plant_id,name,phone,area,address,price_paisa,opening_balance_paisa,opening_bottles,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)''',(u['plant_id'],item.name.strip(),item.phone,item.area,item.address,
        money(item.price),money(item.opening_balance),item.opening_bottles,now())).lastrowid
        pr=product_for(c,u['plant_id'])
        c.execute('INSERT INTO customer_bottles VALUES(?,?,?,?)',(u['plant_id'],cid,pr['id'],item.opening_bottles))
        audit(c,u,'create','customers',cid)
    return {'id':cid}

@app.get('/api/customers/{customer_id}/ledger')
def customer_ledger(customer_id:int,limit:int=Query(100,ge=1,le=500),offset:int=Query(0,ge=0),u=Depends(auth)):
    require(u,'owner','manager')
    with db() as c:
        owned(c,'customers',customer_id,u['plant_id'])
        cust=readable_customer(customer_stats(c,u['plant_id'],customer_id)[0])
        rows=c.execute('''SELECT id,delivered_at AS dt,'delivery' AS type,total_paisa AS amount,qty,empties,notes,product_id FROM deliveries
        WHERE plant_id=? AND customer_id=? UNION ALL SELECT id,paid_at AS dt,'payment' AS type,amount_paisa AS amount,0 AS qty,0 AS empties,notes,NULL AS product_id FROM payments
        WHERE plant_id=? AND customer_id=? UNION ALL SELECT id,returned_at AS dt,'bottle return' AS type,
        0 AS amount,0 AS qty,qty AS empties,notes,product_id FROM bottle_returns
        WHERE plant_id=? AND customer_id=? ORDER BY dt DESC,id DESC LIMIT ? OFFSET ?''',
        (u['plant_id'],customer_id,u['plant_id'],customer_id,u['plant_id'],customer_id,limit,offset)).fetchall()
    return {'customer':cust,'entries':[dict(r,amount=rs(r['amount'])) for r in rows], 'limit':limit,'offset':offset}

@app.get('/api/routes')
def routes(u=Depends(auth)):
    with db() as c:
        if u['role']=='driver':
            return [dict(r) for r in c.execute('''SELECT r.*,u.name AS driver_name FROM routes r LEFT JOIN users u ON u.id=r.driver_id
                WHERE r.plant_id=? AND (r.driver_id=? OR EXISTS (SELECT 1 FROM orders o WHERE o.route_id=r.id AND o.plant_id=r.plant_id AND o.driver_id=?)) ORDER BY r.name''',(u['plant_id'],u['id'],u['id']))]
        return [dict(r) for r in c.execute('''SELECT r.*,u.name AS driver_name FROM routes r LEFT JOIN users u ON u.id=r.driver_id
        WHERE r.plant_id=? ORDER BY r.name''',(u['plant_id'],))]

@app.post('/api/routes')
def add_route(item:RouteIn,u=Depends(auth)):
    require(u,'owner','manager')
    with transaction() as c:
        check_driver(c,item.driver_id,u['plant_id'])
        rid=c.execute('INSERT INTO routes(plant_id,name,driver_id) VALUES(?,?,?)',
                      (u['plant_id'],item.name,item.driver_id)).lastrowid
        audit(c,u,'create','routes',rid)
    return {'id':rid}

def check_driver(c, driver_id, pid):
    if driver_id is not None:
        row=owned(c,'users',driver_id,pid)
        if row['role'] not in ('driver','manager') or not row['active']:
            raise HTTPException(422,'Selected staff member cannot drive')

def check_route(c,route_id,pid):
    if route_id is not None:
        owned(c,'routes',route_id,pid)

@app.get('/api/subscriptions')
def subscriptions(u=Depends(auth)):
    require(u,'owner','manager')
    with db() as c:
        return [dict(r) for r in c.execute('''SELECT s.*,c.name AS customer_name FROM subscriptions s
           JOIN customers c ON c.id=s.customer_id WHERE s.plant_id=? ORDER BY s.id DESC''',(u['plant_id'],))]

@app.post('/api/subscriptions')
def add_subscription(item:SubscriptionIn,u=Depends(auth)):
    require(u,'owner','manager')
    days=sorted(set(item.weekdays))
    if not days or any(d<0 or d>6 for d in days):
        raise HTTPException(422,'Weekdays must be numbers 0 (Monday) through 6 (Sunday)')
    with transaction() as c:
        pr=product_for(c,u['plant_id'],item.product_id)
        owned(c,'customers',item.customer_id,u['plant_id'])
        check_driver(c,item.driver_id,u['plant_id']);check_route(c,item.route_id,u['plant_id'])
        sid=c.execute('''INSERT INTO subscriptions(plant_id,customer_id,qty,weekdays,driver_id,route_id)
                    VALUES(?,?,?,?,?,?)''',(u['plant_id'],item.customer_id,item.qty,','.join(map(str,days)),item.driver_id,item.route_id)).lastrowid
        c.execute('UPDATE subscriptions SET product_id=? WHERE id=?',(pr['id'],sid))
        audit(c,u,'create','subscriptions',sid)
    return {'id':sid}

@app.post('/api/subscriptions/{sid}/pause')
def pause_subscription(sid:int,u=Depends(auth)):
    require(u,'owner','manager')
    with transaction() as c:
        row=owned(c,'subscriptions',sid,u['plant_id'])
        c.execute('UPDATE subscriptions SET active=? WHERE id=?',(0 if row['active'] else 1,sid))
        audit(c,u,'toggle','subscriptions',sid)
    return {'ok':True}

@app.post('/api/orders/generate')
def generate_orders(for_date:str=Query(default=None),u=Depends(auth)):
    require(u,'owner','manager')
    day=valid_date(for_date or today())
    weekday=date.fromisoformat(day).weekday()
    count=0
    with transaction() as c:
        rows=c.execute('SELECT * FROM subscriptions WHERE plant_id=? AND active=1',(u['plant_id'],)).fetchall()
        for sub in rows:
            if not product_for(c,u['plant_id'],sub['product_id'],active=False)['active'] or str(weekday) not in sub['weekdays'].split(','):
                continue
            res=c.execute('''INSERT OR IGNORE INTO orders(plant_id,customer_id,qty,requested_date,driver_id,route_id,subscription_id,created_at)
               VALUES(?,?,?,?,?,?,?,?)''',(u['plant_id'],sub['customer_id'],sub['qty'],day,sub['driver_id'],sub['route_id'],sub['id'],now()))
            if res.rowcount:c.execute('UPDATE orders SET product_id=? WHERE id=?',(sub['product_id'],res.lastrowid))
            count+=res.rowcount
        audit(c,u,'generate','orders',count)
    return {'created':count,'date':day}

@app.get('/api/orders')
def list_orders(status:str='',day:str='',limit:int=Query(500,ge=1,le=500),offset:int=Query(0,ge=0),u=Depends(auth)):
    query='''SELECT o.*,c.name AS customer_name,c.address,c.phone,c.area FROM orders o
        JOIN customers c ON c.id=o.customer_id WHERE o.plant_id=?'''
    params=[u['plant_id']]
    if u['role']=='driver':
        query+=' AND o.driver_id=?';params.append(u['id'])
    if status:
        query+=' AND o.status=?';params.append(status)
    if day:
        query+=' AND o.requested_date=?';params.append(valid_date(day))
    query+=' ORDER BY o.requested_date DESC, o.id DESC LIMIT ? OFFSET ?'
    params.extend([limit,offset])
    with db() as c:
        return [dict(r) for r in c.execute(query,params)]

@app.post('/api/orders')
def add_order(item:OrderIn,u=Depends(auth)):
    require(u,'owner','manager')
    day=valid_date(item.requested_date)
    with transaction() as c:
        pr=product_for(c,u['plant_id'],item.product_id)
        owned(c,'customers',item.customer_id,u['plant_id'])
        check_driver(c,item.driver_id,u['plant_id']);check_route(c,item.route_id,u['plant_id'])
        oid=c.execute('''INSERT INTO orders(plant_id,customer_id,qty,requested_date,driver_id,route_id,note,created_at)
                         VALUES(?,?,?,?,?,?,?,?)''',(u['plant_id'],item.customer_id,item.qty,day,item.driver_id,item.route_id,item.note,now())).lastrowid
        c.execute('UPDATE orders SET product_id=? WHERE id=?',(pr['id'],oid))
        audit(c,u,'create','orders',oid)
    return {'id':oid}

@app.post('/api/orders/{oid}/cancel')
def cancel_order(oid:int,u=Depends(auth)):
    require(u,'owner','manager')
    with transaction() as c:
        order=owned(c,'orders',oid,u['plant_id'])
        if order['status']!='pending':
            raise HTTPException(409,'Only pending orders can be canceled')
        c.execute("UPDATE orders SET status='cancelled' WHERE id=?",(oid,))
        audit(c,u,'cancel','orders',oid)
    return {'ok':True}

@app.get('/api/deliveries')
def list_deliveries(limit:int=Query(500,ge=1,le=500),offset:int=Query(0,ge=0),u=Depends(auth)):
    sql='''SELECT d.*,c.name AS customer_name,usr.name AS driver_name,
       COALESCE((SELECT SUM(a.amount_paisa) FROM payment_allocations a WHERE a.delivery_id=d.id),0) AS paid_invoice_paisa
       FROM deliveries d
       JOIN customers c ON c.id=d.customer_id LEFT JOIN users usr ON usr.id=d.driver_id WHERE d.plant_id=?'''
    args=[u['plant_id']]
    if u['role']=='driver':
        sql+=' AND d.driver_id=?';args.append(u['id'])
    sql+=' ORDER BY d.delivered_at DESC,d.id DESC LIMIT ? OFFSET ?'
    args.extend([limit,offset])
    with db() as c:
        return [dict(r,total=rs(r['total_paisa']),unit_price=rs(r['unit_price_paisa']),discount=rs(r['discount_paisa']),invoice_paid=rs(r['paid_invoice_paisa'])) for r in c.execute(sql,args)]

@app.post('/api/deliveries')
def add_delivery(item:DeliveryIn,u=Depends(auth)):
    day=valid_date(item.delivered_at)
    unit=money(item.unit_price); discount=money(item.discount);paid=money(item.paid_now)
    total=unit*item.qty-discount
    if total<0:
        raise HTTPException(422,'Discount exceeds amount')
    with transaction() as c:
        pr=product_for(c,u['plant_id'],item.product_id)
        cust=owned(c,'customers',item.customer_id,u['plant_id'])
        if u['role']=='driver':
            if item.order_id is None:
                raise HTTPException(403,'Drivers must fulfill an assigned order')
            if item.driver_id not in (None,u['id']):
                raise HTTPException(403,'Driver can only record own deliveries')
            driver=u['id']
        else:
            driver=item.driver_id
        check_driver(c,driver,u['plant_id']);check_route(c,item.route_id,u['plant_id'])
        if item.order_id:
            order=owned(c,'orders',item.order_id,u['plant_id'])
            if order['status']!='pending' or order['customer_id']!=item.customer_id:
                raise HTTPException(409,'Order is not pending for selected customer')
            if u['role']=='driver' and order['driver_id']!=u['id']:
                raise HTTPException(403,'Order is assigned to another driver')
            if u['role']=='driver' and item.route_id not in (None, order['route_id']):
                raise HTTPException(403,'Cannot change route of assigned delivery')
            if order['product_id']!=pr['id']:
                raise HTTPException(409,'Delivery bottle size must match the order')
            # Actual delivered quantity is authoritative; keep original order complete.
            # Orders are auditable by preserving its original quantity in the order record.
        st=pr
        if st['full_stock']<item.qty:
            raise HTTPException(409,'Insufficient filled bottles in plant stock')
        bottles=bottle_balance(c,u['plant_id'],item.customer_id,pr['id'])
        if item.empties>bottles+item.qty:
            # Accept physical returns even when prior circulation was never logged.
            # Record a transparent opening-bottle correction, rather than negative
            # bottles in circulation or silently inventing stock.
            correction=item.empties-bottles-item.qty
            reconcile_bottles(c,u,item.customer_id,pr['id'],correction)
        did=c.execute('''INSERT INTO deliveries(plant_id,customer_id,order_id,qty,empties,unit_price_paisa,discount_paisa,total_paisa,
                       driver_id,route_id,delivered_at,notes,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                      (u['plant_id'],item.customer_id,item.order_id,item.qty,item.empties,unit,discount,total,driver,item.route_id,day,item.notes,u['id'])).lastrowid
        c.execute('UPDATE bottle_products SET full_stock=full_stock-?,empty_stock=empty_stock+? WHERE id=?',
                  (item.qty,item.empties,pr['id']))
        sync_stock(c,u['plant_id'])
        c.execute('UPDATE deliveries SET product_name=?,litres=? WHERE id=?',(pr['name'],pr['litres'],did))
        if paid:
            c.execute('''INSERT INTO payments(plant_id,customer_id,delivery_id,amount_paisa,method,paid_at,notes,created_by)
                         VALUES(?,?,?,?,?,?,?,?)''',(u['plant_id'],item.customer_id,did,paid,item.method,day,'Payment received at delivery',u['id']))
        reconcile_customer(c,u['plant_id'],item.customer_id)
        if item.order_id:
            c.execute("UPDATE orders SET status='delivered',delivery_id=? WHERE id=?",(did,item.order_id))
        c.execute('UPDATE deliveries SET product_id=? WHERE id=?',(pr['id'],did))
        audit(c,u,'create','deliveries',did)
    return {'id':did,'total':rs(total)}

@app.get('/api/bottle-returns')
def list_bottle_returns(limit:int=Query(100,ge=1,le=500),offset:int=Query(0,ge=0),u=Depends(auth)):
    require(u,'owner','manager')
    with db() as c:
        return [dict(r) for r in c.execute('''SELECT br.*,cu.name customer_name FROM bottle_returns br
            JOIN customers cu ON cu.id=br.customer_id AND cu.plant_id=br.plant_id
            WHERE br.plant_id=? ORDER BY br.returned_at DESC,br.id DESC LIMIT ? OFFSET ?''',
            (u['plant_id'],limit,offset))]


@app.post('/api/bottle-returns')
def add_bottle_return(item:BottleReturnIn,u=Depends(auth)):
    require(u,'owner','manager')
    day=valid_date(item.returned_at)
    with transaction() as c:
        pr=product_for(c,u['plant_id'],item.product_id)
        owned(c,'customers',item.customer_id,u['plant_id'])
        previously=bottle_balance(c,u['plant_id'],item.customer_id,pr['id'])
        if item.qty>previously:
            # Owner confirmed actual physical bottles. Keep circulation >= 0.
            correction=item.qty-previously
            reconcile_bottles(c,u,item.customer_id,pr['id'],correction)
        rid=c.execute('''INSERT INTO bottle_returns(plant_id,customer_id,qty,returned_at,notes,created_by)
            VALUES(?,?,?,?,?,?)''',(u['plant_id'],item.customer_id,item.qty,day,item.notes,u['id'])).lastrowid
        c.execute('UPDATE bottle_products SET empty_stock=empty_stock+? WHERE id=?',(item.qty,pr['id']))
        sync_stock(c,u['plant_id'])
        c.execute('UPDATE bottle_returns SET product_id=? WHERE id=?',(pr['id'],rid))
        audit(c,u,'create','bottle_returns',rid)
    return {'id':rid,'qty':item.qty}


@app.get('/api/payments')
def list_payments(limit:int=Query(500,ge=1,le=500),offset:int=Query(0,ge=0),u=Depends(auth)):
    sql='''SELECT p.*,c.name AS customer_name FROM payments p JOIN customers c ON c.id=p.customer_id WHERE p.plant_id=?'''
    args=[u['plant_id']]
    if u['role']=='driver':
        sql+=' AND p.created_by=?';args.append(u['id'])
    sql+=' ORDER BY p.paid_at DESC,p.id DESC LIMIT ? OFFSET ?'
    args.extend([limit,offset])
    with db() as c:
        return [dict(r,amount=rs(r['amount_paisa'])) for r in c.execute(sql,args)]

@app.post('/api/payments')
def add_payment(item:PaymentIn,u=Depends(auth)):
    if u['role']=='driver' and item.delivery_id is None:
        raise HTTPException(403,'Drivers can only collect payment for their own delivered invoices')
    day=valid_date(item.paid_at)
    with transaction() as c:
        owned(c,'customers',item.customer_id,u['plant_id'])
        amount=money(item.amount)
        if item.delivery_id is not None:
            inv=owned(c,'deliveries',item.delivery_id,u['plant_id'])
            if inv['customer_id']!=item.customer_id:
                raise HTTPException(422,'Payment customer must match the invoice customer')
            if u['role']=='driver' and inv['driver_id']!=u['id']:
                raise HTTPException(403,'You cannot collect against another driver’s invoice')
        pid=c.execute('''INSERT INTO payments(plant_id,customer_id,delivery_id,amount_paisa,method,paid_at,notes,created_by)
                      VALUES(?,?,?,?,?,?,?,?)''',(u['plant_id'],item.customer_id,item.delivery_id,amount,item.method,day,item.notes,u['id'])).lastrowid
        reconcile_customer(c,u['plant_id'],item.customer_id)
        applied=c.execute('SELECT COALESCE(SUM(amount_paisa),0) FROM payment_allocations WHERE payment_id=?',(pid,)).fetchone()[0]
        account_balance=customer_stats(c,u['plant_id'],item.customer_id)[0]['balance_paisa']
        audit(c,u,'create','payments',pid)
    return {'id':pid,'allocated':rs(applied),'unallocated_to_invoices':rs(amount-applied),
            'customer_account_balance':rs(account_balance),'customer_advance_credit':rs(max(0,-account_balance))}

@app.get('/api/production')
def list_production(u=Depends(auth)):
    require(u,'owner','manager')
    with db() as c:
        return [dict(r) for r in c.execute('SELECT * FROM production WHERE plant_id=? ORDER BY produced_at DESC,id DESC LIMIT 300',(u['plant_id'],))]

@app.post('/api/production')
def add_production(item:ProductionIn,u=Depends(auth)):
    require(u,'owner','manager')
    day=valid_date(item.produced_at)
    with transaction() as c:
        pr=product_for(c,u['plant_id'],item.product_id)
        st=pr
        if item.qty>st['empty_stock']:
            raise HTTPException(409,'Insufficient empty bottles for production')
        pid=c.execute('''INSERT INTO production(plant_id,qty,tds,ph,produced_at,notes,created_by) VALUES(?,?,?,?,?,?,?)''',
                      (u['plant_id'],item.qty,item.tds,item.ph,day,item.notes,u['id'])).lastrowid
        c.execute('UPDATE bottle_products SET full_stock=full_stock+?,empty_stock=empty_stock-? WHERE id=?',(item.qty,item.qty,pr['id']))
        sync_stock(c,u['plant_id'])
        c.execute('UPDATE production SET product_id=? WHERE id=?',(pr['id'],pid))
        audit(c,u,'create','production',pid)
    return {'id':pid}

@app.get('/api/stock')
def stock(u=Depends(auth)):
    require(u,'owner','manager')
    with db() as c:
        plant=owned(c,'plants',u['plant_id'],u['plant_id'])
        circulating=c.execute('''SELECT COALESCE(SUM(opening_bottles),0) FROM customers WHERE plant_id=?''',(u['plant_id'],)).fetchone()[0]
        circulating+=c.execute('SELECT COALESCE(SUM(qty-empties),0) FROM deliveries WHERE plant_id=?',(u['plant_id'],)).fetchone()[0]
        circulating-=c.execute('SELECT COALESCE(SUM(qty),0) FROM bottle_returns WHERE plant_id=?',(u['plant_id'],)).fetchone()[0]
        moves=[dict(r) for r in c.execute('SELECT * FROM stock_movements WHERE plant_id=? ORDER BY id DESC LIMIT 50',(u['plant_id'],))]
    return {'full':plant['full_stock'],'empty':plant['empty_stock'],'damaged':plant['damaged_stock'],'at_customers':circulating,'movements':moves}

@app.post('/api/stock/adjust')
def adjust_stock(item:StockIn,u=Depends(auth)):
    require(u,'owner','manager')
    col={'full':'full_stock','empty':'empty_stock','damaged':'damaged_stock'}.get(item.kind)
    if col is None or item.qty==0:
        raise HTTPException(422,'Select stock type and nonzero adjustment')
    with transaction() as c:
        pr=product_for(c,u['plant_id'],item.product_id)
        st=pr
        if st[col]+item.qty<0:
            raise HTTPException(409,'Adjustment would create negative stock')
        c.execute(f'UPDATE bottle_products SET {col}={col}+? WHERE id=?',(item.qty,pr['id']))
        sync_stock(c,u['plant_id'])
        aid=c.execute('''INSERT INTO stock_movements(plant_id,kind,qty,reason,created_at,created_by)
            VALUES(?,?,?,?,?,?)''',(u['plant_id'],item.kind,item.qty,item.reason,now(),u['id'])).lastrowid
        c.execute('UPDATE stock_movements SET product_id=? WHERE id=?',(pr['id'],aid))
        audit(c,u,'adjust','stock_movements',aid)
    return {'id':aid}

@app.get('/api/expenses')
def expenses(u=Depends(auth)):
    require(u,'owner','manager')
    with db() as c:
        return [dict(r,amount=rs(r['amount_paisa'])) for r in c.execute('SELECT * FROM expenses WHERE plant_id=? ORDER BY incurred_at DESC,id DESC LIMIT 300',(u['plant_id'],))]

@app.post('/api/expenses')
def add_expense(item:ExpenseIn,u=Depends(auth)):
    require(u,'owner','manager')
    day=valid_date(item.incurred_at)
    with transaction() as c:
        eid=c.execute('''INSERT INTO expenses(plant_id,category,amount_paisa,incurred_at,notes,created_by)
          VALUES(?,?,?,?,?,?)''',(u['plant_id'],item.category,money(item.amount),day,item.notes,u['id'])).lastrowid
        audit(c,u,'create','expenses',eid)
    return {'id':eid}

@app.get('/api/quality')
def quality(u=Depends(auth)):
    require(u,'owner','manager')
    with db() as c:
        return [dict(r) for r in c.execute('SELECT * FROM quality WHERE plant_id=? ORDER BY tested_at DESC,id DESC LIMIT 200',(u['plant_id'],))]

@app.post('/api/quality')
def add_quality(item:QualityIn,u=Depends(auth)):
    require(u,'owner','manager')
    day=valid_date(item.tested_at)
    with transaction() as c:
        qid=c.execute('''INSERT INTO quality(plant_id,tds,ph,sample_point,tested_at,notes,created_by)
         VALUES(?,?,?,?,?,?,?)''',(u['plant_id'],item.tds,item.ph,item.sample_point,day,item.notes,u['id'])).lastrowid
        audit(c,u,'create','quality',qid)
    return {'id':qid}

@app.get('/api/maintenance')
def list_maintenance(u=Depends(auth)):
    require(u,'owner','manager')
    with db() as c:
        return [dict(r) for r in c.execute('SELECT * FROM maintenance WHERE plant_id=? ORDER BY due_date ASC,id DESC',(u['plant_id'],))]

@app.post('/api/maintenance')
def add_maintenance(item:MaintenanceIn,u=Depends(auth)):
    require(u,'owner','manager')
    day=valid_date(item.due_date)
    with transaction() as c:
        mid=c.execute('''INSERT INTO maintenance(plant_id,equipment,due_date,notes,created_by)
           VALUES(?,?,?,?,?)''',(u['plant_id'],item.equipment,day,item.notes,u['id'])).lastrowid
        audit(c,u,'create','maintenance',mid)
    return {'id':mid}

@app.post('/api/maintenance/{mid}/complete')
def complete_maintenance(mid:int,u=Depends(auth)):
    require(u,'owner','manager')
    with transaction() as c:
        owned(c,'maintenance',mid,u['plant_id'])
        c.execute('UPDATE maintenance SET completed_at=? WHERE id=?',(today(),mid))
        audit(c,u,'complete','maintenance',mid)
    return {'ok':True}

def overview(c,pid,start,end):
    def amt(table,col,dayfield):
        return c.execute(f'SELECT COALESCE(SUM({col}),0) FROM {table} WHERE plant_id=? AND {dayfield} BETWEEN ? AND ?',
                         (pid,start,end)).fetchone()[0]
    invoices=amt('deliveries','total_paisa','delivered_at')
    paid=amt('payments','amount_paisa','paid_at')
    spent=amt('expenses','amount_paisa','incurred_at')
    qty=amt('deliveries','qty','delivered_at')
    deliveries=c.execute('SELECT COUNT(*) FROM deliveries WHERE plant_id=? AND delivered_at BETWEEN ? AND ?',(pid,start,end)).fetchone()[0]
    pending=c.execute("SELECT COUNT(*) FROM orders WHERE plant_id=? AND status='pending'",(pid,)).fetchone()[0]
    outstanding=sum(int(r['balance_paisa']) for r in customer_stats(c,pid))
    return {'sales':rs(invoices),'collected':rs(paid),'expenses':rs(spent),'cash_after_expenses':rs(paid-spent),
            'bottles_delivered':qty,'delivery_count':deliveries,'pending_orders':pending,
            'customer_outstanding':rs(outstanding)}

@app.get('/api/dashboard')
def dashboard(u=Depends(auth)):
    require(u,'owner','manager')
    with db() as c:
        period=overview(c,u['plant_id'],today(),today())
        st=owned(c,'plants',u['plant_id'],u['plant_id'])
        customers=customer_stats(c,u['plant_id'])
        overdue=[dict(r) for r in c.execute('''SELECT * FROM maintenance WHERE plant_id=? AND completed_at IS NULL AND due_date<=? ORDER BY due_date LIMIT 5''',
                   (u['plant_id'],today()))]
        pending=[dict(r) for r in c.execute('''SELECT o.id,o.qty,o.requested_date,c.name AS customer_name FROM orders o JOIN customers c ON c.id=o.customer_id
           WHERE o.plant_id=? AND o.status='pending' ORDER BY o.requested_date,o.id LIMIT 8''',(u['plant_id'],))]
        return {'today':period,'stock':{'full':st['full_stock'],'empty':st['empty_stock'],'damaged':st['damaged_stock']},
                'customer_count':len(customers),'overdue_maintenance':overdue,'pending_orders':pending,
                'top_debtors':[{'id':r['id'],'name':r['name'],'balance':rs(r['balance_paisa'])} for r in sorted(customers,key=lambda x:-x['balance_paisa']) if r['balance_paisa']>0][:5]}

@app.get('/api/reports')
def reports(start:str=Query(default=None),end:str=Query(default=None),u=Depends(auth)):
    require(u,'owner','manager')
    a=valid_date(start or today()[:8]+'01');b=valid_date(end or today())
    if a>b:
        raise HTTPException(422,'Start date must be before end date')
    with db() as c:
        summary=overview(c,u['plant_id'],a,b)
        series=[dict(r,day=r['day'],sales=rs(r['sales']),bottles=r['bottles']) for r in c.execute('''SELECT delivered_at AS day,SUM(total_paisa) AS sales,SUM(qty) AS bottles FROM deliveries
          WHERE plant_id=? AND delivered_at BETWEEN ? AND ? GROUP BY delivered_at ORDER BY delivered_at''',(u['plant_id'],a,b))]
        methods=[dict(r,amount=rs(r['amount'])) for r in c.execute('''SELECT method,SUM(amount_paisa) AS amount FROM payments WHERE plant_id=? AND paid_at BETWEEN ? AND ? GROUP BY method ORDER BY amount DESC''',
                                                                    (u['plant_id'],a,b))]
    return {'start':a,'end':b,'summary':summary,'sales_by_day':series,'payment_methods':methods}

@app.get('/api/deliveries/{did}/invoice')
def invoice(did:int,u=Depends(auth)):
    with db() as c:
        r=owned(c,'deliveries',did,u['plant_id'])
        if u['role']=='driver' and r['driver_id']!=u['id']:
            raise HTTPException(403,'This is not your delivery')
        customer=owned(c,'customers',r['customer_id'],u['plant_id'])
        plant=owned(c,'plants',u['plant_id'],u['plant_id'])
        payments=c.execute('''SELECT COALESCE(SUM(a.amount_paisa),0) FROM payment_allocations a
                           JOIN payments p ON p.id=a.payment_id
                           WHERE p.plant_id=? AND a.delivery_id=?''',(u['plant_id'],did)).fetchone()[0]
        cash_on_delivery=c.execute('SELECT COALESCE(SUM(amount_paisa),0) FROM payments WHERE plant_id=? AND delivery_id=?',
                           (u['plant_id'],did)).fetchone()[0]
    with db() as c:
        current_balance = readable_customer(customer_stats(c,u['plant_id'],r['customer_id'])[0])['balance']
    return {'number':f'INV-{did:06d}','date':r['delivered_at'],'customer_id':r['customer_id'],'current_account_balance':current_balance,
           'plant':{'name':plant['name'],'phone':plant['phone'],'address':plant['address']},
           'customer':{'name':customer['name'],'phone':customer['phone'],'address':customer['address']},
           'product_id':r['product_id'],'product_name':r['product_name'],'litres':r['litres'],
           'qty':r['qty'],'empties':r['empties'],'unit_price':rs(r['unit_price_paisa']),
           'discount':rs(r['discount_paisa']),'total':rs(r['total_paisa']),'payment_on_delivery':rs(payments),'cash_received_at_delivery':rs(cash_on_delivery),
           'notes':r['notes']}

def invoice_pdf_bytes(data):
    """Generate a private, downloadable A4 invoice. No public invoice URLs or tokens."""
    out=io.BytesIO()
    pdf=canvas.Canvas(out,pagesize=(595.28,841.89),pageCompression=1)
    pdf.setTitle(f"AquaLedger {data['number']}")
    teal=HexColor('#067E82'); ink=HexColor('#152C42'); faint=HexColor('#738397'); bg=HexColor('#F1F8F8')
    pdf.setFillColor(teal);pdf.rect(0,724,595,118,fill=1,stroke=0)
    pdf.setFillColor(HexColor('#ffffff'));pdf.setFont('Helvetica-Bold',23)
    pdf.drawString(45,795,'AQUALEDGER  /  INVOICE')
    pdf.setFont('Helvetica',11)
    for i,line in enumerate(simpleSplit(data['plant']['name'],'Helvetica',11,470)[:2]):
        pdf.drawString(45,768-14*i,line)
    pdf.setFillColor(ink);pdf.setFont('Helvetica-Bold',18);pdf.drawString(45,676,data['number'])
    pdf.setFont('Helvetica',10);pdf.setFillColor(faint);pdf.drawString(45,655,'Date: '+data['date'])
    pdf.setFillColor(bg);pdf.roundRect(40,545,515,88,10,fill=1,stroke=0)
    pdf.setFillColor(ink);pdf.setFont('Helvetica-Bold',11);pdf.drawString(55,608,'BILLED TO')
    pdf.setFont('Helvetica-Bold',12);pdf.drawString(55,588,data['customer']['name'][:75])
    pdf.setFont('Helvetica',10);pdf.setFillColor(faint)
    pdf.drawString(55,570,(data['customer']['phone'] or 'No phone recorded')[:80])
    pdf.drawString(55,553,(data['customer']['address'] or '')[:85])
    pdf.setFillColor(ink)
    rows=[(f"{data.get('litres',19):g}L bottles delivered",str(data['qty'])),('Empty bottles returned',str(data['empties'])),
          ('Price per bottle',f"PKR {data['unit_price']:,.2f}"),('Discount',f"PKR {data['discount']:,.2f}"),
          ('Invoice total',f"PKR {data['total']:,.2f}"),('Cash received during delivery',f"PKR {data['cash_received_at_delivery']:,.2f}"),('Allocated to this invoice',f"PKR {data['payment_on_delivery']:,.2f}"),
          ('Invoice unpaid amount',f"PKR {data['total']-data['payment_on_delivery']:,.2f}")]
    y=510
    for i,(name,val) in enumerate(rows):
        if i in (4,7):pdf.setFillColor(bg);pdf.roundRect(40,y-10,515,35,6,stroke=0,fill=1)
        pdf.setFillColor(ink);pdf.setFont('Helvetica-Bold' if i in (4,7) else 'Helvetica',11)
        pdf.drawString(55,y+3,name);pdf.drawRightString(540,y+3,val)
        y-=49
    pdf.setFont('Helvetica',10);pdf.setFillColor(faint)
    pdf.drawString(45,125,'Current customer account balance (all transactions):')
    pdf.setFont('Helvetica-Bold',11);pdf.setFillColor(ink)
    pdf.drawRightString(540,125,f"PKR {data['current_account_balance']:,.2f}")
    pdf.setFont('Helvetica',9);pdf.setFillColor(faint)
    pdf.drawString(45,107,'Account balance may include other invoices, older dues, or advance payments.')
    pdf.drawString(45,69,'Thank you for choosing '+data['plant']['name'][:75])
    pdf.drawString(45,53,(data['plant']['phone'] or '')[:75])
    pdf.save()
    return out.getvalue()

@app.get('/api/deliveries/{did}/invoice.pdf')
def invoice_pdf(did:int,u=Depends(auth)):
    data=invoice(did,u)
    payload=invoice_pdf_bytes(data)
    return StreamingResponse(io.BytesIO(payload),media_type='application/pdf',headers={
        'Content-Disposition':f'attachment; filename="{data["number"]}.pdf"',
        'Cache-Control':'no-store'})

@app.get('/api/insights')
def smart_insights(u=Depends(auth)):
    require(u,'owner','manager')
    pid=u['plant_id'];nowday=today()
    with db() as c:
        custs=customer_stats(c,pid)
        debtor_ids=[x['id'] for x in custs if x['balance_paisa']>0]
        debtor_total=sum(x['balance_paisa'] for x in custs if x['balance_paisa']>0)
        stock=owned(c,'plants',pid,pid)
        pending=c.execute("SELECT COUNT(*) FROM orders WHERE plant_id=? AND status='pending' AND requested_date<?",(pid,nowday)).fetchone()[0]
        maintenance=c.execute('SELECT COUNT(*) FROM maintenance WHERE plant_id=? AND completed_at IS NULL AND due_date<=?',(pid,nowday)).fetchone()[0]
        latest=c.execute('SELECT tested_at FROM quality WHERE plant_id=? ORDER BY tested_at DESC LIMIT 1',(pid,)).fetchone()
        debtors=sorted(({'id':x['id'],'name':x['name'],'phone':x['phone'],'balance':rs(x['balance_paisa']),
                         'bottles':x['bottles_out']} for x in custs if x['balance_paisa']>0),key=lambda x:-x['balance'])[:20]
        return {'debtors':debtors,'positive_receivables':rs(debtor_total),'debtor_count':len(debtor_ids),
            'low_full_stock':stock['full_stock']<20,'full_stock':stock['full_stock'],'old_pending_orders':pending,
            'maintenance_due':maintenance,'last_quality_test':latest['tested_at'] if latest else None,
            'customers_with_bottles':sum(1 for x in custs if x['bottles_out']>0)}

@app.get('/api/backup')
def backup(u=Depends(auth)):
    require(u,'owner')
    tables=['plants','customers','routes','subscriptions','orders','deliveries','payments','production',
            'bottle_products','customer_bottles','customer_prices','stock_movements','bottle_returns','payment_allocations','expenses','quality','maintenance','audit','staff']
    pid=u['plant_id']
    exported=now()
    def body():
        yield '{"format":"AquaLedger Pro read-only export v1","exported_at":'+json.dumps(exported)+',"data":{'
        with db() as c:
            for index,table in enumerate(tables):
                if index:yield ','
                yield json.dumps(table)+':['
                if table=='staff':
                    cur=c.execute('SELECT id,name,username,email,role,active FROM users WHERE plant_id=?',(pid,))
                elif table=='payment_allocations':
                    cur=c.execute('''SELECT a.* FROM payment_allocations a JOIN payments p ON p.id=a.payment_id
                        WHERE p.plant_id=?''',(pid,))
                else:
                    cur=c.execute(f'SELECT * FROM {table} WHERE {"id" if table=="plants" else "plant_id"}=?',(pid,))
                first=True
                while True:
                    batch=cur.fetchmany(250)
                    if not batch:break
                    for row in batch:
                        if not first:yield ','
                        yield json.dumps(dict(row),ensure_ascii=False,separators=(',',':'))
                        first=False
                yield ']'
        yield '}}'
    return StreamingResponse(body(),media_type='application/json',
          headers={'Content-Disposition':f'attachment; filename="aqualedger_export_{today()}.json"'})

@app.get('/api/export/{kind}')
def export_csv(kind:str,u=Depends(auth)):
    require(u,'owner','manager')
    queries={
        'customers':'SELECT id,name,phone,area,address,price_paisa,opening_balance_paisa,opening_bottles FROM customers WHERE plant_id=?',
        'deliveries':'SELECT * FROM deliveries WHERE plant_id=?',
        'payments':'SELECT * FROM payments WHERE plant_id=?',
        'expenses':'SELECT * FROM expenses WHERE plant_id=?'}
    if kind not in queries:
        raise HTTPException(404,'Unknown export')
    def content():
        with db() as c:
            cur=c.execute(queries[kind],(u['plant_id'],))
            headers=[x[0] for x in cur.description]
            buf=io.StringIO();writer=csv.writer(buf);writer.writerow(headers)
            yield '\ufeff'+buf.getvalue()  # Excel-compatible UTF-8 for Urdu/English values.
            while True:
                batch=cur.fetchmany(500)
                if not batch:break
                buf=io.StringIO();writer=csv.writer(buf)
                for row in batch:
                    values=[]
                    for field in headers:
                        v=row[field]
                        if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')):
                            v="'"+v
                        values.append(v)
                    writer.writerow(values)
                yield buf.getvalue()
    return StreamingResponse(content(),media_type='text/csv; charset=utf-8',
          headers={'Content-Disposition':f'attachment; filename="{kind}_{today()}.csv"'})

@app.get('/api/audit')
def list_audit(u=Depends(auth)):
    require(u,'owner')
    with db() as c:
        return [dict(r) for r in c.execute('''SELECT a.*,u.name AS by_name FROM audit a LEFT JOIN users u ON u.id=a.user_id
          WHERE a.plant_id=? ORDER BY a.id DESC LIMIT 150''',(u['plant_id'],))]

app.mount('/static',StaticFiles(directory=str(BASE/'static')),name='static')

@app.get('/admin')
def platform_index():
    return FileResponse(BASE/'static'/'admin.html')

@app.get('/')
def index():
    return FileResponse(str(BASE/'static'/'index.html'))

@app.post('/api/demo/fill')
def fill_demo(u=Depends(auth)):
    """Populate a new/empty workspace with illustrative data. Never use on real accounts."""
    require(u,'owner')
    pid=u['plant_id']
    with transaction() as c:
        for table in ('customers','deliveries','orders','payments','production','stock_movements','expenses'):
            if c.execute(f'SELECT 1 FROM {table} WHERE plant_id=? LIMIT 1',(pid,)).fetchone():
                raise HTTPException(409,'Demo data is only available in a completely empty plant workspace')
        st=owned(c,'plants',pid,pid)
        if st['full_stock'] or st['empty_stock'] or st['damaged_stock']:
            raise HTTPException(409,'Demo data requires zero starting stock')
        c.execute('UPDATE plants SET full_stock=150, empty_stock=250 WHERE id=?',(pid,))
        c.execute('''INSERT INTO stock_movements(plant_id,kind,qty,reason,created_at,created_by) VALUES(?,?,?,?,?,?)''',
                  (pid,'full',150,'Illustrative opening stock',now(),u['id']))
        c.execute('''INSERT INTO stock_movements(plant_id,kind,qty,reason,created_at,created_by) VALUES(?,?,?,?,?,?)''',
                  (pid,'empty',250,'Illustrative opening stock',now(),u['id']))
        ids=[]
        names=[('Al-Falah Pharmacy','Clifton','03001234567',140),('Mariam Residence','DHA Phase 5','03011234567',130),
               ('ABC Trading Office','Shahrah-e-Faisal','03021234567',150),('Hasan Family','Gulshan-e-Iqbal','03031234567',120),
               ('Qureshi Medical Centre','Saddar','03041234567',160),('TechPoint Offices','PECHS','03051234567',150),
               ('Noor Boutique','Tariq Road','03061234567',130),('Kashif Residence','Bahadurabad','03071234567',125)]
        for name,area,phone,price in names:
            ids.append(c.execute('''INSERT INTO customers(plant_id,name,area,phone,address,price_paisa,created_at)
                                 VALUES(?,?,?,?,?,?,?)''',(pid,name,area,phone,area+', Karachi',price*100,now())).lastrowid)
        route1=c.execute('INSERT INTO routes(plant_id,name) VALUES(?,?)',(pid,'DHA / Clifton')).lastrowid
        route2=c.execute('INSERT INTO routes(plant_id,name) VALUES(?,?)',(pid,'Gulshan / PECHS')).lastrowid
        for index in (0,1,2,3,4,5):
            q=3 if index in (2,4) else 2
            price=names[index][3]*100
            day=(date.fromisoformat(today())-timedelta(days=index%3)).isoformat()
            did=c.execute('''INSERT INTO deliveries(plant_id,customer_id,qty,empties,unit_price_paisa,total_paisa,delivered_at,created_by,route_id)
                         VALUES(?,?,?,?,?,?,?,?,?)''',(pid,ids[index],q,0,price,price*q,day,u['id'],route1 if index<2 else route2)).lastrowid
            c.execute('UPDATE plants SET full_stock=full_stock-? WHERE id=?',(q,pid))
            if index in (0,2,3,5):
                c.execute('''INSERT INTO payments(plant_id,customer_id,delivery_id,amount_paisa,method,paid_at,created_by)
                             VALUES(?,?,?,?,?,?,?)''',(pid,ids[index],did,price*q,'Cash' if index%2==0 else 'Easypaisa',day,u['id']))
        for index in (0,2,4,6):
            c.execute('''INSERT INTO orders(plant_id,customer_id,qty,requested_date,route_id,created_at) VALUES(?,?,?,?,?,?)''',
                      (pid,ids[index],2,today(),route1 if index==0 else route2,now()))
        for index in (0,1,4):
            c.execute('INSERT INTO subscriptions(plant_id,customer_id,qty,weekdays,route_id) VALUES(?,?,?,?,?)',
                      (pid,ids[index],2,'0,2,4',route1))
        for category,amount in [('Fuel',1400),('Electricity',3400),('Filter & membrane',1900)]:
            c.execute('INSERT INTO expenses(plant_id,category,amount_paisa,incurred_at,created_by) VALUES(?,?,?,?,?)',
                      (pid,category,amount*100,today(),u['id']))
        c.execute('INSERT INTO quality(plant_id,tds,ph,sample_point,tested_at,created_by) VALUES(?,?,?,?,?,?)',
                  (pid,82,7.1,'Plant outlet',today(),u['id']))
        c.execute('INSERT INTO maintenance(plant_id,equipment,due_date,notes,created_by) VALUES(?,?,?,?,?)',
                  (pid,'Sediment pre-filter',(date.fromisoformat(today())+timedelta(days=7)).isoformat(),'Check pressure differential before replacement',u['id']))
        for cid in ids:
            reconcile_customer(c,pid,cid)
        pr=product_for(c,pid)
        c.execute('UPDATE bottle_products SET full_stock=(SELECT full_stock FROM plants WHERE id=?),empty_stock=250 WHERE id=?',(pid,pr['id']))
        for table in PRODUCT_TABLES:c.execute(f'UPDATE {table} SET product_id=? WHERE plant_id=? AND product_id IS NULL',(pr['id'],pid))
        audit(c,u,'seed_sample','plants',pid)
    return {'ok':True,'customers':len(ids)}

class CustomerEdit(BaseModel):
    name: str = Field(min_length=2,max_length=100)
    phone: str = Field(default='',max_length=40)
    area: str = Field(default='',max_length=80)
    address: str = Field(default='',max_length=240)
    price: float = Field(ge=0,le=1000000)

@app.put('/api/customers/{cid}')
def edit_customer(cid:int,item:CustomerEdit,u=Depends(auth)):
    require(u,'owner','manager')
    with transaction() as c:
        owned(c,'customers',cid,u['plant_id'])
        c.execute('''UPDATE customers SET name=?,phone=?,area=?,address=?,price_paisa=? WHERE id=?''',
          (item.name.strip(),item.phone,item.area,item.address,money(item.price),cid))
        audit(c,u,'update','customers',cid)
    return {'ok':True}

class ChangePassword(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8,max_length=200)

@app.post('/api/auth/change-password')
def change_password(item:ChangePassword,u=Depends(auth)):
    with transaction() as c:
        current=owned(c,'users',u['id'],u['plant_id'])
        if not check_pw(item.current_password,current['password_hash']):
            raise HTTPException(403,'Current password is incorrect')
        c.execute('UPDATE users SET password_hash=?,session_version=session_version+1 WHERE id=?',(hash_pw(item.new_password),u['id']))
        audit(c,u,'change_password','users',u['id'])
    return {'ok':True}

@app.post('/api/staff/{sid}/toggle')
def toggle_staff(sid:int,u=Depends(auth)):
    require(u,'owner')
    if sid==u['id']:
        raise HTTPException(409,'Cannot disable your own owner account')
    with transaction() as c:
        row=owned(c,'users',sid,u['plant_id'])
        if row['role']=='owner':
            raise HTTPException(403,'Cannot disable another owner')
        c.execute('UPDATE users SET active=?,session_version=session_version+1 WHERE id=?',(0 if row['active'] else 1,sid))
        audit(c,u,'toggle','users',sid)
    return {'ok':True}

class ResetPassword(BaseModel):
    new_password: str = Field(min_length=8,max_length=200)

@app.post('/api/staff/{sid}/reset-password')
def reset_staff_password(sid:int,item:ResetPassword,u=Depends(auth)):
    require(u,'owner')
    with transaction() as c:
        row=owned(c,'users',sid,u['plant_id'])
        if row['role']=='owner':
            raise HTTPException(403,'Owner password must be changed by the owner')
        c.execute('UPDATE users SET password_hash=?,session_version=session_version+1 WHERE id=?',(hash_pw(item.new_password),sid))
        audit(c,u,'reset_password','users',sid)
    return {'ok':True}


migrate_v5()

class ProductIn(BaseModel):
    name: str = Field(min_length=2,max_length=60)
    litres: float = Field(gt=0,le=1000,allow_inf_nan=False)
    price: float = Field(ge=0,le=1000000,allow_inf_nan=False)
    low_stock: int = Field(default=20,ge=0,le=100000)
    active: bool = True

@app.get('/api/products')
def list_products(u=Depends(auth)):
    with db() as c:
        rows=c.execute('SELECT * FROM bottle_products WHERE plant_id=? ORDER BY litres,id',(u['plant_id'],)).fetchall()
        result=[]
        for row in rows:
            r=dict(row)
            r['price']=rs(r.pop('price_paisa'))
            r['at_customers']=c.execute('SELECT COALESCE(SUM(opening),0) FROM customer_bottles WHERE plant_id=? AND product_id=?',(u['plant_id'],r['id'])).fetchone()[0]
            r['at_customers']+=c.execute('SELECT COALESCE(SUM(qty-empties),0) FROM deliveries WHERE plant_id=? AND product_id=?',(u['plant_id'],r['id'])).fetchone()[0]
            r['at_customers']-=c.execute('SELECT COALESCE(SUM(qty),0) FROM bottle_returns WHERE plant_id=? AND product_id=?',(u['plant_id'],r['id'])).fetchone()[0]
            r['low_stock_alert']=bool(r['active'] and r['full_stock']<r['low_stock'])
            result.append(r)
        return result

@app.post('/api/products',status_code=201)
def add_product(item:ProductIn,u=Depends(auth)):
    require(u,'owner','manager')
    name=item.name.strip()
    if len(name)<2:raise HTTPException(422,'Enter a bottle name of at least two characters')
    with transaction() as c:
        if c.execute('SELECT 1 FROM bottle_products WHERE plant_id=? AND name=?',(u['plant_id'],name)).fetchone():
            raise HTTPException(409,'A bottle with this name already exists')
        pid=c.execute('INSERT INTO bottle_products(plant_id,name,litres,price_paisa,low_stock,active) VALUES(?,?,?,?,?,?)',
                     (u['plant_id'],name,item.litres,money(item.price),item.low_stock,int(item.active))).lastrowid
        audit(c,u,'create','bottle_products',pid)
    return {'id':pid}

@app.put('/api/products/{product_id}')
def edit_product(product_id:int,item:ProductIn,u=Depends(auth)):
    require(u,'owner','manager')
    name=item.name.strip()
    if len(name)<2:raise HTTPException(422,'Enter a bottle name of at least two characters')
    with transaction() as c:
        pr=product_for(c,u['plant_id'],product_id,active=False)
        if item.litres!=pr['litres']:raise HTTPException(409,'Capacity cannot change. Add a new bottle size to preserve stock and history.')
        if c.execute('SELECT 1 FROM bottle_products WHERE plant_id=? AND name=? AND id<>?',(u['plant_id'],name,product_id)).fetchone():
            raise HTTPException(409,'A bottle with this name already exists')
        if not item.active:
            if pr['litres']==19 and product_id==product_for(c,u['plant_id'],active=False)['id']:
                raise HTTPException(409,'Keep the default 19L size active for existing records and opening balances')
            if any(pr[k] for k in ('full_stock','empty_stock','damaged_stock')):
                raise HTTPException(409,'Reconcile remaining stock before archiving this size')
            out=c.execute('SELECT COALESCE(SUM(opening),0) FROM customer_bottles WHERE product_id=?',(product_id,)).fetchone()[0]
            out+=c.execute('SELECT COALESCE(SUM(qty-empties),0) FROM deliveries WHERE product_id=?',(product_id,)).fetchone()[0]
            out-=c.execute('SELECT COALESCE(SUM(qty),0) FROM bottle_returns WHERE product_id=?',(product_id,)).fetchone()[0]
            if out or c.execute("SELECT 1 FROM orders WHERE product_id=? AND status='pending'",(product_id,)).fetchone() or c.execute('SELECT 1 FROM subscriptions WHERE product_id=? AND active=1',(product_id,)).fetchone():
                raise HTTPException(409,'Resolve circulating bottles, pending orders and active recurring plans before archiving')
        c.execute('UPDATE bottle_products SET name=?,price_paisa=?,low_stock=?,active=? WHERE id=?',(name,money(item.price),item.low_stock,int(item.active),product_id))
        audit(c,u,'update','bottle_products',product_id)
    return {'ok':True}

class CustomerPriceIn(BaseModel):
    price: float = Field(ge=0,le=1000000,allow_inf_nan=False)

@app.get('/api/customers/{cid}/bottle-sizes')
def customer_sizes(cid:int,u=Depends(auth)):
    with db() as c:
        customer=owned(c,'customers',cid,u['plant_id'])
        if u['role']=='driver' and not c.execute('SELECT 1 FROM orders WHERE plant_id=? AND customer_id=? AND driver_id=?',(u['plant_id'],cid,u['id'])).fetchone():
            raise HTTPException(403,'Customer is not assigned to you')
        default=product_for(c,u['plant_id'],active=False)['id']
        result=[]
        for pr in c.execute('SELECT * FROM bottle_products WHERE plant_id=? ORDER BY litres,id',(u['plant_id'],)).fetchall():
            override=c.execute('SELECT price_paisa FROM customer_prices WHERE customer_id=? AND product_id=?',(cid,pr['id'])).fetchone()
            price=override[0] if override else customer['price_paisa'] if pr['id']==default else pr['price_paisa']
            result.append({'id':pr['id'],'name':pr['name'],'litres':pr['litres'],'price':rs(price),'custom_price':bool(override),
                           'bottles_out':bottle_balance(c,u['plant_id'],cid,pr['id'])})
        return result

@app.put('/api/customers/{cid}/prices/{product_id}')
def customer_price(cid:int,product_id:int,item:CustomerPriceIn,u=Depends(auth)):
    require(u,'owner','manager')
    with transaction() as c:
        owned(c,'customers',cid,u['plant_id']);product_for(c,u['plant_id'],product_id)
        c.execute('''INSERT INTO customer_prices VALUES(?,?,?,?) ON CONFLICT(customer_id,product_id)
                    DO UPDATE SET price_paisa=excluded.price_paisa''',(u['plant_id'],cid,product_id,money(item.price)))
        audit(c,u,'price_product_'+str(product_id),'customers',cid)
    return {'ok':True}

@app.get('/api/reports/bottle-sizes')
def bottle_report(start:str='',end:str='',u=Depends(auth)):
    require(u,'owner','manager')
    a=valid_date(start or today()[:8]+'01');b=valid_date(end or today())
    if a>b:raise HTTPException(422,'Start date must be before end date')
    with db() as c:
        return [dict(r,sales=rs(r['sales_paisa'])) for r in c.execute('''SELECT product_name,litres,
          SUM(qty) AS bottles,SUM(qty*litres) AS litres_delivered,SUM(total_paisa) AS sales_paisa
          FROM deliveries WHERE plant_id=? AND delivered_at BETWEEN ? AND ?
          GROUP BY product_id,product_name,litres ORDER BY sales_paisa DESC''',(u['plant_id'],a,b))]
