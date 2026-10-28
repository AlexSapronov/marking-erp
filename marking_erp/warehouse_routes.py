"""Physical warehouse reconciliation. Does not assert ownership or True API status."""
from io import BytesIO
import json,time
from datetime import datetime,timezone
from hashlib import sha256
from uuid import UUID, uuid4
from fastapi import Depends, HTTPException, Query, Response
from pydantic import Field, StrictInt
from sqlalchemy import text
import xlsxwriter
from . import auth, gs1, warehouse_check as checks
from .auth_routes import Body


class Transfer(Body):
    request_id: UUID
    source_id: UUID
    source_revision: StrictInt = Field(ge=1)
    kind: str = Field(pattern='^(receipt|shipment)$')
    assignees: list[UUID] = Field(min_length=1, max_length=100)
    expected_codes:list[str]=Field(default_factory=list,max_length=100000)


class Scan(Body):
    request_id: UUID
    raw: str = Field(min_length=1, max_length=1024)
    line_index: StrictInt | None = Field(default=None, ge=0)


class Transition(Body):
    revision: StrictInt = Field(ge=1)
    confirmation: str = Field(min_length=1, max_length=120)
    reason: str = Field(default='', max_length=1000)


class Check(Body):
    request_id:UUID
    revision:StrictInt=Field(ge=1)


