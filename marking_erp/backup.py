"""Consistent PostgreSQL snapshot + referenced immutable files. Restore creates a NEW DB."""
from contextlib import contextmanager
from datetime import datetime,timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from uuid import uuid4
import zipfile
from sqlalchemy import create_engine,text
from sqlalchemy.engine import make_url
import psycopg
from psycopg import sql
from .file_store import FileStore,MAX_FILE
from .migrate import schema_head

FORMAT=1
MAX_MANIFEST=64*1024*1024
MAX_DUMP=128*1024**3
MAX_ITEMS=100000

class BackupError(Exception):pass


def _identifier(value):
    if not isinstance(value,str) or not re.fullmatch('[a-z][a-z0-9_]{0,62}',value):raise BackupError('Invalid database or schema name')
    return value


def _digest_file(file):
    digest=sha256();size=0
    while chunk:=file.read(1024*1024):digest.update(chunk);size+=len(chunk)
    return digest.hexdigest(),size


def _key_hash(settings):
    return sha256(settings.mfa_encryption_key.get_secret_value().encode()).hexdigest() if settings.mfa_encryption_key else None


def _validate_mfa(c,settings):
    ciphertexts=c.execute(text('SELECT mfa_ciphertext FROM users WHERE mfa_ciphertext IS NOT NULL')).scalars().all()
    if ciphertexts:
        if not settings.mfa_encryption_key:raise BackupError('The original MFA encryption key is required separately')
        from cryptography.fernet import Fernet,InvalidToken
        fernet=Fernet(settings.mfa_encryption_key.get_secret_value().encode())
        try:
            for value in ciphertexts:fernet.decrypt(bytes(value))
        except InvalidToken:raise BackupError('MFA encryption key does not match stored data') from None
    return bool(ciphertexts)


class PostgresTools:
    def __init__(self,bin_dir=None,timeout=900):
        if not 1<=timeout<=7200:raise BackupError('Invalid tool timeout')
        self.bin_dir=Path(bin_dir) if bin_dir else None;self.timeout=timeout

    @contextmanager
    def environment(self,url):
        url=make_url(url);port=url.port or 5432
        if not url.database or not re.fullmatch('[A-Za-z0-9_.-]{1,63}',url.database):raise BackupError('Backup requires a simple database name')
        fields=[url.host or '',str(port),url.database or '',url.username or '',url.password or '']
        if any('\n' in x or '\r' in x for x in fields):raise BackupError('Unsupported connection credentials')
        with tempfile.TemporaryDirectory(prefix='erp-pg-auth-') as root:
            path=Path(root)/'pgpass'
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as f:f.write(':'.join(x.replace('\\','\\\\').replace(':','\\:') for x in fields)+'\n')
            env={k:v for k,v in os.environ.items() if not k.startswith('PG')}
            env['PGPASSFILE']=str(path);env['PGCONNECT_TIMEOUT']='10'
            supported={'sslmode':'PGSSLMODE','sslrootcert':'PGSSLROOTCERT','sslcert':'PGSSLCERT','sslkey':'PGSSLKEY','connect_timeout':'PGCONNECT_TIMEOUT'}
            for key,value in url.query.items():
                if key not in supported or not isinstance(value,str):raise BackupError('Unsupported PostgreSQL URL option for backup')
                env[supported[key]]=value
            args=['--host',fields[0],'--port',fields[1],'--username',fields[3],'--dbname',fields[2]]
            yield env,args

    def run(self,name,args,url):
        executable=str(self.bin_dir/name) if self.bin_dir else shutil.which(name)
        if not executable:raise BackupError('Install PostgreSQL client tools matching the server')
        with self.environment(url) as (env,connection):
            try:
                result=subprocess.run([executable,*connection,*args],env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=self.timeout)
            except (OSError,subprocess.TimeoutExpired):raise BackupError('PostgreSQL tool failed or timed out; no credentials were printed') from None
        if result.returncode:raise BackupError('PostgreSQL tool failed; verify versions and privileges privately')


def _rows(c):
    rows=c.execute(text('SELECT id,storage_key,sha256,size,kind FROM files ORDER BY id')).mappings().all()
    if len(rows)>MAX_ITEMS:raise BackupError('File count exceeds supported backup size')
    return [{**dict(r),'id':str(r['id'])} for r in rows]


