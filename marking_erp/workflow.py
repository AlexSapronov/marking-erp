"""Read-only navigation between independently authorized business documents.

Links are local evidence, never an assertion that an external step succeeded.
No writes, API probes, raw payloads, exception messages or automatic retries.
"""
from datetime import datetime,timezone
from uuid import UUID
from fastapi import Depends,HTTPException
from sqlalchemy import text
from .business import store
from .code_card import scope,assignment,business_scope,warehouse_scope

ROUTES={'card':'/catalog','template':'/catalog','sync':'/catalog','import':'/catalog',
        'marking':'/marking','atk':'/atks','transform':'/atks','fts':'/fts','split':'/split','remark':'/remarking'}
STATES={'draft':'Черновик','prepared':'Состав проверен','active':'Синхронизировано с НК','processing':'В обработке',
 'ordering':'Эмиссия и ожидание подписи','receiving':'Получение КМ','labels_ready':'Этикетки готовы',
 'application_confirmed':'Нанесение подтверждено','reporting':'Ожидание подтверждения отчёта',
 'applied':'Отчёт подтверждён','awaiting_approval':'Ожидает согласования','aggregating':'Регистрация АТК',
 'ready':'АТК готов','transferred':'Передано поставщику','fts_submitted':'Подача в ЧЗ и ожидание решения',
 'circulation_confirmed':'Ввод в оборот подтверждён','awaiting_full_invoice':'Возвращено в БМ для полного Excel',
 'warehouse_ready':'Полный состав готов','unmarked_ready':'Состав без КМ готов','completed':'Завершено логистом',
 'done':'Завершено','blocked':'Требуется проверка','rejected':'Отклонено','archived':'В архиве',
 'open':'В работе','closed':'Закрыто','cancelled':'Отменено'}
KINDS={'card':'НК','document':'Накладная БМ','marking':'Маркировка','atk':'АТК','fts':'ФТС / ввод в оборот',
 'transform':'Трансформация АТК','remark':'Перемаркировка','split':'Разделение КМ','receipt':'Приёмка','shipment':'Отгрузка',
 'sync':'Синхронизация НК','import':'Партия НК','template':'Шаблон НК','completion':'Завершение логистики'}
NEXT={
 'ordering':('Дождаться подписи и результата заказа КМ','БМ / подписант'),
 'receiving':('Дождаться получения полного комплекта КМ','БМ'),
 'application_confirmed':('Открыть процесс и отправить отчёт о нанесении','БМ'),
 'reporting':('Дождаться квитанции и подтверждённого статуса КМ','БМ'),
 'applied':('Проверить АТК и продолжить обработку у логиста','БМ / логист'),
 'awaiting_approval':('Согласовать состав АТК','Согласующий АТК'),
 'aggregating':('Дождаться подтверждения регистрации АТК','БМ'),
 'ready':('Подтвердить фактическую передачу готового АТК поставщику','БМ / логист'),
 'transferred':('Продолжить подготовку ФТС и логистику связанных накладных','Логист'),
 'prepared':('Открыть пакет: подписать подачу в ЧЗ либо проверить документ из ЛК','Логист / подписант'),
 'fts_submitted':('Дождаться решения и проверки ввода всех КМ в оборот','Логист'),
 'circulation_confirmed':('Продолжить логистику: полный состав и склад','Логист'),
 'awaiting_full_invoice':('Открыть накладную БМ и загрузить полный Excel поставки','БМ / закупщик'),
 'warehouse_ready':('Передать актуальный полный состав на склад','Логист'),
 'unmarked_ready':('Передать полный состав в логистику','Логист'),
 'completed':('Документ завершён; возврат в работу выполняет логист','Логист'),
 'done':('Просмотреть результат и связанные документы','Ответственный за документ'),
 'blocked':('Открыть документ и устранить причину остановки','Ответственный за документ'),
 'rejected':('Открыть документ и разобрать квитанцию до повторного действия','Ответственный за документ'),
 'archived':('Документ в архиве; для продолжения требуется разрешённый возврат','Администратор / ответственный'),
 'remarking':('Дождаться официального подтверждения замены КМ','Специалист по маркировке'),
}


