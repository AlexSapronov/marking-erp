"""Fallback sources remain separate from BM, with real scopes and warehouse rules."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
from functools import lru_cache
from uuid import uuid4
import json
import pytest
from sqlalchemy import text
from test_auth_postgres import env,client,login
from test_warehouse_postgres import raw,scan,GTIN
from test_warehouse_overlap_postgres import source,request,transfer
from test_warehouse_check_postgres import enable,check,fake_info
from test_warehouse_excel import workbook
from marking_erp.business.live import Live

pytestmark=[pytest.mark.postgres,pytest.mark.usefixtures('fresh_schema')]


@lru_cache(maxsize=1)
def content():return workbook([['00123',GTIN,raw(),10,'8542399000'],['Unmarked','','',4,'']])


def preview(c,env,raw_file=None,kind='receipt',org=None):
    return c.post('/api/warehouse/imports/preview',params={'organization_id':str(org or env[1]),'kind':kind,'filename':'source.xlsx'},content=raw_file or content(),headers={'Content-Type':'application/octet-stream'})


def body(env,id):
    return {'request_id':str(uuid4()),'preview_id':id,'number':'RESERVE-01','document_date':'2026-10-27','supplier':'Контрагент','reason':'Нет исходной накладной ERP','assignees':[str(env[3]['tsd'])]}


def create(c,env,kind='receipt'):
    result=preview(c,env,kind=kind);assert result.status_code==200,result.text
    data=result.json();assert data['preview_id'] and not data['errors'];value=body(env,data['preview_id'])
    response=c.post('/api/warehouse/imports/commit',json=value);assert response.status_code==200,response.text
    return response.json()['id'],value,data


@pytest.mark.parametrize('kind',['receipt','shipment'])
def test_full_lifecycle_source_download_replay_and_no_bm_or_emission(env,engine,kind):
    with client(env) as importer:
        login(importer,'specialist');id,value,data=create(importer,env,kind)
        assert importer.post('/api/warehouse/imports/commit',json=value).json()['replayed']
        changed={**value,'number':'changed'};assert importer.post('/api/warehouse/imports/commit',json=changed).status_code==409
        changed={**value,'request_id':str(uuid4())};assert importer.post('/api/warehouse/imports/commit',json=changed).status_code==409
        download=importer.get('/api/warehouse/tasks/'+id+'/source-file');assert download.status_code==200 and download.content==content()
        assert data['expected_count']==1 and 'expected_payloads' not in data and 'expected_codes' not in data
    with client(env) as c:
        login(c,'tsd');url='/api/warehouse/tasks/'+id;task=c.get(url).json();assert task['task']['source_type']=='excel' and not task['paused']
        assert c.get(url+'/source-file').status_code==403
        assert scan(c,id,{'request_id':str(uuid4()),'raw':raw().replace('93ABCD','93WXYZ')}).status_code==422
        receipt={'request_id':str(uuid4()),'raw':raw()};assert scan(c,id,receipt).status_code==200
        result,_=check(c,id);assert result['ok'] and not result['external_verified']
        task=c.get(url).json();assert c.post(url+'/close',json={'revision':task['task']['revision'],'confirmation':value['number']}).status_code==200
        assert scan(c,id,receipt).json()['replayed']
        if kind=='shipment':assert c.get(url+'/export').status_code==200
    with client(env) as c:
        login(c,'specialist');assert c.post('/api/warehouse/imports/commit',json=value).json()['replayed']
    with engine.connect() as db:
        assert db.execute(text('SELECT count(*) FROM bm_documents')).scalar_one()==0
        assert db.execute(text('SELECT count(*) FROM business_items')).scalar_one()==0
        assert db.execute(text('SELECT count(*) FROM external_operations')).scalar_one()==0
        assert db.execute(text('SELECT count(*) FROM warehouse_sources WHERE kind=\'excel\'')).scalar_one()==1


def test_permissions_and_invalid_preview_never_create_sources(env,engine):
    with client(env) as c:
        login(c,'warehouse');assert preview(c,env).status_code==403
    with client(env) as c:
        login(c,'specialist');assert preview(c,env,org=env[2]).status_code==403
        value=preview(c,env,workbook([[123,GTIN,raw(),10,'']]));assert value.status_code==200 and value.json()['preview_id'] is None
        assert c.post('/api/warehouse/imports/commit',json=body(env,str(uuid4()))).status_code==404
    with engine.connect() as db:
        for table in ('warehouse_imports','warehouse_sources','warehouse_tasks'):assert db.execute(text('SELECT count(*) FROM '+table)).scalar_one()==0


def test_preview_permission_does_not_grant_commit_and_revoke_applies_on_replay(env,engine):
    with client(env) as c:
        login(c,'specialist');data=preview(c,env).json();value=body(env,data['preview_id'])
        with engine.begin() as db:db.execute(text("UPDATE group_grants SET scope='none' WHERE group_slug='specialist' AND permission_id='warehouse.import.commit'"))
        assert c.post('/api/warehouse/imports/commit',json=value).status_code==403
        with engine.begin() as db:db.execute(text("UPDATE group_grants SET scope='all' WHERE group_slug='specialist' AND permission_id='warehouse.import.commit'"))
        response=c.post('/api/warehouse/imports/commit',json=value);assert response.status_code==200,response.text
        with engine.begin() as db:db.execute(text("UPDATE group_grants SET scope='none' WHERE group_slug='specialist' AND permission_id='warehouse.import.commit'"))
        assert c.post('/api/warehouse/imports/commit',json=value).status_code==403


def test_own_scope_cannot_commit_another_users_preview(env,engine):
    with client(env) as c:
        login(c,'specialist');data=preview(c,env).json()
    with engine.begin() as db:
        for p in ('warehouse.import.preview','warehouse.import.commit'):
            db.execute(text("UPDATE group_grants SET scope='own' WHERE group_slug='administrator' AND permission_id=:p"),{'p':p})
    with client(env) as c:
        login(c,'administrator');assert c.post('/api/warehouse/imports/commit',json=body(env,data['preview_id'])).status_code==403


def test_assignment_validation_and_cross_source_overlap(env):
    with client(env) as c:
        login(c,'specialist');data=preview(c,env).json();value=body(env,data['preview_id']);value['assignees']=[str(env[3]['bm'])]
        assert c.post('/api/warehouse/imports/commit',json=value).status_code==422
        id,_,_=create(c,env)
    source_id,_=source(env)
    with client(env) as c:
        login(c,'logistics');assert transfer(c,request(env,source_id)).status_code==409
    with client(env) as c:
        login(c,'specialist');data=preview(c,env).json();assert c.post('/api/warehouse/imports/commit',json=body(env,data['preview_id'])).status_code==409


def test_concurrent_duplicate_commit_has_one_task(env,engine):
    with client(env) as a,client(env) as b:
        login(a,'specialist');login(b,'specialist');data=preview(a,env).json();value=body(env,data['preview_id'])
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(c.post,'/api/warehouse/imports/commit',json=value) for c in (a,b)]
            results=[f.result(timeout=10) for f in futures];assert all(r.status_code==200 for r in results)
            assert sorted(r.json().get('replayed',False) for r in results)==[False,True]
    with engine.connect() as db:assert db.execute(text('SELECT count(*) FROM warehouse_tasks')).scalar_one()==1


def test_live_check_uses_imported_full_expectations_and_can_close(env,monkeypatch):
    enable(env);calls=[];monkeypatch.setattr(Live,'info',fake_info(calls))
    with client(env) as c:
        login(c,'specialist');id,value,_=create(c,env)
    with client(env) as c:
        login(c,'tsd');assert scan(c,id).status_code==200 and not calls
        result,_=check(c,id);assert result['ok'] and result['external_verified'] and len(calls)==1
        revision=c.get('/api/warehouse/tasks/'+id).json()['task']['revision']
        assert c.post('/api/warehouse/tasks/'+id+'/close',json={'revision':revision,'confirmation':value['number']}).status_code==200
        assert len(calls)==1


def test_import_identifier_collision_cancel_and_replay_keep_source(env):
    with client(env) as c:
        login(c,'specialist');data=preview(c,env).json();value=body(env,data['preview_id'])
        assert c.post('/api/warehouse/imports/commit',json={**value,'request_id':data['preview_id']}).status_code==422
        created=c.post('/api/warehouse/imports/commit',json=value);assert created.status_code==200,created.text
        id=created.json()['id'];revision=c.get('/api/warehouse/tasks/'+id).json()['task']['revision']
        assert c.post('/api/warehouse/tasks/'+id+'/cancel',json={'revision':revision,'confirmation':value['number'],'reason':'Ошибочный резервный состав'}).status_code==200
        assert c.post('/api/warehouse/imports/commit',json=value).json()['replayed']
        assert c.get('/api/warehouse/tasks/'+id+'/source-file').content==content()
    with client(env) as c:
        login(c,'tsd');assert scan(c,id).status_code==409


def test_migration_backfills_existing_bm_sources_without_changing_tasks(env,engine):
    from alembic import command
    from marking_erp.migrate import migration_config
    from test_warehouse_postgres import setup
    id,number,source_id=setup(env,labels=1)
    with engine.begin() as db:
        before=dict(db.execute(text('SELECT * FROM warehouse_tasks WHERE id=:id'),{'id':id}).mappings().one())
        config=migration_config();config.attributes['connection']=db
        command.downgrade(config,'0016_warehouse_overlap');command.upgrade(config,'head')
        after=dict(db.execute(text('SELECT * FROM warehouse_tasks WHERE id=:id'),{'id':id}).mappings().one())
        assert before==after
        assert str(db.execute(text('SELECT bm_document_id FROM warehouse_sources WHERE id=:id'),{'id':source_id}).scalar_one())==source_id
    with client(env) as c:
        login(c,'tsd');assert c.get('/api/warehouse/tasks/'+id).status_code==200
        assert scan(c,id).status_code==200
