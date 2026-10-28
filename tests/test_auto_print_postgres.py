"""Durable opt-in, exact multipart jobs and no automatic resend after claim."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4,UUID
import json
import pytest
from sqlalchemy import text
from test_auth_postgres import env,client,login
from test_business_registry_postgres import allocated
from test_gs1 import sample,SERIAL
from marking_erp.business.auto_print import tick
from marking_erp.business.printing import register_printer,current_parts
from marking_erp.business.worker import background
from marking_erp.business import store
from marking_erp.business.marking import labels

pytestmark=[pytest.mark.postgres,pytest.mark.usefixtures('fresh_schema')]


@pytest.fixture(autouse=True)
def explicit_worker_ticks(monkeypatch):
    # These tests control each transition around a real tick and PostgreSQL.
    # The browser scenario exercises the app with its normal background worker.
    monkeypatch.setattr('marking_erp.business.worker.BusinessWorker.start',lambda self:None)


def enable(c,item,device):
    revision=c.get('/api/business/'+str(item)).json()['item']['revision']
    body={'request_id':str(uuid4()),'revision':revision,'printer_id':str(device),'confirmed':True}
    response=c.post('/api/business/'+str(item)+'/auto-print',json=body)
    assert response.status_code==200,response.text
    return response.json(),body


def test_waits_for_labels_then_queues_once_across_workers_and_replays_intent(allocated,engine):
    env,item,source,code=allocated
    with engine.begin() as db:
        device,token=register_printer(db,env[1],'Warehouse','Printer')
        row=store.item(db,item);bundle=row['body']['label_file_id']
        db.execute(text("UPDATE business_items SET body=body-'label_file_id',state='receiving' WHERE id=:id"),{'id':item})
    with client(env) as c:
        login(c,'specialist');request,body=enable(c,item,device)
        tick(engine)
        assert c.get('/api/business/'+str(item)+'/auto-print').json()['requests'][0]['state']=='waiting'
        with engine.begin() as db:db.execute(text("UPDATE business_items SET body=body||jsonb_build_object('label_file_id',CAST(:bundle AS text)),state='labels_ready' WHERE id=:id"),{'id':item,'bundle':bundle})
        with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(lambda _:tick(engine),range(2)))
        saved=c.get('/api/business/'+str(item)+'/auto-print').json()['requests'][0]
        assert saved['state']=='queued' and len(saved['jobs'])==1
        assert c.post('/api/business/'+str(item)+'/auto-print',json=body).json()['id']==request['id']
        assert c.post('/api/business/'+str(item)+'/auto-print',json={**body,'request_id':str(uuid4())}).status_code==409
    with client(env) as agent:
        agent.headers['Authorization']='Bearer '+token
        job=agent.post('/api/print-agent/claim').json()['job'];assert job
        tick(engine)
        assert agent.post('/api/print-agent/claim').json()['blocked']
        assert agent.post('/api/print-agent/result',json={'job_id':job['id'],'lease':job['lease'],'pdf_sha256':job['pdf_sha256'],'outcome':'unknown'}).status_code==200
        tick(engine)
        assert agent.post('/api/print-agent/claim').json()['blocked']
    with engine.connect() as db:
        assert db.execute(text('SELECT count(*) FROM print_jobs')).scalar_one()==1
        assert db.execute(text('SELECT count(*) FROM external_operations')).scalar_one()==0


@pytest.mark.parametrize('change',['permission','source','printer','bundle'])
def test_rechecks_guards_before_atomic_queue_and_blocks_without_jobs(allocated,engine,change):
    env,item,source,code=allocated
    with engine.begin() as db:device,_=register_printer(db,env[1],'Warehouse','Printer')
    with client(env) as c:
        login(c,'specialist');request,body=enable(c,item,device)
        with engine.begin() as db:
            if change=='permission':db.execute(text("UPDATE group_grants SET scope='none' WHERE group_slug='specialist' AND permission_id='labels.print.confirm'"))
            elif change=='source':db.execute(text('UPDATE bm_documents SET revision=revision+1 WHERE id=:id'),{'id':source})
            elif change=='printer':db.execute(text('UPDATE printer_devices SET enabled=false WHERE id=:id'),{'id':device})
            else:
                actor=background(db,env[3]['specialist'],env[1]);row=store.item(db,item)
                bundle=labels(actor,row,env[0]);store.save(actor,row,'fixture_replaced',body={**row['body'],'label_file_id':str(bundle)})
        tick(engine)
    with engine.connect() as db:
        assert db.execute(text('SELECT state FROM auto_print_requests')).scalar_one()=='blocked'
        assert db.execute(text('SELECT count(*) FROM print_jobs')).scalar_one()==0


def test_quarantine_retry_and_cancel_have_no_implicit_reprint(allocated,engine):
    env,item,source,code=allocated
    with engine.begin() as db:device,_=register_printer(db,env[1],'Warehouse','Printer')
    with client(env) as c:
        login(c,'specialist');request,body=enable(c,item,device)
        issue=c.post('/api/incidents',json={'organization_id':str(env[1]),'request_id':str(uuid4()),'code':bytes(code['payload']).decode(),'category':'damaged_label','description':'Печать временно остановлена до сверки','restrictions':['reprint']})
        assert issue.status_code==200,issue.text
        tick(engine)
        row=c.get('/api/business/'+str(item)+'/auto-print').json()['requests'][0]
        assert row['state']=='blocked' and row['jobs']==[]
        resolved=c.post('/api/incidents/'+issue.json()['id']+'/actions',json={'request_id':str(uuid4()),'revision':1,'action':'resolve','message':'Сохранённый КМ и этикетка проверены'})
        assert resolved.status_code==200,resolved.text
        tick(engine);assert c.get('/api/business/'+str(item)+'/auto-print').json()['requests'][0]['state']=='blocked'
        assert c.post('/api/auto-print/'+request['id']+'/retry').status_code==200
        tick(engine)
        assert c.post('/api/auto-print/'+request['id']+'/cancel').status_code==200
        assert c.post('/api/auto-print/'+request['id']+'/retry').status_code==409
        assert c.post('/api/business/'+str(item)+'/auto-print',json={**body,'request_id':str(uuid4())}).status_code==409
    with engine.connect() as db:assert db.execute(text('SELECT state FROM print_jobs')).scalar_one()=='cancelled'


def test_scope_confirmation_manual_conflict_and_cancel_before_ready(allocated,engine):
    env,item,source,code=allocated
    with engine.begin() as db:
        device,_=register_printer(db,env[1],'Warehouse','Printer')
        foreign,_=register_printer(db,env[2],'Outside','Printer')
    with client(env) as c:
        login(c,'bm')
        assert c.post('/api/business/'+str(item)+'/auto-print',json={'request_id':str(uuid4()),'revision':2,'printer_id':str(device),'confirmed':True}).status_code==403
    with client(env) as c:
        login(c,'specialist');revision=c.get('/api/business/'+str(item)).json()['item']['revision']
        body={'request_id':str(uuid4()),'revision':revision,'printer_id':str(device),'confirmed':False}
        assert c.post('/api/business/'+str(item)+'/auto-print',json=body).status_code==422
        assert c.post('/api/business/'+str(item)+'/auto-print',json={**body,'printer_id':str(foreign),'confirmed':True}).status_code==403
        request,body=enable(c,item,device)
        with engine.connect() as db:pdf=db.execute(text("SELECT id FROM business_artifacts WHERE item_id=:id AND kind='labels_pdf'"),{'id':item}).scalar_one()
        assert c.post('/api/business-files/'+str(pdf)+'/print',json={'request_id':str(uuid4()),'printer_id':str(device)}).status_code==409
        assert c.post('/api/auto-print/'+request['id']+'/cancel').status_code==200
        tick(engine)
        assert c.post('/api/business-files/'+str(pdf)+'/print',json={'request_id':str(uuid4()),'printer_id':str(device)}).status_code==200


def test_real_501_label_kit_queues_both_parts_in_order_and_supports_exact_reprint(allocated,engine):
    env,item,source,code=allocated
    with engine.begin() as db:
        actor=background(db,env[3]['specialist'],env[1]);row=store.item(db,item)
        for n in range(500):
            raw=sample(37,SERIAL[:-3]+f'{n:03d}'.encode(),quantity=10)
            db.execute(text('''INSERT INTO business_codes(id,organization_id,cis,gtin,base,payload,item_id,line_index,quantity,state,tnved,pn,evidence_ref)
                SELECT :id,organization_id,:cis,gtin,:raw,:raw,item_id,line_index,quantity,'emitted',tnved,pn,evidence_ref FROM business_codes WHERE id=:old'''),
                {'id':uuid4(),'cis':raw[:38].decode(),'raw':raw,'old':code['id']})
        bundle=labels(actor,row,env[0]);row=store.save(actor,row,'fixture_multipart',body={**row['body'],'label_file_id':str(bundle)})
        _,parts=current_parts(db,row);assert len(parts)==2
        device,token=register_printer(db,env[1],'Warehouse','Printer')
    with client(env) as c,client(env) as agent:
        login(c,'specialist');request,_=enable(c,item,device)
        tick(engine)
        saved=c.get('/api/business/'+str(item)+'/auto-print').json()['requests'][0]
        assert [j['part_index'] for j in saved['jobs']]==[1,2]
        agent.headers['Authorization']='Bearer '+token
        for i in (0,1):
            job=agent.post('/api/print-agent/claim').json()['job'];assert job and job['id']==saved['jobs'][i]['id']
            if i==0:assert c.post('/api/auto-print/'+request['id']+'/cancel').status_code==409
            assert agent.post('/api/print-agent/result',json={'job_id':job['id'],'lease':job['lease'],'pdf_sha256':job['pdf_sha256'],'outcome':'spooled'}).status_code==200
        assert agent.post('/api/print-agent/claim').json()['job'] is None
        result=c.post('/api/business-codes/'+str(code['id'])+'/reprint',json={'request_id':str(uuid4()),'reason':'Повреждена одна этикетка большого комплекта'})
        assert result.status_code==200,result.text
        assert c.get('/api/business-files/'+result.json()['file_id']).content.startswith(b'%PDF')
        tick(engine)
        assert agent.post('/api/print-agent/claim').json()['job'] is None


def test_restore_cancels_waiting_intent_before_devices_can_be_reenabled(allocated,engine):
    from marking_erp.backup import _freeze
    env,item,source,code=allocated
    with engine.begin() as db:device,_=register_printer(db,env[1],'Warehouse','Printer')
    with client(env) as c:
        login(c,'specialist');request,_=enable(c,item,device)
    with engine.begin() as db:_freeze(db,'0'*64,'2026-10-28T00:00:00Z')
    tick(engine)
    with engine.connect() as db:
        assert db.execute(text('SELECT state FROM auto_print_requests')).scalar_one()=='cancelled'
        assert db.execute(text('SELECT count(*) FROM print_jobs')).scalar_one()==0


def test_agent_rechecks_source_after_auto_queue_and_does_not_print_stopped_invoice(allocated,engine):
    env,item,source,code=allocated
    with engine.begin() as db:device,token=register_printer(db,env[1],'Warehouse','Printer')
    with client(env) as c:
        login(c,'specialist');enable(c,item,device)
    tick(engine)
    with engine.begin() as db:db.execute(text('UPDATE bm_documents SET revision=revision+1 WHERE id=:id'),{'id':source})
    with client(env) as agent:
        agent.headers['Authorization']='Bearer '+token
        assert agent.post('/api/print-agent/claim').json()['job'] is None
    tick(engine)
    with engine.connect() as db:
        assert db.execute(text('SELECT state FROM print_jobs')).scalar_one()=='cancelled'
        assert db.execute(text('SELECT count(*) FROM print_jobs')).scalar_one()==1
