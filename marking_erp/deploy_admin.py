"""Operator-only setup: private config, no secret argv, no remote service activation."""
import argparse
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit
from cryptography.fernet import Fernet
from sqlalchemy.engine import URL
from .config import Settings
from .service import load_settings


class SetupError(ValueError):
    pass


def write_configs(directory,public_url,release_sha,owner_password,runtime_password):
    directory=Path(directory)
    if not re.fullmatch('[a-f0-9]{40}',release_sha):raise SetupError('Нужен полный SHA релиза')
    parsed=urlsplit(public_url)
    try:port=parsed.port
    except ValueError:raise SetupError('Некорректный HTTPS-адрес') from None
    if not parsed.hostname or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9.-]{0,252}',parsed.hostname) or port not in (None,443):
        raise SetupError('Нужен DNS hostname HTTPS с портом 443')
    if not owner_password or not runtime_password or owner_password==runtime_password:
        raise SetupError('Введите разные непустые пароли ролей БД')
    if any(p.is_symlink() for p in [directory,*directory.parents]):raise SetupError('Каталог настроек не может быть симлинком')
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    metadata=directory.stat()
    if metadata.st_uid!=os.getuid() or metadata.st_mode&0o077:raise SetupError('Каталог настроек должен принадлежать оператору и иметь режим 0700')
    paths=[directory/'runtime.json',directory/'maintenance.json',directory/'nginx.generated.conf']
    if any(p.exists() or p.is_symlink() for p in paths):raise SetupError('Настройки уже существуют. Перезапись и замена MFA-ключа запрещены.')
    key=Fernet.generate_key().decode()
    values=[]
    for role,password in [('marking_erp_runtime',runtime_password),('marking_erp_owner',owner_password)]:
        value={'database_url':URL.create('postgresql+psycopg',username=role,password=password,host='127.0.0.1',port=5432,database='marking_erp').render_as_string(hide_password=False),
               'mfa_encryption_key':key,'public_url':public_url,'secure_cookie':True,
               'file_storage_path':'/var/lib/marking-erp/files','build_sha':release_sha}
        Settings(**value)  # Validate before writing any files; errors hide input.
        values.append(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    # The installed wheel has no repository-relative deploy directory. Use a
    # fixed template packaged below; hostname is separately validated above.
    nginx='''server {
    listen 443 ssl;
    server_name HOSTNAME;
    ssl_certificate /etc/marking-erp/tls/fullchain.pem;
    ssl_certificate_key /etc/marking-erp/tls/private.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    client_max_body_size 12m;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 60s;
        proxy_buffering off;
    }
}
'''.replace('HOSTNAME',parsed.hostname)
    created=[]
    try:
        for path,content in zip(paths,[*values,nginx]):
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);created.append(path)
            with os.fdopen(fd,'w') as stream:stream.write(content);stream.flush();os.fsync(stream.fileno())
    except OSError:
        for path in created:path.unlink(missing_ok=True)
        raise SetupError('Не удалось сохранить настройки; созданные этим запуском файлы удалены') from None
    return paths


def command_environment(settings):
    env={k:v for k,v in os.environ.items() if not k.startswith('ERP_')}
    for key in ('database_url','mfa_encryption_key','public_url','secure_cookie','file_storage_path','build_sha'):
        value=getattr(settings,key)
        if hasattr(value,'get_secret_value'):value=value.get_secret_value()
        env['ERP_'+key.upper()]=str(value)
    env['ERP_PG_BIN_DIR']='/usr/lib/postgresql/16/bin'
    return env


def main():
    parser=argparse.ArgumentParser(description='Локальное администрирование ERP; не выполняет deploy или внешние операции')
    parser.add_argument('--config',type=Path,default=Path('/etc/marking-erp/maintenance.json'))
    parser.add_argument('--release-sha')
    parser.add_argument('action',choices=['configure','migrate','seed','bootstrap-admin','grant-runtime','backup','verify-backup','register-signer','revoke-signer','trust-windows-verifier'])
    args,rest=parser.parse_known_args()
    if os.geteuid()!=0:parser.exit(1,'Команда требует sudo; файлы настроек доступны только root.\n')
    try:
        if args.action=='configure':
            if rest or not args.release_sha:raise SetupError('configure требует --release-sha и не принимает другие аргументы')
            public=input('HTTPS-адрес ERP (например https://erp.company.ru): ').strip()
            owner=getpass.getpass('Пароль PostgreSQL-роли marking_erp_owner (из шага создания БД): ')
            runtime=getpass.getpass('Пароль PostgreSQL-роли marking_erp_runtime: ')
            write_configs(args.config.parent,public,args.release_sha,owner,runtime)
            print('Настройки созданы, MFA-ключ сохранён без вывода. Сохраните защищённую резервную копию настроек отдельно от БД.')
            return
        executable=Path(sys.executable).with_name('marking-erp')
        if args.action=='verify-backup':
            command=[str(executable.with_name('marking-erp-backup')),'verify',*rest];env=None
        else:
            settings=load_settings(args.config);env=command_environment(settings)
            if args.action=='backup':
                if not rest:
                    root=Path('/var/backups/marking-erp');root.mkdir(mode=0o700,parents=True,exist_ok=True)
                    if root.is_symlink() or root.stat().st_mode&0o077:raise SetupError('Каталог резервных копий должен иметь режим 0700')
                    rest=['--output',str(root/('erp-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.zip'))]
                command=[str(executable.with_name('marking-erp-backup')),'create',*rest]
            else:command=[str(executable),args.action,*rest]
        result=subprocess.run(command,env=env,check=False)
        if result.returncode:raise SetupError('Операция завершилась с ошибкой. Не продолжайте следующие шаги.')
    except SetupError as error:
        parser.exit(1,str(error)+'\n')
    except (ValueError,OSError):
        parser.exit(1,'Настройка/операция не выполнена. Проверьте ввод и права локально; секреты не выводились.\n')


if __name__=='__main__':main()