def allowed(current,permission,row):
    try:current.require(permission,**store.context(current,row))
    except HTTPException as e:
        if e.status_code!=403:raise
        return False
    return True


def business_access(current,alias='i'):
    archive=scope(current,'documents.archive.view',alias+'.owner_id',assignment(alias))
    return business_scope(current,alias)+f" AND ({alias}.state<>'archived' OR {archive})"


def document_access(current,alias='d'):
    view=scope(current,'marking.documents.view',alias+'.owner_id',assignment(alias))
    archive=scope(current,'documents.archive.view',alias+'.owner_id',assignment(alias))
    return view+f" AND ({alias}.state<>'archived' OR {archive})"


def hint(row):
    state=row['state'];kind=row.get('kind','document');body=row.get('body',{})
    if kind=='document':
        if state=='prepared':return 'Начать маркировку проверенной накладной','БМ'
        if state=='draft':return 'Заполнить реквизиты и проверить состав','БМ / закупщик'
    if kind=='card' and state!='archived':
        statuses=body.get('external',{}).get('statuses',[])
        if state=='processing':return 'Дождаться результата текущей операции НК','Менеджер НК'
        if body.get('dirty'):return 'Проверить обязательные поля и сохранить изменения в НК','Менеджер НК'
        if not body.get('gtin'):return 'Заполнить характеристики и получить GTIN','Менеджер НК'
        if 'published' in statuses:return 'Использовать подтверждённый товар при подготовке накладной БМ','БМ / закупщик'
        if 'notsigned' in statuses:return 'Подписать и опубликовать карточку','Менеджер НК / подписант'
        if 'moderation' in statuses:return 'Дождаться модерации и обновить статус из НК','Менеджер НК'
        return 'Открыть карточку и продолжить публикацию в НК','Менеджер НК'
    if state=='labels_ready':
        transferred=body.get('label_file_id') and body.get('labels_handoff',{}).get('file_id')==body.get('label_file_id')
        return ('Подтвердить фактическое нанесение поставщиком' if transferred or kind!='marking' else 'Передать актуальные этикетки поставщику и зафиксировать передачу'),'БМ / поставщик'
    if kind in ('receipt','shipment'):
        if row.get('archived_at'):return NEXT['archived']
        if state=='open':return 'Сверить состав, выполнить проверку и закрыть задание без расхождений','Склад: '+KINDS[kind].lower()
        if state=='closed':return 'Передать результат логисту; архивирование доступно администратору','Логист / администратор'
        return 'Уточнить у логиста актуальное складское задание','Логист'
    return NEXT.get(state,('Открыть документ и проверить доступные действия','Ответственный за документ'))


def target(row,type='business',route=None):
    kind=row.get('kind','document')
    return {'type':type,'id':row['id'],'kind':kind,'organization_id':row['organization_id'],
            'route':route or ROUTES.get(kind,'/documents'),'archived':bool(row.get('archived_at') or row['state']=='archived')}


