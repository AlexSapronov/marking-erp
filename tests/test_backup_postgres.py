import os
from pathlib import Path
from uuid import uuid4
import psycopg
from psycopg import sql
import pytest
from sqlalchemy import create_engine,event,text
from sqlalchemy.engine import make_url
from fastapi.testclient import TestClient
from test_auth_postgres import env,client,login,PASSWORD
from test_registry_postgres import registry_env
from test_operations_postgres import queue_env,prepare
from test_gs1 import sample,SERIAL
from marking_erp.backup import BackupError,PostgresTools,create_backup,restore_backup,verify_backup,_connect_args
from marking_erp.registry import capture_code,save_payload_file
from marking_erp.file_store import FileStore
from marking_erp.config import Settings
from marking_erp.web import create_app

pytestmark=[pytest.mark.postgres,pytest.mark.usefixtures('fresh_schema')]

@pytest.fixture(autouse=True)
def actual_source_dsn(env):
    from pydantic import SecretStr
    env[0].database_url=SecretStr(os.environ['ERP_TEST_DATABASE_URL'])

@pytest.fixture
def restore_name():
    admin=make_url(os.environ['ERP_TEST_DATABASE_URL']).set(database='postgres');name='erp_restore_'+uuid4().hex
    yield admin,name
    with psycopg.connect(**_connect_args(admin),autocommit=True) as c:
        c.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s AND pid<>pg_backend_pid()',(name,))
        c.execute(sql.SQL('DROP DATABASE IF EXISTS {}').format(sql.Identifier(name)))


def clone_engine(url,schema):
    engine=create_engine(url,hide_parameters=True)
    @event.listens_for(engine,'connect')
    def search_path(dbapi,record):
        old=dbapi.autocommit;dbapi.autocommit=True
        with dbapi.cursor() as cursor:cursor.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
        dbapi.autocommit=old
    return engine


def test_real_pg_dump_restore_files_auth_and_dispatch_quarantine(registry_env,queue_env,engine,tmp_path,restore_name):
    env,code,binary,pdf=registry_env;admin,name=restore_name
    with client(env) as user:
        login(user);cookie=user.cookies.get('__Host-erp_session')
    with engine.begin() as c:
        op=prepare(c,queue_env);schema=c.execute(text('SELECT current_schema()')).scalar_one()
    bundle=tmp_path/'backup.zip';summary=create_backup(env[0],engine,bundle,db_schema=schema)
    assert summary['file_count']==2 and bundle.stat().st_mode&0o777==0o600
    manifest=verify_backup(bundle);assert manifest['mfa_key_required']
    files=tmp_path/'restored-files';result=restore_backup(env[0],bundle,admin_url=admin,new_database=name,files_path=files)
    assert result['external_dispatch_disabled'] and files.exists()
    clone=clone_engine(admin.set(database=name),schema)
    try:
        settings=Settings(database_url=admin.set(database=name).render_as_string(hide_password=False),public_url='https://testserver',file_storage_path=files,mfa_encryption_key=env[0].mfa_encryption_key)
        with TestClient(create_app(settings,clone),base_url='https://testserver',headers={'Origin':'https://testserver','X-Requested-With':'marking-erp'}) as c:
            c.cookies.set('__Host-erp_session',cookie);assert c.get('/api/session').status_code==401
            assert c.post('/api/session',json={'username':'u_bm','password':PASSWORD}).status_code==200
            assert c.get('/api/files/'+str(binary)).content==sample(36,quantity=50)
            assert c.get('/api/files/'+str(pdf)).content.startswith(b'%PDF')
        with clone.connect() as c:
            assert c.execute(text('SELECT count(*) FROM sessions')).scalar_one()==1 # Only the NEW login.
            assert c.execute(text('SELECT state FROM external_operations WHERE id=:id'),{'id':op['id']}).scalar_one()=='blocked'
            assert c.execute(text('SELECT count(*) FROM operation_contracts WHERE approved')).scalar_one()==0
            assert c.execute(text("SELECT count(*) FROM audit_events WHERE action='system.restored'")).scalar_one()==1
        with engine.connect() as c:
            assert c.execute(text('SELECT state FROM external_operations WHERE id=:id'),{'id':op['id']}).scalar_one()=='queued'
    finally:clone.dispose()


