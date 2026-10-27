"""Local incident lifecycle, dynamic scopes and quarantine at actual business boundaries."""
from uuid import UUID,uuid4
import pytest
from sqlalchemy import text
from fastapi import HTTPException
from test_auth_postgres import env,client,login
from test_warehouse_postgres import setup,raw,scan
from test_business_registry_postgres import allocated
from marking_erp.incidents import enforce,enforce_business
from marking_erp.business import store
from marking_erp.business.worker import background
pytestmark=[pytest.mark.postgres,pytest.mark.usefixtures('fresh_schema')]


def incident(env,task=None,**extra):return {'organization_id':str(env[1]),'request_id':str(uuid4()),'source_kind':'task' if task else 'none','source_id':task,'category':'damaged_label','description':'Повреждена маркировка при доставке','code':raw(),**extra}

def action(c,id,revision,kind,message='Проверены сведения и физический товар',**extra):
    return c.post('/api/incidents/'+id+'/actions',json={'request_id':str(uuid4()),'revision':revision,'action':kind,'message':message,**extra})


def test_tsd_reports_specialist_assigns_and_resolves_with_immutable_history(env,engine):
    task,number,source=setup(env)
    value=incident(env,task)
    with client(env) as c:
        login(c,'tsd');response=c.post('/api/incidents',json=value);assert response.status_code==200,response.text
        id=response.json()['id'];assert c.post('/api/incidents',json=value).json()['revision']==1
        assert c.post('/api/incidents',json={**value,'description':'Другой текст для прежнего запроса'}).status_code==409
        assert action(c,id,1,'quarantine',restrictions=['shipment']).status_code==403
        assert action(c,id,1,'comment','Передано специалисту по маркировке').status_code==200
    with client(env) as c:
        login(c,'specialist');assert action(c,id,2,'assign',assignee_id=str(env[3]['specialist'])).status_code==200
        assert action(c,id,3,'start').status_code==200
        assert action(c,id,4,'quarantine',restrictions=['shipment']).status_code==200
        assert action(c,id,4,'resolve').status_code==409
        body={'request_id':str(uuid4()),'revision':5,'action':'resolve','message':'Этикетка заменена; полный КМ проверен сканером'}
        result=c.post('/api/incidents/'+id+'/actions',json=body);assert result.status_code==200,result.text
        assert result.json()['status']=='resolved' and result.json()['restrictions']==[]
        assert c.post('/api/incidents/'+id+'/actions',json=body).json()==result.json()
        data=c.get('/api/incidents/'+id).json();assert len(data['events'])==6
        assert action(c,id,6,'reopen').json()['status']=='open'
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM external_operations')).scalar_one()==0
        assert c.execute(text('SELECT count(*) FROM incident_events')).scalar_one()==7
    with pytest.raises(Exception):
        with engine.begin() as c:c.execute(text('UPDATE incident_events SET action=:action'),{'action':'fake'})


def test_cross_task_and_org_access_are_denied_and_revocation_is_live(env,engine):
    task,_,_=setup(env)
    with client(env) as warehouse:
        login(warehouse,'warehouse');assert warehouse.post('/api/incidents',json=incident(env,task)).status_code==403
    with client(env) as c:
        login(c,'tsd');result=c.post('/api/incidents',json=incident(env,task));id=result.json()['id']
        assert c.get('/api/incidents',params={'organization_id':str(env[2])}).status_code==403
        with engine.begin() as db:db.execute(text('DELETE FROM operation_assignments WHERE target_id=:id AND user_id=:user'),{'id':task,'user':env[3]['tsd']})
        assert c.get('/api/incidents/'+id).status_code==403
        assert c.get('/api/incidents',params={'organization_id':str(env[1])}).json()['items']==[]
    with client(env) as c:
        login(c,'specialist');response=c.post('/api/incidents',json=incident(env,category='other',code='',restrictions=['shipment']));assert response.status_code==422
        assert action(c,id,1,'assign',assignee_id=str(env[3]['tsd'])).status_code==422