@contextmanager
def _migration_lock(engine):
    # Acquire before opening the exported snapshot, not inside REPEATABLE READ.
    with engine.connect().execution_options(isolation_level='AUTOCOMMIT') as lock:
        acquired=False
        try:
            lock.execute(text('SELECT pg_advisory_lock_shared(683273527)'));acquired=True
            yield
        finally:
            if acquired:
                try:lock.execute(text('SELECT pg_advisory_unlock_shared(683273527)'))
                except Exception:lock.invalidate()


def create_backup(settings,engine,output,*,db_schema='public',tools=None):
    schema=_identifier(db_schema);output=Path(output);tools=tools or PostgresTools(os.environ.get('ERP_PG_BIN_DIR'))
    if output.resolve().is_relative_to(Path(__file__).parent.joinpath('static').resolve()):raise BackupError('Backup output cannot be inside public assets')
    output.parent.mkdir(parents=True,exist_ok=True)
    try:fd=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    except OSError:raise BackupError('Backup output must be a new writable file') from None
    success=False
    try:
        with os.fdopen(fd,'wb') as destination,tempfile.TemporaryDirectory(prefix='erp-backup-') as temp,_migration_lock(engine):
            dump=Path(temp)/'database.dump'
            # Exported transaction stays open until pg_dump and immutable files finish.
            with engine.connect().execution_options(isolation_level='REPEATABLE READ') as c,c.begin():
                c.exec_driver_sql('SET TRANSACTION READ ONLY')
                snapshot_at=c.execute(text('SELECT transaction_timestamp()')).scalar_one().astimezone(timezone.utc).isoformat()
                c.exec_driver_sql('SET search_path TO '+c.dialect.identifier_preparer.quote(schema))
                revisions=c.execute(text('SELECT version_num FROM alembic_version')).scalars().all()
                if revisions!=[schema_head()]:raise BackupError('Backup requires the current schema revision')
                files=_rows(c);required=_validate_mfa(c,settings)
                if required and not settings.mfa_encryption_key:raise BackupError('Configure the MFA encryption key and save it separately')
                exported=c.execute(text('SELECT pg_export_snapshot()')).scalar_one()
                server=c.execute(text('SHOW server_version_num')).scalar_one()
                tools.run('pg_dump',['--format=custom','--no-owner','--no-acl','--schema='+schema,'--snapshot='+exported,'--file='+str(dump)],settings.database_url.get_secret_value())
                with dump.open('rb') as f:dump_hash,dump_size=_digest_file(f)
                if not 1<=dump_size<=MAX_DUMP:raise BackupError('Invalid database dump size')
                manifest={'format':FORMAT,'snapshot_at':snapshot_at,'created_at':datetime.now(timezone.utc).isoformat(),'build_sha':settings.build_sha,'schema_revision':schema_head(),'db_schema':schema,
                    'server_version_num':server,'database':{'sha256':dump_hash,'size':dump_size},'mfa_key_required':bool(required),'mfa_key_sha256':_key_hash(settings) if required else None,'files':files}
                with zipfile.ZipFile(destination,'w',compression=zipfile.ZIP_STORED,allowZip64=True) as archive:
                    archive.write(dump,'database.dump')
                    store=FileStore(settings.file_storage_path)
                    for item in files:archive.writestr('files/'+item['storage_key'],store.read(item))
                    manifest['created_at']=c.execute(text('SELECT clock_timestamp()')).scalar_one().astimezone(timezone.utc).isoformat()
                    archive.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False,separators=(',',':')).encode())
            destination.flush();os.fsync(destination.fileno())
        verify_backup(output);success=True
        return {'file_count':len(files),'schema_revision':schema_head(),'created_at':manifest['created_at']}
    except BackupError:raise
    except Exception:raise BackupError('Backup failed; output was not kept') from None
    finally:
        if not success:output.unlink(missing_ok=True)


