import sqlite3
import pytest
import main
from tests.test_api import client, signup, ok


def setup(client):
    h=signup(client,'sizes@example.com','Multi Size Plant')
    products=ok(client.get('/api/products',headers=h))
    ids={int(p['litres']):p['id'] for p in products}
    cid=ok(client.post('/api/customers',headers=h,json={'name':'Mixed Customer','price':150,'opening_bottles':2}))['id']
    return h,ids,cid


def adjust(client,h,p,kind,qty):
    return client.post('/api/stock/adjust',headers=h,json={'product_id':p,'kind':kind,'qty':qty,'reason':'Opening inventory'})


def deliver(client,h,p,cid,**extra):
    return client.post('/api/deliveries',headers=h,json={'product_id':p,'customer_id':cid,'qty':3,'empties':1,'unit_price':100,'delivered_at':main.today(),**extra})


def test_separate_stock_production_returns_and_balances(client):
    h,ids,cid=setup(client)
    assert set(ids)=={12,14,19}
    ok(adjust(client,h,ids[19],'full',20));ok(adjust(client,h,ids[12],'empty',10))
    assert deliver(client,h,ids[12],cid).status_code==409
    ok(client.post('/api/production',headers=h,json={'product_id':ids[12],'qty':6,'produced_at':main.today()}))
    inv=ok(deliver(client,h,ids[12],cid,paid_now=450))
    assert inv['total']==300
    ok(client.post('/api/bottle-returns',headers=h,json={'product_id':ids[12],'customer_id':cid,'qty':2,'returned_at':main.today()}))
    products={p['litres']:p for p in ok(client.get('/api/products',headers=h))}
    assert products[12]['full_stock']==3 and products[12]['empty_stock']==7
    assert products[19]['full_stock']==20
    sizes={p['litres']:p for p in ok(client.get(f'/api/customers/{cid}/bottle-sizes',headers=h))}
    assert sizes[12]['bottles_out']==0 and sizes[19]['bottles_out']==2
    stock=ok(client.get('/api/stock',headers=h))
    assert stock['full']==23 and stock['at_customers']==2
    invoice=ok(client.get(f'/api/deliveries/{inv["id"]}/invoice',headers=h))
    assert invoice['litres']==12 and invoice['product_name']=='12L Bottle'
    assert invoice['current_account_balance']==-150
    pdf=client.get(f'/api/deliveries/{inv["id"]}/invoice.pdf',headers=h)
    assert pdf.content.startswith(b'%PDF')
    report=ok(client.get('/api/reports/bottle-sizes',headers=h))[0]
    assert report['litres_delivered']==36 and report['sales']==300


def test_custom_price_immutable_capacity_and_historical_invoice(client):
    h,ids,cid=setup(client)
    data={'name':'Family 5.5L','litres':5.5,'price':70,'low_stock':10}
    r=client.post('/api/products',headers=h,json=data);assert r.status_code==201,r.text
    pid=r.json()['id']
    assert client.post('/api/products',headers=h,json={**data,'name':'family 5.5l'}).status_code==409
    assert client.post('/api/products',headers=h,json={**data,'name':'Invalid','litres':0}).status_code==422
    ok(client.put(f'/api/customers/{cid}/prices/{pid}',headers=h,json={'price':65}))
    size=next(x for x in ok(client.get(f'/api/customers/{cid}/bottle-sizes',headers=h)) if x['id']==pid)
    assert size['price']==65 and size['custom_price']
    ok(adjust(client,h,pid,'full',4))
    inv=ok(deliver(client,h,pid,cid,unit_price=65,empties=0))
    assert client.put(f'/api/products/{pid}',headers=h,json={**data,'litres':6}).status_code==409
    ok(client.put(f'/api/products/{pid}',headers=h,json={**data,'name':'Renamed 5.5L','price':90}))
    invoice=ok(client.get(f'/api/deliveries/{inv["id"]}/invoice',headers=h))
    assert invoice['product_name']=='Family 5.5L' and invoice['unit_price']==65
    assert client.put(f'/api/products/{pid}',headers=h,json={**data,'active':False}).status_code==409
    exported=ok(client.get('/api/backup',headers=h))['data']
    assert len(exported['bottle_products'])==4 and len(exported['customer_prices'])==1


def test_tenant_boundary_and_recurring_size(client):
    h,ids,cid=setup(client)
    other=signup(client,'other-size@example.com','Other Plant')
    assert adjust(client,other,ids[12],'full',5).status_code==404
    assert client.put(f'/api/customers/{cid}/prices/{ids[12]}',headers=other,json={'price':1}).status_code==404
    day=main.today();weekday=main.date.fromisoformat(day).weekday()
    ok(client.post('/api/subscriptions',headers=h,json={'customer_id':cid,'product_id':ids[14],'qty':2,'weekdays':[weekday]}))
    assert ok(client.post('/api/orders/generate',headers=h,json={}))['created']==1
    assert ok(client.post('/api/orders/generate',headers=h,json={}))['created']==0
    order=ok(client.get('/api/orders',headers=h))[0]
    assert order['product_id']==ids[14]
    ok(adjust(client,h,ids[12],'full',10));ok(adjust(client,h,ids[14],'full',10))
    assert deliver(client,h,ids[12],cid,order_id=order['id']).status_code==409
    ok(deliver(client,h,ids[14],cid,order_id=order['id']))
    assert next(p for p in ok(client.get('/api/products',headers=h)) if p['id']==ids[12])['full_stock']==10


def test_driver_cannot_manage_catalog(client):
    h,ids,cid=setup(client)
    ok(client.post('/api/staff',headers=h,json={'name':'Driver Test','username':'sizes-driver','password':'DriverSecret123','role':'driver'}))
    token=ok(client.post('/api/auth/login',json={'identifier':'sizes-driver','password':'DriverSecret123'}))['token']
    driver={'Authorization':'Bearer '+token}
    assert client.post('/api/products',headers=driver,json={'name':'Bad Bottle','litres':7,'price':1}).status_code==403
    assert client.get(f'/api/customers/{cid}/bottle-sizes',headers=driver).status_code==403


def test_legacy_migration_idempotent_and_preserves_money(tmp_path,monkeypatch):
    monkeypatch.setattr(main,'DB_PATH',tmp_path/'legacy.sqlite3')
    monkeypatch.setattr(main,'DATA',tmp_path)
    with main.db() as c:
        c.executescript(main.SCHEMA)
        pid=c.execute("INSERT INTO plants(name,full_stock,empty_stock,created_at) VALUES('Legacy',17,3,?)",(main.now(),)).lastrowid
        cid=c.execute("INSERT INTO customers(plant_id,name,price_paisa,opening_bottles,created_at) VALUES(?,'Legacy Customer',12000,4,?)",(pid,main.now())).lastrowid
        c.execute('INSERT INTO deliveries(plant_id,customer_id,qty,empties,unit_price_paisa,total_paisa,delivered_at) VALUES(?,?,3,1,12000,36000,?)',(pid,cid,main.today()))
    main.migrate_v5();main.migrate_v5()
    with main.db() as c:
        pr=main.product_for(c,pid)
        assert pr['litres']==19 and pr['full_stock']==17
        assert c.execute('SELECT COUNT(*) FROM bottle_products').fetchone()[0]==3
        d=c.execute('SELECT * FROM deliveries').fetchone()
        assert d['product_id']==pr['id'] and d['total_paisa']==36000
        assert main.bottle_balance(c,pid,cid,pr['id'])==6
    assert len(list((tmp_path/'backups').glob('pre_v5_*.sqlite3')))==1
