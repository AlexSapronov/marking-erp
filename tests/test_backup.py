from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile
import pytest
from marking_erp.backup import BackupError,PostgresTools,verify_backup,create_backup
from marking_erp.migrate import schema_head


def archive(path,*,data=b'PGDMPsynthetic-format-only',files=None,extra=None,manifest_change=None):
    files=files or {};items=[{'id':str(i),'storage_key':key,'size':len(value),'sha256':sha256(value).hexdigest(),'kind':'payload'} for i,(key,value) in enumerate(files.items())]
    manifest={'format':1,'snapshot_at':'2026-10-25T00:00:00+00:00','created_at':'2026-10-25T00:01:00+00:00','schema_revision':schema_head(),'db_schema':'public','database':{'size':len(data),'sha256':sha256(data).hexdigest()},'files':items,'mfa_key_required':False,'mfa_key_sha256':None}
    if manifest_change:manifest_change(manifest)
    with zipfile.ZipFile(path,'w') as z:
        z.writestr('database.dump',data)
        for key,value in files.items():z.writestr('files/'+key,value)
        if extra:z.writestr(extra,b'bad')
        z.writestr('manifest.json',json.dumps(manifest))
    return manifest


def test_offline_verification_and_checksum_failure(tmp_path):
    key='ab/'+'a'*32+'.bin';p=tmp_path/'good.zip';expected=archive(p,files={key:b'original\x1dbytes'})
    assert verify_backup(p)==expected
    bad=tmp_path/'bad.zip';archive(bad,manifest_change=lambda m:m['database'].update(sha256='0'*64))
    with pytest.raises(BackupError):verify_backup(bad)


@pytest.mark.parametrize('extra',['../escape','/absolute','files/../../outside','unknown.file'])
def test_unexpected_archive_paths_are_rejected(tmp_path,extra):
    p=tmp_path/'bad.zip';archive(p,extra=extra)
    with pytest.raises(BackupError):verify_backup(p)
    assert not (tmp_path.parent/'escape').exists()


def test_incomplete_version_and_unknown_dump_are_rejected(tmp_path):
    for name,kwargs in [('version',{'manifest_change':lambda m:m.update(schema_revision='older')}),('format',{'data':b'not a pg_dump'})]:
        p=tmp_path/(name+'.zip');archive(p,**kwargs)
        with pytest.raises(BackupError):verify_backup(p)
    p=tmp_path/'truncated.zip';p.write_bytes(b'PK\x03\x04incomplete')
    with pytest.raises(BackupError):verify_backup(p)


def test_pg_password_not_in_arguments_or_errors_and_temp_file_is_private(tmp_path,monkeypatch):
    password='private:with\\characters';captured=[]
    def run(args,**kwargs):
        assert all(password not in str(x) for x in args)
        pgpass=Path(kwargs['env']['PGPASSFILE']);assert pgpass.stat().st_mode&0o777==0o600
        assert 'private\\:with\\\\characters' in pgpass.read_text();captured.append(pgpass)
        return SimpleNamespace(returncode=1,stderr=password.encode())
    monkeypatch.setattr('marking_erp.backup.subprocess.run',run)
    from sqlalchemy.engine import URL
    url=URL.create('postgresql+psycopg',username='fixture',password=password,host='127.0.0.1',database='fixture')
    with pytest.raises(BackupError) as error:PostgresTools(tmp_path).run('pg_dump',[],url)
    assert password not in str(error.value) and not captured[0].exists()


def test_existing_backup_is_never_overwritten_and_public_output_is_rejected(tmp_path):
    path=tmp_path/'existing';path.write_bytes(b'keep')
    with pytest.raises(BackupError):create_backup(SimpleNamespace(),None,path)
    assert path.read_bytes()==b'keep'
    import marking_erp
    with pytest.raises(BackupError):create_backup(SimpleNamespace(),None,Path(marking_erp.__file__).parent/'static'/'backup.zip')
