"""Policy, exact payload control and one-shot printer delivery through real API."""
from uuid import uuid4
import pytest
from sqlalchemy import text
from test_business_registry_postgres import allocated
from test_auth_postgres import env,client,login
from marking_erp.business.printing import register_printer
pytestmark=[pytest.mark.postgres,pytest.mark.usefixtures('fresh_schema')]

def test_reprint_requires_reason_and_limits_copies_and_verifies_exact_payload(allocated,engine):
    env,item,source,code=allocated;id=str(code['id']);request_id=str(uuid4())
    with client(env) as c:
        login(c,'specialist')
        assert c.post('/api/business-codes/'+id+'/reprint').status_code==422
        body={'request_id':request_id,'reason':'Damaged label replacement','copies':2}
        assert c.post('/api/business-codes/'+id+'/reprint',json=body).status_code==422
        body['copies']=1
        first=c.post('/api/business-codes/'+id+'/reprint',json=body)
        assert first.status_code==200,first.text
        assert c.post('/api/business-codes/'+id+'/reprint',json=body).json()['id']==request_id
        assert c.post('/api/reprints/'+request_id+'/scans',json={'request_id':str(uuid4()),'raw':bytes(code['payload']).decode()}).status_code==409
        assert c.post('/api/reprints/'+request_id+'/printed').status_code==200
        assert c.post('/api/reprints/'+request_id+'/scans',json={'request_id':str(uuid4()),'raw':bytes(code['base']).decode()}).status_code==409
        scan={'request_id':str(uuid4()),'raw':bytes(code['payload']).decode()}
        result=c.post('/api/reprints/'+request_id+'/scans',json=scan)
        assert result.status_code==200,result.text
        assert result.json()['state']=='completed'
        assert c.post('/api/reprints/'+request_id+'/scans',json=scan).json()['scanned_copies']==1
    with engine.connect() as db:
        assert db.execute(text('SELECT count(*) FROM external_operations')).scalar_one()==0
        assert db.execute(text('SELECT payload FROM business_codes')).scalar_one()==code['payload']
        assert db.execute(text('SELECT count(*) FROM reprint_scans')).scalar_one()==1

def test_printer_claims_once_and_receipt_replays_without_reprint(allocated,engine):
    env,item,source,code=allocated
    with engine.begin() as db:device,token=register_printer(db,env[1],'Warehouse','Label Printer')
    with client(env) as c,client(env) as agent:
        login(c,'specialist')
        rid=str(uuid4());body={'request_id':rid,'reason':'Damaged label replacement'}
        file=c.post('/api/business-codes/'+str(code['id'])+'/reprint',json=body).json()['file_id']
        result=c.post('/api/business-files/'+file+'/print',json={'request_id':str(uuid4()),'printer_id':str(device),'reprint_id':rid})
        assert result.status_code==200,result.text
        agent.headers['Authorization']='Bearer '+token
        job=agent.post('/api/print-agent/claim').json()['job']
        assert job['copies']==1 and job['printer_name']=='Label Printer'
        assert agent.post('/api/print-agent/claim').json()['job'] is None
        receipt={'job_id':job['id'],'lease':job['lease'],'pdf_sha256':job['pdf_sha256'],'outcome':'spooled','spool_reference':'fixture'}
        assert agent.post('/api/print-agent/result',json=receipt).status_code==200
        assert agent.post('/api/print-agent/result',json=receipt).status_code==200
        assert agent.post('/api/print-agent/claim').json()['job'] is None
        assert c.post('/api/reprints/'+rid+'/printed').json()['state']=='printed'
        assert c.post('/api/business-files/'+file+'/print',json={'request_id':str(uuid4()),'printer_id':str(device),'reprint_id':rid}).status_code==409

def test_policy_snapshot_requires_a_scan_per_copy_and_cannot_change_mid_job(allocated,engine):
    env,item,source,code=allocated;rid=str(uuid4())
    with engine.begin() as db:
        db.execute(text("INSERT INTO policy_revisions(scope_key,organization_id,revision) VALUES (:scope,:org,1) ON CONFLICT DO NOTHING"),{'scope':str(env[1]),'org':env[1]})
        db.execute(text("INSERT INTO policy_values(scope_key,rule_id,value) VALUES (:scope,'labels.reprint.max_copies','2')"),{'scope':str(env[1])})
    with client(env) as c:
        login(c,'specialist');data={'request_id':rid,'reason':'Two replacement copies approved','copies':2}
        response=c.post('/api/business-codes/'+str(code['id'])+'/reprint',json=data);assert response.status_code==200,response.text
        with engine.begin() as db:
            db.execute(text("INSERT INTO policy_values(scope_key,rule_id,value) VALUES (:scope,'labels.reprint.control_scan','false')"),{'scope':str(env[1])})
        assert c.post('/api/reprints/'+rid+'/printed').json()['state']=='printed'
        for state in ('printed','completed'):
            response=c.post('/api/reprints/'+rid+'/scans',json={'request_id':str(uuid4()),'raw':bytes(code['payload']).decode()})
            assert response.status_code==200,response.text
            assert response.json()['state']==state

def test_revoked_access_and_unknown_printer_outcome_do_not_emit_more_copies(allocated,engine):
    env,item,source,code=allocated
    with engine.begin() as db:device,token=register_printer(db,env[1],'Warehouse','Label Printer')
    with client(env) as c,client(env) as agent:
        login(c,'specialist');rid=str(uuid4());prepared=c.post('/api/business-codes/'+str(code['id'])+'/reprint',json={'request_id':rid,'reason':'Damaged label replacement'}).json();file=prepared['file_id']
        job=c.post('/api/business-files/'+file+'/print',json={'request_id':str(uuid4()),'printer_id':str(device),'reprint_id':rid}).json()
        agent.headers['Authorization']='Bearer '+token;claimed=agent.post('/api/print-agent/claim').json()['job']
        receipt={'job_id':claimed['id'],'lease':claimed['lease'],'pdf_sha256':claimed['pdf_sha256'],'outcome':'unknown'}
        assert agent.post('/api/print-agent/result',json=receipt).status_code==200
        assert agent.post('/api/print-agent/claim').json().get('blocked')
        assert c.post('/api/reprints/'+rid+'/printed').status_code==409
        with engine.begin() as db:db.execute(text("UPDATE group_grants SET scope='none' WHERE group_slug='specialist' AND permission_id='labels.reprint'"))
        assert c.get('/api/business-files/'+file).status_code==403
        assert c.post('/api/reprints/'+rid+'/printed').status_code==403

def test_revoked_print_confirmation_cancels_a_queued_reprint_before_agent_delivery(allocated,engine):
    env,item,source,code=allocated
    with engine.begin() as db:device,token=register_printer(db,env[1],'Warehouse','Label Printer')
    with client(env) as c,client(env) as agent:
        login(c,'specialist');rid=str(uuid4());file=c.post('/api/business-codes/'+str(code['id'])+'/reprint',json={'request_id':rid,'reason':'Damaged label replacement'}).json()['file_id']
        response=c.post('/api/business-files/'+file+'/print',json={'request_id':str(uuid4()),'printer_id':str(device),'reprint_id':rid});assert response.status_code==200,response.text
        with engine.begin() as db:db.execute(text("UPDATE group_grants SET scope='none' WHERE group_slug='specialist' AND permission_id='labels.print.confirm'"))
        agent.headers['Authorization']='Bearer '+token
        assert agent.post('/api/print-agent/claim').json()['job'] is None
    with engine.connect() as db:assert db.execute(text('SELECT state FROM print_jobs')).scalar_one()=='cancelled'
