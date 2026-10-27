"""Local problem resolution. Never writes a code status in ChZ or replays a mutation."""
from hashlib import sha256
import json,re
from uuid import UUID
from fastapi import Depends,HTTPException,Query
from pydantic import Field,StrictInt
from sqlalchemy import text
from . import auth,gs1
from .auth_routes import Body

CATEGORIES={'foreign_owner':'Чужой владелец','invalid_status':'Недопустимый статус','damaged_label':'Повреждённая этикетка','duplicate_code':'Повтор КМ','composition_mismatch':'Несовпадение состава','shortage':'Недостача','remote_error':'Ошибка ЧЗ','unknown_operation':'Неизвестный результат операции','other':'Другая проблема'}
RESTRICTIONS={'receipt':'Закрытие приёмки','shipment':'Отгрузка и её выгрузка','split':'Разделение','reprint':'Печать / перепечать','external':'Эмиссия / отчёт / АТК / ФТС','remark':'Официальная перемаркировка'}

class Create(Body):
    organization_id:UUID
    request_id:UUID
    source_kind:str=Field(default='none',pattern='^(none|task|document|business)$')
    source_id:UUID|None=None
    operation_id:UUID|None=None
    code:str=Field(default='',max_length=1024)
    category:str=Field(pattern='^(foreign_owner|invalid_status|damaged_label|duplicate_code|composition_mismatch|shortage|remote_error|unknown_operation|other)$')
    description:str=Field(min_length=8,max_length=5000)
    assignee_id:UUID|None=None
    restrictions:list[str]=Field(default_factory=list,max_length=6)
class Action(Body):
    request_id:UUID
    revision:StrictInt=Field(ge=1)
    action:str=Field(pattern='^(comment|assign|start|resolve|reopen|quarantine)$')
    message:str=Field(min_length=1,max_length=5000)
    assignee_id:UUID|None=None
    restrictions:list[str]=Field(default_factory=list,max_length=6)


def fingerprint(body):return sha256(body.model_dump_json().encode()).hexdigest()

def source_context(current,kind,id,org,*,creation=False):
    c=current.connection
    if kind=='none':
        if id is not None:raise HTTPException(422,'Не указан вид связанного документа')
        return {'organization_id':org,'owner_id':current.id,'assigned':False},''
    if id is None:raise HTTPException(422,'Выберите связанный документ')
    table={'task':'warehouse_tasks','document':'bm_documents','business':'business_items'}[kind]
    row=c.execute(text('SELECT * FROM '+table+' WHERE id=:id AND organization_id=:org'),{'id':id,'org':org}).mappings().first()
    if not row:raise HTTPException(404,'Связанный документ не найден')
    assigned=c.execute(text('SELECT EXISTS(SELECT 1 FROM operation_assignments WHERE target_id=:id AND user_id=:user)'),{'id':id,'user':current.id}).scalar_one()
    ctx=dict(organization_id=org,owner_id=row['owner_id'],assigned=assigned)
    if creation:
        if kind=='task':
            try:current.require('warehouse.'+row['kind']+'.view',**ctx)
            except HTTPException as e:
                if e.status_code!=403:raise
                current.require('logistics.warehouse.result.view',**ctx)
        elif kind=='document':current.require('marking.documents.view',**ctx)
        else:
            from .business import store
            store.require(current,row)
    return ctx,row['title'] if kind=='business' else row['number']

def context(current,row):
    assigned=row['assignee_id']==current.id
    if row['source_id'] and not assigned:
        assigned=current.connection.execute(text('SELECT EXISTS(SELECT 1 FROM operation_assignments WHERE target_id=:id AND user_id=:user)'),{'id':row['source_id'],'user':current.id}).scalar_one()
    return dict(organization_id=row['organization_id'],owner_id=row['reporter_id'],assigned=assigned)

def allowed(current,row,manage=False):
    return current.require('incidents.manage' if manage else 'incidents.create',**context(current,row))

def get(current,id,lock=False):
    row=current.connection.execute(text('SELECT * FROM incidents WHERE id=:id'+(' FOR UPDATE' if lock else '')),{'id':id}).mappings().first()
    if not row:raise HTTPException(404,'Проблема не найдена')
    allowed(current,row);return dict(row)

def restrictions(values):
    if len(set(values))!=len(values) or not set(values)<=RESTRICTIONS.keys():raise HTTPException(422,'Выберите допустимые ограничения карантина')
    return sorted(values)

