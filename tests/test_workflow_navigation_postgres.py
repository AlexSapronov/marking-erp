"""Local chain reads with independent relationship scopes and zero commands."""
import json
from uuid import uuid4,UUID
import pytest
from sqlalchemy import text
from test_auth_postgres import env,client,login
from test_business_registry_postgres import allocated
from test_documents_postgres import create
from test_warehouse_postgres import setup
from marking_erp.business import store
from marking_erp.business.worker import background

pytestmark=[pytest.mark.postgres,pytest.mark.usefixtures('fresh_schema')]


@pytest.fixture(autouse=True)
def no_worker_timer(monkeypatch):
    monkeypatch.setattr('marking_erp.business.worker.BusinessWorker.start',lambda self:None)


def chain(c,kind,id):
    response=c.get('/api/workflow/'+kind+'/'+str(id))
    assert response.status_code==200,response.text
    return response.json()


def links(data):return [item for group in data['groups'] for item in group['items']]


def card(c,env,pn='PN/A-1',gtin='04640638349704',**kw):
    actor=background(c,env[3]['bm'],env[1])
    return store.create(actor,'card',kw.pop('title',pn),{'pn':pn,'gtin':gtin,'dirty':False,'external':{'statuses':['published']},'private_token':'PRIVATE_TOKEN'},env[1],**kw)


def test_invoice_process_catalog_and_back_links_are_exact_and_read_only(allocated,engine,monkeypatch):
    env,item,source,_=allocated
    def no_network(*a,**k):raise AssertionError('Navigation must not call ChZ or NK')
    monkeypatch.setattr('marking_erp.business.live.Live.info',no_network)
    with engine.begin() as db:
        correct=card(db,env);wrong=card(db,env,pn='Other PN');unconfirmed=card(db,env,gtin='')
        before=[db.execute(text('SELECT count(*) FROM '+t)).scalar_one() for t in ['external_operations','audit_events','business_events']]
    with client(env) as c:
        login(c)
        # Login audit is unrelated to navigation; compare subsequent read snapshots.
        with engine.connect() as db:before=[db.execute(text('SELECT count(*) FROM '+t)).scalar_one() for t in ['external_operations','audit_events','business_events']]
        data=chain(c,'document',source);all_links=links(data)
        assert any(v['target']['id']==str(item) for v in all_links)
        assert any(v['target']['id']==str(correct['id']) for v in all_links)
        assert str(wrong['id']) not in json.dumps(data) and str(unconfirmed['id']) not in json.dumps(data)
        assert data['stage']=='Этикетки готовы' and 'этикетки' in data['next_action']
        assert any(v['target']['id']==str(source) for v in links(chain(c,'business',item)))
        assert any(v['target']['id']==str(source) for v in links(chain(c,'business',correct['id'])))
        assert 'PRIVATE_TOKEN' not in json.dumps(data)
    with engine.connect() as db:
        assert [db.execute(text('SELECT count(*) FROM '+t)).scalar_one() for t in ['external_operations','audit_events','business_events']]==before


def test_hidden_process_is_not_exposed_through_invoice_or_navigation(allocated,engine):
    env,item,source,_=allocated
    with engine.begin() as db:
        db.execute(text('UPDATE business_items SET owner_id=:owner,title=:title WHERE id=:id'),{'owner':env[3]['specialist'],'title':'PRIVATE_PROCESS','id':item})
        db.execute(text("UPDATE group_grants SET scope='own' WHERE group_slug='bm' AND permission_id='marking.documents.view'"))
    with client(env) as c:
        login(c);data=chain(c,'document',source)
        assert str(item) not in json.dumps(data) and 'PRIVATE_PROCESS' not in json.dumps(data)
        assert 'назначенный' in ' '.join(data['blockers'])
        doc=c.get('/api/documents/'+str(source)).json()['document']
        assert doc['marking_id'] is None and doc['marking_in_progress'] is True
        assert doc['workflow']['process'] is None
        assert c.get('/api/workflow/business/'+str(item)).status_code==403


