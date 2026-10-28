"""Explicit one-shot intents: enqueue one exact kit when ready, never retry a spool."""
from uuid import UUID,uuid5
from fastapi import Depends,HTTPException
from pydantic import StrictBool,StrictInt,Field
from sqlalchemy import text
from .. import auth
from ..auth_routes import Body
from . import store as s
from .printing import context,digest,event,authorize_job,current_parts


class Enable(Body):
    request_id:UUID
    printer_id:UUID
    revision:StrictInt=Field(ge=1)
    confirmed:StrictBool=False


def access(current,item):
    if item['kind'] not in ('marking','split','remark'):raise HTTPException(422,'У этого процесса нет комплекта этикеток')
    s.require(current,item)
    current.require('labels.print.view',**context(current,item))
    current.require('labels.print.confirm',**context(current,item))
    s.require(current,item,{'marking':'marking.labels.download','split':'split.operations.view','remark':'labels.remarking.create'}[item['kind']])


def public(c,row):
    jobs=c.execute(text('''SELECT j.id,j.artifact_id,j.part_index,j.state,a.filename FROM print_jobs j
        JOIN business_artifacts a ON a.id=j.artifact_id WHERE j.auto_request_id=:id ORDER BY j.part_index,j.id'''),{'id':row['id']}).mappings().all()
    return {**{k:row[k] for k in ('id','item_id','actor_id','device_id','bundle_id','state','reason','created_at','updated_at')},'jobs':[dict(j) for j in jobs]}


def install_auto_print(app,actor):
    dep=Depends(actor,scope='function')

    @app.get('/api/business/{id}/auto-print')
    def detail(id:UUID,current=dep):
        item=s.get(current,id);access(current,item)
        rows=current.connection.execute(text('SELECT * FROM auto_print_requests WHERE item_id=:id ORDER BY created_at DESC,id DESC LIMIT 25'),{'id':id}).mappings().all()
        return {'requests':[public(current.connection,r) for r in rows]}

    @app.post('/api/business/{id}/auto-print')
    def enable(id:UUID,body:Enable,current=dep):
        item=s.get(current,id,lock=True);access(current,item);c=current.connection
        fingerprint=digest({'item':id,**body.model_dump()})
        old=c.execute(text('SELECT * FROM auto_print_requests WHERE id=:id'),{'id':body.request_id}).mappings().first()
        if old:
            if old['actor_id']!=current.id or old['request_sha256']!=fingerprint:raise HTTPException(409,'Идентификатор запроса уже использован')
            return public(c,old)
        s.version(item,body.revision)
        if not body.confirmed:raise HTTPException(422,'Подтвердите автоматическую печать одного комплекта')
        if item['state'] not in ('ordering','receiving','labels_ready'):raise HTTPException(409,'Автопечать включается до нанесения этикеток')
        if c.execute(text("SELECT EXISTS(SELECT 1 FROM auto_print_requests WHERE item_id=:id AND state IN ('waiting','blocked'))"),{'id':id}).scalar_one():raise HTTPException(409,'Автопечать уже запланирована')
        if c.execute(text('SELECT EXISTS(SELECT 1 FROM print_jobs j JOIN business_artifacts a ON a.id=j.artifact_id WHERE a.item_id=:id)'),{'id':id}).scalar_one():raise HTTPException(409,'Этикетки уже передавались в очередь. Используйте контролируемую перепечать.')
        device=c.execute(text('SELECT id FROM printer_devices WHERE id=:id AND organization_id=:org AND enabled'),{'id':body.printer_id,'org':item['organization_id']}).scalar_one_or_none()
        if not device:raise HTTPException(403,'Принтер отключён или относится к другой организации')
        bundle=item['body'].get('label_file_id')
        if bundle:current_parts(c,item)
        c.execute(text('''INSERT INTO auto_print_requests(id,item_id,organization_id,actor_id,device_id,bundle_id,request_sha256,state)
            VALUES (:id,:item,:org,:actor,:device,:bundle,:sha,'waiting')'''),
            {'id':body.request_id,'item':id,'org':item['organization_id'],'actor':current.id,'device':device,'bundle':bundle,'sha':fingerprint})
        event(c,body.request_id,'auto_print_enabled',actor=current.id,body={'item_id':str(id),'device_id':str(device),'bundle_id':bundle})
        return public(c,c.execute(text('SELECT * FROM auto_print_requests WHERE id=:id'),{'id':body.request_id}).mappings().one())

    def get(current,id):
        row=current.connection.execute(text('SELECT * FROM auto_print_requests WHERE id=:id FOR UPDATE'),{'id':id}).mappings().first()
        if not row:raise HTTPException(404,'Задание автопечати не найдено')
        item=s.get(current,row['item_id']);access(current,item)
        return row,item

    @app.post('/api/auto-print/{id}/retry')
    def retry(id:UUID,current=dep):
        row,item=get(current,id);c=current.connection
        if row['actor_id']!=current.id:raise HTTPException(403,'Продолжить может инициатор автопечати')
        if row['state']=='waiting':return public(c,row)
        if row['state']!='blocked':raise HTTPException(409,'Повтор разрешён только до передачи комплекта в очередь')
        if item['state']=='archived':raise HTTPException(409,'Процесс остановлен')
        c.execute(text("UPDATE auto_print_requests SET state='waiting',reason='',updated_at=now() WHERE id=:id"),{'id':id})
        event(c,id,'auto_print_resumed',actor=current.id)
        return {'state':'waiting'}

    @app.post('/api/auto-print/{id}/cancel')
    def cancel(id:UUID,current=dep):
        row,item=get(current,id);c=current.connection
        if row['state']=='cancelled':return public(c,row)
        jobs=c.execute(text('SELECT * FROM print_jobs WHERE auto_request_id=:id ORDER BY id FOR UPDATE'),{'id':id}).mappings().all()
        if any(j['state'] not in ('queued','held','cancelled') for j in jobs):raise HTTPException(409,'Агент уже получил часть комплекта. Сначала сверьте результат печати.')
        for job in jobs:
            if job['state']!='cancelled':
                c.execute(text("UPDATE print_jobs SET state='cancelled',finished_at=now() WHERE id=:id"),{'id':job['id']})
                event(c,job['id'],'print_cancelled_with_auto_request',actor=current.id)
        c.execute(text("UPDATE auto_print_requests SET state='cancelled',reason='',updated_at=now() WHERE id=:id"),{'id':id})
        event(c,id,'auto_print_cancelled',actor=current.id)
        return {'state':'cancelled'}


