from uuid import uuid4
import pytest
from sqlalchemy import text
from test_auth_postgres import client,login,env
from test_business_registry_postgres import allocated

pytestmark=[pytest.mark.postgres,pytest.mark.usefixtures('fresh_schema')]


def test_supplier_handoff_pins_pdf_replays_and_gates_application(allocated,engine):
    env,item,source,code=allocated
    with engine.begin() as db:db.execute(text("UPDATE business_items SET body=jsonb_set(body,'{labels_handoff_required}','true') WHERE id=:id"),{'id':item})
    with client(env) as c:
        login(c)
        row=c.get('/api/business/'+str(item)).json()['item']
        url='/api/business/'+str(item)
        assert c.post(url+'/applied',json={'revision':row['revision'],'confirmed':True}).status_code==409
        body={'request_id':str(uuid4()),'revision':row['revision'],'file_id':row['body']['label_file_id'],'confirmed':True}
        assert c.post(url+'/labels-handoff',json={**body,'file_id':str(uuid4())}).status_code==409
        first=c.post(url+'/labels-handoff',json=body);assert first.status_code==200,first.text
        assert c.post(url+'/labels-handoff',json=body).json()['replayed']
        assert c.post(url+'/labels-handoff',json={**body,'confirmed':False}).status_code==409
        doc=c.get('/api/documents/'+str(source)).json()['document']
        assert doc['workflow']['labels_transferred'] is True
        latest=c.get(url).json()['item']
        assert c.post(url+'/applied',json={'revision':latest['revision'],'confirmed':True}).status_code==200
        assert c.post(url+'/labels-handoff',json=body).json()['replayed']
    with client(env) as c:
        login(c,'tsd');assert c.post(url+'/labels-handoff',json=body).status_code==403
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM business_events WHERE item_id=:id AND action='labels_handoff_confirmed'"),{'id':item}).scalar_one()==1
        assert c.execute(text('SELECT count(*) FROM external_operations')).scalar_one()==0


def test_bulk_prepare_partial_failure_and_retry(env,engine):
    from test_documents_postgres import body as invoice
    with client(env) as c:
        login(c)
        a=c.post('/api/documents',json=invoice(env)).json()
        v=invoice(env);v['number']='OTHER';b=c.post('/api/documents',json=v).json()
        body={'request_id':str(uuid4()),'organization_id':str(env[1]),'action':'prepare','confirmed':True,'entries':[{'id':a['id'],'revision':1},{'id':b['id'],'revision':99}]}
        first=c.post('/api/bm-bulk',json=body);assert first.status_code==200,first.text
        assert [r['ok'] for r in first.json()['results']]==[True,False]
        retry=c.post('/api/bm-bulk',json=body);assert retry.json()['replayed'] and retry.json()['results']==first.json()['results']
        assert c.post('/api/bm-bulk',json={**body,'action':'start'}).status_code==409
    with client(env) as c:
        login(c,'tsd');assert c.post('/api/bm-bulk',json=body).status_code==403


def test_large_invoice_orders_are_bounded_and_require_confirmation(env,engine):
    from test_documents_postgres import body as invoice
    from test_business_postgres import connections
    from test_gs1 import GTIN
    from marking_erp.registry import register_product
    with client(env) as c:
        login(c,'administrator');connections(c,env,writes_enabled=True)
    with engine.begin() as c:register_product(c,env[1],GTIN,'PN/A-1','8542399000',source='external_verified',source_ref='fixture',evidence_sha256='0'*64)
    with client(env) as c:
        login(c);v=invoice(env);v['lines'][0]['labels']=1001
        doc=c.post('/api/documents',json=v).json();assert c.post('/api/documents/'+doc['id']+'/prepare',json={'revision':1}).status_code==200
        body={'source_id':doc['id'],'revision':2,'request_id':str(uuid4())}
        assert c.post('/api/marking/start',json=body).status_code==422
        response=c.post('/api/marking/start',json={**body,'confirmed':True});assert response.status_code==200,response.text
    import json
    with engine.connect() as c:
        payloads=c.execute(text("SELECT payload FROM external_operations WHERE kind='bm.order'")).scalars().all()
        assert sorted(json.loads(bytes(p))['products'][0]['quantity'] for p in payloads)==[1,1000]


def test_partial_report_only_applies_its_codes(allocated,engine):
    from uuid import UUID
    from marking_erp.business import store
    from marking_erp.business.marking import apply
    from marking_erp.business.worker import background
    from test_gs1 import sample
    env,item,source,code=allocated
    with engine.begin() as c:
        other=sample(37,serial=b'5'+b'B'*19,quantity=10)
        c.execute(text("INSERT INTO business_codes(id,organization_id,cis,gtin,base,payload,item_id,line_index,quantity,state,tnved,pn,evidence_ref) SELECT :new,organization_id,:cis,gtin,base,:payload,item_id,line_index,quantity,'emitted',tnved,pn,'fixture' FROM business_codes WHERE id=:id"),{'new':uuid4(),'cis':other[:38].decode(),'payload':other,'id':code['id']})
        actor=background(c,env[3]['bm'],env[1]);row=store.item(c,item)
        apply(actor,row,{'kind':'bm.report'},{},{'sntins':[bytes(code['payload']).decode()]},{},None,env[0])
        assert store.item(c,item)['state']=='reporting'
        assert c.execute(text("SELECT count(*) FROM business_codes WHERE item_id=:id AND state='applied'"),{'id':item}).scalar_one()==1
        c.execute(text("UPDATE business_items SET state='rejected',body=jsonb_set(body,'{last_rejected_step}','\"bm.report\"') WHERE id=:id"),{'id':item})
        apply(actor,store.item(c,item),{'kind':'bm.report'},{},{'sntins':[bytes(code['payload']).decode()]},{},None,env[0])
        assert store.item(c,item)['state']=='rejected'
        apply(actor,store.item(c,item),{'kind':'bm.report'},{},{'sntins':[other.decode()]},{},None,env[0])
        assert store.item(c,item)['state']=='applied'


def test_atk_approval_policy_defers_external_command(allocated,engine):
    env,item,source,code=allocated
    with engine.begin() as c:
        c.execute(text("INSERT INTO policy_values(scope_key,rule_id,value) VALUES ('system','bm.atk.approval_required','true')"))
        c.execute(text("UPDATE business_items SET state='applied' WHERE id=:id"),{'id':item})
        c.execute(text("UPDATE business_codes SET state='applied' WHERE item_id=:id"),{'id':item})
    with client(env) as c:
        login(c);created=c.post('/api/atks',json={'organization_id':str(env[1]),'ids':[str(item)],'request_id':str(uuid4())});assert created.status_code==200,created.text
        id=created.json()['ids'][0]
        with engine.connect() as db:
            assert db.execute(text('SELECT state FROM business_items WHERE id=:id'),{'id':id}).scalar_one()=='awaiting_approval'
            assert db.execute(text('SELECT count(*) FROM external_operations')).scalar_one()==0
        response=c.post('/api/atks/'+id+'/approve',json={'revision':1,'confirmed':True});assert response.status_code==200,response.text
    with engine.connect() as c:assert c.execute(text("SELECT count(*) FROM external_operations WHERE kind='bm.atk'")).scalar_one()==1
