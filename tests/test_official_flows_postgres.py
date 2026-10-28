"""Official payloads and durable legal intent; network calls are test fixtures only."""
import json
from datetime import date
from uuid import UUID,uuid4
import pytest
from sqlalchemy import text
from test_auth_postgres import env,client,login
from test_business_postgres import connections
from test_business_registry_postgres import allocated
from test_logistics_postgres import evidence
from marking_erp.business import store
from marking_erp.business.worker import background
from marking_erp.business.marking import apply
from marking_erp.business.live import Live
from marking_erp.business.remarking import verify_result
from test_gs1 import sample

pytestmark=[pytest.mark.postgres,pytest.mark.usefixtures('fresh_schema')]

def setup_connection(env):
    with client(env) as c:login(c,'administrator');connections(c,env,writes_enabled=True,true_token='fixture',oms_token='fixture')

def test_fts_queues_signed_atk_document_and_keeps_circulation_unconfirmed(allocated,engine,monkeypatch):
    env,item,source,code=allocated;setup_connection(env);atk=evidence(env,engine,item,code,fts=False)
    monkeypatch.setattr(Live,'verify',lambda self,codes,status,**kw:{c['cis']:{'parent':'1'*25} for c in codes})
    with client(env) as c:
        login(c,'logistics');id=str(uuid4());day=date.today()
        data={'organization_id':str(env[1]),'request_id':id,'atks':[str(atk)],'declaration_number':'12345678/'+day.strftime('%d%m%y')+'/1234567','declaration_date':day.isoformat(),'country_of_origin':'China','conformity_document':'Document present','closed_segment':True,'production_country_code':'156'}
        response=c.post('/api/fts',json=data);assert response.status_code==200,response.text
        response=c.post('/api/fts/'+id+'/submit',json={'revision':1,'confirmed':True});assert response.status_code==200,response.text
        assert response.json()['state']=='fts_submitted'
        assert c.post('/api/fts/'+id+'/submit',json={'revision':2,'confirmed':True}).status_code==409
    with engine.connect() as c:
        op=c.execute(text("SELECT * FROM external_operations WHERE kind='fts.submit'")).mappings().one()
        assert op['state']=='awaiting_signature' and op['requires_signature']
        payload=json.loads(bytes(op['payload']));assert payload['products_list'][0]['atk']=='1'*25
        assert payload['products_list'][0]['children'][0]=={'cis':code['cis'],'tn_ved':code['tnved'],'production_country_code':'156'}
        assert c.execute(text("SELECT state FROM business_items WHERE id=:id"),{'id':id}).scalar_one()!='circulation_confirmed'