def overview(current,kind,id):
    c=current.connection;explicit_atks=[]
    if kind=='document':
        root=c.execute(text('SELECT * FROM bm_documents WHERE id=:id'),{'id':id}).mappings().first()
        if not root:raise HTTPException(404,'Документ не найден')
        current.require('marking.documents.view',**store.context(current,root))
        if root['state']=='archived':current.require('documents.archive.view',**store.context(current,root))
        root={**root,'kind':'document','title':root['number']};sources=[id]
    elif kind in ('business','logistics'):
        root=store.get(current,id,permission='logistics.documents.complete' if kind=='logistics' else None)
        if kind=='logistics' and root['kind']!='marking':raise HTTPException(404,'Накладная не найдена')
        if root['state']=='archived':current.require('documents.archive.view',**store.context(current,root))
        sources=[root['source_id']] if root['kind'] in ('marking','remark') and root['source_id'] else []
        if root['kind'] in ('atk','fts','transform'):
            # References establish a relationship, but grant no target visibility.
            atks=[str(id)] if root['kind']=='atk' else root['body'].get('atks',[]) if root['kind']=='fts' else [root['body'].get('atk_item')]
            explicit_atks=[v for v in atks if v]
            sources=list(c.execute(text('''SELECT DISTINCT m.source_id FROM business_items m
                JOIN business_items a ON a.organization_id=m.organization_id AND a.kind='atk'
                AND a.body->'documents' ? m.id::text WHERE m.organization_id=:org AND m.kind='marking'
                AND a.id=ANY(CAST(:ids AS uuid[])) ORDER BY m.source_id'''),{'org':root['organization_id'],'ids':[v for v in atks if v]}).scalars())
    elif kind=='warehouse':
        root=c.execute(text('SELECT * FROM warehouse_tasks WHERE id=:id'),{'id':id}).mappings().first()
        if not root:raise HTTPException(404,'Задание не найдено')
        ctx=store.context(current,root)
        if not allowed(current,'warehouse.'+root['kind']+'.view',root):current.require('logistics.warehouse.result.view',**ctx)
        visible=c.execute(text(f'SELECT 1 FROM warehouse_tasks w WHERE w.id=:id AND {warehouse_scope(current)}'),{'id':id,'user':current.id}).scalar_one_or_none()
        if not visible:raise HTTPException(403,'Нет доступа к архивному заданию')
        root={**root,'title':root['number']};sources=[root['source_id']] if root['source_type']=='bm' else []
    else:raise HTTPException(404,'Раздел не найден')
    org=root['organization_id'];params={'org':org,'user':current.id,'sources':sources,'root':id}
    if org not in current.organizations():raise HTTPException(403,'Нет доступа к организации')
    if root['kind']=='card':
        sources=list(c.execute(text(f'''SELECT d.id FROM bm_documents d WHERE d.organization_id=:org AND {document_access(current)}
            AND EXISTS(SELECT 1 FROM jsonb_array_elements(d.lines) l WHERE l->>'pn'=:pn AND :gtin<>'' AND l->>'gtin'=:gtin)
            ORDER BY d.created_at DESC,d.id DESC LIMIT 25'''),{**params,'pn':root['body'].get('pn',''),'gtin':root['body'].get('gtin','')}).scalars())
        params['sources']=sources
    # Apply scopes in SQL BEFORE pagination. Raw business bodies never leave this read model.
    documents=c.execute(text(f'''SELECT d.* FROM bm_documents d WHERE d.organization_id=:org
        AND d.id=ANY(CAST(:sources AS uuid[])) AND {document_access(current)} ORDER BY d.created_at DESC,d.id DESC LIMIT 26'''),params).mappings().all()
    processes=list(c.execute(text("SELECT id FROM business_items WHERE organization_id=:org AND kind='marking' AND source_id=ANY(CAST(:sources AS uuid[]))"),params).scalars())
    params['processes']=processes
    atk_ids=list(c.execute(text("SELECT id FROM business_items WHERE organization_id=:org AND kind='atk' AND body->'documents' ?| CAST(:processes AS text[])"),{**params,'processes':list(map(str,processes))}).scalars())
    atk_ids=list(dict.fromkeys([*map(str,atk_ids),*map(str,explicit_atks)]))
    params['atks']=list(map(str,atk_ids));params['processes']=list(map(str,processes))
    groups=[];truncated=False;related={}
    def group(name,rows,type,route=None):
        nonlocal truncated
        truncated|=len(rows)>25
        entries=[]
        for r in rows[:25]:
            r=dict(r);r.setdefault('kind','document');r.setdefault('title',r.get('number',''))
            label=KINDS.get(r['kind'],'Документ')
            entries.append({'title':r['title'],'label':label,'state':STATES.get('archived' if r.get('archived_at') else r['state'],'Требуется просмотр документа'),
                'target':target(r,type,route or ('/logistics' if type=='warehouse' and not allowed(current,'warehouse.'+r['kind']+'.view',r) else '/'+r['kind'] if type=='warehouse' else None))})
        if entries:groups.append({'name':name,'items':entries})
    group('Накладные БМ',documents,'document')
    for group_kind,name,condition in [
        ('card','Национальный каталог',"(i.body->>'pn',i.body->>'gtin') IN (SELECT l->>'pn',l->>'gtin' FROM bm_documents d CROSS JOIN LATERAL jsonb_array_elements(d.lines) l WHERE d.id=ANY(CAST(:sources AS uuid[])) AND d.organization_id=:org AND l->>'gtin'<>'')"),
        ('marking','Эмиссия и нанесение',"i.source_id=ANY(CAST(:sources AS uuid[]))"),
        ('atk','АТК',"i.id=ANY(CAST(:atks AS uuid[]))"),
        ('fts','ФТС и ввод в оборот',"i.body->'atks' ?| CAST(:atks AS text[])"),
        ('transform','Трансформации АТК',"i.body->>'atk_item'=ANY(CAST(:atks AS text[]))"),
        ('remark','Замена повреждённых КМ',"i.source_id=ANY(CAST(:sources AS uuid[]))")]:
        rows=c.execute(text(f'''SELECT i.* FROM business_items i WHERE i.organization_id=:org AND i.kind=:kind
            AND ({condition} OR i.id=:root) AND {business_access(current)} ORDER BY i.created_at DESC,i.id DESC LIMIT 26'''),{**params,'kind':group_kind}).mappings().all()
        related[group_kind]=rows
        group(name,rows,'business')
    tasks=c.execute(text(f'''SELECT w.* FROM warehouse_tasks w WHERE w.organization_id=:org
        AND (w.source_id=ANY(CAST(:sources AS uuid[])) OR w.id=:root) AND {warehouse_scope(current)}
        ORDER BY w.created_at DESC,w.id DESC LIMIT 26'''),params).mappings().all()
    group('Приёмка и отгрузка',tasks,'warehouse')
    completion_scope=scope(current,'logistics.documents.complete','i.owner_id',assignment('i'))
    completions=c.execute(text(f'''SELECT i.* FROM business_items i WHERE i.organization_id=:org AND i.kind='marking'
        AND i.source_id=ANY(CAST(:sources AS uuid[])) AND i.state<>'archived' AND {completion_scope}
        ORDER BY i.created_at DESC,i.id DESC LIMIT 26'''),params).mappings().all()
    group('Завершение логистики',completions,'completion','/logistics')
    next_row=dict(root);blockers=[]
    if kind=='document' and root['state']!='archived':
        process=c.execute(text("SELECT * FROM business_items WHERE source_id=:id AND kind='marking' AND state<>'archived'"),{'id':id}).mappings().first()
        if process:
            if allowed(current,store.VIEW['marking'],process):next_row=dict(process)
            else:blockers.append('Следующий этап ведёт назначенный сотрудник. Попросите его проверить ход обработки.')
    if next_row['kind']=='marking' and allowed(current,'logistics.documents.complete',next_row):
        from .business.logistics import facts
        facts_value=facts(c,next_row);blockers=facts_value['blockers']
        if next_row['state']=='completed':blockers=[]
        if facts_value['can_complete'] and next_row['state']!='completed':next_row={**next_row,'state':'completion_ready'}
    if kind=='warehouse':
        if root['source_type']=='bm':
            source=c.execute(text('SELECT state,revision FROM bm_documents WHERE id=:id'),{'id':root['source_id']}).mappings().one()
            if source['state']=='archived' or source['revision']!=root['source_revision']:blockers.append('Исходная накладная остановлена или изменена. Обратитесь к логисту.')
        if root.get('check_result') and not root['check_result'].get('ok'):blockers.append('Последняя сверка выявила расхождения. Откройте результат проверки задания.')
    if root['kind']=='card' and root['body'].get('dirty'):blockers.append('Локальные изменения ещё не подтверждены Национальным каталогом.')
    operation_states=set()
    if next_row['kind'] not in ('document','receipt','shipment'):
        operation_states=set(c.execute(text('''SELECT DISTINCT o.state FROM external_operations o
            JOIN business_links l ON l.operation_id=o.id WHERE l.item_id=:id'''),{'id':next_row['id']}).scalars())
        messages={'unknown':'Исход внешней операции неизвестен. Сначала сверьте результат по квитанции; повторная отправка не разрешается автоматически.',
            'blocked':'Внешняя операция заблокирована. Откройте документ и проверьте полномочия, источник и условия отправки.',
            'awaiting_signature':'Для продолжения требуется подпись разрешённого подписанта.'}
        blockers.extend(message for state,message in messages.items() if state in operation_states)
    label,role=hint(next_row)
    if 'unknown' in operation_states:label,role='Сверить неизвестный результат внешней операции до продолжения','Специалист по маркировке / администратор'
    elif 'blocked' in operation_states:label,role='Разобрать причину блокировки в исходном документе','Ответственный за документ'
    elif 'awaiting_signature' in operation_states:label,role='Подписать ожидающий документ разрешённым подписантом','Подписант'

    if next_row['state']=='completion_ready' and not operation_states.intersection({'unknown','blocked','awaiting_signature'}):label,role='Открыть проверку этапов и завершить накладную','Логист'
    if blockers and kind=='document' and next_row['kind']=='document':label,role='Уточнить ход обработки у ответственного сотрудника','Назначенный сотрудник'
    next_target=None
    if next_row['kind'] not in ('document','receipt','shipment') and allowed(current,store.VIEW[next_row['kind']],next_row):next_target=target(next_row)
    elif kind=='document':next_target=target(root,'document')
    elif kind=='warehouse':next_target=target(root,'warehouse','/'+root['kind'] if allowed(current,'warehouse.'+root['kind']+'.view',root) else '/logistics')
    waiting=operation_states.intersection({'unknown','blocked','awaiting_signature'})
    if next_row['state']=='awaiting_full_invoice':
        source=next((d for d in documents if d['id']==next_row.get('source_id')),None)
        next_target=target(source,'document') if source else None
    elif next_row['state']=='completion_ready' and not waiting:
        next_target=target(next_row,'completion','/logistics')
    elif next_row['kind']=='marking' and next_row['state']=='warehouse_ready' and not waiting:
        active=next((t for t in tasks if t['state']=='open' and not t['archived_at']),None)
        if active:
            label,role='Открыть '+KINDS[active['kind']].lower()+' и сверить поставку','Склад / логист'
            next_target=target(active,'warehouse','/'+active['kind'] if allowed(current,'warehouse.'+active['kind']+'.view',active) else '/logistics')
    elif next_row['kind']=='marking' and next_row['state']=='applied' and not waiting:
        pending=next((a for a in related.get('atk',[]) if a['state'] in ('awaiting_approval','aggregating','ready')),None)
        if pending:
            label,role=hint(dict(pending));next_target=target(pending)
    # Navigation targets always retain the independently checked visibility of
    # their group. No unavailable ID is returned as a suggested next step.
    owner=c.execute(text('SELECT display_name FROM users WHERE id=:id'),{'id':root['owner_id']}).scalar_one()
    return {'title':root['title'],'stage':STATES.get(next_row['state'] if kind=='document' else 'archived' if root.get('archived_at') else root['state'],'Требуется просмотр документа'),'next_action':label,
        'next_target':next_target,'responsible_role':role,'owner_name':owner,'blockers':blockers,'groups':groups,'truncated':truncated,
        'checked_at':datetime.now(timezone.utc),'read_only':True}


def install_workflow(app,actor):
    @app.get('/api/workflow/{kind}/{id}')
    def detail(kind:str,id:UUID,current=Depends(actor,scope='function')):
        return overview(current,kind,id)
