"""Security, upgrade and non-empty dataset regression coverage."""
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient
from tests.test_api import client,ok,signup
import main


def test_8_character_password_username_and_optional_email(client):
    setup=client.post('/api/platform/plants',headers=client.platform_headers,json={
        'plant':'Al Noor Water','name':'Owner Test','username':'alnoor123','password':'Abc12345'})
    assert setup.status_code==201,setup.text
    login=client.post('/api/auth/login',json={'identifier':'alnoor123','password':'Abc12345'})
    assert login.status_code==200
    h={'Authorization':'Bearer '+login.json()['token']}
    who=ok(client.get('/api/me',headers=h))
    assert who['email'] is None and who['username']=='alnoor123'
    staff=client.post('/api/staff',headers=h,json={'name':'Driver Ali','username':'driver-ali','password':'GoodPass8','role':'driver'})
    assert staff.status_code==200,staff.text
    assert ok(client.post('/api/auth/login',json={'identifier':'driver-ali','password':'GoodPass8'}))['role']=='driver'
    assert client.post('/api/staff',headers=h,json={'name':'Duplicate','username':'driver-ali','password':'Another88','role':'driver'}).status_code==409
    assert client.post('/api/staff',headers=h,json={'name':'Invalid','username':'new-staff','email':'wrong-address','password':'Another88','role':'driver'}).status_code==422
    assert client.post('/api/platform/plants',headers=client.platform_headers,json={'plant':'A','name':'Someone','username':'bad','password':'1234567'}).status_code==422


def test_public_signup_permanently_disabled(client,monkeypatch):
    assert ok(client.get('/api/public/config'))['registration_open'] is False
    for allow in (None,'1'):
        if allow:monkeypatch.setenv('AQUA_ALLOW_EXTRA_PLANTS',allow)
        else:monkeypatch.delenv('AQUA_ALLOW_EXTRA_PLANTS',raising=False)
        denied=client.post('/api/auth/register',json={'plant':'Bad Plant','name':'Other','username':'friend2','password':'12345678'})
        assert denied.status_code==403


def test_throttling_password_revocation_and_headers(client):
    owner=client.post('/api/platform/plants',headers=client.platform_headers,json={'plant':'Rate Plant','name':'Owner','username':'rateowner','password':'GoodPass8'})
    assert owner.status_code==201
    owner_login=ok(client.post('/api/auth/login',json={'identifier':'rateowner','password':'GoodPass8'}))
    h={'Authorization':'Bearer '+owner_login['token']}
    for i in range(5):
        assert client.post('/api/auth/login',json={'identifier':'rateowner','password':'incorrect'}).status_code==401
    assert client.post('/api/auth/login',json={'identifier':'rateowner','password':'GoodPass8'}).status_code==429
    assert ok(client.post('/api/auth/change-password',headers=h,json={'current_password':'GoodPass8','new_password':'NewPassword8'}))['ok']
    assert client.get('/api/me',headers=h).status_code==401
    r=client.get('/')
    assert 'frame-ancestors' in r.headers['content-security-policy']
    assert r.headers['x-content-type-options']=='nosniff'
    assert client.get('/api/me',headers={'Host':'attacker.example','Authorization':'Bearer dead'}).status_code==400
    assert client.post('/api/auth/login',headers={'Content-Length':'2000000'},content=b'{}').status_code==413
    assert client.get('/api/docs').status_code==404


def test_driver_isolation_and_invoice_limits(client):
    owner=signup(client,'driver-owner@example.com','Driver Plant')
    c1=ok(client.post('/api/customers',headers=owner,json={'name':'Assigned customer','price':100}))['id']
    c2=ok(client.post('/api/customers',headers=owner,json={'name':'Private customer','price':100}))['id']
    ok(client.post('/api/staff',headers=owner,json={'name':'Driver','username':'driver-run','password':'PassWord8','role':'driver'}))
    row=next(x for x in ok(client.get('/api/staff',headers=owner)) if x['username']=='driver-run')
    d={'Authorization':'Bearer '+ok(client.post('/api/auth/login',json={'identifier':'driver-run','password':'PassWord8'}))['token']}
    ok(client.post('/api/stock/adjust',headers=owner,json={'kind':'full','qty':10,'reason':'Opening'}))
    oid=ok(client.post('/api/orders',headers=owner,json={'customer_id':c1,'qty':2,'requested_date':main.today(),'driver_id':row['id']}))['id']
    visible=ok(client.get('/api/customers',headers=d))
    assert [x['id'] for x in visible]==[c1]
    assert visible[0]['balance']==0
    assert client.get(f'/api/customers/{c1}/ledger',headers=d).status_code==403
    assert client.get('/api/stock',headers=d).status_code==403
    assert client.post('/api/deliveries',headers=d,json={'customer_id':c2,'qty':1,'empties':0,'unit_price':100,'delivered_at':main.today()}).status_code==403
    assert client.post('/api/payments',headers=d,json={'customer_id':c2,'amount':100,'paid_at':main.today()}).status_code==403
    overpay=ok(client.post('/api/deliveries',headers=owner,json={'customer_id':c1,'qty':2,'empties':0,'unit_price':100,'paid_now':201,'delivered_at':main.today()}))
    assert ok(client.get(f'/api/deliveries/{overpay["id"]}/invoice',headers=owner))['payment_on_delivery']==200
    assert ok(client.get('/api/customers',headers=owner))[0]['balance']==-1
    made=ok(client.post('/api/deliveries',headers=d,json={'customer_id':c1,'order_id':oid,'qty':2,'empties':0,'unit_price':100,'delivered_at':main.today()}))
    assert made['total']==200
    assert client.post('/api/payments',headers=d,json={'customer_id':c2,'delivery_id':made['id'],'amount':100,'paid_at':main.today()}).status_code==422
    ok(client.post('/api/payments',headers=d,json={'customer_id':c1,'delivery_id':made['id'],'amount':100,'paid_at':main.today()}))


