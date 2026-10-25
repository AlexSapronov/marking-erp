"""Read-only queue diagnostics. No public enqueue, retry, confirmation or raw CMS."""
from uuid import UUID
from fastapi import Depends,HTTPException,Query
from sqlalchemy import text

STATES=('awaiting_signature','queued','dispatching','submitted','confirmed','rejected','blocked','unknown','cancelled_before_dispatch')


def install_operations(app,actor):
    dependency=Depends(actor,scope='function')
    @app.get('/api/operations')
    def operations(organization_id:UUID,state:str=Query('',max_length=40),before_id:UUID|None=None,current=dependency):
        grant=current.grants().get('operations.view')
        if not grant or organization_id not in current.organizations():raise HTTPException(403,'Очередь недоступна')
        if state and state not in STATES:raise HTTPException(422,'Неизвестное состояние')
        rows=current.connection.execute(text('''SELECT o.id,o.kind,o.state,o.reason,o.payload_sha256,o.target_revision,o.created_at,o.updated_at,o.external_id,o.fence,
            j.state AS signature_state FROM external_operations o JOIN operation_targets t ON t.id=o.target_id
            LEFT JOIN signer_jobs j ON j.operation_id=o.id WHERE o.organization_id=:org
            AND (:scope='all' OR (:scope='own' AND t.owner_id=:user) OR (:scope='assigned' AND EXISTS(SELECT 1 FROM operation_assignments a WHERE a.target_id=t.id AND a.user_id=:user)))
            AND (:state='' OR o.state=:state) AND (CAST(:before AS uuid) IS NULL OR o.id<CAST(:before AS uuid)) ORDER BY o.id DESC LIMIT 26'''),
            {'org':organization_id,'scope':grant,'user':current.id,'state':state,'before':str(before_id) if before_id else None}).mappings().all()
        return {'operations':[dict(r) for r in rows[:25]],'next_cursor':str(rows[24]['id']) if len(rows)>25 else None,'dispatch_available':False,
                'notice':'Продуктивные адаптеры и валидатор ГОСТ-подписи пока не подключены.'}

    @app.get('/api/operations/{id}')
    def detail(id:UUID,current=dependency):
        c=current.connection
        op=c.execute(text('''SELECT o.*,t.owner_id,EXISTS(SELECT 1 FROM operation_assignments a WHERE a.target_id=o.target_id AND a.user_id=:user) AS assigned
            FROM external_operations o JOIN operation_targets t ON t.id=o.target_id WHERE o.id=:id'''),{'id':id,'user':current.id}).mappings().first()
        if not op:raise HTTPException(404,'Операция не найдена')
        current.require('operations.view',organization_id=op['organization_id'],owner_id=op['owner_id'],assigned=op['assigned'])
        events=c.execute(text('SELECT id,state,reason,created_at FROM operation_events WHERE operation_id=:id ORDER BY id DESC LIMIT 100'),{'id':id}).mappings().all()
        receipts=c.execute(text('SELECT id,phase,fence,sha256,applied,classification,created_at FROM operation_receipts WHERE operation_id=:id ORDER BY id DESC LIMIT 100'),{'id':id}).mappings().all()
        signature=c.execute(text('SELECT state,fence,signature_sha256,verification_ref,lease_until FROM signer_jobs WHERE operation_id=:id'),{'id':id}).mappings().first()
        return {'operation':{k:op[k] for k in ('id','kind','state','reason','payload_sha256','contract_version','target_revision','external_id','fence','created_at','updated_at')},
                'events':[dict(e) for e in events],'receipts':[dict(r) for r in receipts],'signature':dict(signature) if signature else None,
                'notice':'Принятие запроса не означает завершение. Неизвестный результат требует сверки; повторная отправка здесь недоступна.'}