def tick(engine):
    with engine.begin() as c:
        # Same fence as manual queueing, archive, permissions and source edits.
        if not c.execute(text('SELECT pg_try_advisory_xact_lock(683273529)')).scalar_one():return
        row=c.execute(text("SELECT * FROM auto_print_requests WHERE state='waiting' ORDER BY updated_at,id FOR UPDATE SKIP LOCKED LIMIT 1")).mappings().first()
        if not row:return
        try:
            with c.begin_nested():
                user=c.execute(text('SELECT * FROM users WHERE id=:id AND enabled'),{'id':row['actor_id']}).mappings().first()
                if not user:raise HTTPException(403,'Инициатор автопечати отключён')
                current=auth.Actor(c,dict(user),'auto-print',None)
                item=s.get(current,row['item_id'],lock=True);access(current,item)
                device=c.execute(text('SELECT id FROM printer_devices WHERE id=:id AND organization_id=:org AND enabled'),{'id':row['device_id'],'org':row['organization_id']}).scalar_one_or_none()
                if not device:raise HTTPException(403,'Принтер отключён')
                if item['state']=='archived' or item['body'].get('codes_released'):raise HTTPException(409,'Процесс остановлен или КМ возвращены в пул')
                if not item['body'].get('label_file_id'):
                    c.execute(text('UPDATE auto_print_requests SET updated_at=now() WHERE id=:id'),{'id':row['id']});return
                # An intent made before receipt targets the FIRST kit, not a later reallocation.
                target=row['bundle_id'] or c.execute(text("SELECT id FROM business_artifacts WHERE item_id=:id AND kind='labels' ORDER BY version LIMIT 1"),{'id':item['id']}).scalar_one()
                if str(target)!=item['body']['label_file_id']:raise HTTPException(409,'Комплект изменён. Отмените задание и подтвердите актуальные этикетки заново.')
                if c.execute(text('SELECT EXISTS(SELECT 1 FROM print_jobs j JOIN business_artifacts a ON a.id=j.artifact_id WHERE a.item_id=:id)'),{'id':item['id']}).scalar_one():raise HTTPException(409,'По документу уже есть задания печати; автоматическая отправка остановлена')
                _,parts=current_parts(c,item)
                # Validate every part before inserting any jobs.
                for file in parts:authorize_job(c,{'actor_id':row['actor_id'],'artifact_id':file['id'],'reprint_id':None})
                for index,file in enumerate(parts,1):
                    id=uuid5(row['id'],str(file['id']))
                    c.execute(text('''INSERT INTO print_jobs(id,organization_id,actor_id,device_id,artifact_id,copies,request_sha256,auto_request_id,part_index)
                        VALUES (:id,:org,:actor,:device,:file,1,:sha,:request,:part)'''),
                        {'id':id,'org':row['organization_id'],'actor':row['actor_id'],'device':row['device_id'],'file':file['id'],
                         'sha':digest({'auto_request':row['id'],'artifact':file['id']}),'request':row['id'],'part':index})
                    event(c,id,'print_queued',actor=row['actor_id'],body={'auto_request_id':str(row['id']),'part':index,'parts':len(parts),'copies':1})
                c.execute(text("UPDATE auto_print_requests SET state='queued',bundle_id=:bundle,updated_at=now() WHERE id=:id"),{'id':row['id'],'bundle':target})
                event(c,row['id'],'auto_print_queued',actor=row['actor_id'],body={'parts':len(parts),'bundle_id':str(target)})
        except HTTPException as error:
            reason=error.detail if isinstance(error.detail,str) else error.detail.get('message','Автопечать заблокирована')
            c.execute(text("UPDATE auto_print_requests SET state='blocked',reason=:reason,updated_at=now() WHERE id=:id"),{'id':row['id'],'reason':reason})
            event(c,row['id'],'auto_print_blocked',actor=row['actor_id'],body={'reason':reason})
