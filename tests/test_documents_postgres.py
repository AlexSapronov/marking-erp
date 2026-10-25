from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_auth_postgres import env,client,login
from marking_erp import invoice_excel as excel,operations as ops

pytestmark=[pytest.mark.postgres,pytest.mark.usefixtures('fresh_schema')]
GTIN='04640638349704'


def body(env,**extra):
    return {'request_id':str(uuid4()),'organization_id':str(env[1]),'number':'INV-01','document_date':'2026-10-26','supplier':'Поставщик А',
            'lines':[{'pn':'PN/A-1','gtin':GTIN,'tnved':'8542399000','quantity':10,'labels':3}],**extra}


def create(c,env,**extra):
    value=body(env,**extra);response=c.post('/api/documents',json=value)
    assert response.status_code==200,response.text
    return response.json(),value


def test_create_replay_edit_prepare_and_history(env,engine):
    with client(env) as c:
        login(c);doc,value=create(c,env)
        assert c.post('/api/documents',json=value).json()['replayed']
        assert c.post('/api/documents',json={**value,'supplier':'different'}).status_code==409
        data=c.get('/api/documents/'+doc['id']).json()
        assert data['document']['lines'][0]['quantity']==10 and not data['external_dispatch_available']
        assert 'request_sha256' not in data['document']
        response=c.post('/api/documents/'+doc['id']+'/prepare',json={'revision':1})
        assert response.status_code==200 and response.json()['state']=='prepared'
        edit={k:v for k,v in value.items() if k!='request_id'};edit['revision']=1
        assert c.put('/api/documents/'+doc['id'],json=edit).status_code==409
        edit['revision']=2;edit['lines'][0]['quantity']=20
        assert c.put('/api/documents/'+doc['id'],json=edit).json()['state']=='draft'
        history=c.get('/api/documents/'+doc['id']+'/history').json()['history']
        assert [r['revision'] for r in history]==[3,2,1]
        with engine.connect() as db:
            assert db.execute(text('SELECT count(*) FROM external_operations')).scalar_one()==0
            assert db.execute(text('SELECT revision FROM operation_targets WHERE id=:id'),{'id':doc['id']}).scalar_one()==3
    with pytest.raises(DBAPIError),engine.begin() as db:db.execute(text('DELETE FROM bm_document_versions'))


def test_draft_allows_missing_identifiers_but_prepare_rejects(env):
    with client(env) as c:
        login(c);value=body(env);value['lines'][0].update(gtin='',tnved='')
        response=c.post('/api/documents',json=value);assert response.status_code==200
        assert c.post('/api/documents/'+response.json()['id']+'/prepare',json={'revision':1}).status_code==422


@pytest.mark.parametrize('field,value',[('quantity',0),('labels',1.5),('gtin','04640638349705'),('pn','bad\nPN')])
def test_invalid_lines_never_persist(env,engine,field,value):
    with client(env) as c:
        login(c);data=body(env);data['lines'][0][field]=value
        assert c.post('/api/documents',json=data).status_code==422
        with engine.connect() as db:assert db.execute(text('SELECT count(*) FROM bm_documents')).scalar_one()==0


def test_roles_org_and_dynamic_revoke(env,engine):
    with client(env) as c:
        assert c.get('/api/documents',params={'organization_id':str(env[1])}).status_code==401
        login(c);doc,value=create(c,env)
        assert c.post('/api/documents',json={**value,'request_id':str(uuid4()),'organization_id':str(env[2])}).status_code==403
        for group in ['logistics','warehouse','tsd']:
            with client(env) as other:
                login(other,group)
                assert other.post('/api/documents',json=body(env)).status_code==403
                assert other.get('/api/documents/'+doc['id']).status_code==403
        with engine.begin() as db:db.execute(text("UPDATE group_grants SET scope='none' WHERE group_slug='bm' AND permission_id='marking.documents.view'"))
        assert c.get('/api/documents/'+doc['id']).status_code==403


def test_own_assigned_and_all_scopes(env,engine):
    with client(env) as creator,client(env) as specialist,client(env) as logistics:
        login(creator);doc,_=create(creator,env);login(specialist,'specialist');login(logistics,'logistics')
        assert specialist.get('/api/documents/'+doc['id']).status_code==200
        assert logistics.get('/api/documents',params={'organization_id':str(env[1])}).json()['documents']==[]
        with engine.begin() as db:db.execute(text('INSERT INTO operation_assignments VALUES (:id,:user)'),{'id':doc['id'],'user':env[3]['logistics']})
        assert logistics.get('/api/documents/'+doc['id']).status_code==200
        assert len(logistics.get('/api/documents',params={'organization_id':str(env[1])}).json()['documents'])==1
        with engine.begin() as db:db.execute(text("UPDATE group_grants SET scope='all' WHERE group_slug='logistics' AND permission_id='marking.documents.edit'"))
        # edit depends on view: assigned still checked independently.
        with engine.begin() as db:db.execute(text('DELETE FROM operation_assignments WHERE target_id=:id'),{'id':doc['id']})
        assert logistics.get('/api/documents/'+doc['id']).status_code==403