def test_assigned_warehouse_does_not_gain_source_or_process_visibility(env,engine):
    id,_,source=setup(env,labels=1)
    with client(env) as tsd:
        login(tsd,'tsd');data=chain(tsd,'warehouse',id)
        assert any(v['target']['id']==id for v in links(data))
        assert source not in json.dumps(data)
        assert data['responsible_role'].startswith('Склад')
        with engine.begin() as db:db.execute(text('DELETE FROM operation_assignments WHERE target_id=:id'),{'id':id})
        assert tsd.get('/api/workflow/warehouse/'+id).status_code==403


def test_relationship_scope_filters_before_limit_and_tracks_archive(allocated,engine):
    env,item,source,_=allocated
    with engine.begin() as db:
        visible=card(db,env,title='VISIBLE_CARD')
        for _ in range(30):card(db,env,title='PRIVATE_CARD',owner=env[3]['logistics'])
        db.execute(text("UPDATE group_grants SET scope='own' WHERE group_slug='bm' AND permission_id='nk.cards.view'"))
    with client(env) as c:
        login(c);data=chain(c,'document',source);serialized=json.dumps(data)
        assert str(visible['id']) in serialized and 'PRIVATE_CARD' not in serialized
        with engine.begin() as db:
            db.execute(text("UPDATE business_items SET state='archived' WHERE id=:id"),{'id':visible['id']})
            db.execute(text("UPDATE group_grants SET scope='none' WHERE group_slug='bm' AND permission_id='documents.archive.view'"))
        assert str(visible['id']) not in json.dumps(chain(c,'document',source))
        assert c.get('/api/workflow/business/'+str(visible['id'])).status_code==403


def test_atk_fts_transform_chain_and_logistics_blockers(allocated,engine):
    env,item,source,code=allocated
    with engine.begin() as db:
        actor=background(db,env[3]['specialist'],env[1])
        atk=store.create(actor,'atk','ATK fixture',{'documents':[str(item)],'codes':[str(code['id'])],'atk':'confirmed-fixture'},env[1],state='transferred')
        fts=store.create(actor,'fts','FTS fixture',{'atks':[str(atk['id'])],'codes':[]},env[1],state='prepared')
        transform=store.create(actor,'transform','Transform fixture',{'atk_item':str(atk['id'])},env[1],state='awaiting_approval')
    with client(env) as c:
        login(c,'specialist')
        for root in [atk,fts,transform]:
            data=chain(c,'business',root['id']);ids=[v['target']['id'] for v in links(data)]
            assert str(source) in ids and str(item) in ids and str(atk['id']) in ids and str(fts['id']) in ids
        data=chain(c,'logistics',item)
        assert data['blockers'] and any('приёмку' in v for v in data['blockers'])
        assert any(v['target']['type']=='completion' for v in links(data))
    with engine.connect() as db:assert db.execute(text('SELECT count(*) FROM external_operations')).scalar_one()==0


def test_catalog_publication_hints_and_local_dirty_blocker(env,engine):
    with engine.begin() as db:row=card(db,env)
    with client(env) as c:
        login(c);data=chain(c,'business',row['id']);assert 'подтверждённый товар' in data['next_action']
        with engine.begin() as db:db.execute(text("UPDATE business_items SET body=jsonb_set(body,'{dirty}','true') WHERE id=:id"),{'id':row['id']})
        data=chain(c,'business',row['id']);assert data['blockers'] and 'сохранить' in data['next_action']
        assert c.get('/api/workflow/unknown/'+str(row['id'])).status_code==404
        assert c.get('/api/workflow/business/not-a-uuid').status_code==422


def test_cross_organization_root_never_grants_navigation(env,engine):
    with client(env) as c:
        login(c);doc,_=create(c,env)
        with engine.begin() as db:
            db.execute(text('DELETE FROM user_organizations WHERE user_id=:user'),{'user':env[3]['bm']})
            db.execute(text('INSERT INTO user_organizations VALUES (:user,:org)'),{'user':env[3]['bm'],'org':env[2]})
        assert c.get('/api/workflow/document/'+doc['id']).status_code==403


