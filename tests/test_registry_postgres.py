from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from uuid import uuid4
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_auth_postgres import env,client,login
from test_gs1 import sample,GTIN,SERIAL
from marking_erp.registry import capture_code,register_product,save_payload_file,save_label
from marking_erp.file_store import FileStore
from marking_erp.gs1 import GS

pytestmark=[pytest.mark.postgres,pytest.mark.usefixtures('fresh_schema')]

@pytest.fixture
def registry_env(env,engine,tmp_path):
    env[0].file_storage_path=tmp_path/'files'
    with engine.begin() as c:
        product=register_product(c,env[1],GTIN,'PN/A-1','8542399000',source='nk_confirmed',source_ref='fixture:nk/1',evidence_sha256='1'*64)
        capture=capture_code(c,env[1],env[3]['bm'],sample(36,quantity=50),actor_id=env[3]['bm'],source_ref='fixture:scanner')
        binary=save_payload_file(c,FileStore(env[0].file_storage_path),capture['payload_id'])
        pdf=save_label(c,FileStore(env[0].file_storage_path),capture['payload_id'],'PN/A-1')
        c.execute(text('INSERT INTO code_assignments VALUES (:code,:org,:user)'),{'code':capture['id'],'org':env[1],'user':env[3]['warehouse']})
    return env,capture,binary,pdf


def test_registry_own_assigned_all_tsd_and_foreign_org(registry_env,engine):
    env,code,binary,pdf=registry_env;org=str(env[1]);path='/api/codes?organization_id='+org
    for group,expected in [('bm',1),('logistics',0),('warehouse',1),('specialist',1),('administrator',1)]:
        with client(env) as c:
            login(c,group);r=c.get(path);assert r.status_code==200,r.text
            assert len(r.json()['codes'])==expected
            assert 'raw' not in r.text and 'crypto' not in r.text
    with client(env) as c:
        login(c,'tsd');assert c.get(path).status_code==403
        assert c.get('/api/codes/'+str(code['id'])+'/history?organization_id='+org).status_code==403
    with client(env) as c:
        login(c,'bm')
        assert c.get('/api/codes?organization_id='+str(env[2])).status_code==403
        r=c.get('/api/codes/'+str(code['id'])+'/payload/'+str(code['payload_id'])+'?organization_id='+org)
        assert r.content==sample(36,quantity=50) and r.headers['content-type'].startswith('application/octet-stream')
        assert c.get('/api/files/'+str(binary)).content==sample(36,quantity=50)
        assert c.get('/api/files/'+str(pdf)).content.startswith(b'%PDF')
    with client(env) as c:
        login(c,'logistics');assert c.get('/api/files/'+str(binary)).status_code==403
        assert c.get('/api/codes/'+str(code['id'])+'/history?organization_id='+org).status_code==403
    with engine.begin() as c:
        outsider=uuid4();c.execute(text("INSERT INTO users(id,username,display_name,password_hash,group_slug) SELECT :id,'outsider','Outside',password_hash,'bm' FROM users WHERE group_slug='bm'"),{'id':outsider})
        c.execute(text('INSERT INTO user_organizations VALUES (:u,:o)'),{'u':outsider,'o':env[2]})
        other=capture_code(c,env[2],outsider,sample(37,SERIAL[:-1]+b'Y',12),actor_id=outsider,source_ref='outside')
        otherfile=save_payload_file(c,FileStore(env[0].file_storage_path),other['payload_id'])
    with client(env) as c:
        login(c,'bm');assert c.get('/api/files/'+str(otherfile)).status_code==403