def test_replacement_emits_remark_code_and_requires_independent_official_submission(allocated,engine,monkeypatch):
    env,item,source,code=allocated;setup_connection(env)
    with engine.begin() as c:c.execute(text("UPDATE business_codes SET state='applied' WHERE id=:id"),{'id':code['id']})
    def info(self,cises):return {cis:{'cis':cis,'gtin':code['gtin'],'ownerInn':self.inn,'status':'INTRODUCED' if cis==code['cis'] else 'APPLIED','statusEx':'EMPTY','packageType':'UNIT','quantityInPack':code['quantity'],'emissionType':'FOREIGN' if cis==code['cis'] else 'REMARK'} for cis in cises}
    monkeypatch.setattr(Live,'info',info)
    with client(env) as c:
        login(c,'specialist');id=str(uuid4());body={'organization_id':str(env[1]),'request_id':id,'old_code_id':str(code['id']),'reason':'Damaged original marking label','confirmed':True}
        response=c.post('/api/remarks',json=body);assert response.status_code==200,response.text
        assert c.post('/api/remarks',json=body).json()['id']==id
        assert c.post('/api/remarks',json={**body,'request_id':str(uuid4())}).status_code==409
    with engine.begin() as c:
        op=c.execute(text("SELECT * FROM external_operations WHERE kind='remark.order'")).mappings().one()
        assert json.loads(bytes(op['payload']))['attributes']['releaseMethodType']=='REMARK'
        actor=background(c,env[3]['specialist'],env[1]);row=store.item(c,UUID(id))
        apply(actor,row,{'kind':'remark.order','external_id':str(uuid4())},{},json.loads(bytes(op['payload'])),{},None,env[0])
        assert c.execute(text("SELECT count(*) FROM external_operations WHERE kind='remark.fetch'")).scalar_one()==1
        apply(actor,store.item(c,UUID(id)),{'id':uuid4(),'kind':'remark.fetch'},{'metadata':{'gtin':code['gtin'],'expected':1}},{},{'codes':[sample(37,serial=b'R'*20).decode()]},None,env[0])
        c.execute(text("UPDATE business_items SET state='applied' WHERE id=:id"),{'id':id})
        c.execute(text("UPDATE business_codes SET state='applied' WHERE item_id=:id"),{'id':id})
        # Clear only synthetic un-dispatched intents; no external side effects exist in this fixture.
        c.execute(text("UPDATE external_operations SET state='cancelled_before_dispatch' WHERE state IN ('queued','awaiting_signature')"))
    with client(env) as c:
        login(c,'specialist');row=c.get('/api/business/'+id).json()['item']
        assert c.post('/api/business/'+id+'/applied',json={'revision':row['revision'],'confirmed':True}).status_code==404
        response=c.post('/api/remarks/'+id+'/submit',json={'revision':row['revision'],'confirmed':True,'remarking_date':date.today().isoformat()});assert response.status_code==200,response.text
    with engine.connect() as c:
        op=c.execute(text("SELECT * FROM external_operations WHERE kind='remark.submit'")).mappings().one()
        payload=json.loads(bytes(op['payload']));assert payload['remarking_cause']=='KM_SPOILED'
        assert payload['products'][0]['last_uin']==code['cis']
        assert payload['products'][0]['new_uin']!=code['cis']
        assert c.execute(text('SELECT state FROM business_codes WHERE id=:id'),{'id':code['id']}).scalar_one()=='applied'
    from marking_erp.business.worker import BusinessWorker
    with engine.begin() as c:external=dispatched_fixture(c,op)
    monkeypatch.setattr(Live,'call',lambda *a,**kw:[{'number':external,'status':'CHECKED_OK','body':payload}])
    worker=BusinessWorker(engine,env[0]);worker.reconcile(op['id'])
    with engine.connect() as c:assert c.execute(text('SELECT state FROM business_items WHERE id=:id'),{'id':id}).scalar_one()=='remarking'
    def retired_info(self,cises):
        return {cis:{'cis':cis,'gtin':code['gtin'],'ownerInn':self.inn,'status':'WRITTEN_OFF' if cis==code['cis'] else 'INTRODUCED','statusEx':'REMARK_RETIRED' if cis==code['cis'] else 'EMPTY','packageType':'UNIT','quantityInPack':code['quantity'],'emissionType':'FOREIGN' if cis==code['cis'] else 'REMARK'} for cis in cises}
    monkeypatch.setattr(Live,'info',retired_info);worker.reconcile(op['id'])
    with engine.connect() as c:
        assert c.execute(text('SELECT state FROM business_items WHERE id=:id'),{'id':id}).scalar_one()=='done'
        assert c.execute(text('SELECT state FROM business_codes WHERE id=:id'),{'id':code['id']}).scalar_one()=='withdrawn'
    with client(env) as c:
        login(c,'specialist')
        assert c.post('/api/business-codes/'+str(code['id'])+'/reprint',json={'request_id':str(uuid4()),'reason':'Old label is invalid now'}).status_code==409
    from marking_erp.business.replacements import effective
    with engine.connect() as c:
        replacement=effective(c,[code['id']])[code['id']]
        assert replacement['cis']==payload['products'][0]['new_uin']
        assert c.execute(text('SELECT count(*) FROM code_replacements')).scalar_one()==1
    with client(env) as c:
        login(c,'logistics');task=str(uuid4())
        response=c.post('/api/logistics/warehouse-transfer',json={'request_id':task,'source_id':str(source),'source_revision':2,'confirmed':True,'kind':'receipt','assignees':[str(env[3]['warehouse'])]})
        assert response.status_code==200,response.text
    with engine.connect() as c:assert c.execute(text('SELECT expected_codes FROM warehouse_tasks WHERE id=:id'),{'id':task}).scalar_one()==[replacement['cis']]

