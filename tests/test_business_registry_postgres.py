"""Regression through business allocation, registry history and saved artifacts."""
from hashlib import sha256
from io import BytesIO
from uuid import UUID,uuid4
import zipfile
import pytest
from sqlalchemy import text
from test_auth_postgres import env,client,login
from test_documents_postgres import body as invoice
from test_gs1 import sample,GTIN
from marking_erp.registry import capture_code,register_product
from marking_erp.gs1 import with_quantity
from marking_erp.business import store
from marking_erp.business.marking import apply
from marking_erp.business.worker import background
from marking_erp.business.registry import backfill

pytestmark=[pytest.mark.postgres,pytest.mark.usefixtures('fresh_schema')]


@pytest.fixture
def allocated(env,engine,tmp_path):
    env[0].file_storage_path=tmp_path/'files'
    with client(env) as c:
        login(c)
        value=invoice(env);value['lines'][0]['labels']=1
        id=c.post('/api/documents',json=value).json()['id']
        assert c.post('/api/documents/'+id+'/prepare',json={'revision':1}).status_code==200
    with engine.begin() as c:
        register_product(c,env[1],GTIN,'PN/A-1','8542399000',source='external_verified',source_ref='fixture:nk',evidence_sha256='1'*64)
        actor=background(c,env[3]['specialist'],env[1])
        row=store.create(actor,'marking','INV-01',{'supplier':value['supplier'],'lines':value['lines'],'source_revision':2},env[1],UUID(id),state='receiving',owner=env[3]['bm'])
        apply(actor,row,{'id':uuid4(),'kind':'bm.fetch'},{'metadata':{'gtin':GTIN,'expected':1}},{},{'codes':[sample(37).decode()]},None,env[0])
        code=dict(c.execute(text('SELECT * FROM business_codes WHERE item_id=:id'),{'id':row['id']}).mappings().one())
    return env,row['id'],UUID(id),code


def test_received_code_is_owned_by_bm_and_registry_pdf_is_exact_saved_pdf(allocated,engine):
    env,item,source,code=allocated;org=str(env[1])
    with client(env) as c:
        login(c)
        result=c.get('/api/codes?organization_id='+org).json()['codes']
        assert len(result)==1 and result[0]['label_quantity']==10
        assert result[0]['id']==str(code['registry_code_id'])
        history=c.get('/api/codes/'+result[0]['id']+'/history?organization_id='+org).json()
        pdf=next(f for f in history['files'] if f['kind']=='label_pdf')
        data=c.get('/api/files/'+pdf['id']).content
        with engine.connect() as db:
            saved=db.execute(text("SELECT body FROM business_artifacts WHERE item_id=:id AND kind='labels'"),{'id':item}).scalar_one()
        with zipfile.ZipFile(BytesIO(bytes(saved))) as archive:assert data==archive.read('00001.pdf')
    with engine.connect() as c:
        assert c.execute(text('SELECT owner_id FROM code_access')).scalar_one()==env[3]['bm']
        assert c.execute(text('SELECT owner_id FROM operation_targets WHERE id=:id'),{'id':item}).scalar_one()==env[3]['bm']


def test_reallocation_versions_ai30_without_false_conflict_and_blocks_stale_pdf(allocated,engine):
    env,item,source,code=allocated;org=str(env[1]);cid=str(code['registry_code_id'])
    with client(env) as c:
        login(c)
        history=c.get('/api/codes/'+cid+'/history?organization_id='+org).json()
        old_pdf=next(f['id'] for f in history['files'] if f['kind']=='label_pdf')
        row=c.get('/api/business/'+str(item)).json()['item']
        lines=[{**row['body']['lines'][0],'quantity':25}]
        result=c.put('/api/business/'+str(item)+'/reallocate',json={'revision':row['revision'],'lines':lines,'confirmed':True,'reason':'Changed packing allocation'})
        assert result.status_code==200,result.text
        history=c.get('/api/codes/'+cid+'/history?organization_id='+org).json()
        assert [e['kind'] for e in history['history']]==['quantity_adjusted','captured']
        assert c.get('/api/files/'+old_pdf).status_code==409
        result=c.get('/api/codes?organization_id='+org).json()['codes'][0]
        assert result['label_quantity']==25 and result['conflicts']==0
    # Observing the current label is harmless; an old label or changed crypto
    # remains a conflict even when the caller requests a quantity adjustment.
    with engine.begin() as c:
        same=capture_code(c,env[1],env[3]['bm'],with_quantity(sample(37),25),actor_id=env[3]['bm'],source_ref='fixture:current')
        old=capture_code(c,env[1],env[3]['bm'],with_quantity(sample(37),10),actor_id=env[3]['bm'],source_ref='fixture:old-label')
        changed=capture_code(c,env[1],env[3]['bm'],with_quantity(sample(37)[:-1]+b'2',50),actor_id=env[3]['bm'],source_ref='fixture:changed-crypto',allow_quantity_change=True)
        assert same['kind']=='reobserved' and old['conflict'] and changed['conflict']