def assignee(current,org,id,reporter):
    if id is None:return
    row=current.connection.execute(text('SELECT u.* FROM users u JOIN user_organizations o ON o.user_id=u.id WHERE u.id=:id AND o.organization_id=:org AND u.enabled'),{'id':id,'org':org}).mappings().first()
    if not row:raise HTTPException(422,'Ответственный отключён или не имеет доступа к организации')
    try:auth.Actor(current.connection,dict(row),'incident-recipient',None).require('incidents.manage',organization_id=org,owner_id=reporter,assigned=True)
    except HTTPException:raise HTTPException(422,'У ответственного нет права работать с проблемами') from None

def receipt(row):return {k:row[k] for k in ('id','revision','status','assignee_id','restrictions')}

def record(current,row,request_id,hash,action,body):
    result=receipt(row)
    current.connection.execute(text('''INSERT INTO incident_events(incident_id,actor_id,request_id,request_sha256,action,body,result)
        VALUES (:id,:actor,:request,:hash,:action,CAST(:body AS jsonb),CAST(:result AS jsonb))'''),{'id':row['id'],'actor':current.id,'request':request_id,'hash':hash,'action':action,'body':json.dumps(body,default=str),'result':json.dumps(result,default=str)})
    auth.audit(current.connection,current.id,'incidents.'+action,row['id'],{'category':row['category'],'restrictions':row['restrictions']})
    return result


def blockers(c,org,action,*,cises=(),targets=()):
    """Read-only check; replacement ancestry retains a hold until explicitly released."""
    values=list(set(cises));ids=list(set(v for v in targets if v))
    return c.execute(text('''WITH RECURSIVE ancestors(id,cis) AS (
        SELECT id,cis FROM business_codes WHERE organization_id=:org AND cis=ANY(CAST(:codes AS text[]))
        UNION SELECT old.id,old.cis FROM ancestors a JOIN code_replacements r ON r.new_code_id=a.id
        JOIN business_codes old ON old.id=r.old_code_id AND old.organization_id=:org)
        SELECT i.id,i.category FROM incidents i WHERE i.organization_id=:org AND i.status<>'resolved'
        AND :action=ANY(i.restrictions) AND (i.cis=ANY(CAST(:codes AS text[])) OR i.cis IN (SELECT cis FROM ancestors)
        OR (i.cis IS NULL AND i.source_id=ANY(CAST(:targets AS uuid[])))) ORDER BY i.created_at,i.id LIMIT 20'''),{'org':org,'action':action,'codes':values,'targets':ids}).mappings().all()

def enforce(c,org,action,*,cises=(),targets=()):
    if blockers(c,org,action,cises=cises,targets=targets):raise HTTPException(409,{'code':'local_quarantine','message':'Действие заблокировано локальным карантином. Передайте проблему ответственному; статус ЧЗ не изменён.'})

def business_scope(c,row,step,payload=None):
    if step.startswith('nk.') or step.endswith('.fetch'):return None,[],[]
    action='remark' if step.startswith('remark.') else 'split' if step.startswith('split.') else 'external'
    body=row['body'];codes={r for r in (body.get('source_cis'),body.get('old_cis'),body.get('new_cis')) if r}
    codes.update(c.execute(text('SELECT cis FROM business_codes WHERE item_id=:id'),{'id':row['id']}).scalars())
    if body.get('codes'):
        if isinstance(body['codes'][0],dict):codes.update(v['cis'] for v in body['codes'] if v.get('cis'))
        else:codes.update(c.execute(text('SELECT cis FROM business_codes WHERE organization_id=:org AND id=ANY(CAST(:ids AS uuid[]))'),{'org':row['organization_id'],'ids':body['codes']}).scalars())
    targets=[row['id'],row.get('source_id'),body.get('atk_item'),*body.get('atks',[]),*body.get('documents',[])]
    return action,codes,[UUID(str(v)) for v in targets if v]

def enforce_business(c,row,step):
    action,codes,targets=business_scope(c,row,step)
    if action:enforce(c,row['organization_id'],action,cises=codes,targets=targets)


