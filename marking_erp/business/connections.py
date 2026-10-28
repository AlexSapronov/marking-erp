import json
from uuid import UUID
from cryptography.fernet import Fernet
from fastapi import Depends,HTTPException
from pydantic import Field,SecretStr,StrictBool,StrictInt
from sqlalchemy import text
from ..auth_routes import Body
from .. import auth

class Connection(Body):
    organization_id:UUID
    revision:StrictInt=Field(ge=0)
    environment:str=Field(pattern='^(production|sandbox)$')
    oms_id:str=Field(default='',max_length=120)
    oms_connection:str=Field(default='',max_length=120)
    kpp:str=Field(default='',pattern='^([0-9]{9})?$')
    fias_id:str=Field(default='',max_length=36)
    template_id:StrictInt=Field(default=36,ge=36,le=37)
    writes_enabled:StrictBool=False
    auto_refresh_tokens:StrictBool=False
    nk_auto_sync:StrictBool=False
    warehouse_live_checks:StrictBool=True
    ignore_legal_entity:StrictBool=False
    nk_key:SecretStr|None=None
    true_token:SecretStr|None=None
    oms_token:SecretStr|None=None


def load(c,settings,org):
    row=c.execute(text('SELECT * FROM integration_settings WHERE organization_id=:org'),{'org':org}).mappings().first()
    if not row:raise ValueError('Настройте подключения НК/ЧЗ/СУЗ в разделе «Подключения»')
    if not settings.mfa_encryption_key:raise ValueError('Не настроен серверный ключ шифрования')
    secret=json.loads(Fernet(settings.mfa_encryption_key.get_secret_value().encode()).decrypt(bytes(row['secret'])))
    return {**row['public'],**secret}


def install_connections(app,actor,settings):
    dep=Depends(actor,scope='function')
    @app.get('/api/connections')
    def show(organization_id:UUID,current=dep):
        current.require('integrations.manage',organization_id=organization_id,administrative=True)
        row=current.connection.execute(text('SELECT * FROM integration_settings WHERE organization_id=:org'),{'org':organization_id}).mappings().first()
        if not row:return {'revision':0,'settings':{},'credentials':{}}
        config=load(current.connection,settings,organization_id)
        return {'revision':row['revision'],'settings':row['public'],'credentials':{k:bool(config.get(k)) for k in ('nk_key','true_token','oms_token')}}
    @app.put('/api/connections')
    def update(body:Connection,current=dep):
        current.require('integrations.manage',organization_id=body.organization_id,administrative=True)
        if body.ignore_legal_entity:raise HTTPException(422,'Строгая проверка владельца обязательна; исключения не поддерживаются')
        if not settings.mfa_encryption_key:raise HTTPException(409,'Не настроен серверный ключ шифрования')
        if body.fias_id:
            try:UUID(body.fias_id)
            except ValueError:raise HTTPException(422,'Некорректный ФИАС') from None
        c=current.connection;old=c.execute(text('SELECT revision FROM integration_settings WHERE organization_id=:org FOR UPDATE'),{'org':body.organization_id}).scalar_one_or_none()
        if body.revision!=(old or 0):raise HTTPException(409,'Настройки изменились. Обновите данные.')
        secret={k:load(c,settings,body.organization_id).get(k,'') for k in ('nk_key','true_token','oms_token')} if old else {}
        for k in ('nk_key','true_token','oms_token'):
            value=getattr(body,k)
            if value is not None:
                raw=value.get_secret_value().strip()
                if len(raw)>8192 or any(ord(v)<33 for v in raw):raise HTTPException(422,'Проверьте формат токена')
                secret[k]=raw
        public=body.model_dump(exclude={'organization_id','revision','nk_key','true_token','oms_token'})
        if body.auto_refresh_tokens:public['token_refresh_actor']=str(current.id)
        if body.nk_auto_sync:
            current.require('nk.sync.execute',organization_id=body.organization_id,owner_id=current.id)
            if not secret.get('nk_key'):raise HTTPException(422,'Для автоматической синхронизации нужен ключ НК')
            public['nk_sync_actor']=str(current.id)
        encrypted=Fernet(settings.mfa_encryption_key.get_secret_value().encode()).encrypt(json.dumps(secret).encode())
        c.execute(text('''INSERT INTO integration_settings(organization_id,public,secret) VALUES (:org,CAST(:public AS jsonb),:secret)
            ON CONFLICT(organization_id) DO UPDATE SET public=EXCLUDED.public,secret=EXCLUDED.secret,revision=integration_settings.revision+1,updated_at=now()'''),{'org':body.organization_id,'public':json.dumps(public),'secret':encrypted})
        if body.auto_refresh_tokens:c.execute(text('UPDATE integration_auth_state SET next_refresh_at=now() WHERE organization_id=:org'),{'org':body.organization_id})
        from .contracts import register
        register(c)
        auth.audit(c,current.id,'integrations.updated',body.organization_id,{'writes_enabled':body.writes_enabled})
        return {'saved':True}