def test_exported_snapshot_excludes_files_committed_after_snapshot(registry_env,engine,tmp_path,restore_name):
    env,*_=registry_env;admin,name=restore_name
    with engine.connect() as c:schema=c.execute(text('SELECT current_schema()')).scalar_one()
    class ConcurrentTools(PostgresTools):
        def run(self,program,args,url):
            if program=='pg_dump':
                with engine.begin() as c:
                    code=capture_code(c,env[1],env[3]['bm'],sample(37,SERIAL[:-1]+b'Q'),actor_id=env[3]['bm'],source_ref='after exported snapshot')
                    save_payload_file(c,FileStore(env[0].file_storage_path),code['payload_id'])
            return super().run(program,args,url)
    bundle=tmp_path/'snapshot.zip';create_backup(env[0],engine,bundle,db_schema=schema,tools=ConcurrentTools(os.environ.get('ERP_PG_BIN_DIR')))
    assert len(verify_backup(bundle)['files'])==2
    with engine.connect() as c:assert c.execute(text('SELECT count(*) FROM files')).scalar_one()==3
    restore_backup(env[0],bundle,admin_url=admin,new_database=name,files_path=tmp_path/'isolated')
    clone=clone_engine(admin.set(database=name),schema)
    try:
        with clone.connect() as c:assert c.execute(text('SELECT count(*) FROM files')).scalar_one()==2
    finally:clone.dispose()


def test_missing_file_fails_backup_without_leaving_partial_output(registry_env,engine,tmp_path):
    env,_,binary,_=registry_env
    with engine.connect() as c:
        schema=c.execute(text('SELECT current_schema()')).scalar_one();key=c.execute(text('SELECT storage_key FROM files WHERE id=:id'),{'id':binary}).scalar_one()
    (env[0].file_storage_path/key).unlink();output=tmp_path/'missing.zip'
    with pytest.raises(BackupError):create_backup(env[0],engine,output,db_schema=schema)
    assert not output.exists()


def test_existing_database_directory_and_wrong_mfa_key_are_never_overwritten(registry_env,engine,tmp_path,restore_name):
    env,*_=registry_env;admin,name=restore_name
    with engine.connect() as c:schema=c.execute(text('SELECT current_schema()')).scalar_one()
    bundle=tmp_path/'backup.zip';create_backup(env[0],engine,bundle,db_schema=schema)
    # Existing DB is never cleaned, even if it has no ERP tables.
    with psycopg.connect(**_connect_args(admin),autocommit=True) as c:
        c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    with pytest.raises(BackupError):restore_backup(env[0],bundle,admin_url=admin,new_database=name,files_path=tmp_path/'new')
    with psycopg.connect(**_connect_args(admin),autocommit=True) as c:assert c.execute('SELECT 1 FROM pg_database WHERE datname=%s',(name,)).fetchone()
    existing=tmp_path/'existing';existing.mkdir();(existing/'keep').write_text('keep')
    with pytest.raises(BackupError):restore_backup(env[0],bundle,admin_url=admin,new_database=name+'x',files_path=existing)
    assert (existing/'keep').read_text()=='keep'
    from cryptography.fernet import Fernet
    wrong=env[0].model_copy(update={'mfa_encryption_key':Settings(database_url=env[0].database_url,mfa_encryption_key=Fernet.generate_key().decode()).mfa_encryption_key})
    with pytest.raises(BackupError):restore_backup(wrong,bundle,admin_url=admin,new_database=name+'x',files_path=tmp_path/'wrong-key')
