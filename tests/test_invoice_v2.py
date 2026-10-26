from tests.test_api import client
from tests.test_api import signup,ok
import main


def test_pdf_invoice_and_collections_tenant_security(client):
    a=signup(client,'aqua@example.com','Sea Breeze Water')
    b=signup(client,'another@example.com','Other Water')
    customer=ok(client.post('/api/customers',headers=a,json={'name':'Pakistan Customer','phone':'03001234567','price':140,'opening_balance':200}))['id']
    ok(client.post('/api/stock/adjust',headers=a,json={'kind':'full','qty':30,'reason':'Opening'}))
    inv=ok(client.post('/api/deliveries',headers=a,json={'customer_id':customer,'qty':4,'empties':0,'unit_price':140,'paid_now':150,'delivered_at':main.today()}))
    invoice=ok(client.get(f'/api/deliveries/{inv["id"]}/invoice',headers=a))
    assert invoice['total']==560
    assert invoice['payment_on_delivery']==150
    assert invoice['current_account_balance']==610
    assert invoice['customer']['phone']=='03001234567'
    pdf=client.get(f'/api/deliveries/{inv["id"]}/invoice.pdf',headers=a)
    assert pdf.status_code==200
    assert pdf.content.startswith(b'%PDF-')
    assert 'attachment' in pdf.headers['content-disposition']
    assert pdf.headers['cache-control']=='no-store'
    assert client.get(f'/api/deliveries/{inv["id"]}/invoice.pdf',headers=b).status_code==404
    assert client.get(f'/api/deliveries/{inv["id"]}/invoice.pdf').status_code==401
    insights=ok(client.get('/api/insights',headers=a))
    assert insights['debtor_count']==1
    assert insights['positive_receivables']==610
    assert insights['debtors'][0]['phone']=='03001234567'
    assert ok(client.get('/api/insights',headers=b))['debtor_count']==0


def test_driver_does_not_see_whole_plant_collections(client):
    owner=signup(client,'boss@example.com','Boss Plant')
    ok(client.post('/api/staff',headers=owner,json={'name':'Driver User','username':'driver-alt','email':'drive@example.com','password':'driverpass123456','role':'driver'}))
    token=ok(client.post('/api/auth/login',json={'email':'drive@example.com','password':'driverpass123456'}))['token']
    assert client.get('/api/insights',headers={'Authorization':'Bearer '+token}).status_code==403

def test_allocate_payment_to_invoice_and_allow_advance_credit(client):
    a=signup(client,'alloc@example.com','Invoice Plant')
    b=signup(client,'evilalloc@example.com','Other Plant')
    cid=ok(client.post('/api/customers',headers=a,json={'name':'Ahmed Water Buyer','price':100}))['id']
    cid2=ok(client.post('/api/customers',headers=a,json={'name':'Other Buyer','price':100}))['id']
    ok(client.post('/api/stock/adjust',headers=a,json={'kind':'full','qty':10,'reason':'Opening'}))
    inv=ok(client.post('/api/deliveries',headers=a,json={'customer_id':cid,'qty':3,'empties':0,'unit_price':100,'paid_now':100,'delivered_at':main.today()}))['id']
    assert client.post('/api/payments',headers=a,json={'customer_id':cid2,'delivery_id':inv,'amount':50,'paid_at':main.today()}).status_code==422
    assert client.post('/api/payments',headers=b,json={'customer_id':cid,'delivery_id':inv,'amount':50,'paid_at':main.today()}).status_code==404
    ok(client.post('/api/payments',headers=a,json={'customer_id':cid,'delivery_id':inv,'amount':200,'paid_at':main.today(),'method':'JazzCash'}))
    assert ok(client.get('/api/customers',headers=a))[0]['balance']==0
    assert ok(client.get(f'/api/deliveries/{inv}/invoice',headers=a))['payment_on_delivery']==300
    deliveries=ok(client.get('/api/deliveries',headers=a))
    assert deliveries[0]['invoice_paid']==300
    receipt=ok(client.post('/api/payments',headers=a,json={'customer_id':cid,'delivery_id':inv,'amount':1,'paid_at':main.today()}))
    assert receipt['customer_advance_credit']==1
    assert receipt['unallocated_to_invoices']==1
    assert ok(client.get(f'/api/deliveries/{inv}/invoice',headers=a))['payment_on_delivery']==300
    assert ok(client.get('/api/customers',headers=a))[0]['balance']==-1
