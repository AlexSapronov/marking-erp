"""Read-only invoice chain for the BM workspace; facts never imply remote success."""
from sqlalchemy import text
from fastapi import HTTPException
from . import store as s

NAMES={'ordering':'Эмиссия и ожидание подписи','receiving':'Получение КМ','labels_ready':'Этикетки готовы',
       'application_confirmed':'Нанесение подтверждено','reporting':'Отчёт ожидает подтверждения',
       'applied':'Отчёт подтверждён — формирование АТК','awaiting_full_invoice':'Нужно дополнить состав',
       'warehouse_ready':'Полный состав готов для логистики','completed':'Завершена логистом',
       'rejected':'Требуется разбор квитанции'}


def facts(current, doc):
    c=current.connection
    row=c.execute(text("SELECT * FROM business_items WHERE source_id=:id AND kind='marking' AND state<>'archived'"),{'id':doc['id']}).mappings().first()
    if not row:return {'stage':'Состав проверен' if doc['state']=='prepared' else 'Подготовка состава',
        'next_action':'Начать маркировку' if doc['state']=='prepared' else 'Проверить состав', 'process':None,'atks':[],'tasks':[]}
    # Source access alone never grants additional process or assignment scopes.
    try:s.require(current,row)
    except HTTPException:return {'stage':'Процесс доступен назначенному сотруднику','next_action':'Обратитесь к ответственному','process':None,'atks':[],'tasks':[]}
    d=row['body'];handoff=d.get('labels_handoff',{})
    transferred=bool(handoff and handoff.get('file_id')==d.get('label_file_id'))
    stage=NAMES.get(row['state'],row['state'])
    next_action=stage
    if row['state']=='labels_ready':next_action='Дождаться подтверждения нанесения поставщиком' if transferred else 'Передать актуальные этикетки поставщику'
    atks=[]
    for a in c.execute(text("SELECT * FROM business_items WHERE kind='atk' AND organization_id=:org AND state<>'archived' AND body->'documents' @> CAST(:docs AS jsonb)"),{'org':doc['organization_id'],'docs':'["'+str(row['id'])+'"]'}).mappings():
        try:s.require(current,a)
        except HTTPException:continue
        atks.append({'id':a['id'],'title':a['title'],'state':a['state']})
    if row['state']=='applied' and atks:
        if any(a['state']=='awaiting_approval' for a in atks):stage='АТК ожидает согласования';next_action='Согласовать состав АТК'
        elif any(a['state']=='aggregating' for a in atks):stage='АТК формируются';next_action='Дождаться подтверждения АТК'
        elif all(a['state'] in ('ready','transferred') for a in atks):stage='Груз сформирован';next_action='Ожидается обработка логистом'
    # Warehouse summaries only; device assignments and full scans stay in warehouse APIs.
    tasks=c.execute(text("SELECT kind,state,count(*) AS count FROM warehouse_tasks WHERE source_id=:id GROUP BY kind,state ORDER BY kind,state"),{'id':doc['id']}).mappings().all()
    operations=c.execute(text('SELECT o.kind,o.state,o.reason FROM external_operations o JOIN business_links l ON l.operation_id=o.id WHERE l.item_id=:id ORDER BY o.created_at DESC LIMIT 100'),{'id':row['id']}).mappings().all()
    return {'operations':[dict(o) for o in operations],'stage':stage,'next_action':next_action,'process':{'id':row['id'],'revision':row['revision'],'state':row['state']},
            'labels_transferred':transferred,'atks':atks,'tasks':[dict(t) for t in tasks]}