def _unique_json(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise BackupError('Duplicate manifest key')
        result[key]=value
    return result


def _manifest(archive):
    infos=archive.infolist();names=[i.filename for i in infos]
    if len(names)!=len(set(names)) or 'manifest.json' not in names or 'database.dump' not in names:raise BackupError('Invalid backup members')
    if archive.getinfo('manifest.json').file_size>MAX_MANIFEST:raise BackupError('Manifest too large')
    manifest=json.loads(archive.read('manifest.json'),object_pairs_hook=_unique_json)
    if type(manifest['format']) is not int or type(manifest['mfa_key_required']) is not bool:raise BackupError('Invalid manifest types')
    if manifest['format']!=FORMAT or manifest['schema_revision']!=schema_head():raise BackupError('Unsupported backup schema/format; use the matching release')
    _identifier(manifest['db_schema'])
    for key in ('snapshot_at','created_at'):
        if datetime.fromisoformat(manifest[key]).tzinfo is None:raise BackupError('Backup timestamps must include timezone')
    if not isinstance(manifest['files'],list) or len(manifest['files'])>MAX_ITEMS:raise BackupError('Invalid file list')
    expected={'manifest.json','database.dump'};keys=set();ids=set()
    for item in manifest['files']:
        key=item['storage_key']
        if not re.fullmatch(r'[a-f0-9]{2}/[a-f0-9]{32}\.bin',key) or key in keys or item['id'] in ids:raise BackupError('Invalid or duplicate file identity')
        keys.add(key);ids.add(item['id']);expected.add('files/'+key)
        if type(item['size']) is not int or not 1<=item['size']<=MAX_FILE or not re.fullmatch('[a-f0-9]{64}',item['sha256']):raise BackupError('Invalid file metadata')
    if set(names)!=expected:raise BackupError('Unexpected or missing backup members')
    for info in infos:
        if stat.S_IFMT(info.external_attr>>16) not in (0,stat.S_IFREG) or info.flag_bits&1:raise BackupError('Links, directories and encrypted ZIP members are not accepted')
    return manifest


def verify_backup(bundle):
    try:
        with zipfile.ZipFile(bundle) as archive:
            manifest=_manifest(archive)
            items=[('database.dump',manifest['database'],MAX_DUMP)]+[('files/'+item['storage_key'],item,MAX_FILE) for item in manifest['files']]
            for name,metadata,maximum in items:
                if type(metadata['size']) is not int or not 1<=metadata['size']<=maximum or archive.getinfo(name).file_size!=metadata['size']:raise BackupError('Backup size mismatch')
                with archive.open(name) as f:
                    if name=='database.dump' and f.read(5)!=b'PGDMP':raise BackupError('Not a PostgreSQL custom dump')
                with archive.open(name) as f:digest,size=_digest_file(f)
                if digest!=metadata['sha256'] or size!=metadata['size']:raise BackupError('Backup checksum mismatch')
        return manifest
    except BackupError:raise
    except Exception:raise BackupError('Backup is incomplete, corrupt or unsupported') from None


def _connect_args(url):
    value=make_url(url);args={'host':value.host,'port':value.port or 5432,'user':value.username,'password':value.password,'dbname':value.database,'connect_timeout':10}
    for key,value in value.query.items():
        if key not in ('sslmode','sslrootcert','sslcert','sslkey','connect_timeout'):raise BackupError('Unsupported restore connection option')
        args[key]=value
    return args


def _freeze(c,bundle_hash,snapshot_at):
    c.execute(text('DELETE FROM sessions'))
    c.execute(text("UPDATE users SET auth_version=auth_version+1,mfa_last_counter=GREATEST(mfa_last_counter,floor(extract(epoch from clock_timestamp())/30)::bigint)"))
    c.execute(text('UPDATE signer_devices SET enabled=false'))
    c.execute(text("UPDATE signer_jobs SET state='cancelled',lease_hash=NULL,lease_until=NULL"))
    c.execute(text('UPDATE operation_contracts SET approved=false'))
    rows=c.execute(text("SELECT id,state FROM external_operations WHERE state NOT IN ('confirmed','rejected','cancelled_before_dispatch') FOR UPDATE")).mappings().all()
    for row in rows:
        state='unknown' if row['state'] in ('dispatching','unknown') else 'submitted' if row['state']=='submitted' else 'blocked'
        c.execute(text('UPDATE external_operations SET state=:state,reason=:reason,fence=fence+1,lease_hash=NULL,lease_until=NULL,updated_at=clock_timestamp() WHERE id=:id'),{'id':row['id'],'state':state,'reason':'restore_requires_reconciliation'})
        c.execute(text('INSERT INTO operation_events(operation_id,state,reason) VALUES (:id,:state,:reason)'),{'id':row['id'],'state':state,'reason':'restore_requires_reconciliation'})
    from .auth import audit
    audit(c,None,'system.restored','backup',{'bundle_sha256':bundle_hash,'snapshot_at':snapshot_at,'external_dispatch_disabled':True})


def restore_backup(settings,bundle,*,admin_url,new_database,files_path,tools=None):
    try:fd=os.open(bundle,os.O_RDONLY|os.O_NOFOLLOW)
    except OSError:raise BackupError('Backup must be an accessible regular file') from None
    with os.fdopen(fd,'rb') as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):raise BackupError('Backup must be a regular file')
        manifest=verify_backup(source)
        return _restore_verified(settings,source,manifest,admin_url=admin_url,new_database=new_database,files_path=files_path,tools=tools)