def test_warehouse_assignment_is_live_and_cancel_revokes_registry_access(allocated,engine):
    env,item,source,code=allocated;org=str(env[1]);task=uuid4()
    with client(env) as logistics:
        login(logistics,'logistics')
        response=logistics.post('/api/logistics/warehouse-transfer',json={'request_id':str(task),'source_id':str(source),'source_revision':2,'kind':'receipt','assignees':[str(env[3]['warehouse'])]})
        assert response.status_code==200,response.text
    with client(env) as c:
        login(c,'warehouse')
        assert len(c.get('/api/codes?organization_id='+org).json()['codes'])==1
        assert c.get('/api/codes/'+str(code['registry_code_id'])+'/history?organization_id='+org).status_code==200
        with engine.begin() as db:db.execute(text("UPDATE warehouse_tasks SET state='cancelled' WHERE id=:id"),{'id':task})
        assert c.get('/api/codes?organization_id='+org).json()['codes']==[]
        assert c.get('/api/codes/'+str(code['registry_code_id'])+'/history?organization_id='+org).status_code==403


def test_backfill_copies_existing_zip_and_is_idempotent(allocated,engine):
    env,item,source,code=allocated
    # Emulate a legacy unlinked allocation while keeping immutable history.
    with engine.begin() as c:
        c.execute(text('UPDATE business_codes SET registry_code_id=NULL,registry_payload_id=NULL WHERE id=:id'),{'id':code['id']})
        assert backfill(c,env[0],env[3]['administrator'])==1
        assert backfill(c,env[0],env[3]['administrator'])==0
        latest=c.execute(text('SELECT registry_payload_id FROM business_codes WHERE id=:id'),{'id':code['id']}).scalar_one()
        saved=c.execute(text("SELECT body FROM business_artifacts WHERE item_id=:id AND kind='labels'"),{'id':item}).scalar_one()
        file=c.execute(text("SELECT * FROM files WHERE payload_id=:id AND kind='label_pdf'"),{'id':latest}).mappings().one()
        from marking_erp.file_store import FileStore
        with zipfile.ZipFile(BytesIO(bytes(saved))) as archive:assert FileStore(env[0].file_storage_path).read(file)==archive.read('00001.pdf')
        assert sha256(FileStore(env[0].file_storage_path).read(file)).hexdigest()==file['sha256']


def test_split_one_unit_still_has_explicit_ai30_and_registry_entry(env,engine,tmp_path):
    env[0].file_storage_path=tmp_path/'files'
    with engine.begin() as c:
        register_product(c,env[1],GTIN,'PN/A-1','8542399000',source='external_verified',source_ref='fixture:nk',evidence_sha256='1'*64)
        actor=background(c,env[3]['specialist'],env[1])
        row=store.create(actor,'split','PN/A-1',{'pn':'PN/A-1','tnved':'8542399000','quantity':1},env[1],state='receiving')
        apply(actor,row,{'id':uuid4(),'kind':'split.fetch'},{'metadata':{'gtin':GTIN,'expected':1}},{},{'codes':[sample(37).decode()]},None,env[0])
        code=c.execute(text('SELECT payload,registry_payload_id FROM business_codes')).mappings().one()
        assert bytes(code['payload'])==with_quantity(sample(37),1) and code['registry_payload_id']
