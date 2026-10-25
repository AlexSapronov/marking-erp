"""Destructive ONLY to fresh GitHub runner fixtures; never run on an operator VPS."""
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import tempfile
import time
from uuid import uuid4
import psycopg
from psycopg import sql
import pyotp
from sqlalchemy import text
from sqlalchemy.engine import make_url
from marking_erp.deploy_admin import write_configs
from marking_erp.service import load_settings
from marking_erp.database import database_engine
from marking_erp.auth import bootstrap_admin
from marking_erp.backup import restore_backup


def run(*args,**kwargs):return subprocess.run([str(a) for a in args],check=True,**kwargs)


def main():
    if os.geteuid()!=0 or os.environ.get('GITHUB_ACTIONS')!='true':raise SystemExit('Fresh GitHub Actions runner only')
    release=Path('/opt/marking-erp/releases')/os.environ['ERP_RELEASE_SHA'];admin=release/'venv/bin/marking-erp-admin'
    source_url=os.environ['ERP_TEST_DATABASE_URL'];pg=make_url(source_url)
    connect={'host':pg.host,'port':pg.port,'user':pg.username,'password':pg.password,'dbname':pg.database}
    with psycopg.connect(**connect,autocommit=True) as c:
        for role,password in [('marking_erp_owner','synthetic-owner-password'),('marking_erp_runtime','synthetic-runtime-password')]:
            c.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}').format(sql.Identifier(role),sql.Literal(password)))
        c.execute('CREATE DATABASE marking_erp OWNER marking_erp_owner')
        c.execute('REVOKE ALL ON DATABASE marking_erp FROM PUBLIC')
        c.execute('GRANT CONNECT ON DATABASE marking_erp TO marking_erp_runtime')
    with psycopg.connect(**{**connect,'dbname':'marking_erp'},autocommit=True) as c:c.execute('REVOKE CREATE ON SCHEMA public FROM PUBLIC')
    run('useradd','--system','--home-dir','/var/lib/marking-erp','--shell','/usr/sbin/nologin','marking-erp')
    uid=pwd.getpwnam('marking-erp').pw_uid;gid=pwd.getpwnam('marking-erp').pw_gid
    for path in ['/var/lib/marking-erp','/var/lib/marking-erp/files']:
        p=Path(path);p.mkdir(mode=0o700,parents=True,exist_ok=True);os.chown(p,uid,gid);p.chmod(0o700)
    write_configs('/etc/marking-erp','https://erp.ci.test',os.environ['ERP_RELEASE_SHA'],'synthetic-owner-password','synthetic-runtime-password')
    run(admin,'migrate');run(admin,'seed')
    settings=load_settings('/etc/marking-erp/maintenance.json');engine=database_engine(settings)
    secret=pyotp.random_base32();password='Synthetic-admin-password-2026'
    with engine.begin() as c:bootstrap_admin(c,settings,'admin','CI administrator',password,secret,pyotp.TOTP(secret).now(),'7700000000','CI organization')
    run(admin,'grant-runtime','--role','marking_erp_runtime')
    Path('/opt/marking-erp/current').symlink_to(release)
    shutil.copy2(release/'deploy/marking-erp.service','/etc/systemd/system/marking-erp.service')
    run('systemd-analyze','verify','/etc/systemd/system/marking-erp.service')
    run('systemctl','daemon-reload');run('systemctl','start','marking-erp')
    try:
        for attempt in range(40):
            result=subprocess.run(['curl','--silent','--fail','http://127.0.0.1:8000/health/ready'],capture_output=True,text=True)
            if result.returncode==0:break
            time.sleep(.5)
        else:
            run('journalctl','-u','marking-erp','--no-pager','-n','40');raise RuntimeError('systemd service did not become ready')
        assert json.loads(result.stdout)['build_sha']==os.environ['ERP_RELEASE_SHA']
        run('runuser','-u','marking-erp','--','test','!','-r','/etc/marking-erp/maintenance.json')
        tls=Path('/etc/marking-erp/tls');tls.mkdir(mode=0o700)
        run('openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=erp.ci.test','-addext','subjectAltName=DNS:erp.ci.test','-keyout',tls/'private.key','-out',tls/'fullchain.pem',stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        (tls/'private.key').chmod(0o600)
        shutil.copy2('/etc/marking-erp/nginx.generated.conf','/etc/nginx/sites-available/marking-erp')
        Path('/etc/nginx/sites-enabled/marking-erp').symlink_to('/etc/nginx/sites-available/marking-erp')
        run('nginx','-t');run('systemctl','start','nginx');run('systemctl','reload','nginx')
        with tempfile.TemporaryDirectory() as root:
            cookie=Path(root)/'cookies'
            def request(path,body=None,csrf=None):
                command=['curl','--silent','--show-error','--fail-with-body','--noproxy','*','--resolve','erp.ci.test:443:127.0.0.1','--cacert',str(tls/'fullchain.pem'),'--cookie',str(cookie),'--cookie-jar',str(cookie),'https://erp.ci.test'+path]
                data=None
                if body is not None:
                    command+=['--header','Origin: https://erp.ci.test','--header','X-Requested-With: marking-erp','--header','Content-Type: application/json','--data-binary','@-'];data=json.dumps(body)
                if csrf:command+=['--header','X-CSRF-Token: '+csrf]
                response=subprocess.run(command,input=data,capture_output=True,text=True)
                if response.returncode:raise RuntimeError('HTTPS smoke request failed for '+path)
                return json.loads(response.stdout)
            assert request('/health/ready')['build_sha']==os.environ['ERP_RELEASE_SHA']
            session=request('/api/session',{'username':'admin','password':password,'otp':pyotp.TOTP(secret).at(time.time()+30)})
            status=request('/api/system/status');assert status['technical_ready'] and not status['business_integrations_verified']
            invoice=request('/api/documents',{'request_id':str(uuid4()),'organization_id':session['organization_ids'][0],'number':'DEPLOY-SMOKE','document_date':'2026-10-26','supplier':'Synthetic supplier','lines':[{'pn':'Fixture','gtin':'04640638349704','tnved':'8542399000','quantity':10,'labels':3}]},session['csrf'])
            assert request('/api/documents/'+invoice['id'])['document']['number']=='DEPLOY-SMOKE'
        backup=Path('/var/backups/marking-erp');backup.mkdir(mode=0o700,parents=True)
        archive=backup/'smoke.zip';run(admin,'backup','--output',archive);run(admin,'verify-backup','--bundle',archive)
        restore_backup(settings,archive,admin_url=source_url,new_database='marking_erp_smoke_restore',files_path=Path('/var/lib/marking-erp/smoke-restored'))
        restored=database_engine(settings.model_copy(update={'database_url':__import__('pydantic').SecretStr(pg.set(database='marking_erp_smoke_restore').render_as_string(hide_password=False))}))
        try:
            with restored.connect() as c:
                assert c.execute(text('SELECT count(*) FROM bm_documents')).scalar_one()==1
                assert c.execute(text('SELECT count(*) FROM sessions')).scalar_one()==0
            with engine.connect() as c:assert c.execute(text('SELECT count(*) FROM bm_documents')).scalar_one()==1
        finally:restored.dispose()
        print('PASS: offline installation, real systemd credentials/sandbox, HTTPS login, all preflight checks, invoice save, backup and isolated restore. No external business adapters called.')
    finally:
        run('systemctl','stop','marking-erp');engine.dispose()


if __name__=='__main__':main()