def install_incidents(app,actor):
    dep=Depends(actor,scope='function')
    @app.get('/api/incidents/options')
    def options(organization_id:UUID,current=dep):
        if organization_id not in current.organizations() or 'incidents.create' not in current.grants():raise HTTPException(403,'Раздел недоступен')
        people=[] if 'incidents.manage' not in current.grants() else current.connection.execute(text("SELECT u.id,u.display_name FROM users u JOIN user_organizations o ON o.user_id=u.id JOIN group_grants g ON g.group_slug=u.group_slug AND g.permission_id='incidents.manage' AND g.scope<>'none' WHERE u.enabled AND o.organization_id=:org ORDER BY u.display_name,u.id"),{'org':organization_id}).mappings().all()
        return {'categories':CATEGORIES,'restrictions':RESTRICTIONS,'assignees':[dict(r) for r in people]}
    @app.post('/api/incidents')
    def create(body:Create,current=dep):
        c=current.connection;hash=fingerprint(body)
        prior=c.execute(text('SELECT * FROM incidents WHERE id=:id'),{'id':body.request_id}).mappings().first()
        if prior:
            allowed(current,prior)
            if prior['reporter_id']!=current.id or prior['request_sha256']!=hash:raise HTTPException(409,'Идентификатор запроса уже использован')
            return receipt(prior)
        ctx,title=source_context(current,body.source_kind,body.source_id,body.organization_id,creation=True);current.require('incidents.create',**ctx)
        if len(body.description.strip())<8:raise HTTPException(422,'Опишите проблему (от 8 символов)')
        holds=restrictions(body.restrictions)
        if holds or body.assignee_id:current.require('incidents.manage',**ctx)
        assignee(current,body.organization_id,body.assignee_id,current.id)
        cis=None
        if body.code:
            raw=body.code.encode('utf-8')
            try:
                if re.fullmatch(r'01[0-9]{14}21[!-~]{20}',body.code) and gs1.valid_gtin(body.code[2:16]):cis=body.code
                else:cis=gs1.parse(raw,scanner=True).cis
            except ValueError:raise HTTPException(422,'КМ не прочитан. Оставьте поле пустым и опишите повреждение; карантин без КМ действует на связанный документ.') from None
        if holds and not cis and body.source_id is None:raise HTTPException(422,'Для карантина выберите КМ или связанный документ')
        if body.operation_id:
            op=c.execute(text('SELECT o.*,t.owner_id FROM external_operations o JOIN operation_targets t ON t.id=o.target_id WHERE o.id=:id AND o.organization_id=:org'),{'id':body.operation_id,'org':body.organization_id}).mappings().first()
            if not op:raise HTTPException(404,'Внешняя операция не найдена')
            assigned=c.execute(text('SELECT EXISTS(SELECT 1 FROM operation_assignments WHERE target_id=:id AND user_id=:user)'),{'id':op['target_id'],'user':current.id}).scalar_one()
            current.require('operations.view',organization_id=body.organization_id,owner_id=op['owner_id'],assigned=assigned)
        c.execute(text('''INSERT INTO incidents(id,organization_id,reporter_id,assignee_id,source_kind,source_id,source_title,operation_id,cis,category,description,restrictions,request_sha256)
            VALUES (:id,:org,:reporter,:assignee,:kind,:source,:title,:op,:cis,:category,:description,CAST(:restrictions AS text[]),:hash)'''),{'id':body.request_id,'org':body.organization_id,'reporter':current.id,'assignee':body.assignee_id,'kind':body.source_kind,'source':body.source_id,'title':title,'op':body.operation_id,'cis':cis,'category':body.category,'description':body.description.strip(),'restrictions':holds,'hash':hash})
        row=dict(c.execute(text('SELECT * FROM incidents WHERE id=:id'),{'id':body.request_id}).mappings().one())
        return record(current,row,body.request_id,hash,'created',{'description':body.description.strip(),'cis':cis,'source':title,'restrictions':holds,'assignee_id':body.assignee_id})
    @app.get('/api/incidents')
    def listing(organization_id:UUID,status:str=Query('active',pattern='^(active|all|open|in_progress|resolved)$'),q:str=Query('',max_length=120),category:str=Query('',max_length=50),source_id:UUID|None=None,before_id:UUID|None=None,quarantined:bool=False,current=dep):
        if category and category not in CATEGORIES:raise HTTPException(422,'Неизвестный тип проблемы')
        grant=current.grants().get('incidents.create')
        if not grant or organization_id not in current.organizations():raise HTTPException(403,'Раздел недоступен')
        rows=current.connection.execute(text('''SELECT i.*,r.display_name AS reporter_name,u.display_name AS assignee_name FROM incidents i
            JOIN users r ON r.id=i.reporter_id LEFT JOIN users u ON u.id=i.assignee_id
            WHERE i.organization_id=:org AND (:scope='all' OR (:scope='own' AND i.reporter_id=:user) OR (:scope='assigned' AND
                (i.assignee_id=:user OR EXISTS(SELECT 1 FROM operation_assignments a WHERE a.target_id=i.source_id AND a.user_id=:user))))
            AND (:status='all' OR (:status='active' AND i.status<>'resolved') OR i.status=:status)
            AND (:category='' OR i.category=:category)
            AND (NOT :quarantine OR cardinality(i.restrictions)>0)
            AND (CAST(:source AS uuid) IS NULL OR i.source_id=CAST(:source AS uuid))
            AND (CAST(:before AS uuid) IS NULL OR (i.created_at,i.id)<(SELECT created_at,id FROM incidents WHERE id=CAST(:before AS uuid) AND organization_id=:org))
            AND (i.description ILIKE :q ESCAPE '!' OR i.source_title ILIKE :q ESCAPE '!' OR i.cis LIKE :q ESCAPE '!')
            ORDER BY i.created_at DESC,i.id DESC LIMIT 26'''),{'org':organization_id,'scope':grant,'user':current.id,'status':status,'category':category,'source':source_id,'before':before_id,'quarantine':quarantined,'q':'%'+q.replace('!','!!').replace('%','!%').replace('_','!_')+'%'}).mappings().all()
        return {'items':[{k:v for k,v in r.items() if k!='request_sha256'} for r in rows[:25]],'next_cursor':str(rows[24]['id']) if len(rows)>25 else None}
    @app.get('/api/incidents/{id}')
    def detail(id:UUID,before_event:StrictInt|None=Query(None,ge=1),current=dep):
        row=get(current,id);row.pop('request_sha256')
        row['assignee_name']=current.connection.execute(text('SELECT display_name FROM users WHERE id=:id'),{'id':row['assignee_id']}).scalar_one_or_none() if row['assignee_id'] else None
        events=current.connection.execute(text('''SELECT e.id,e.action,e.body,e.result,e.created_at,u.display_name AS actor_name
            FROM incident_events e JOIN users u ON u.id=e.actor_id WHERE e.incident_id=:id
            AND (CAST(:before AS bigint) IS NULL OR e.id<CAST(:before AS bigint)) ORDER BY e.id DESC LIMIT 51'''),{'id':id,'before':before_event}).mappings().all()
        can_manage=True
        try:allowed(current,row,True)
        except HTTPException:can_manage=False
        return {'incident':row,'events':[dict(r) for r in events[:50]],'next_event':events[49]['id'] if len(events)>50 else None,'can_manage':can_manage}
    @app.post('/api/incidents/{id}/actions')
    def act(id:UUID,body:Action,current=dep):
        row=get(current,id,True);c=current.connection;hash=fingerprint(body)
        prior=c.execute(text('SELECT * FROM incident_events WHERE incident_id=:id AND request_id=:request'),{'id':id,'request':body.request_id}).mappings().first()
        if body.action!='comment':allowed(current,row,True)
        if prior:
            if prior['actor_id']!=current.id or prior['request_sha256']!=hash:raise HTTPException(409,'Идентификатор запроса уже использован')
            return prior['result']
        if body.revision!=row['revision']:raise HTTPException(409,'Проблема изменилась. Обновите карточку.')
        if not body.message.strip() or (body.action!='comment' and len(body.message.strip())<8):raise HTTPException(422,'Укажите содержательный комментарий к действию')
        state=row['status'];holds=row['restrictions'];owner=row['assignee_id']
        if body.action=='assign':assignee(current,row['organization_id'],body.assignee_id,row['reporter_id']);owner=body.assignee_id
        elif body.action=='start':
            if state!='open':raise HTTPException(409,'В работу принимается открытая проблема')
            state='in_progress'
        elif body.action=='resolve':
            if state=='resolved':raise HTTPException(409,'Проблема уже решена')
            if row['operation_id']:
                remote=c.execute(text('SELECT state FROM external_operations WHERE id=:id'),{'id':row['operation_id']}).scalar_one()
                if remote not in ('confirmed','rejected','cancelled_before_dispatch'):raise HTTPException(409,'Исход внешней операции ещё не подтверждён. Локальное решение не заменяет сверку ЧЗ.')
            state='resolved';holds=[]
        elif body.action=='reopen':
            if state!='resolved':raise HTTPException(409,'Вернуть можно только решённую проблему')
            state='open'
        elif body.action=='quarantine':
            if state=='resolved':raise HTTPException(409,'Сначала верните проблему в работу')
            holds=restrictions(body.restrictions)
            if holds and row['cis'] is None and row['source_id'] is None:raise HTTPException(422,'Для карантина нужен КМ или документ')
        c.execute(text('''UPDATE incidents SET status=:status,assignee_id=:assignee,restrictions=CAST(:holds AS text[]),revision=revision+1,updated_at=now(),
            resolved_at=CASE WHEN :status='resolved' THEN now() ELSE NULL END WHERE id=:id'''),{'id':id,'status':state,'assignee':owner,'holds':holds})
        row=dict(c.execute(text('SELECT * FROM incidents WHERE id=:id'),{'id':id}).mappings().one())
        return record(current,row,body.request_id,hash,body.action,{'message':body.message.strip(),'assignee_id':owner,'restrictions':holds})
