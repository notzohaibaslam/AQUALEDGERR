from tests.test_api import client
from tests.test_api import signup,ok
import main


def test_overpay_current_invoice_then_old_debt_then_advance(client):
    h=signup(client,'flex@example.com','Flexible Plant')
    cid=ok(client.post('/api/customers',headers=h,json={'name':'Saim Customer','price':120}))['id']
    ok(client.post('/api/stock/adjust',headers=h,json={'kind':'full','qty':20,'reason':'Opening'}))
    older=ok(client.post('/api/deliveries',headers=h,json={
        'customer_id':cid,'qty':2,'empties':0,'unit_price':120,'delivered_at':main.today()}))['id']
    current=ok(client.post('/api/deliveries',headers=h,json={
        'customer_id':cid,'qty':5,'empties':0,'unit_price':120,'paid_now':3500,'delivered_at':main.today()}))['id']
    # Total billed 840; received 3500 => 2660 in advance. One cash entry only.
    older_invoice=ok(client.get(f'/api/deliveries/{older}/invoice',headers=h))
    now_invoice=ok(client.get(f'/api/deliveries/{current}/invoice',headers=h))
    assert older_invoice['payment_on_delivery']==240
    assert now_invoice['payment_on_delivery']==600
    assert now_invoice['cash_received_at_delivery']==3500
    assert now_invoice['current_account_balance']==-2660
    assert sum(x['amount'] for x in ok(client.get('/api/payments',headers=h)))==3500
    # Advance automatically applies to next invoice, without a new receipt.
    next_id=ok(client.post('/api/deliveries',headers=h,json={
        'customer_id':cid,'qty':3,'empties':0,'unit_price':120,'delivered_at':main.today()}))['id']
    next_inv=ok(client.get(f'/api/deliveries/{next_id}/invoice',headers=h))
    assert next_inv['payment_on_delivery']==360
    assert next_inv['cash_received_at_delivery']==0
    assert next_inv['current_account_balance']==-2300
    assert sum(x['amount'] for x in ok(client.get('/api/payments',headers=h)))==3500


def test_opening_dues_and_zero_qty_return_only(client):
    h=signup(client,'initial@example.com','Initial Debts Plant')
    cid=ok(client.post('/api/customers',headers=h,json={
        'name':'Opening Due Customer','price':120,'opening_balance':2900}))['id']
    ok(client.post('/api/stock/adjust',headers=h,json={'kind':'full','qty':10,'reason':'Opening'}))
    receipt=ok(client.post('/api/deliveries',headers=h,json={
        'customer_id':cid,'qty':5,'empties':2,'unit_price':120,'paid_now':3500,'delivered_at':main.today()}))
    inv=ok(client.get(f'/api/deliveries/{receipt["id"]}/invoice',headers=h))
    assert inv['total']==600 and inv['payment_on_delivery']==600
    assert inv['current_account_balance']==0
    assert ok(client.get('/api/customers',headers=h))[0]['bottles_out']==3
    # The customer previously held no bottles in the system; incoming 2 were reconciled.
    item=ok(client.post('/api/bottle-returns',headers=h,json={
        'customer_id':cid,'qty':3,'returned_at':main.today()}))
    assert item['qty']==3
    assert ok(client.get('/api/customers',headers=h))[0]['bottles_out']==0
    assert len(ok(client.get('/api/bottle-returns',headers=h)))==1
    assert client.post('/api/bottle-returns',headers=h,json={
        'customer_id':cid,'qty':0,'returned_at':main.today()}).status_code==422


def test_unlimited_invoice_cash_allocation_but_separated_plants(client):
    h1=signup(client,'independent1@example.com','Plant 1')
    h2=signup(client,'independent2@example.com','Plant 2')
    cid=ok(client.post('/api/customers',headers=h1,json={'name':'First Client','price':80}))['id']
    ok(client.post('/api/stock/adjust',headers=h1,json={'kind':'full','qty':4,'reason':'Opening'}))
    did=ok(client.post('/api/deliveries',headers=h1,json={
        'customer_id':cid,'qty':2,'empties':0,'unit_price':80,'delivered_at':main.today()}))['id']
    result=ok(client.post('/api/payments',headers=h1,json={
        'customer_id':cid,'delivery_id':did,'amount':3000,'paid_at':main.today()}))
    assert result['allocated']==160
    assert result['unallocated_to_invoices']==2840
    assert result['customer_advance_credit']==2840
    assert ok(client.get(f'/api/deliveries/{did}/invoice',headers=h1))['payment_on_delivery']==160
    assert client.get(f'/api/deliveries/{did}/invoice',headers=h2).status_code==404
    assert client.post('/api/payments',headers=h2,json={'customer_id':cid,'delivery_id':did,
        'amount':1,'paid_at':main.today()}).status_code==404


