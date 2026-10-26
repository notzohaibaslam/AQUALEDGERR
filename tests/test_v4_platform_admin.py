"""New platform-owner / tenant-owner trust boundary tests."""
from tests.test_api import client,ok


def provision(client,plant,name,username,password='owner-pass-8'):
    return client.post('/api/platform/plants',headers=client.platform_headers,json={
       'plant':plant,'name':name,'username':username,'password':password})


def login_plant(client,username,password='owner-pass-8'):
    return {'Authorization':'Bearer '+ok(client.post('/api/auth/login',json={
        'identifier':username,'password':password}))['token']}


def test_admin_only_provision_and_tenant_denials(client):
    assert client.post('/api/platform/plants',json={'plant':'Friend Plant','name':'Saim','username':'saim-plant','password':'owner-pass-8'}).status_code==401
    r=provision(client,'Saim RO Water','Saim','saim-plant')
    assert r.status_code==201,r.text
    saim=login_plant(client,'saim-plant')
    assert client.post('/api/platform/plants',headers=saim,json={'plant':'Secret','name':'Bad','username':'badowner','password':'owner-pass-8'}).status_code in (401,403)
    assert client.get('/api/platform/plants',headers=saim).status_code==403
    assert client.get('/api/me',headers=client.platform_headers).status_code==403
    assert client.get('/api/dashboard',headers=client.platform_headers).status_code==403
    assert client.post('/api/auth/register',headers=saim,json={'plant':'Other Plant','name':'X','username':'owner2','password':'12345678'}).status_code==403
    assert client.post('/api/staff',headers=saim,json={'name':'Saim Other Plant Owner','username':'fakeother','password':'owner-pass-8','role':'owner'}).status_code==422
    created=client.post('/api/staff',headers=saim,json={'name':'Mujahid','username':'mujahid-staff','password':'staffpass8','role':'manager'})
    assert created.status_code==200,created.text
    manager=login_plant(client,'mujahid-staff','staffpass8')
    assert client.post('/api/staff',headers=manager,json={'name':'Other','username':'other-staff','password':'staffpass8','role':'manager'}).status_code==403
    assert client.post('/api/platform/plants',headers=manager,json={'plant':'Other Plant','name':'Other','username':'another-plant','password':'owner-pass-8'}).status_code==403
    assert client.get('/api/platform/audit',headers=manager).status_code==403
    assert client.get('/api/staff',headers=manager).status_code==200


def test_admin_can_create_many_independent_plants_without_email(client):
    one=provision(client,'Saim Water','Saim','saimowner')
    two=provision(client,'Ahmed Water','Ahmed','ahmedowner')
    assert one.status_code==two.status_code==201
    assert one.json()['id']!=two.json()['id']
    for item in ok(client.get('/api/platform/plants',headers=client.platform_headers)):
        assert item['owner_email'] is None
    duplicate=provision(client,'Duplicate Water','Other','saimowner')
    assert duplicate.status_code==409
    saim=login_plant(client,'saimowner')
    ahmed=login_plant(client,'ahmedowner')
    customer=ok(client.post('/api/customers',headers=saim,json={'name':'Saim Customer','price':170}))['id']
    ok(client.post('/api/stock/adjust',headers=saim,json={'kind':'full','qty':10,'reason':'Stock'}))
    delivery=ok(client.post('/api/deliveries',headers=saim,json={'customer_id':customer,'qty':2,'empties':0,'unit_price':170,'delivered_at':'2026-10-26'}))['id']
    assert client.get('/api/customers',headers=ahmed).json()==[]
    assert client.get('/api/deliveries/'+str(delivery)+'/invoice',headers=ahmed).status_code==404
    assert client.get('/api/deliveries/'+str(delivery)+'/invoice.pdf',headers=ahmed).status_code==404
    assert client.get('/api/customers/'+str(customer)+'/ledger',headers=ahmed).status_code==404
    assert client.post('/api/payments',headers=ahmed,json={'customer_id':customer,'amount':10,'paid_at':'2026-10-26'}).status_code==404
    assert ok(client.get('/api/settings',headers=saim))['name']=='Saim Water'
    assert ok(client.get('/api/settings',headers=ahmed))['name']=='Ahmed Water'
    assert len(ok(client.get('/api/platform/audit',headers=client.platform_headers)))>=2


def test_suspend_plant_revokes_sessions_and_does_not_impact_other_plant(client):
    pid=provision(client,'Saim RO','Saim','saim-ops').json()['id']
    provision(client,'Ahmed RO','Ahmed','ahmed-ops')
    a=login_plant(client,'saim-ops')
    b=login_plant(client,'ahmed-ops')
    suspended=client.put(f'/api/platform/plants/{pid}/status',headers=client.platform_headers,json={'active':False})
    assert suspended.status_code==200
    assert client.get('/api/me',headers=a).status_code==401
    assert client.post('/api/auth/login',json={'identifier':'saim-ops','password':'owner-pass-8'}).status_code==401
    assert client.get('/api/me',headers=b).status_code==200
    assert client.put(f'/api/platform/plants/{pid}/status',headers=b,json={'active':True}).status_code==403
    assert client.put(f'/api/platform/plants/{pid}/status',headers=client.platform_headers,json={'active':True}).status_code==200
    assert client.get('/api/me',headers=a).status_code==401 # revoked even after reactivation
    assert client.post('/api/auth/login',json={'identifier':'saim-ops','password':'owner-pass-8'}).status_code==200


def test_owner_reset_and_admin_password_change_revoke_sessions(client):
    pid=provision(client,'Saim RO','Saim','saim-reset').json()['id']
    owner=login_plant(client,'saim-reset')
    reset=client.put(f'/api/platform/plants/{pid}/owner-password',headers=client.platform_headers,json={'password':'newowner88'})
    assert reset.status_code==200
    assert client.get('/api/me',headers=owner).status_code==401
    assert client.post('/api/auth/login',json={'identifier':'saim-reset','password':'owner-pass-8'}).status_code==401
    assert client.post('/api/auth/login',json={'identifier':'saim-reset','password':'newowner88'}).status_code==200
    p=client.platform_headers
    changed=client.post('/api/platform/change-password',headers=p,json={'current_password':'SecureMaster123!','new_password':'ChangedMaster123!'})
    assert changed.status_code==200
    assert client.get('/api/platform/plants',headers=p).status_code==401
    assert client.post('/api/platform/login',json={'username':'platform-test','password':'SecureMaster123!'}).status_code==401
    assert client.post('/api/platform/login',json={'username':'platform-test','password':'ChangedMaster123!'}).status_code==200


def test_platform_admin_ui_and_no_signup_visible(client):
    assert client.get('/admin').status_code==200
    assert 'Create a new water plant' not in client.get('/').text
    assert 'Create a plant account' not in client.get('/').text
    assert client.get('/static/admin.js').status_code==200
    assert client.get('/static/admin.css').status_code==200
    assert 'name="email"' in client.get('/static/admin.js').text or "formInput('email'" in client.get('/static/admin.js').text