def test_code_quarantine_blocks_only_affected_shipment_and_close_then_releases(env):
    task,number,source=setup(env,labels=2,kind='shipment')
    with client(env) as specialist:
        login(specialist,'specialist');result=specialist.post('/api/incidents',json=incident(env,task,restrictions=['shipment']));assert result.status_code==200,result.text
        id=result.json()['id']
    with client(env) as c:
        login(c,'tsd');response=scan(c,task);assert response.status_code==409 and response.json()['detail']['code']=='local_quarantine'
        assert scan(c,task,{'request_id':str(uuid4()),'raw':raw('b')}).status_code==200
    with client(env) as specialist:
        login(specialist,'specialist');assert action(specialist,id,1,'resolve').status_code==200
    with client(env) as c:
        login(c,'tsd');assert scan(c,task).status_code==200
        data=c.get('/api/warehouse/tasks/'+task).json();revision=data['task']['revision']
    with client(env) as specialist:
        login(specialist,'specialist');assert action(specialist,id,2,'reopen').status_code==200
        assert action(specialist,id,3,'quarantine',restrictions=['shipment']).status_code==200
    with client(env) as c:
        login(c,'tsd');response=c.post('/api/warehouse/tasks/'+task+'/close',json={'revision':revision,'confirmation':number});assert response.status_code==409


def test_receipt_can_record_evidence_while_quarantine_prevents_final_close(env):
    task,number,source=setup(env,labels=1)
    with client(env) as c:
        login(c,'specialist');result=c.post('/api/incidents',json=incident(env,task,restrictions=['receipt']));assert result.status_code==200,result.text
    with client(env) as c:
        login(c,'tsd');assert scan(c,task).status_code==200
        row=c.get('/api/warehouse/tasks/'+task).json()['task']
        assert c.post('/api/warehouse/tasks/'+task+'/close',json={'revision':row['revision'],'confirmation':number}).status_code==409


def test_external_and_reprint_holds_do_not_alter_code_or_remote_state(allocated,engine):
    env,item,source,code=allocated
    with client(env) as c:
        login(c,'specialist');body=incident(env,code=bytes(code['payload']).decode(),restrictions=['external','reprint']);response=c.post('/api/incidents',json=body);assert response.status_code==200,response.text
        id=response.json()['id']
        response=c.post('/api/business-codes/'+str(code['id'])+'/reprint',json={'request_id':str(uuid4()),'reason':'Повреждена этикетка в пути'});assert response.status_code==409
    with engine.begin() as c:
        actor=background(c,env[3]['specialist'],env[1]);row=store.item(c,item)
        with pytest.raises(HTTPException):enforce_business(c,row,'bm.report')
        enforce_business(c,row,'bm.fetch') # Collecting already issued codes remains possible.
        enforce_business(c,row,'nk.save')
        assert c.execute(text('SELECT state FROM business_codes WHERE id=:id'),{'id':code['id']}).scalar_one()==code['state']
        assert c.execute(text('SELECT count(*) FROM external_operations')).scalar_one()==0


def test_quarantine_follows_a_confirmed_replacement(allocated,engine):
    env,item,source,code=allocated
    with client(env) as c:
        login(c,'specialist');result=c.post('/api/incidents',json=incident(env,code=code['cis'],restrictions=['shipment']));assert result.status_code==200,result.text
    with engine.begin() as c:
        new=uuid4();newcis='01'+code['gtin']+'21'+'n'*20
        c.execute(text('''INSERT INTO business_codes(id,organization_id,cis,gtin,base,payload,item_id,line_index,quantity,state,tnved,pn,evidence_ref)
            VALUES (:id,:org,:cis,:gtin,:base,:payload,:item,0,10,'applied',:tnved,:pn,'synthetic fixture')'''),{'id':new,'org':env[1],'cis':newcis,'gtin':code['gtin'],'base':raw('n').encode(),'payload':raw('n').encode(),'item':item,'tnved':code['tnved'],'pn':code['pn']})
        c.execute(text('INSERT INTO code_replacements(old_code_id,new_code_id,remark_id,external_document_id) VALUES (:old,:new,:remark,:doc)'),{'old':code['id'],'new':new,'remark':item,'doc':uuid4()})
        with pytest.raises(HTTPException):enforce(c,env[1],'shipment',cises=[newcis])
        enforce(c,env[1],'reprint',cises=[newcis]) # A shipment hold does not prohibit repair.