def test_order_quantity_change_and_bottle_stock_boundary(client):
    h=signup(client,'quantities@example.com','Variable Qty Plant')
    cid=ok(client.post('/api/customers',headers=h,json={'name':'Variable Qty Client','price':100}))['id']
    ok(client.post('/api/stock/adjust',headers=h,json={'kind':'full','qty':10,'reason':'Opening'}))
    oid=ok(client.post('/api/orders',headers=h,json={'customer_id':cid,'qty':5,'requested_date':main.today()}))['id']
    did=ok(client.post('/api/deliveries',headers=h,json={
        'customer_id':cid,'order_id':oid,'qty':4,'empties':0,'unit_price':100,'delivered_at':main.today()}))['id']
    assert ok(client.get(f'/api/deliveries/{did}/invoice',headers=h))['total']==400
    assert ok(client.get('/api/orders',headers=h))[0]['qty']==5
    assert ok(client.get('/api/orders',headers=h))[0]['status']=='delivered'
    assert client.post('/api/deliveries',headers=h,json={
        'customer_id':cid,'qty':11,'empties':0,'unit_price':100,'delivered_at':main.today()}).status_code==409


def test_reconcile_existing_v4_payments_once(client):
    h=signup(client,'migration41@example.com','Migrating Plant')
    cid=ok(client.post('/api/customers',headers=h,json={'name':'Legacy Customer','price':100}))['id']
    ok(client.post('/api/stock/adjust',headers=h,json={'kind':'full','qty':5,'reason':'Opening'}))
    did=ok(client.post('/api/deliveries',headers=h,json={
        'customer_id':cid,'qty':2,'empties':0,'unit_price':100,'delivered_at':main.today()}))['id']
    with main.transaction() as c:
        c.execute("DELETE FROM schema_migrations WHERE key='payment_allocation_v41'") if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone() else None
        c.execute('INSERT INTO payments(plant_id,customer_id,delivery_id,amount_paisa,method,paid_at) VALUES(?,?,?,?,?,?)',
                  (main.customer_stats(c,1,cid)[0]['plant_id'],cid,did,10000,'Cash',main.today()))
    main.migrate_payment_allocations()
    main.migrate_payment_allocations()
    inv=ok(client.get(f'/api/deliveries/{did}/invoice',headers=h))
    assert inv['payment_on_delivery']==100
    with main.db() as c:
        assert c.execute('SELECT COUNT(*) FROM payment_allocations').fetchone()[0]==1


def test_demo_invoices_payments_are_allocated(client):
    h=signup(client,'demo-pay-v41@example.com','Seed Plant')
    ok(client.post('/api/demo/fill',headers=h,json={}))
    invs=ok(client.get('/api/deliveries',headers=h))
    assert sum(1 for x in invs if x['invoice_paid']>0)==4
    assert sum(x['invoice_paid'] for x in invs)==sum(x['amount'] for x in ok(client.get('/api/payments',headers=h)))


def test_empty_pickup_other_tenant_and_driver_blocked(client):
    owner=signup(client,'pickupowner@example.com','Owner Plant')
    other=signup(client,'pickupother@example.com','Other Plant')
    cid=ok(client.post('/api/customers',headers=owner,json={'name':'Buyer','price':120}))['id']
    assert client.post('/api/bottle-returns',headers=other,json={
        'customer_id':cid,'qty':7,'returned_at':main.today()}).status_code==404
    ok(client.post('/api/staff',headers=owner,json={
        'name':'Pickup Driver','username':'pickup-driver','password':'DriverPass87','role':'driver'}))
    driver={'Authorization':'Bearer '+ok(client.post('/api/auth/login',json={
        'identifier':'pickup-driver','password':'DriverPass87'}))['token']}
    assert client.get('/api/bottle-returns',headers=driver).status_code==403
    assert client.post('/api/bottle-returns',headers=driver,json={
        'customer_id':cid,'qty':7,'returned_at':main.today()}).status_code==403
    ok(client.post('/api/bottle-returns',headers=owner,json={
        'customer_id':cid,'qty':7,'returned_at':main.today()}))
    assert ok(client.get('/api/customers',headers=owner))[0]['bottles_out']==0
    assert ok(client.get('/api/stock',headers=owner))['empty']==7
    assert len(ok(client.get('/api/customers/'+str(cid)+'/ledger',headers=owner))['entries'])==1