def test_conflicting_payload_does_not_replace_original_and_history_is_immutable(registry_env,engine):
    env,code,_,_=registry_env;original=sample(36,quantity=50)
    with engine.begin() as c:
        repeat=capture_code(c,env[1],env[3]['bm'],b']d2'+original+b'\r\n',actor_id=env[3]['bm'],source_ref='fixture:rescan',scanner=True)
        conflict=capture_code(c,env[1],env[3]['bm'],sample(36,quantity=25),actor_id=env[3]['bm'],source_ref='fixture:conflict')
        assert repeat['kind']=='reobserved' and conflict['conflict'] and conflict['id']==code['id']
        with pytest.raises(ValueError):save_label(c,FileStore(env[0].file_storage_path),conflict['payload_id'],'PN/A-1')
    with client(env) as c:
        login(c,'bm');result=c.get('/api/codes?organization_id='+str(env[1])).json()['codes'][0]
        assert result['label_quantity']==50 and result['conflicts']==1
        rows=c.get('/api/codes/'+str(code['id'])+'/history?organization_id='+str(env[1])).json()['history'];assert len(rows)==3 and rows[0]['kind']=='payload_conflict'
        assert rows[1]['corrections']==['aim-prefix-removed','crlf-terminator-removed']
    for table in ('codes','products','product_pns','code_payload_versions','code_events','files'):
        with pytest.raises(DBAPIError),engine.begin() as c:c.execute(text('DELETE FROM '+table))


def test_concurrent_capture_unique_identity_and_versions(env,engine):
    def capture(_):
        with engine.begin() as c:return capture_code(c,env[1],env[3]['bm'],sample(37),actor_id=env[3]['bm'],source_ref='fixture:concurrent')
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(capture,range(2)))
    assert results[0]['id']==results[1]['id'] and sorted(x['version'] for x in results)==[1,2]


def test_product_provenance_aliases_tnved_conflict_and_checksum(registry_env,engine):
    env,code,_,_=registry_env
    with engine.begin() as c:
        result=register_product(c,env[1],GTIN,'PN/a-2','8542399001',source='external_verified',source_ref='fixture:other',evidence_sha256='2'*64)
        assert result['tnved_conflict'] is True
    with client(env) as c:
        login(c,'bm');rows=c.get('/api/products?organization_id='+str(env[1])).json()['products'];assert len(rows)==2
        assert {r['pn'] for r in rows}=={'PN/A-1','PN/a-2'} and all(r['source_ref'] for r in rows)
    with pytest.raises(ValueError),engine.begin() as c:register_product(c,env[1],GTIN,'fake','8542399000',source='title_guess',source_ref='x',evidence_sha256='1'*64)
    with pytest.raises(DBAPIError),engine.begin() as c:c.execute(text("INSERT INTO products(id,gtin) VALUES (:id,'04640638349705')"),{'id':uuid4()})


def test_files_hash_failure_revoked_grant_and_sql_literal_search(registry_env,engine):
    env,code,binary,_=registry_env
    with client(env) as c:
        login(c,'bm');org=str(env[1])
        assert c.get('/api/codes?organization_id='+org+'&q=%25').json()['codes']==[]
        with engine.connect() as db:key=db.execute(text('SELECT storage_key FROM files WHERE id=:id'),{'id':binary}).scalar_one()
        (env[0].file_storage_path/key).write_bytes(b'corrupt')
        r=c.get('/api/files/'+str(binary));assert r.status_code==503 and str(env[0].file_storage_path) not in r.text
        with engine.begin() as db:db.execute(text("UPDATE group_grants SET scope='none' WHERE group_slug='bm' AND permission_id='codes.payload.export'"))
        assert c.get('/api/codes/'+str(code['id'])+'/payload/'+str(code['payload_id'])+'?organization_id='+org).status_code==403
        assert c.get('/api/files/'+str(binary)).status_code==403


def test_keyset_pagination_has_no_duplicate_or_missing_codes(env,engine):
    with engine.begin() as c:
        for i in range(3):capture_code(c,env[1],env[3]['bm'],sample(37,SERIAL[:-1]+str(i).encode()),actor_id=env[3]['bm'],source_ref='fixture:pages')
    with client(env) as c:
        login(c,'bm');base='/api/codes?organization_id='+str(env[1])+'&limit=2';one=c.get(base).json();two=c.get(base+'&before_id='+one['next_cursor']).json()
        assert len(one['codes'])==2 and len(two['codes'])==1 and two['next_cursor'] is None
        assert len({x['id'] for x in one['codes']+two['codes']})==3