def install_warehouse(app, actor, settings=None):
    dep = Depends(actor, scope='function')

    def context(current, task):
        assigned = current.connection.execute(text('SELECT EXISTS(SELECT 1 FROM operation_assignments WHERE target_id=:id AND user_id=:user)'), {'id':task['id'], 'user':current.id}).scalar_one()
        return dict(organization_id=task['organization_id'], owner_id=task['owner_id'], assigned=assigned)

    def require(current, task, action='view', logistics=False):
        ctx = context(current, task)
        if logistics:
            current.require('logistics.warehouse.result.view', **ctx)
        else:
            current.require('warehouse.'+task['kind']+'.'+action, **ctx)

    def archive_admin(current,permission,**ctx):
        if current.user['group_slug']!='administrator' and not auth.group_has_admin(current.connection,current.user['group_slug']):raise HTTPException(403,'Архивирование склада доступно администратору')
        current.require(permission,**ctx,administrative=True)

    def get(current, id, lock=False):
        task = current.connection.execute(text('SELECT * FROM warehouse_tasks WHERE id=:id'+(' FOR UPDATE' if lock else '')), {'id':id}).mappings().first()
        if not task: raise HTTPException(404, 'Задание не найдено')
        return dict(task)

    def source_status(current,task,lock=False):
        if task['source_type']=='excel':return {'state':'prepared','revision':1}
        return current.connection.execute(text('SELECT state,revision FROM bm_documents WHERE id=:id'+(' FOR UPDATE' if lock else '')),{'id':task['source_id']}).mappings().one()

    def writable(current, task):
        if task.get('archived_at'):raise HTTPException(409,'Задание в архиве')
        if task['state']!='open': raise HTTPException(409, 'Задание закрыто или отменено')
        source = source_status(current,task,True)
        if source['state']=='archived': raise HTTPException(409, 'Исходная накладная в архиве. Работа остановлена.')
        if source['revision']!=task['source_revision']: raise HTTPException(409, 'Состав исходной накладной изменился. Обратитесь к логисту.')

    def event(current, task, action, detail=None):
        c=current.connection
        c.execute(text('INSERT INTO warehouse_events(task_id,actor_id,action,detail) VALUES (:id,:user,:action,CAST(:detail AS jsonb))'), {'id':task['id'], 'user':current.id, 'action':action, 'detail':json.dumps(detail or {})})
        auth.audit(c, current.id, 'warehouse.'+action, task['id'], {'kind':task['kind']})
        c.execute(text('UPDATE warehouse_tasks SET revision=revision+1,updated_at=now() WHERE id=:id'), {'id':task['id']})
        if action in ('scanned','scan_removed','reopen','cancel'):
            c.execute(text('UPDATE warehouse_tasks SET verified_at=NULL,check_result=NULL,check_digest=NULL,check_policy_sha256=NULL WHERE id=:id'),{'id':task['id']})

    def exclusive_codes(current, task, cises, *, scanning=False):
        """One source may receive and ship a KM; different open sources may not.

        Transfer/reopen use the exclusive authorization fence. Scans share that
        fence, so a per-organization/CIS lock also coordinates legacy tasks that
        have no expected list. No network I/O is performed under this lock.
        """
        codes=sorted(set(cises))
        if not codes:return
        c=current.connection
        if scanning:
            c.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'),
                {'key':'warehouse-cis:'+str(task['organization_id'])+':'+codes[0]})
        conflict=c.execute(text("""SELECT EXISTS(
            SELECT 1 FROM warehouse_tasks t
            WHERE t.organization_id=:org AND t.source_id<>:source AND t.state='open'
            AND t.expected_codes ?| CAST(:codes AS text[])) OR EXISTS(
            SELECT 1 FROM warehouse_scans s JOIN warehouse_tasks t ON t.id=s.task_id
            WHERE t.organization_id=:org AND t.source_id<>:source AND t.state='open'
            AND s.removed_at IS NULL AND s.cis=ANY(CAST(:codes AS text[])))"""),
            {'org':task['organization_id'],'source':task['source_id'],'codes':codes}).scalar_one()
        if conflict:raise HTTPException(409,'КМ уже закреплён за другой активной накладной. Обратитесь к логисту для сверки состава.')

    def summary(current, task):
        counts={r['line_index']:dict(r) for r in current.connection.execute(text('SELECT line_index,count(*) AS scanned,coalesce(sum(quantity),0) AS units FROM warehouse_scans WHERE task_id=:id AND removed_at IS NULL GROUP BY line_index'), {'id':task['id']}).mappings()}
        return [{**line, 'line_index':i, 'scanned':int(counts.get(i,{}).get('scanned',0)), 'units':int(counts.get(i,{}).get('units',0)), 'expected_units':line['quantity']*line['labels']} for i,line in enumerate(task['lines'])]

    def permitted(current, task, action='view', logistics=False):
        try:
            require(current,task,action,logistics)
            return True
        except HTTPException as error:
            if error.status_code!=403: raise
            return False

    def live_policy(current,task):
        if settings is None:return None
        configured=current.connection.execute(text('SELECT EXISTS(SELECT 1 FROM integration_settings WHERE organization_id=:org)'),{'org':task['organization_id']}).scalar_one()
        if not configured:return None
        from .business.connections import load
        config=load(current.connection,settings,task['organization_id'])
        return config if config.get('warehouse_live_checks',True) else None

    def verify_codes(current,task,codes,config,timeout):
        from .business.live import Live
        from datetime import datetime,timezone
        inn=current.connection.execute(text('SELECT inn FROM organizations WHERE id=:id'),{'id':task['organization_id']}).scalar_one()
        live=Live(config,inn);verified={}
        expected=set(task['expected_codes'])
        if not expected:raise HTTPException(409,'Нет ожидаемого перечня КМ поставки. Логисту нужно передать точный состав КМ.')
        if any(code['cis'] not in expected for code in codes):raise HTTPException(422,'Этот КМ не ожидается в данной поставке')
        records={};deadline=time.monotonic()+timeout
        for start in range(0,len(codes),500):
            remaining=deadline-time.monotonic()
            if remaining<=0:raise HTTPException(422,'Истёк срок пакетной проверки. Повторите проверку.')
            records.update(live.info([c['cis'] for c in codes[start:start+500]],timeout=min(40,remaining)))
        if time.monotonic()>deadline:raise HTTPException(422,'Истёк срок пакетной проверки. Повторите проверку.')
        for code in codes:
            info=records[code['cis']];statuses=('INTRODUCED',) if task['kind']=='shipment' else ('APPLIED','INTRODUCED')
            if info.get('status') not in statuses:raise HTTPException(422,'Недопустимый статус КМ в ЧЗ: '+str(info.get('status','не установлен')))
            if not info.get('ownerInn') or (not config.get('ignore_legal_entity') and info['ownerInn']!=inn):raise HTTPException(422,'КМ принадлежит другому юридическому лицу или владелец не установлен')
            if str(info.get('gtin',''))!=code['gtin']:raise HTTPException(422,'GTIN в ответе ЧЗ не совпадает с КМ')
            if str(info.get('quantityInPack',''))!=str(code['quantity']):raise HTTPException(422,'Количество в ЧЗ не совпадает с количеством в КМ')
            verified[code['cis']]={'checked_at':datetime.now(timezone.utc).isoformat(),'status':info['status'],'owner_inn':info['ownerInn'],'quantity':code['quantity']}
        return verified

    def active_scans(current,task):
        return [dict(r) for r in current.connection.execute(text('SELECT id,cis,gtin,quantity,line_index,payload FROM warehouse_scans WHERE task_id=:id AND removed_at IS NULL'),{'id':task['id']}).mappings()]

    def check_context(current,task):
        config=live_policy(current,task)
        inn=current.connection.execute(text('SELECT inn FROM organizations WHERE id=:id'),{'id':task['organization_id']}).scalar_one()
        public=checks.public_policy(current.connection,task,config,inn)
        return config,public

    def fresh(task,scans,public):
        result=task['check_result']
        if not result or not result.get('ok') or task['check_digest']!=checks.scan_digest(task,scans) or task['check_policy_sha256']!=checks.digest(public):return False
        age=(datetime.now(timezone.utc)-datetime.fromisoformat(result['checked_at'])).total_seconds()
        return 0<=age<=public['rules']['warehouse.external_fact_max_age_seconds']

    def run_check(current,task,request_id,request_hash,scans,config,public):
        from .business.errors import RemoteError
        exclusive_codes(current,task,task['expected_codes']+[v['cis'] for v in scans])
        counts=summary(current,task);expected=set(task['expected_codes']);accepted={v['cis'] for v in scans}
        differences=[{'line_index':v['line_index'],'pn':v['pn'],'expected':v['labels'],'scanned':v['scanned'],'expected_units':v['expected_units'],'units':v['units']}
            for v in counts if v['scanned']!=v['labels'] or v['units']!=v['expected_units']]
        missing=expected-accepted;extra=accepted-expected if expected else set()
        mismatch=[v['cis'] for v in scans if v['cis'] in task['expected_payloads'] and (
            sha256(bytes(v['payload'])).hexdigest()!=task['expected_payloads'][v['cis']]['sha256']
            or ('line_index' in task['expected_payloads'][v['cis']] and v['line_index']!=task['expected_payloads'][v['cis']]['line_index']))]
        result={'ok':not(differences or missing or extra or mismatch),'checked_at':datetime.now(timezone.utc).isoformat(),
            'external_verified':False,'expectation_level':checks.level(task),'differences':differences[:50],
            'difference_count':len(differences),'missing_count':len(missing),'extra_count':len(extra),'payload_mismatch_count':len(mismatch),
            'message':'Есть расхождения в составе или количестве'}
        if result['ok'] and config and result['expectation_level'] not in ('full','not_required'):
            result.update(ok=False,message='Нет полного ожидаемого КМ для сверки криптохвоста. Логисту нужно передать задание заново с полными КМ.')
        if result['ok'] and config and any(v.get('marked',True) for v in task['lines']):
            try:
                verified=verify_codes(current,task,scans,config,public['rules']['warehouse.check.timeout_seconds'])
                for scan in scans:current.connection.execute(text('UPDATE warehouse_scans SET verification=CAST(:value AS jsonb) WHERE id=:id'),{'id':scan['id'],'value':json.dumps(verified[scan['cis']])})
                result['external_verified']=True
            except HTTPException as error:result.update(ok=False,message=error.detail if isinstance(error.detail,str) else 'Не подтверждены сведения о КМ')
            except RemoteError as error:result.update(ok=False,message=str(error))
        if result['ok']:
            result['message']='Состав и сведения ЧЗ проверены' if result['external_verified'] else 'Локальная сверка пройдена; сведения ЧЗ не проверялись'
        event(current,task,'checked',{'request_id':str(request_id),'request_sha256':request_hash,'result':result,'policy':public})
        current.connection.execute(text('''UPDATE warehouse_tasks SET check_result=CAST(:result AS jsonb),check_digest=:digest,
            check_policy_sha256=:policy,verified_at=CASE WHEN :verified THEN now() ELSE NULL END WHERE id=:id'''),
            {'id':task['id'],'result':json.dumps(result),'digest':checks.scan_digest(task,scans),'policy':checks.digest(public),'verified':result['external_verified'] and result['ok']})
        return result

    @app.post('/api/warehouse/tasks/{id}/check')
    def check_task(id:UUID,body:Check,current=dep):
        task=get(current,id,True);require(current,task,'check')
        current.connection.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'),{'key':'warehouse-check:'+str(body.request_id)})
        fingerprint=checks.digest({'task':id,'revision':body.revision})
        previous=current.connection.execute(text("SELECT task_id,detail FROM warehouse_events WHERE action='checked' AND detail->>'request_id'=:request"),{'request':str(body.request_id)}).mappings().first()
        if previous:
            if previous['task_id']!=id or previous['detail']['request_sha256']!=fingerprint:raise HTTPException(409,'Запрос проверки уже использован')
            return {**previous['detail']['result'],'replayed':True}
        writable(current,task)
        if body.revision!=task['revision']:raise HTTPException(409,'Задание изменено. Обновите данные.')
        config,public=check_context(current,task)
        return run_check(current,task,body.request_id,fingerprint,active_scans(current,task),config,public)

    @app.get('/api/logistics/warehouse-options')
    def options(organization_id:UUID, current=dep):
        current.require('logistics.warehouse.transfer', organization_id=organization_id, owner_id=current.id)
        sources=current.connection.execute(text("SELECT id,owner_id,number,supplier,document_date,revision FROM bm_documents d WHERE organization_id=:org AND state='prepared' AND NOT EXISTS(SELECT 1 FROM business_items i WHERE i.source_id=d.id AND i.kind='marking' AND i.state='completed') AND NOT EXISTS(SELECT 1 FROM business_items p WHERE p.source_id=d.id AND p.kind='marking' AND p.state<>'archived' AND COALESCE(p.body->'warehouse_composition'->>'state','')<>'ready') ORDER BY updated_at DESC LIMIT 200"), {'org':organization_id}).mappings().all()
        people=current.connection.execute(text("""SELECT u.id,u.display_name,g.name AS group_name FROM users u JOIN groups g ON g.slug=u.group_slug
            JOIN user_organizations o ON o.user_id=u.id WHERE o.organization_id=:org AND u.enabled
            AND u.group_slug IN ('warehouse','tsd','specialist','administrator') ORDER BY u.display_name"""), {'org':organization_id}).mappings().all()
        visible=[]
        for r in sources:
            assigned=current.connection.execute(text('SELECT EXISTS(SELECT 1 FROM operation_assignments WHERE target_id=:id AND user_id=:user)'),{'id':r['id'],'user':current.id}).scalar_one()
            try: current.require('logistics.warehouse.transfer',organization_id=organization_id,owner_id=r['owner_id'],assigned=assigned)
            except HTTPException as error:
                if error.status_code!=403: raise
                continue
            visible.append({k:v for k,v in r.items() if k!='owner_id'})
        return {'sources':visible, 'assignees':[dict(r) for r in people]}

    @app.post('/api/logistics/warehouse-transfer')
    def transfer(body:Transfer, current=dep):
        c=current.connection
        source=c.execute(text('SELECT * FROM bm_documents WHERE id=:id FOR UPDATE'), {'id':body.source_id}).mappings().first()
        if not source: raise HTTPException(404, 'Накладная не найдена')
        assigned=c.execute(text('SELECT EXISTS(SELECT 1 FROM operation_assignments WHERE target_id=:id AND user_id=:user)'),{'id':source['id'],'user':current.id}).scalar_one()
        current.require('logistics.warehouse.transfer', organization_id=source['organization_id'], owner_id=source['owner_id'],assigned=assigned)
        existing=c.execute(text('SELECT * FROM warehouse_tasks WHERE id=:id'), {'id':body.request_id}).mappings().first()
        assignees=sorted(set(map(str,body.assignees)))
        transfer_hash=checks.digest({**body.model_dump(mode='json'),'assignees':assignees,'expected_codes':sorted(body.expected_codes)})
        if existing:
            if existing['transfer_request_sha256'] and existing['transfer_request_sha256']!=transfer_hash:raise HTTPException(409,'Повтор запроса отличается от сохранённого')
            assigned=sorted(map(str,c.execute(text('SELECT user_id FROM operation_assignments WHERE target_id=:id'), {'id':body.request_id}).scalars()))
            if existing['source_id']!=body.source_id or existing['kind']!=body.kind or existing['owner_id']!=current.id or existing['source_revision']!=body.source_revision or assigned!=assignees:
                raise HTTPException(409, 'Повтор запроса отличается от сохранённого')
            return {'id':existing['id'], 'replayed':True}
        if c.execute(text('SELECT EXISTS(SELECT 1 FROM operation_targets WHERE id=:id)'),{'id':body.request_id}).scalar_one(): raise HTTPException(409,'Идентификатор задания уже занят')
        process=c.execute(text("SELECT body FROM business_items WHERE source_id=:id AND kind='marking' AND state<>'archived'"),{'id':source['id']}).scalar_one_or_none()
        if process is not None and process.get('warehouse_composition',{}).get('state')!='ready':raise HTTPException(409,'Сначала верните документ в БМ и получите полный Excel поставки')
        expected=[];evidence={};seen=set()
        if body.expected_codes:
            import re
            for raw in body.expected_codes:
                value=raw.strip()
                if re.fullmatch(r'01[0-9]{14}21[!-~]{20}',value):
                    cis=value
                    if not gs1.valid_gtin(cis[2:16]):raise HTTPException(422,'Неверный GTIN ожидаемого КМ')
                else:
                    try:cis,fact=checks.payload_evidence(value)
                    except (ValueError,UnicodeError):raise HTTPException(422,'Некорректный полный КМ в ожидаемом составе') from None
                    evidence[cis]=fact
                if cis in seen:raise HTTPException(422,'Ожидаемый состав содержит повтор КМ')
                seen.add(cis);expected.append(cis)
        else:
            from .business.replacements import effective
            original=list(c.execute(text("SELECT c.id FROM business_codes c JOIN business_items i ON i.id=c.item_id WHERE i.source_id=:id AND i.kind='marking' AND i.state<>'archived'"),{'id':source['id']}).scalars())
            for value in effective(c,original).values():
                if value['state']!='applied':continue
                cis,fact=checks.payload_evidence(value['payload'],value['line_index']);expected.append(cis);evidence[cis]=fact
        marked=any(r.get('marked',True) for r in source['lines'])
        if marked and live_policy(current,{'organization_id':source['organization_id']}) and (not expected or any(cis not in evidence for cis in expected)):
            raise HTTPException(409,'Для проверки ЧЗ передайте полные ожидаемые КМ вместе с криптохвостами')
        if expected:
            from collections import Counter
            plan=Counter()
            for line in source['lines']:
                if line.get('marked',True):plan[line['gtin']]+=line['labels']
            if Counter(cis[2:16] for cis in expected)!=plan:raise HTTPException(422,'Ожидаемые КМ по GTIN не совпадают с полным составом накладной')
            if all(cis in evidence for cis in expected):
                packs=Counter((cis[2:16],evidence[cis]['quantity']) for cis in expected)
                planned=Counter()
                for line in source['lines']:
                    if line.get('marked',True):planned[(line['gtin'],line['quantity'])]+=line['labels']
                if packs!=planned:raise HTTPException(422,'Фасовка ожидаемых КМ не совпадает с планом накладной')
        from .business.logistics import is_completed
        if is_completed(c,source['id']):raise HTTPException(409,'Накладная завершена логистом. Сначала верните её в работу.')
        if source['state']!='prepared' or source['revision']!=body.source_revision: raise HTTPException(409, 'Обновите список: нужен актуальный проверенный состав')
        if c.execute(text("SELECT EXISTS(SELECT 1 FROM warehouse_tasks WHERE source_id=:id AND kind=:kind AND state<>'cancelled')"), {'id':body.source_id,'kind':body.kind}).scalar_one(): raise HTTPException(409, 'Задание этого типа уже существует')
        people=c.execute(text("""SELECT u.id,u.group_slug FROM users u JOIN user_organizations o ON o.user_id=u.id
            WHERE u.enabled AND o.organization_id=:org AND u.id=ANY(CAST(:ids AS uuid[]))
            AND u.group_slug IN ('warehouse','tsd','specialist','administrator')"""), {'org':source['organization_id'], 'ids':assignees}).mappings().all()
        if {str(p['id']) for p in people}!=set(assignees): raise HTTPException(422, 'Проверьте сотрудников и их доступ к организации')
        for person in people:
            recipient=auth.Actor(c,dict(person),'',None)
            try: recipient.require('warehouse.'+body.kind+'.scan',organization_id=source['organization_id'],owner_id=current.id,assigned=True)
            except HTTPException: raise HTTPException(422,'У выбранного сотрудника нет права сканировать задания этого типа') from None
        exclusive_codes(current,{'organization_id':source['organization_id'],'source_id':source['id']},expected)
        c.execute(text('INSERT INTO operation_targets(id,organization_id,owner_id,revision) VALUES (:id,:org,:user,1)'), {'id':body.request_id,'org':source['organization_id'],'user':current.id})
        c.execute(text('''INSERT INTO warehouse_tasks(id,organization_id,owner_id,source_id,source_revision,kind,number,supplier,document_date,lines)
            VALUES (:id,:org,:user,:source,:revision,:kind,:number,:supplier,:date,CAST(:lines AS jsonb))'''), {'id':body.request_id,'org':source['organization_id'],'user':current.id,'source':source['id'],'revision':source['revision'],'kind':body.kind,'number':source['number'],'supplier':source['supplier'],'date':source['document_date'],'lines':json.dumps(source['lines'])})
        c.execute(text('UPDATE warehouse_tasks SET expected_codes=CAST(:codes AS jsonb),expected_payloads=CAST(:evidence AS jsonb),transfer_request_sha256=:hash WHERE id=:id'),{'id':body.request_id,'codes':json.dumps(expected),'evidence':json.dumps(evidence),'hash':transfer_hash})
        for person in people: c.execute(text('INSERT INTO operation_assignments(target_id,user_id) VALUES (:id,:user)'), {'id':body.request_id,'user':person['id']})
        task=get(current,body.request_id);event(current,task,'transferred',{'assignees':assignees})
        return {'id':body.request_id}

    @app.get('/api/warehouse/tasks')
    def listing(organization_id:UUID, kind:str=Query('receipt',pattern='^(receipt|shipment)$'), logistics:bool=False, closed:bool=False, archived:bool=False, q:str=Query('',max_length=120), before_id:UUID|None=None, current=dep):
        if organization_id not in current.organizations(): raise HTTPException(403, 'Нет доступа к организации')
        permission='logistics.warehouse.result.view' if logistics else 'warehouse.'+kind+'.view'
        grant=current.grants().get(permission)
        if not grant: raise HTTPException(403, 'Нет доступа к заданиям')
        if archived:archive_admin(current,'documents.archive.view',organization_id=organization_id,owner_id=current.id)
        rows=current.connection.execute(text('''SELECT t.id,t.organization_id,t.owner_id,t.kind,t.source_type,t.number,t.supplier,t.document_date,t.state,t.revision,t.archived_at,
            jsonb_array_length(t.lines) AS line_count,
            (SELECT count(*) FROM warehouse_scans s WHERE s.task_id=t.id AND s.removed_at IS NULL) AS scanned,
            (SELECT sum((l->>'labels')::bigint) FROM jsonb_array_elements(t.lines) l) AS expected
            FROM warehouse_tasks t WHERE t.organization_id=:org AND t.kind=:kind
            AND (t.archived_at IS NOT NULL)=:archived AND (:closed OR :archived OR t.state='open') AND (CAST(:before AS uuid) IS NULL OR (t.created_at,t.id)<(SELECT created_at,id FROM warehouse_tasks WHERE id=CAST(:before AS uuid) AND organization_id=:org))
            AND (:grant='all' OR (:grant='own' AND t.owner_id=:user) OR (:grant='assigned' AND EXISTS(SELECT 1 FROM operation_assignments a WHERE a.target_id=t.id AND a.user_id=:user)))
            AND (t.number ILIKE :q ESCAPE '!' OR t.supplier ILIKE :q ESCAPE '!') ORDER BY t.created_at DESC,t.id DESC LIMIT 26'''), {'org':organization_id,'kind':kind,'closed':closed,'archived':archived,'grant':grant,'user':current.id,'before':str(before_id) if before_id else None,'q':'%'+q.replace('!','!!').replace('%','!%').replace('_','!_')+'%'}).mappings().all()
        return {'tasks':[{k:v for k,v in r.items() if k not in ('organization_id','owner_id','kind')} for r in rows[:25] if permitted(current,r,logistics=logistics)], 'next_cursor':str(rows[24]['id']) if len(rows)>25 else None}

    @app.get('/api/warehouse/tasks/{id}')
    def detail(id:UUID, logistics:bool=False, sync_token:str=Query('',max_length=64,pattern='^[a-f0-9]*$'), current=dep):
        task=get(current,id);require(current,task,logistics=logistics)
        source=source_status(current,task)
        # Conditional reads still authorize every request. The token covers more
        # than scans: source/assignment/permission/policy changes and check expiry.
        config,public=check_context(current,task)
        assignees=current.connection.execute(text('SELECT u.id,u.display_name FROM operation_assignments a JOIN users u ON u.id=a.user_id WHERE a.target_id=:id ORDER BY u.display_name,u.id'),{'id':id}).mappings().all()
        grants=current.grants();permissions={};ctx=context(current,task)
        actions=[f'warehouse.{task["kind"]}.{action}' for action in ('scan','scan.delete','check','complete','export')]
        actions+=['warehouse.documents.reopen','incidents.create','documents.archive','documents.restore','logistics.warehouse.refresh']
        for permission in actions:
            try:current.require(permission,**ctx)
            except HTTPException as error:
                if error.status_code!=403:raise
            else:permissions[permission]=grants[permission]
        administrative=current.user['group_slug']=='administrator' or auth.group_has_admin(current.connection,current.user['group_slug'])
        result=task['check_result']
        expired=bool(result and (datetime.now(timezone.utc)-datetime.fromisoformat(result['checked_at'])).total_seconds()>public['rules']['warehouse.external_fact_max_age_seconds'])
        token=checks.digest({'revision':task['revision'],'archive':task['archived_at'],'source':dict(source),'policy':public,
            'expired':expired,'permissions':permissions,'administrative':administrative,'assignees':[dict(a) for a in assignees]})
        if sync_token==token:return {'unchanged':True,'sync_token':token}
        scans=current.connection.execute(text('''SELECT s.id,s.line_index,s.gtin,s.quantity,substr(s.cis,19,20) AS serial,u.display_name,s.created_at
            FROM warehouse_scans s JOIN users u ON u.id=s.actor_id WHERE s.task_id=:id AND s.removed_at IS NULL ORDER BY s.created_at DESC,s.id DESC LIMIT 100'''), {'id':id}).mappings().all()
        events=current.connection.execute(text('''SELECT e.action,e.created_at,e.detail->>'reason' AS reason,u.display_name FROM warehouse_events e JOIN users u ON u.id=e.actor_id WHERE e.task_id=:id ORDER BY e.id DESC LIMIT 30'''), {'id':id}).mappings().all()
        import_source=current.connection.execute(text('SELECT filename,sha256,created_at FROM warehouse_imports WHERE id=:id'),{'id':task['source_id']}).mappings().first() if task['source_type']=='excel' else None
        source_download=False
        if import_source:
            try:
                current.require('warehouse.import.preview',**context(current,task));source_download=True
            except HTTPException as error:
                if error.status_code!=403:raise
        is_fresh=source['state']!='archived' and source['revision']==task['source_revision'] and fresh(task,active_scans(current,task),public)
        return {'sync_token':token,'permissions':permissions,'administrative_access':administrative,'source_download':source_download,'import_source':dict(import_source) if import_source else None,'task':{k:v for k,v in task.items() if k not in ('expected_payloads','transfer_request_sha256','check_digest','check_policy_sha256')}, 'assignees':[a['display_name'] for a in assignees], 'lines':summary(current,task),'scans':[dict(r) for r in scans],'events':[dict(r) for r in events], 'paused':source['state']=='archived' or source['revision']!=task['source_revision'], 'external_verified':bool(task['verified_at']),'live_checks':config is not None,'check_fresh':is_fresh,'expectation_level':checks.level(task),'expected_codes_count':len(task['expected_codes'])}

    @app.post('/api/warehouse/tasks/{id}/scans')
    def scan(id:UUID, body:Scan, current=dep):
        task=get(current,id,True);require(current,task,'scan');c=current.connection
        try: parsed=gs1.parse(body.raw.encode('ascii'),scanner=True)
        except (ValueError,UnicodeError) as error: raise HTTPException(422, str(error) if isinstance(error,ValueError) else 'Переключите сканер на английскую раскладку') from None
        quantity=parsed.quantity or 1
        existing=c.execute(text('SELECT * FROM warehouse_scans WHERE id=:id'), {'id':body.request_id}).mappings().first()
        if existing:
            if existing['task_id']==id and bytes(existing['payload'])==parsed.normalized and existing['removed_at'] is None and (body.line_index is None or body.line_index==existing['line_index']): return {'id':existing['id'],'replayed':True}
            raise HTTPException(409, 'Запрос уже использован. Обновите задание.')
        writable(current,task)
        if task['kind']=='shipment':
            from .incidents import enforce
            enforce(c,task['organization_id'],'shipment',cises=[parsed.cis],targets=[task['id'],task['source_id']])
        duplicate=c.execute(text('SELECT id FROM warehouse_scans WHERE task_id=:id AND cis=:cis AND removed_at IS NULL'), {'id':id,'cis':parsed.cis}).scalar_one_or_none()
        if duplicate: raise HTTPException(409, {'code':'duplicate_scan','message':'Этот КМ уже отсканирован. При ошибке удалите ранее принятый скан.','scan_id':str(duplicate)})
        candidates=[i for i,r in enumerate(task['lines']) if r.get('marked',True) and r['gtin']==parsed.gtin and r['quantity']==quantity]
        if not any(r['gtin']==parsed.gtin for r in task['lines']): raise HTTPException(422, 'GTIN отсутствует в задании. Отложите товар и проверьте накладную.')
        if not candidates: raise HTTPException(422, 'Количество в КМ не соответствует упаковкам в задании')
        if task['expected_codes'] and parsed.cis not in set(task['expected_codes']):raise HTTPException(422,'Этот КМ не ожидается в данной поставке')
        expected=task['expected_payloads'].get(parsed.cis)
        if expected and sha256(parsed.normalized).hexdigest()!=expected['sha256']:raise HTTPException(422,'Полный КМ или криптохвост не совпадает с ожидаемым')
        if expected and 'line_index' in expected:
            if expected['line_index'] not in candidates or (body.line_index is not None and body.line_index!=expected['line_index']):raise HTTPException(422,'КМ закреплён за другой строкой накладной')
            index=expected['line_index']
        elif body.line_index is None:
            if len(candidates)!=1: raise HTTPException(409, 'Для этого GTIN несколько PN. Выберите строку товара.')
            index=candidates[0]
        elif body.line_index not in candidates: raise HTTPException(422, 'КМ не соответствует выбранной строке PN/GTIN/количеству')
        else: index=body.line_index
        line=summary(current,task)[index]
        if line['scanned']>=line['labels']: raise HTTPException(409, 'Все КМ этой строки уже отсканированы')
        exclusive_codes(current,task,[parsed.cis],scanning=True)
        c.execute(text('''INSERT INTO warehouse_scans(id,task_id,line_index,cis,gtin,quantity,payload,actor_id)
            VALUES (:id,:task,:line,:cis,:gtin,:quantity,:payload,:user)'''), {'id':body.request_id,'task':id,'line':index,'cis':parsed.cis,'gtin':parsed.gtin,'quantity':quantity,'payload':parsed.normalized,'user':current.id})
        event(current,task,'scanned',{'scan_id':str(body.request_id),'line_index':index})
        return {'id':body.request_id,'pn':line['pn'],'quantity':quantity}

    @app.delete('/api/warehouse/tasks/{id}/scans/{scan_id}')
    def remove(id:UUID, scan_id:UUID, current=dep):
        task=get(current,id,True);require(current,task,'scan.delete');writable(current,task)
        found=current.connection.execute(text('UPDATE warehouse_scans SET removed_at=now(),removed_by=:user WHERE id=:scan AND task_id=:task AND removed_at IS NULL RETURNING id'), {'user':current.id,'scan':scan_id,'task':id}).scalar_one_or_none()
        if found: event(current,task,'scan_removed',{'scan_id':str(scan_id)})
        return {'removed':bool(found)}

    @app.post('/api/warehouse/tasks/{id}/archive')
    def archive_task(id:UUID,body:Transition,current=dep):
        task=get(current,id,True);require(current,task)
        archive_admin(current,'documents.archive',**context(current,task))
        if body.confirmation!=task['number']:raise HTTPException(422,'Введите точный номер накладной')
        if task['archived_at']:return {'archived':True}
        if body.revision!=task['revision']:raise HTTPException(409,'Задание изменено')
        if task['state']=='open':
            transition(id,'close',body,current)
            task=get(current,id,True)
        if task['state']!='closed' or any(r['scanned']!=r['labels'] or r['units']!=r['expected_units'] for r in summary(current,task)):raise HTTPException(409,'Архив доступен только после заполнения без расхождений')
        from .incidents import enforce
        accepted=[v['cis'] for v in active_scans(current,task)]
        enforce(current.connection,task['organization_id'],task['kind'],cises=accepted+task['expected_codes'],targets=[task['id'],task['source_id']])
        current.connection.execute(text('UPDATE warehouse_tasks SET archived_at=now(),archived_by=:user WHERE id=:id'),{'id':id,'user':current.id})
        event(current,task,'archived',{'reason':body.reason.strip()})
        return {'archived':True}

    @app.post('/api/warehouse/tasks/{id}/unarchive')
    def unarchive_task(id:UUID,body:Transition,current=dep):
        task=get(current,id,True);require(current,task)
        archive_admin(current,'documents.restore',**context(current,task))
        if body.confirmation!=task['number']:raise HTTPException(422,'Введите точный номер накладной')
        if not task['archived_at']:return {'archived':False}
        if body.revision!=task['revision']:raise HTTPException(409,'Задание изменено')
        current.connection.execute(text('UPDATE warehouse_tasks SET archived_at=NULL,archived_by=NULL WHERE id=:id'),{'id':id})
        event(current,task,'unarchived',{'reason':body.reason.strip()})
        return {'archived':False}

    @app.post('/api/warehouse/tasks/{id}/{action}')
    def transition(id:UUID, action:str, body:Transition, current=dep):
        if action not in ('close','reopen','cancel'): raise HTTPException(404, 'Действие не найдено')
        task=get(current,id,True)
        if task['archived_at']:raise HTTPException(409,'Сначала верните задание из архива')
        if action=='cancel':
            require(current,task,logistics=True)
            current.require('logistics.warehouse.refresh',**context(current,task))
        elif action=='reopen':
            require(current,task)
            current.require('warehouse.documents.reopen',**context(current,task))
        else: require(current,task,'complete')
        if body.revision!=task['revision']: raise HTTPException(409, 'Задание изменено. Обновите данные.')
        if body.confirmation!=task['number']: raise HTTPException(422, 'Введите точный номер накладной')
        from .business.logistics import is_completed
        if is_completed(current.connection,task['source_id']):raise HTTPException(409,'Накладная завершена логистом. Сначала верните её в работу.')
        if action=='close':
            writable(current,task)
            if any(r['scanned']!=r['labels'] or r['units']!=r['expected_units'] for r in summary(current,task)): raise HTTPException(409, 'Есть расхождения. Отсканируйте недостающие КМ.')
            from .incidents import enforce
            accepted=current.connection.execute(text('SELECT cis FROM warehouse_scans WHERE task_id=:id AND removed_at IS NULL'),{'id':id}).scalars().all()
            enforce(current.connection,task['organization_id'],task['kind'],cises=list(accepted)+task['expected_codes'],targets=[task['id'],task['source_id']])
            exclusive_codes(current,task,list(accepted)+task['expected_codes'])
            config,public=check_context(current,task);scans=active_scans(current,task)
            if not fresh(task,scans,public):
                if config and any(v.get('marked',True) for v in task['lines']):raise HTTPException(409,'Сначала нажмите «Проверить»: нужна актуальная пакетная сверка ЧЗ')
                require(current,task,'check')
                result=run_check(current,task,uuid4(),checks.digest({'task':id,'revision':task['revision']}),scans,config,public)
                if not result['ok']:raise HTTPException(409,result['message'])
            state='closed'
        else:
            if len(body.reason.strip())<8: raise HTTPException(422, 'Укажите причину от 8 символов')
            if action=='reopen' and task['state']!='closed': raise HTTPException(409, 'Вернуть можно только закрытое задание')
            if action=='cancel' and task['state']!='open': raise HTTPException(409, 'Отменить можно только открытое задание')
            if action=='reopen':
                accepted=current.connection.execute(text('SELECT cis FROM warehouse_scans WHERE task_id=:id AND removed_at IS NULL'),{'id':id}).scalars().all()
                exclusive_codes(current,task,task['expected_codes']+list(accepted))
            state='open' if action=='reopen' else 'cancelled'
            current.connection.execute(text('UPDATE warehouse_tasks SET verified_at=NULL WHERE id=:id'),{'id':id})
        current.connection.execute(text("UPDATE warehouse_tasks SET state=:state,closed_at=CASE WHEN :state='closed' THEN now() ELSE NULL END WHERE id=:id"), {'id':id,'state':state})
        event(current,task,action,{'reason':body.reason.strip()})
        return {'state':state}

    @app.get('/api/warehouse/tasks/{id}/export')
    def export(id:UUID, current=dep):
        task=get(current,id);require(current,task,'export')
        if task['kind']!='shipment': raise HTTPException(403, 'Экспорт доступен для отгрузки')
        if task['state']!='closed': raise HTTPException(409,'Итоговая выгрузка доступна после полной сверки и закрытия отгрузки')
        from .incidents import enforce
        cises=current.connection.execute(text('SELECT cis FROM warehouse_scans WHERE task_id=:id AND removed_at IS NULL'),{'id':id}).scalars().all()
        enforce(current.connection,task['organization_id'],'shipment',cises=cises,targets=[task['id'],task['source_id']])
        out=BytesIO();book=xlsxwriter.Workbook(out,{'in_memory':True,'strings_to_formulas':False,'strings_to_urls':False});sheet=book.add_worksheet('Отгрузка')
        sheet.write_row(0,0,['PN','GTIN','Количество','КМ (GS = <GS>)','Сотрудник','Дата сканирования'])
        rows=current.connection.execute(text('SELECT s.line_index,s.gtin,s.quantity,s.payload,u.display_name,s.created_at FROM warehouse_scans s JOIN users u ON u.id=s.actor_id WHERE s.task_id=:id AND s.removed_at IS NULL ORDER BY s.line_index,s.created_at'), {'id':id}).mappings()
        for i,r in enumerate(rows,1): sheet.write_row(i,0,[task['lines'][r['line_index']]['pn'],r['gtin'],r['quantity'],bytes(r['payload']).decode('ascii').replace('\x1d','<GS>'),r['display_name'],r['created_at'].isoformat()])
        info=book.add_worksheet('Немаркированные строки')
        info.write_row(0,0,['PN','Количество по накладной','Примечание'])
        for i,line in enumerate((r for r in task['lines'] if not r.get('marked',True)),1):info.write_row(i,0,[line['pn'],line['quantity'],'Информационно; не является результатом сверки КМ'])
        info.freeze_panes(1,0);info.set_column(0,0,30);info.set_column(1,1,25);info.set_column(2,2,65)
        sheet.freeze_panes(1,0);sheet.set_column(0,1,24);sheet.set_column(2,2,14);sheet.set_column(3,3,70);sheet.set_column(4,5,28);book.close()
        auth.audit(current.connection,current.id,'warehouse.export',id,{'kind':task['kind']})
        return Response(out.getvalue(),media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':'attachment; filename="shipment.xlsx"'})

    from .warehouse_imports import install as install_imports
    install_imports(app,actor,require,get,event,exclusive_codes,context)