def test_document_success_does_not_replace_old_code_without_retirement():
    class Readback:
        inn='123'
        def info(self,cises):return {cis:{'status':'INTRODUCED','ownerInn':'123','gtin':'g','quantityInPack':1} for cis in cises}
    assert not verify_result(Readback(),{'products':[{'last_uin':'old','new_uin':'new'}]})

def dispatched_fixture(c,op):
    """Represent a durably accepted upstream mutation, never contact a remote service."""
    from hashlib import sha256
    external=str(uuid4());raw=json.dumps({'result':external}).encode()
    c.execute(text("UPDATE external_operations SET state='submitted',external_id=:external,fence=1 WHERE id=:id"),{'id':op['id'],'external':external})
    c.execute(text("INSERT INTO operation_receipts(operation_id,fence,phase,raw,sha256,applied,classification) VALUES (:id,1,'dispatch',:raw,:sha,true,'submitted')"),{'id':op['id'],'raw':raw,'sha':sha256(raw).hexdigest()})
    return external


def test_fts_readback_waits_for_introduced_even_when_document_is_checked_ok(allocated,engine,monkeypatch):
    from marking_erp.business.worker import BusinessWorker
    from marking_erp.business.errors import RemoteError
    env,item,source,code=allocated;setup_connection(env);atk=evidence(env,engine,item,code,fts=False)
    phase={'status':'APPLIED'}
    def remote_info(self,cises):return {cis:{'cis':cis,'gtin':code['gtin'],'ownerInn':self.inn,'status':phase['status'],'statusEx':'EMPTY','parent':'1'*25} for cis in cises}
    monkeypatch.setattr(Live,'info',remote_info)
    with client(env) as c:
        login(c,'logistics');id=str(uuid4());day=date.today();number='12345678/'+day.strftime('%d%m%y')+'/1234567'
        response=c.post('/api/fts',json={'organization_id':str(env[1]),'request_id':id,'atks':[str(atk)],'declaration_number':number,'declaration_date':day.isoformat(),'country_of_origin':'China','conformity_document':'Existing permit'})
        assert response.status_code==200,response.text
        assert c.post('/api/fts/'+id+'/submit',json={'revision':1,'confirmed':True}).status_code==200
    with engine.begin() as c:
        op=dict(c.execute(text("SELECT * FROM external_operations WHERE kind='fts.submit'")).mappings().one());external=dispatched_fixture(c,op)
    monkeypatch.setattr(Live,'call',lambda *a,**kw:[{'number':external,'status':'CHECKED_OK','body':{'declaration_number':number,'products_list':[{'atk':'1'*25}]}}])
    worker=BusinessWorker(engine,env[0])
    with pytest.raises(RemoteError):worker.reconcile(op['id'])
    with engine.connect() as c:assert c.execute(text('SELECT state FROM business_items WHERE id=:id'),{'id':id}).scalar_one()=='fts_submitted'
    phase['status']='INTRODUCED';worker.reconcile(op['id'])
    with engine.connect() as c:
        assert c.execute(text('SELECT state FROM business_items WHERE id=:id'),{'id':id}).scalar_one()=='circulation_confirmed'
        assert c.execute(text("SELECT count(*) FROM business_artifacts WHERE item_id=:id AND kind='fts_receipt'"),{'id':id}).scalar_one()==1
