import pytest
from fastapi.testclient import TestClient
import main

@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(main,'DB_PATH',tmp_path/'test.sqlite3')
    with main.db() as c:
        c.executescript(main.SCHEMA)
        c.executescript("""CREATE TABLE IF NOT EXISTS platform_admins (
         id INTEGER PRIMARY KEY, name TEXT NOT NULL,
         username TEXT NOT NULL UNIQUE COLLATE NOCASE, password_hash TEXT NOT NULL,
         active INTEGER NOT NULL DEFAULT 1,session_version INTEGER NOT NULL DEFAULT 0,
         created_at TEXT NOT NULL);
         CREATE TABLE IF NOT EXISTS platform_audit (
         id INTEGER PRIMARY KEY,admin_id INTEGER REFERENCES platform_admins(id),
         action TEXT NOT NULL,plant_id INTEGER REFERENCES plants(id),
         subject_id INTEGER,created_at TEXT NOT NULL);
         """)
        cols={r['name'] for r in c.execute('PRAGMA table_info(plants)')}
        if 'active' not in cols:c.execute('ALTER TABLE plants ADD COLUMN active INTEGER NOT NULL DEFAULT 1')
        c.execute('INSERT INTO platform_admins(name,username,password_hash,created_at) VALUES(?,?,?,?)',
          ('Test Platform','platform-test',main.hash_pw('SecureMaster123!'),main.now()))
    main.migrate_v5()
    c=TestClient(main.app)
    r=c.post('/api/platform/login',json={'username':'platform-test','password':'SecureMaster123!'})
    assert r.status_code==200,r.text
    c.platform_headers={'Authorization':'Bearer '+r.json()['token']}
    return c

def signup(client,email,plant):
    username=email.split('@')[0].replace('.','-')
    response=client.post('/api/platform/plants',headers=client.platform_headers,json={
        'plant':plant,'name':'Test Owner','username':username,'email':email,'password':'a-secure-password-123'})
    assert response.status_code==201,response.text
    signed=client.post('/api/auth/login',json={'identifier':username,'password':'a-secure-password-123'})
    assert signed.status_code==200,signed.text
    return {'Authorization':'Bearer '+signed.json()['token']}

def ok(response):
    assert response.status_code==200,response.text
    return response.json()

def test_register_login_and_tenant_isolation(client):
    a=signup(client,'owner-a@example.com','Plant A')
    b=signup(client,'owner-b@example.com','Plant B')
    cust=ok(client.post('/api/customers',headers=a,json={'name':'Customer A','price':135}))['id']
    assert len(ok(client.get('/api/customers',headers=a)))==1
    assert ok(client.get('/api/customers',headers=b))==[]
    assert client.get(f'/api/customers/{cust}/ledger',headers=b).status_code==404
    assert client.post('/api/payments',headers=b,json={'customer_id':cust,'amount':30,'paid_at':main.today()}).status_code==404
    login=client.post('/api/auth/login',json={'email':'owner-a@example.com','password':'a-secure-password-123'})
    assert login.status_code==200
    assert client.get('/api/customers').status_code==401

def test_stock_customer_debt_ledger_and_production(client):
    h=signup(client,'inventory@example.com','Sample Plant')
    customer=ok(client.post('/api/customers',headers=h,json={'name':'Ali Hassan','price':120,'opening_bottles':3}))['id']
    # No stock -> blocked
    payload={'customer_id':customer,'qty':5,'empties':3,'unit_price':120,'discount':20,'paid_now':200,'delivered_at':main.today()}
    assert client.post('/api/deliveries',headers=h,json=payload).status_code==409
    ok(client.post('/api/stock/adjust',headers=h,json={'kind':'full','qty':20,'reason':'Opening stock'}))
    ok(client.post('/api/stock/adjust',headers=h,json={'kind':'empty','qty':10,'reason':'Opening empties'}))
    row=ok(client.post('/api/deliveries',headers=h,json=payload))
    assert row['total']==580
    stocked=ok(client.get('/api/stock',headers=h))
    assert (stocked['full'],stocked['empty'],stocked['at_customers'])==(15,13,5)
    cust=ok(client.get('/api/customers',headers=h))[0]
    assert cust['balance']==380 and cust['bottles_out']==5
    ok(client.post('/api/production',headers=h,json={'qty':7,'produced_at':main.today(),'tds':75,'ph':7.3}))
    stocked=ok(client.get('/api/stock',headers=h))
    assert (stocked['full'],stocked['empty'])==(22,6)
    ok(client.post('/api/payments',headers=h,json={'customer_id':customer,'amount':380,'paid_at':main.today(),'method':'Easypaisa'}))
    assert ok(client.get('/api/customers',headers=h))[0]['balance']==0
    assert client.post('/api/stock/adjust',headers=h,json={'kind':'empty','qty':-1000,'reason':'bad adjustment'}).status_code==409
    extra=ok(client.post('/api/deliveries',headers=h,json={**payload,'empties':100}))
    assert extra['total']==580
    assert ok(client.get('/api/customers',headers=h))[0]['bottles_out']==0
    assert ok(client.get('/api/stock',headers=h))['empty']==106
    assert ok(client.get(f'/api/deliveries/{row["id"]}/invoice',headers=h))['total']==580