def test_late_quarantine_blocks_prepared_dispatch_without_contacting_an_external_service(allocated,engine,monkeypatch):
    from test_business_postgres import connections
    from marking_erp.business.worker import BusinessWorker
    from marking_erp import operations
    env,item,source,code=allocated
    with client(env) as admin:login(admin,'administrator');connections(admin,env,writes_enabled=True,oms_token='fixture',true_token='fixture')
    with engine.begin() as c:
        actor=background(c,env[3]['specialist'],env[1]);row=store.item(c,item)
        op_id=store.queue(actor,row,'bm.report',{'productGroup':'radio','sntins':[bytes(code['payload']).decode()]})
        op=dict(c.execute(text('SELECT * FROM external_operations WHERE id=:id'),{'id':op_id}).mappings().one())
    with client(env) as c:
        login(c,'specialist');assert c.post('/api/incidents',json=incident(env,code=code['cis'],restrictions=['external'])).status_code==200
    worker=BusinessWorker(engine,env[0])
    with pytest.raises(operations.DispatchBlocked):worker.dispatch({**op,'payload':bytes(op['payload']),'signature':b'synthetic-not-a-real-signature'})
    with engine.connect() as c:assert c.execute(text('SELECT state FROM external_operations WHERE id=:id'),{'id':op_id}).scalar_one()=='awaiting_signature'


def test_unknown_operation_ticket_cannot_replace_real_reconciliation(env,engine):
    from test_operations_postgres import queue_env,prepare
    from marking_erp.business import store
    from marking_erp.business.worker import background
    # A real durable command record with a deliberately unknown synthetic outcome.
    with engine.begin() as c:
        c.execute(text("INSERT INTO operation_contracts(kind,permission_id,version,semantic,approved,evidence_ref) VALUES ('fixture.incident','marking.codes.issue','fixture','mutation',true,'synthetic only')"))
        actor=background(c,env[3]['specialist'],env[1]);row=store.create(actor,'marking','Incident fixture',{'lines':[]},env[1])
        from marking_erp.operations import prepare
        op=prepare(c,organization_id=env[1],actor_id=actor.id,kind='fixture.incident',target_id=row['id'],target_revision=1,idempotency_key='incident-test',payload=b'{}')
        c.execute(text("UPDATE external_operations SET state='unknown' WHERE id=:id"),{'id':op['id']})
    with client(env) as c:
        login(c,'specialist');result=c.post('/api/incidents',json=incident(env,code='',category='unknown_operation',operation_id=str(op['id'])));assert result.status_code==200,result.text
        id=result.json()['id'];assert action(c,id,1,'resolve').status_code==409
        with engine.begin() as db:db.execute(text("UPDATE external_operations SET state='rejected' WHERE id=:id"),{'id':op['id']})
        assert action(c,id,1,'resolve').status_code==200

def test_prepared_print_is_held_and_resumes_same_job_after_quarantine_release(allocated,engine):
    from marking_erp.business.printing import register_printer
    env,item,source,code=allocated
    with engine.begin() as db:device,token=register_printer(db,env[1],'Warehouse','Label Printer')
    with client(env) as c,client(env) as agent:
        login(c,'specialist');rid=str(uuid4());file=c.post('/api/business-codes/'+str(code['id'])+'/reprint',json={'request_id':rid,'reason':'Повреждённая этикетка при доставке'}).json()['file_id']
        job=c.post('/api/business-files/'+file+'/print',json={'request_id':str(uuid4()),'printer_id':str(device),'reprint_id':rid}).json()['id']
        result=c.post('/api/incidents',json=incident(env,code=code['cis'],restrictions=['reprint']));assert result.status_code==200,result.text
        id=result.json()['id'];agent.headers['Authorization']='Bearer '+token
        assert agent.post('/api/print-agent/claim').json()['job'] is None
        assert c.get('/api/print-jobs/'+job).json()['state']=='held'
        assert c.post('/api/print-jobs/'+job+'/resume').status_code==409
        assert action(c,id,1,'resolve').status_code==200
        assert c.post('/api/print-jobs/'+job+'/resume').json()['state']=='queued'
        claimed=agent.post('/api/print-agent/claim').json()['job'];assert claimed['id']==job and claimed['copies']==1
        assert agent.post('/api/print-agent/claim').json()['job'] is None
    with engine.connect() as c:assert c.execute(text('SELECT count(*) FROM print_jobs')).scalar_one()==1