def test_large_customer_summary_and_pagination(client):
    h=signup(client,'bulk-owner@example.com','Bulk Plant')
    # Insert transactions directly to isolate database query performance from password/HTTP overhead.
    with main.transaction() as c:
        pid=c.execute('SELECT id FROM plants').fetchone()[0]
        cust=[(pid,f'Customer {i:05}',10000,'2026-01-01') for i in range(6000)]
        c.executemany('INSERT INTO customers(plant_id,name,price_paisa,created_at) VALUES(?,?,?,?)',cust)
        c.executemany('INSERT INTO deliveries(plant_id,customer_id,qty,empties,unit_price_paisa,total_paisa,delivered_at) VALUES(?,?,?,?,?,?,?)',
          ((pid,i,1,0,10000,10000,'2026-01-01') for i in range(1,6001)))
        c.executemany('INSERT INTO payments(plant_id,customer_id,amount_paisa,method,paid_at) VALUES(?,?,?,?,?)',
          ((pid,i,5000,'Cash','2026-01-02') for i in range(1,6001)))
    started=time.monotonic()
    r=client.get('/api/customers',headers=h)
    assert r.status_code==200,r.text[:100]
    customers=r.json()
    assert len(customers)==6000
    assert customers[0]['balance']==50
    led=ok(client.get('/api/customers/1/ledger?limit=1&offset=0',headers=h))
    assert len(led['entries'])==1
    assert client.get('/api/customers/1/ledger?limit=10000',headers=h).status_code==422
    d=ok(client.get('/api/dashboard',headers=h))
    assert d['customer_count']==6000
    assert d['today']['customer_outstanding']==300000
    assert time.monotonic()-started<20


def test_v2_database_migration_preserves_records(tmp_path):
    # Self-contained v2-style users table: no external old checkout required.
    import sqlite3
    import main
    new=Path(__file__).resolve().parent.parent
    datadir=tmp_path/'legacy-data';datadir.mkdir()
    env={**os.environ,'AQUA_DATA_DIR':str(datadir)}
    with sqlite3.connect(datadir/'aqualedger.sqlite3') as c:
        c.executescript(main.SCHEMA)
        c.execute('DROP TABLE users')
        c.execute("""CREATE TABLE users(id INTEGER PRIMARY KEY,plant_id INTEGER NOT NULL REFERENCES plants(id),
          name TEXT NOT NULL,email TEXT NOT NULL UNIQUE,password_hash TEXT NOT NULL,
          role TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1)""")
        c.execute("INSERT INTO plants(id,name,full_stock,created_at) VALUES(1,'Old Water',20,?)",(main.now(),))
        c.execute("INSERT INTO users(id,plant_id,name,email,password_hash,role) VALUES(1,1,'Old Owner','old@example.com',?,'owner')",(main.hash_pw('oldpassword123'),))
        c.execute("INSERT INTO customers(plant_id,name,price_paisa,created_at) VALUES(1,'Existing Customer',12500,?)",(main.now(),))
    migrate_code="""import main
from fastapi.testclient import TestClient
c=TestClient(main.app)
r=c.post('/api/auth/login',json={'identifier':'old@example.com','password':'oldpassword123'})
assert r.status_code==200,r.text
h={'Authorization':'Bearer '+r.json()['token']}
cs=c.get('/api/customers',headers=h).json();assert len(cs)==1 and cs[0]['name']=='Existing Customer'
assert c.get('/api/stock',headers=h).json()['full']==20
import sqlite3
with sqlite3.connect(main.DB_PATH) as con:assert not con.execute('PRAGMA foreign_key_check').fetchall()
"""
    subprocess.run([sys.executable,'-c',migrate_code],cwd=new,env=env,check=True,capture_output=True,text=True)
    assert list((datadir/'backups').glob('pre_v3_migration_*.sqlite3'))