def test_recurring_idempotence_and_order_fulfillment(client):
    h=signup(client,'schedule@example.com','Example Plant')
    cid=ok(client.post('/api/customers',headers=h,json={'name':'Scheduled Customer','price':130}))['id']
    ok(client.post('/api/stock/adjust',headers=h,json={'kind':'full','qty':30,'reason':'Opening stock'}))
    weekday=main.date.fromisoformat(main.today()).weekday()
    ok(client.post('/api/subscriptions',headers=h,json={'customer_id':cid,'qty':2,'weekdays':[weekday]}))
    generated=ok(client.post('/api/orders/generate?for_date='+main.today(),headers=h,json={}))
    assert generated['created']==1
    assert ok(client.post('/api/orders/generate?for_date='+main.today(),headers=h,json={}))['created']==0
    orders=ok(client.get('/api/orders',headers=h));assert len(orders)==1
    order=orders[0]
    r=ok(client.post('/api/deliveries',headers=h,json={'order_id':order['id'],'customer_id':cid,'qty':2,'empties':0,'unit_price':130,'delivered_at':main.today()}))
    assert r['total']==260
    assert ok(client.get('/api/orders',headers=h))[0]['status']=='delivered'
    assert client.post('/api/deliveries',headers=h,json={'order_id':order['id'],'customer_id':cid,'qty':2,'empties':0,'unit_price':130,'delivered_at':main.today()}).status_code==409

def test_staff_driver_permissions(client):
    h=signup(client,'owner@example.com','Roles Plant')
    cid=ok(client.post('/api/customers',headers=h,json={'name':'Role Customer','price':140}))['id']
    ok(client.post('/api/stock/adjust',headers=h,json={'kind':'full','qty':15,'reason':'Initial'}))
    ok(client.post('/api/staff',headers=h,json={'name':'Driver','username':'driver-one','email':'driver@example.com','password':'driver-password-123','role':'driver'}))
    log=ok(client.post('/api/auth/login',json={'email':'driver@example.com','password':'driver-password-123'}))
    d={'Authorization':'Bearer '+log['token']}
    assert client.get('/api/dashboard',headers=d).status_code==403
    assert client.get('/api/expenses',headers=d).status_code==403
    assert client.get('/api/staff',headers=d).status_code==403
    assert client.get('/api/backup',headers=d).status_code==403
    assert client.post('/api/customers',headers=d,json={'name':'Evil Customer','price':1}).status_code==403
    driverid=ok(client.get('/api/me',headers=d))['id']
    orderid=ok(client.post('/api/orders',headers=h,json={'customer_id':cid,'qty':2,'requested_date':main.today(),'driver_id':driverid}))['id']
    assert len(ok(client.get('/api/orders',headers=d)))==1
    ok(client.post('/api/deliveries',headers=d,json={'customer_id':cid,'qty':2,'empties':0,'unit_price':140,'delivered_at':main.today(),'order_id':orderid}))
    assert len(ok(client.get('/api/deliveries',headers=d)))==1
    assert client.post('/api/stock/adjust',headers=d,json={'kind':'full','qty':5,'reason':'Not allowed'}).status_code==403


def test_demo_fill_and_reports(client):
    h=signup(client,'demo@example.com','Demo Plant')
    assert ok(client.post('/api/demo/fill',headers=h,json={}))['customers']==8
    assert len(ok(client.get('/api/customers',headers=h)))==8
    assert len(ok(client.get('/api/orders',headers=h)))==4
    assert client.post('/api/demo/fill',headers=h,json={}).status_code==409
    rpt=ok(client.get('/api/reports',headers=h))
    assert rpt['summary']['sales']>=0 and rpt['summary']['expenses']>0
    assert client.get('/api/backup',headers=h).status_code==200
    assert client.get('/api/export/deliveries',headers=h).status_code==200

def test_edit_password_reset_and_disable(client):
    h=signup(client,'superadmin@example.com','Admin Plant')
    cid=ok(client.post('/api/customers',headers=h,json={'name':'Old Customer','price':110}))['id']
    ok(client.put(f'/api/customers/{cid}',headers=h,json={'name':'New Name','price':170}))
    assert ok(client.get('/api/customers',headers=h))[0]['price']==170
    ok(client.post('/api/staff',headers=h,json={'name':'Driver One','username':'driver-two','email':'driver2@example.com','password':'driver-password-123','role':'driver'}))
    staff=ok(client.get('/api/staff',headers=h));sid=next(x['id'] for x in staff if x['role']=='driver')
    ok(client.post(f'/api/staff/{sid}/reset-password',headers=h,json={'new_password':'new-password-456'}))
    tok=ok(client.post('/api/auth/login',json={'email':'driver2@example.com','password':'new-password-456'}))['token']
    assert client.post('/api/auth/login',json={'email':'driver2@example.com','password':'driver-password-123'}).status_code==401
    ok(client.post('/api/auth/change-password',headers={'Authorization':'Bearer '+tok},json={'current_password':'new-password-456','new_password':'another-password-789'}))
    ok(client.post(f'/api/staff/{sid}/toggle',headers=h,json={}))
    assert client.get('/api/me',headers={'Authorization':'Bearer '+tok}).status_code==401