def _restore_verified(settings,bundle,manifest,*,admin_url,new_database,files_path,tools=None):
    """Never overwrite an existing DB or directory; only clean up resources created here."""
    name=_identifier(new_database);root=Path(files_path);tools=tools or PostgresTools(os.environ.get('ERP_PG_BIN_DIR'))
    if not root.is_absolute() or root.exists() or root.is_symlink():raise BackupError('Restore files path must be a new absolute directory')
    for parent in root.parents:
        if parent.is_symlink():raise BackupError('Restore directory cannot traverse symlinks')
    # Reuse the same validation as runtime storage; never restore into public assets.
    from .config import Settings
    Settings.validate_file_storage(root)
    if manifest['mfa_key_required'] and _key_hash(settings)!=manifest['mfa_key_sha256']:raise BackupError('The original MFA encryption key is required separately')
    admin=make_url(admin_url);destination=admin.set(database=name)
    if admin.database==name:raise BackupError('Restore database must differ from maintenance database')
    created=False;root_created=False;engine=None
    try:
        with psycopg.connect(**_connect_args(admin),autocommit=True) as maintenance:
            # CREATE fails if the name already exists. No --clean ever targets an old DB.
            maintenance.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)));created=True
        root.mkdir(parents=True,mode=0o700,exist_ok=False);root_created=True
        with tempfile.TemporaryDirectory(prefix='erp-restore-') as temp,zipfile.ZipFile(bundle) as archive:
            dump=Path(temp)/'database.dump'
            with archive.open('database.dump') as source,dump.open('wb') as target:shutil.copyfileobj(source,target)
            tools.run('pg_restore',['--clean','--if-exists','--single-transaction','--exit-on-error','--no-owner','--no-acl',str(dump)],destination)
            store=FileStore(root)
            for item in manifest['files']:
                directory=store._directory(item['storage_key'][:2],True);target=directory/item['storage_key'].split('/')[1]
                fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                with archive.open('files/'+item['storage_key']) as source,os.fdopen(fd,'wb') as output:shutil.copyfileobj(source,output);output.flush();os.fsync(output.fileno())
                store.read(item)
        engine=create_engine(destination,hide_parameters=True)
        with engine.begin() as c:
            c.exec_driver_sql('SET search_path TO '+c.dialect.identifier_preparer.quote(manifest['db_schema']))
            if c.execute(text('SELECT version_num FROM alembic_version')).scalar_one()!=schema_head() or _rows(c)!=manifest['files']:raise BackupError('Restored database/files do not match the manifest')
            if _validate_mfa(c,settings)!=manifest['mfa_key_required']:raise BackupError('Restored MFA metadata mismatch')
            bundle.seek(0);bundle_hash,_=_digest_file(bundle)
            _freeze(c,bundle_hash,manifest['snapshot_at'])
        return {'database':name,'file_count':len(manifest['files']),'files_path':str(root),'external_dispatch_disabled':True}
    except BackupError:raise
    except Exception:raise BackupError('Restore failed; existing databases were not modified') from None
    finally:
        if engine:engine.dispose()
        # A failed run removes only its own freshly-created resources.
        import sys
        if sys.exc_info()[0] is not None:
            if root_created:
                try:shutil.rmtree(root)
                except OSError:pass
            if created:
                try:
                    with psycopg.connect(**_connect_args(admin),autocommit=True) as maintenance:maintenance.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))
                except Exception:pass