def test_own_incident_scope_refers_to_reporter_after_independent_source_read_check(env,engine):
    from test_documents_postgres import body as invoice
    with client(env) as c:
        login(c,'specialist');result=c.post('/api/documents',json=invoice(env));assert result.status_code==200,result.text
        document=result.json()['id']
    with engine.begin() as c:c.execute(text("UPDATE group_grants SET scope='all' WHERE group_slug='bm' AND permission_id='marking.documents.view'"))
    with client(env) as c:
        login(c);result=c.post('/api/incidents',json=incident(env,source_kind='document',source_id=document,code='',category='other'));assert result.status_code==200,result.text
        assert c.get('/api/incidents/'+result.json()['id']).status_code==200

def test_reprint_of_another_code_in_same_invoice_is_not_blocked_by_code_quarantine(allocated,engine):
    from marking_erp.business.marking import apply
    from marking_erp.business.printing import register_printer
    from test_gs1 import sample
    env,item,source,old=allocated
    with engine.begin() as c:
        actor=background(c,env[3]['specialist'],env[1]);row=store.item(c,item);body=dict(row['body']);body['lines'][0]['labels']=2
        row=store.save(actor,row,'fixture_second_code',body=body)
        apply(actor,row,{'id':uuid4(),'kind':'bm.fetch'},{'metadata':{'gtin':old['gtin'],'expected':1}},{},{'codes':[sample(37,serial=b'Z'*20).decode()]},None,env[0])
        fresh=c.execute(text('SELECT id FROM business_codes WHERE item_id=:id AND id<>:old'),{'id':item,'old':old['id']}).scalar_one()
        device,token=register_printer(c,env[1],'Warehouse','Label Printer')
    with client(env) as c,client(env) as agent:
        login(c,'specialist');result=c.post('/api/incidents',json=incident(env,code=old['cis'],restrictions=['reprint']));assert result.status_code==200,result.text
        rid=str(uuid4());response=c.post('/api/business-codes/'+str(fresh)+'/reprint',json={'request_id':rid,'reason':'Другой товар требует замены этикетки'});assert response.status_code==200,response.text
        file=response.json()['file_id'];assert c.get('/api/business-files/'+file).status_code==200
        response=c.post('/api/business-files/'+file+'/print',json={'request_id':str(uuid4()),'printer_id':str(device),'reprint_id':rid});assert response.status_code==200,response.text
        agent.headers['Authorization']='Bearer '+token;job=agent.post('/api/print-agent/claim').json()['job'];assert job and job['id']==response.json()['id']

def test_document_hold_cannot_be_bypassed_via_registry_pdf_or_split_entrypoint(allocated,engine):
    env,item,source,code=allocated
    with client(env) as c:
        login(c,'specialist');result=c.post('/api/incidents',json=incident(env,source_kind='document',source_id=str(source),code='',restrictions=['reprint','split']));assert result.status_code==200,result.text
        with engine.connect() as db:pdf=db.execute(text("SELECT id FROM files WHERE code_id=:code AND kind='label_pdf'"),{'code':code['registry_code_id']}).scalar_one()
        assert c.get('/api/files/'+str(pdf)).status_code==409
        response=c.post('/api/splits',json={'organization_id':str(env[1]),'request_id':str(uuid4()),'raw':bytes(code['payload']).decode(),'quantity':1,'pn':code['pn'],'tnved':code['tnved']})
        assert response.status_code==409 and response.json()['detail']['code']=='local_quarantine'
    with engine.connect() as c:
        with pytest.raises(HTTPException):enforce(c,env[1],'reprint',cises=[code['cis']])