def test_warehouse_archived_chain_requires_fresh_admin_scope(env,engine):
    id,_,_=setup(env,labels=1)
    with engine.begin() as db:db.execute(text("UPDATE warehouse_tasks SET state='closed',archived_at=now() WHERE id=:id"),{'id':id})
    with client(env) as c:
        login(c,'tsd');assert c.get('/api/workflow/warehouse/'+id).status_code==403
    with client(env) as c:
        login(c,'administrator');data=chain(c,'warehouse',id)
        assert 'архиве' in data['next_action']
        with engine.begin() as db:db.execute(text("UPDATE sessions SET elevated_at=now()-interval '6 minutes' WHERE user_id=:id"),{'id':env[3]['administrator']})
        assert c.get('/api/workflow/warehouse/'+id).status_code==403


def test_unknown_external_result_blocks_naive_next_step_without_repeating(allocated,engine):
    from marking_erp import operations
    env,item,source,_=allocated
    with engine.begin() as db:
        actor=background(db,env[3]['bm'],env[1]);row=store.item(db,item)
        op=store.queue(actor,row,'bm.order',{'fixture':'navigation-only'})
        operations.transition(db,op,'unknown','PRIVATE_REASON_WITH_TOKEN')
        before=db.execute(text('SELECT count(*) FROM external_operations')).scalar_one()
    with client(env) as c:
        login(c);data=chain(c,'document',source)
        assert 'неизвестный результат' in data['next_action']
        assert any('неизвестен' in b for b in data['blockers'])
        assert 'PRIVATE_REASON' not in json.dumps(data)
    with engine.connect() as db:
        assert db.execute(text('SELECT count(*) FROM external_operations')).scalar_one()==before
        assert db.execute(text('SELECT state FROM external_operations WHERE id=:id'),{'id':op}).scalar_one()=='unknown'


def test_standalone_excel_warehouse_has_no_artificial_upstream_documents(env,engine):
    from test_warehouse_import_postgres import create as import_source
    with client(env) as c:
        login(c,'specialist');id,_,_=import_source(c,env)
        data=chain(c,'warehouse',id)
        assert [group['name'] for group in data['groups']]==['Приёмка и отгрузка']
    with engine.connect() as db:
        assert db.execute(text('SELECT count(*) FROM bm_documents')).scalar_one()==0
        assert db.execute(text('SELECT count(*) FROM external_operations')).scalar_one()==0


def test_large_atk_filters_access_before_public_document_limit(allocated,engine):
    from test_documents_postgres import body as invoice
    env,item,source,_=allocated;processes=[str(item)]
    with client(env) as c:
        login(c)
        for n in range(26):
            value=invoice(env,request_id=str(UUID(int=n+1)),number='PRIVATE-SOURCE-'+str(n))
            response=c.post('/api/documents',json=value);assert response.status_code==200,response.text
            with engine.begin() as db:
                db.execute(text('UPDATE bm_documents SET owner_id=:owner WHERE id=:id'),{'owner':env[3]['logistics'],'id':value['request_id']})
                actor=background(db,env[3]['bm'],env[1])
                hidden=store.create(actor,'marking','PRIVATE-PROCESS',{'lines':value['lines'],'supplier':value['supplier'],'source_revision':1},env[1],UUID(value['request_id']),owner=env[3]['logistics'])
                processes.append(str(hidden['id']))
        with engine.begin() as db:
            actor=background(db,env[3]['bm'],env[1])
            atk=store.create(actor,'atk','Visible ATK',{'documents':processes,'codes':[]},env[1],state='awaiting_approval')
            db.execute(text("UPDATE group_grants SET scope='own' WHERE group_slug='bm' AND permission_id='marking.documents.view'"))
        data=chain(c,'business',atk['id']);serialized=json.dumps(data)
        assert str(source) in serialized and str(item) in serialized
        assert 'PRIVATE-SOURCE' not in serialized and 'PRIVATE-PROCESS' not in serialized