def test_cross_document_tnved_conflict_and_duplicate_header(env):
    with client(env) as c:
        login(c);_,value=create(c,env)
        assert c.post('/api/documents',json={**value,'request_id':str(uuid4())}).status_code==409
        value['lines'][0]['tnved']='8536699008'
        assert c.post('/api/documents',json={**value,'request_id':str(uuid4()),'number':'INV-02'}).status_code==409


def test_policy_merge_and_never(env,engine):
    with client(env) as c:
        login(c);value=body(env);value['lines']*=2
        response=c.post('/api/documents',json=value);id=response.json()['id']
        assert c.get('/api/documents/'+id).json()['document']['lines'][0]['labels']==6
        with engine.begin() as db:db.execute(text("INSERT INTO policy_values(scope_key,rule_id,value) VALUES ('system','bm.lines.merge','\"never\"')"))
        value.update(request_id=str(uuid4()),number='INV-02')
        response=c.post('/api/documents',json=value)
        assert len(c.get('/api/documents/'+response.json()['id']).json()['document']['lines'])==2


def test_archive_confirmation_restore_and_no_automatic_progress(env,engine):
    with client(env) as c,client(env) as specialist:
        login(c);doc,_=create(c,env);id=doc['id'];change={'revision':1,'confirmation':'INV-01','reason':'Закупка отменена'}
        assert c.post('/api/documents/'+id+'/archive',json={**change,'confirmation':'wrong'}).status_code==422
        assert c.post('/api/documents/'+id+'/archive',json=change).json()['state']=='archived'
        assert c.get('/api/documents',params={'organization_id':str(env[1])}).json()['documents']==[]
        assert len(c.get('/api/documents',params={'organization_id':str(env[1]),'archived':True}).json()['documents'])==1
        assert c.post('/api/documents/'+id+'/restore',json={**change,'revision':2}).status_code==403
        login(specialist,'specialist')
        assert specialist.post('/api/documents/'+id+'/restore',json={**change,'revision':2}).json()['state']=='draft'
        with engine.connect() as db:
            assert not db.execute(text('SELECT stopped FROM operation_targets WHERE id=:id'),{'id':id}).scalar_one()
            assert db.execute(text('SELECT count(*) FROM external_operations')).scalar_one()==0


def test_archive_cancels_only_waiting_commands_and_blocks_unsafe_restore(env,engine):
    with client(env) as c,client(env) as specialist:
        login(c);doc,_=create(c,env);id=doc['id']
        with engine.begin() as db:
            db.execute(text("INSERT INTO operation_contracts(kind,permission_id,version,semantic,approved,evidence_ref) VALUES ('fixture.doc','marking.codes.issue','synthetic','read',true,'fixture only')"))
            command=ops.prepare(db,organization_id=env[1],actor_id=env[3]['bm'],kind='fixture.doc',target_id=id,target_revision=1,idempotency_key='queued',payload=b'synthetic')
            sent=ops.prepare(db,organization_id=env[1],actor_id=env[3]['bm'],kind='fixture.doc',target_id=id,target_revision=1,idempotency_key='sent',payload=b'synthetic')
            ops.transition(db,sent['id'],'unknown','synthetic uncertainty')
        value={'revision':1,'confirmation':'INV-01','reason':'Закупка отменена'}
        assert c.post('/api/documents/'+id+'/archive',json=value).status_code==200
        with engine.connect() as db:
            assert db.execute(text('SELECT state FROM external_operations WHERE id=:id'),{'id':command['id']}).scalar_one()=='cancelled_before_dispatch'
            assert db.execute(text('SELECT state FROM external_operations WHERE id=:id'),{'id':sent['id']}).scalar_one()=='unknown'
        login(specialist,'specialist');assert specialist.post('/api/documents/'+id+'/restore',json={**value,'revision':2}).status_code==409


def test_concurrent_edits_have_single_winner(env):
    with client(env) as first,client(env) as second:
        login(first);login(second);doc,value=create(first,env)
        edit={k:v for k,v in value.items() if k!='request_id'};edit['revision']=1
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda c:c.put('/api/documents/'+doc['id'],json=edit).status_code,[first,second]))
        assert sorted(results)==[200,409]


def test_excel_preview_mapping_formula_errors_and_commit_permission(env,engine):
    content=excel.xlsx({'Товары':[['A','B','C','D','E'],['PN/A-1',GTIN,10,3,'8542399000']]})
    with client(env) as c:
        login(c);url='/api/documents/import-preview?organization_id='+str(env[1])
        result=c.post(url,content=content).json();assert result['requires_mapping']
        params={'organization_id':str(env[1]),'sheet_name':'Товары','header_row':1,'mapping':'{"pn":0,"gtin":1,"quantity":2,"labels":3,"tnved":4}'}
        response=c.post(url,params=params,content=content)
        assert response.status_code==200,response.text
        result=response.json()
        assert not result['errors'] and result['lines'][0]['pn']=='PN/A-1' and result['external_calls'] is False
        with engine.begin() as db:db.execute(text("UPDATE group_grants SET scope='none' WHERE group_slug='bm' AND permission_id='marking.import.commit'"))
        assert c.post('/api/documents',json=body(env,imported=True,lines=result['lines'])).status_code==403
        assert c.post(url,content=b'not an xlsx').status_code==422
        assert c.post(url,content=b'x'*(10*1024*1024+1)).status_code==413
        assert c.get('/api/documents/template',params={'organization_id':str(env[1])}).status_code==200
