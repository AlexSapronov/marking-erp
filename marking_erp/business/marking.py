"""Emission, application, supplier ATK and division using durable live adapters."""
from datetime import date
from hashlib import sha256
from io import BytesIO
import json
from uuid import UUID,uuid4
import zipfile
from fastapi import Depends,HTTPException,Query,Response
from pydantic import Field,StrictInt,StrictBool
from sqlalchemy import text
from .. import gs1,auth
from ..auth_routes import Body
from ..labels import render_label,render_labels
from . import store as s
from . import registry as registry_bridge
from .connections import load
from .live import Live
from .nk import workbook,Client
from .split_eligibility import check as check_split_eligibility
from .suppliers import canonical,expression

class Reallocate(Body):
    revision:StrictInt=Field(ge=1)
    lines:list[dict]=Field(min_length=1,max_length=1000)
    confirmed:StrictBool=False
    reason:str=Field(min_length=8,max_length=1000)

class Start(Body):
    confirmed:StrictBool=False
    source_id:UUID
    revision:StrictInt=Field(ge=1)
    request_id:UUID

class Handoff(Body):
    request_id:UUID
    revision:StrictInt=Field(ge=1)
    file_id:UUID
    confirmed:StrictBool=False

class Step(Body):
    revision:StrictInt=Field(ge=1)
    confirmed:StrictBool=False
    production_date:date|None=None
    reason:str=Field(default='',max_length=1000)

class Atk(Body):
    organization_id:UUID
    ids:list[UUID]=Field(min_length=1,max_length=1000)
    request_id:UUID

class Split(Body):
    organization_id:UUID
    request_id:UUID
    raw:str=Field(min_length=1,max_length=1024)
    quantity:StrictInt=Field(ge=1,le=99999998)
    pn:str=Field(min_length=1,max_length=120)
    tnved:str=Field(pattern='^[0-9]{10}$')

class Verify(Body):
    organization_id:UUID
    raw:str=Field(min_length=1,max_length=1024)

class Transform(Body):
    revision:StrictInt=Field(ge=1)
    kind:str=Field(pattern='^(ADDING|REMOVING)$')
    codes:list[UUID]=Field(min_length=1,max_length=10000)
    reason:str=Field(min_length=8,max_length=1000)


def rows(c,id):return [dict(r) for r in c.execute(text('SELECT * FROM business_codes WHERE item_id=:id ORDER BY line_index,id'),{'id':id}).mappings()]

def organization(c,id):return c.execute(text('SELECT inn FROM organizations WHERE id=:id'),{'id':id}).scalar_one()

def order_payload(gtin,count,config,id,split=False):
    return {'productGroup':'radio','products':[{'gtin':gtin,'quantity':count,'serialNumberType':'OPERATOR','templateId':config['template_id'],'cisType':'UNIT'}],
        'attributes':{'releaseMethodType':'REAPPLY' if split else 'IMPORT','paymentType':1,'productionOrderId':str(id)}}

def labels(current,row,settings):
    codes=rows(current.connection,row['id'])
    if not codes:raise HTTPException(409,'Коды ещё не получены')
    if any(c['state'] not in ('emitted','reserved','applied') for c in codes):raise HTTPException(409,'Есть неподтверждённые КМ')
    out=BytesIO();manifest=[]
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as archive:
        for offset in range(0,len(codes),500):
            part=codes[offset:offset+500]
            pdf=render_labels([{'raw':bytes(v['payload']),'pn':v['pn'],'quantity':v['quantity']} for v in part])
            filename='labels-58x40.pdf' if len(codes)<=500 else f'labels-{offset+1:05d}-{offset+len(part):05d}.pdf'
            file=s.artifact(current,row,'labels_pdf',filename,pdf,'application/pdf',{'codes':[v['cis'] for v in part]})
            manifest.append({'file':filename,'artifact_id':str(file),'sha256':sha256(pdf).hexdigest(),'count':len(part)})
            if len(codes)>500:archive.writestr(filename,pdf)
            for i,code in enumerate(part,offset+1):
                data=render_label(bytes(code['payload']),code['pn'],code['quantity']);registry_bridge.label(current,row,code,settings,data)
                if len(codes)<=500:archive.writestr(f'{i:05d}.pdf',data)
        archive.writestr('manifest.json',json.dumps({'count':len(codes),'parts':manifest},ensure_ascii=False))
    return s.artifact(current,row,'labels','labels-58x40.zip',out.getvalue(),'application/zip',{'parts':manifest,'codes':[{'cis':v['cis'],'pn':v['pn'],'quantity':v['quantity'],'sha256':sha256(bytes(v['payload'])).hexdigest()} for v in codes]})

def validate_source(current,row):
    if row['source_id'] and row['kind']=='marking':
        source=current.connection.execute(text('SELECT state,revision FROM bm_documents WHERE id=:id'),{'id':row['source_id']}).mappings().one()
        if source['state']=='archived' or source['revision']!=row['body']['source_revision']:raise HTTPException(409,'Исходная накладная остановлена или изменена')

def validate_atk_sources(c,row):
    if row['kind']!='atk':return
    ids=row['body']['documents']
    values=c.execute(text('SELECT i.state,i.body,d.state AS source_state,d.revision FROM business_items i JOIN bm_documents d ON d.id=i.source_id WHERE i.id=ANY(CAST(:ids AS uuid[]))'),{'ids':ids}).mappings().all()
    if len(values)!=len(ids) or any(v['state']=='archived' or v['source_state']=='archived' or v['body']['source_revision']!=v['revision'] for v in values):raise HTTPException(409,'Исходная накладная АТК остановлена или изменена')

def apply(current,row,op,link,payload,result,live,settings):
    c=current.connection;step=op['kind'];body=dict(row['body'])
    if step.endswith('.order'):
        # Fetch every GTIN separately. Each consuming request has a unique durable command.
        for product in payload['products']:
            s.queue(current,row,'remark.fetch' if row['kind']=='remark' else 'split.fetch' if row['kind']=='split' else 'bm.fetch',{'orderId':op['external_id'],'gtin':product['gtin'],'quantity':product['quantity']},{'gtin':product['gtin'],'expected':product['quantity'],'order_id':op['external_id']})
        s.save(current,row,'order_accepted',state='receiving')
    elif step.endswith('.fetch'):
        gtin=link['metadata']['gtin'];expected=link['metadata']['expected'];raws=result['codes']
        if len(raws)>expected:raise ValueError('СУЗ выдал больше КМ, чем запрошено')
        parsed=[gs1.parse(raw.encode()) for raw in raws]
        if len({p.cis for p in parsed})!=len(parsed) or any(p.gtin!=gtin or p.quantity is not None for p in parsed):raise ValueError('СУЗ вернул другой GTIN, повтор КМ или неожиданный AI30')
        allocations=[]
        if row['kind'] in ('split','remark'):allocations=[(body.get('line_index',0),body['pn'],body['tnved'],body['quantity'])]
        else:
            for index,line in enumerate(body['lines']):
                if line['gtin']!=gtin:continue
                used=c.execute(text('SELECT count(*) FROM business_codes WHERE item_id=:id AND line_index=:line'),{'id':row['id'],'line':index}).scalar_one()
                allocations.extend([(index,line['pn'],line['tnved'],line['quantity'])]*max(0,line['labels']-used))
        if len(allocations)<len(parsed):raise ValueError('КМ не помещаются в состав')
        for value,allocation in zip(parsed,allocations):
            index,pn,tnved,quantity=allocation
            full=gs1.with_quantity(value.base,quantity)
            code_id=uuid4()
            c.execute(text('''INSERT INTO business_codes(id,organization_id,cis,gtin,base,payload,item_id,line_index,quantity,state,tnved,pn,evidence_ref)
                VALUES (:id,:org,:cis,:gtin,:base,:payload,:item,:line,:qty,'emitted',:tnved,:pn,:ref)'''),{'id':code_id,'org':row['organization_id'],'cis':value.cis,'gtin':value.gtin,'base':value.base,'payload':full,'item':row['id'],'line':index,'qty':quantity,'tnved':tnved,'pn':pn,'ref':'operation:'+str(op['id'])})
            code=dict(c.execute(text('SELECT * FROM business_codes WHERE id=:id'),{'id':code_id}).mappings().one())
            registry_bridge.capture(current,row,code,settings)
        if len(parsed)<expected:
            s.queue(current,row,step,{'orderId':link['metadata']['order_id'],'gtin':gtin,'quantity':expected-len(parsed)},{**link['metadata'],'expected':expected-len(parsed)})
        expected_total=1 if row['kind'] in ('split','remark') else sum(r['labels'] for r in body['lines'])
        if len(rows(c,row['id']))==expected_total:
            file=labels(current,row,settings);s.save(current,row,'codes_received',body={**body,'label_file_id':str(file)},state='labels_ready')
    elif step.endswith('.report'):
        confirmed=[gs1.parse(raw.split('\x1e')[0].encode()).cis for raw in payload['sntins']]
        c.execute(text("UPDATE business_codes SET state='applied' WHERE item_id=:id AND cis=ANY(CAST(:cises AS text[]))"),{'id':row['id'],'cises':confirmed})
        if row['kind']=='split':
            fresh=live.info([body['source_cis']])[body['source_cis']]
            actual=fresh.get('quantityInPack')
            if not isinstance(actual,(int,float,str)) or int(actual)!=body['baseline']-body['quantity']:raise ValueError('Отчёт принят; остаток исходного КМ ещё не подтверждён')
            body['remaining']=int(actual)
        s.save(current,row,'report_confirmed',body=body,state='applied' if all(v['state']=='applied' for v in rows(c,row['id'])) else 'reporting')
    elif step=='bm.atk':
        documents=result['document'][0];actual=result['registry']
        parents={r.get('parent') for r in actual.values()};atk=next(iter(parents)) if len(parents)==1 else None
        import re
        if not isinstance(atk,str) or re.fullmatch(r'[0-9]{20}[0-9A-Za-z]{5}',atk) is None:raise ValueError('ЧЗ не подтвердил единый АТК')
        body['atk']=atk;body['document_id']=op['external_id']
        updated=s.save(current,row,'atk_confirmed',body=body,state='ready')
        atk_export(current,updated)
    elif step=='bm.transform':
        parent=s.item(c,UUID(body['atk_item']),lock=True);parentbody=dict(parent['body']);ids=set(parentbody['codes'])
        if body['kind']=='ADDING':ids.update(body['codes'])
        else:ids.difference_update(body['codes'])
        updated=s.save(current,parent,'atk_transformed',body={**parentbody,'codes':sorted(ids)})
        atk_export(current,updated)
        s.save(current,row,'transformation_confirmed',state='done')


def atk_export(current,row):
    codes=current.connection.execute(text('SELECT * FROM business_codes WHERE organization_id=:org AND id=ANY(CAST(:ids AS uuid[])) ORDER BY pn,cis'),{'org':row['organization_id'],'ids':row['body']['codes']}).mappings().all()
    if len(codes)!=len(set(row['body']['codes'])) or any(v['tnved']!=row['body']['tnved'] for v in codes):raise ValueError('Состав АТК не совпадает с зарегистрированными КМ')
    atk=row['body']['atk']
    data=workbook([[v['pn'],v['gtin'],v['quantity'],v['cis'],atk] for v in codes],['PN','GTIN','Количество','КМ','АТК'])
    file=s.artifact(current,row,'atk','ATK.xlsx',data,'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',{'atk':atk,'codes':row['body']['codes'],'documents':row['body']['documents']})
    return s.save(current,row,'atk_export_saved',body={**row['body'],'file_id':str(file)})


def advance(worker):
    # Business progression is driven by explicit user actions, never implicit report submission.
    return

def install_marking(app,actor,settings):
    from .bm_bulk import install
    install(app,actor)
    dep=Depends(actor,scope='function')
    def label_context(current,row):
        assigned=current.connection.execute(text('''SELECT EXISTS(SELECT 1 FROM warehouse_tasks w JOIN operation_assignments a ON a.target_id=w.id
            WHERE w.source_id=:source AND w.state<>'cancelled' AND a.user_id=:user)'''),{'source':row['source_id'],'user':current.id}).scalar_one()
        return dict(organization_id=row['organization_id'],owner_id=row['owner_id'],assigned=assigned)
    @app.post('/api/check-code')
    def check(body:Verify,current=dep):
        current.require('codes.verify',organization_id=body.organization_id,owner_id=current.id)
        try:code=gs1.parse(body.raw.encode('ascii'),scanner=True)
        except (ValueError,UnicodeError):raise HTTPException(422,'Не удалось прочитать полный КМ') from None
        config=load(current.connection,settings,body.organization_id);live=Live(config,organization(current.connection,body.organization_id));data=live.info([code.cis])[code.cis]
        return {'gtin':code.gtin,'status':data.get('status','Не указан'),'owner':data.get('ownerInn','Не указан'),'own':data.get('ownerInn')==live.inn,'pn':data.get('model',''),'quantity':data.get('quantityInPack')}
    @app.get('/api/reprint-codes')
    def reprints(organization_id:UUID,q:str=Query('',max_length=120),current=dep):
        if organization_id not in current.organizations():raise HTTPException(403,'Нет доступа к организации')
        values=current.connection.execute(text('''SELECT c.id,c.cis,c.pn,c.quantity,c.item_id FROM business_codes c WHERE c.organization_id=:org AND c.item_id IS NOT NULL AND c.state<>'withdrawn'
            AND (c.pn ILIKE :q ESCAPE '!' OR c.gtin LIKE :q ESCAPE '!' OR c.cis LIKE :q ESCAPE '!') ORDER BY c.id DESC LIMIT 100'''),{'org':organization_id,'q':'%'+q.replace('!','!!').replace('%','!%').replace('_','!_')+'%'}).mappings().all()
        out=[]
        for code in values:
            row=s.item(current.connection,code['item_id'])
            try:current.require('labels.print.view',**label_context(current,row))
            except HTTPException:continue
            out.append({k:code[k] for k in ('id','cis','pn','quantity')})
        return {'codes':out}
    @app.get('/api/business')
    def listing(organization_id:UUID,kind:str=Query('marking',pattern='^(marking|atk|transform|split|fts|remark)$'),q:str=Query('',max_length=120),archived:bool=False,before_id:UUID|None=None,current=dep):
        permission=s.VIEW[kind];grant=current.grants().get(permission)
        if not grant or organization_id not in current.organizations():raise HTTPException(403,'Раздел недоступен')
        values=current.connection.execute(text('''SELECT i.id,i.title,i.state,i.revision,i.body,i.updated_at FROM business_items i WHERE organization_id=:org AND kind=:kind AND (state='archived')=:archived
            AND (:grant='all' OR (:grant='own' AND owner_id=:user) OR (:grant='assigned' AND EXISTS(SELECT 1 FROM operation_assignments a WHERE a.target_id=i.id AND a.user_id=:user)))
            AND (CAST(:before AS uuid) IS NULL OR (i.created_at,i.id)<(SELECT created_at,id FROM business_items WHERE id=CAST(:before AS uuid) AND organization_id=:org)) AND title ILIKE :q ESCAPE '!' ORDER BY i.created_at DESC,i.id DESC LIMIT 26'''),{'org':organization_id,'kind':kind,'archived':archived,'grant':grant,'user':current.id,'before':str(before_id) if before_id else None,'q':'%'+q.replace('!','!!').replace('%','!%').replace('_','!_')+'%'}).mappings().all()
        return {'items':[{**r,'body':{k:v for k,v in r['body'].items() if k not in ('source_raw','source_base')}} for r in values[:25]],'next_cursor':str(values[24]['id']) if len(values)>25 else None}
    @app.get('/api/business/{id}')
    def detail(id:UUID,code_after:UUID|None=None,current=dep):
        row=s.get(current,id);codes=current.connection.execute(text('SELECT id,cis,gtin,pn,quantity,tnved,state,line_index FROM business_codes WHERE item_id=:id AND (CAST(:after AS uuid) IS NULL OR id>CAST(:after AS uuid)) ORDER BY id LIMIT 1001'),{'id':id,'after':code_after}).mappings().all()
        ops=current.connection.execute(text('SELECT o.id,o.kind,o.state,o.reason,o.external_id FROM external_operations o JOIN business_links l ON l.operation_id=o.id WHERE l.item_id=:id ORDER BY o.created_at DESC LIMIT 100'),{'id':id}).mappings().all()
        files=current.connection.execute(text('SELECT id,kind,version,filename,created_at,snapshot FROM business_artifacts WHERE item_id=:id ORDER BY created_at DESC'),{'id':id}).mappings().all()
        active=next((f for f in files if str(f['id'])==row['body'].get('label_file_id')),None)
        current_parts={v['artifact_id'] for v in active['snapshot'].get('parts',[])} if active else set()
        files=[{**f,'current':str(f['id'])==row['body'].get('label_file_id') or str(f['id']) in current_parts} if f['kind'].startswith('labels') else dict(f) for f in files]
        public={**row,'body':{k:v for k,v in row['body'].items() if k not in ('source_raw','source_base')}}
        return {'item':public,'codes':[dict(r) for r in codes[:1000]],'code_next':str(codes[999]['id']) if len(codes)>1000 else None,'code_count':current.connection.execute(text('SELECT count(*) FROM business_codes WHERE item_id=:id'),{'id':id}).scalar_one(),'operations':[dict(r) for r in ops],'files':[dict(r) for r in files]}
    @app.put('/api/business/{id}/reallocate')
    def reallocate(id:UUID,body:Reallocate,current=dep):
        from collections import Counter
        from ..document_routes import Line,normalize_lines
        from pydantic import ValidationError
        row=s.get(current,id,'marking',True,'marking.documents.edit');s.version(row,body.revision);s.idle(current.connection,id);validate_source(current,row)
        if any(v.get('marked',True) is False for v in body.lines):raise HTTPException(409,'Немаркируемые строки добавляются после возврата логистом через полный Excel')
        if row['body'].get('bm_policy',{}).get('values',{}).get('bm.quantity.edit_until')=='before_emission':raise HTTPException(409,'Правила организации запрещают изменение количества после начала эмиссии')
        if row['state'] not in ('labels_ready','application_confirmed') or not body.confirmed:raise HTTPException(409,'Перераспределение разрешено до отчёта с подтверждением замены этикеток')
        try:lines,_=normalize_lines([Line(**v).model_dump() for v in body.lines])
        except ValidationError:raise HTTPException(422,'Проверьте строки распределения') from None
        c=current.connection
        if c.execute(text("SELECT EXISTS(SELECT 1 FROM warehouse_tasks WHERE source_id=:id AND state<>'cancelled')"),{'id':row['source_id']}).scalar_one():raise HTTPException(409,'Сначала отмените складские задания с прежним составом')
        codes=rows(c,id);available=Counter(v['gtin'] for v in codes);requested=Counter()
        for line in lines:
            if not line.get('marked',True):continue
            requested[line['gtin']]+=line['labels']
            if not c.execute(text('SELECT 1 FROM product_pns p JOIN products r ON r.id=p.product_id WHERE p.organization_id=:org AND p.pn=:pn AND r.gtin=:gtin AND p.tnved=:tnved LIMIT 1'),{'org':row['organization_id'],'pn':line['pn'],'gtin':line['gtin'],'tnved':line['tnved']}).scalar():raise HTTPException(409,'PN/GTIN/ТН ВЭД не подтверждены в НК: '+line['pn'])
        if available!=requested:raise HTTPException(409,'Количество КМ каждого GTIN должно совпадать с полученным пулом. Новая эмиссия не выполняется.')
        pool={}
        for code in codes:pool.setdefault(code['gtin'],[]).append(code)
        for index,line in enumerate(lines):
            if not line.get('marked',True):continue
            for _ in range(line['labels']):
                code=pool[line['gtin']].pop(0);payload=gs1.with_quantity(bytes(code['base']),line['quantity'])
                c.execute(text('UPDATE business_codes SET line_index=:line,pn=:pn,tnved=:tnved,quantity=:quantity,payload=:payload WHERE id=:id'),{'id':code['id'],'line':index,'pn':line['pn'],'tnved':line['tnved'],'quantity':line['quantity'],'payload':payload})
                registry_bridge.capture(current,row,{**code,'payload':payload},settings,quantity_change=True)
        source=dict(c.execute(text('SELECT * FROM bm_documents WHERE id=:id FOR UPDATE'),{'id':row['source_id']}).mappings().one());source.update(lines=lines,revision=source['revision']+1,state='prepared')
        c.execute(text("UPDATE bm_documents SET lines=CAST(:lines AS jsonb),revision=:revision,updated_at=now() WHERE id=:id"),{'id':source['id'],'lines':json.dumps(lines),'revision':source['revision']})
        c.execute(text('UPDATE operation_targets SET revision=:revision WHERE id=:id'),{'id':source['id'],'revision':source['revision']})
        snapshot={k:source[k] for k in ('id','organization_id','owner_id','number','document_date','supplier','revision','state','lines','policy_snapshot')}
        c.execute(text("INSERT INTO bm_document_versions(document_id,organization_id,revision,actor_id,actor_name,action,reason,snapshot) VALUES (:id,:org,:revision,:user,:name,'edited',:reason,CAST(:snapshot AS jsonb))"),{'id':source['id'],'org':source['organization_id'],'revision':source['revision'],'user':current.id,'name':current.user['display_name'],'reason':body.reason,'snapshot':json.dumps(snapshot,default=str)})
        body_data={**row['body'],'lines':lines,'source_revision':source['revision']};updated=s.save(current,row,'codes_reallocated',body=body_data,state='labels_ready');file=labels(current,updated,settings)
        return s.save(current,updated,'labels_replaced',body={**body_data,'label_file_id':str(file)})
    @app.post('/api/marking/start')
    def start(body:Start,current=dep):
        c=current.connection;source=c.execute(text('SELECT * FROM bm_documents WHERE id=:id FOR UPDATE'),{'id':body.source_id}).mappings().first()
        if not source:raise HTTPException(404,'Накладная не найдена')
        current.require('marking.codes.issue',organization_id=source['organization_id'],owner_id=source['owner_id'])
        if source['revision']!=body.revision or source['state']!='prepared':raise HTTPException(409,'Нужен актуальный проверенный состав')
        existing=c.execute(text("SELECT * FROM business_items WHERE source_id=:source AND kind='marking' AND state<>'archived'"),{'source':body.source_id}).mappings().first()
        if existing:s.require(current,existing);return dict(existing)
        if source['policy_snapshot']['values'].get('bm.issue.extra_confirmation',True) and not body.confirmed:raise HTTPException(422,'Подтвердите заказ недостающих КМ')
        if sum(r['labels'] for r in source['lines'])>10000:raise HTTPException(422,'В одной накладной допускается до 10000 КМ; создайте следующую накладную для превышения')
        marked=[line for line in source['lines'] if line.get('marked',True)]
        if not marked:raise HTTPException(409,'В исходной накладной нет маркируемых строк')
        config=load(c,settings,source['organization_id'])
        if not config.get('writes_enabled'):raise HTTPException(409,'Отправка внешних операций отключена')
        # PN/GTIN must be known from a confirmed catalogue source before paid emission.
        for line in marked:
            if c.execute(text('SELECT EXISTS(SELECT 1 FROM product_pns p JOIN products r ON r.id=p.product_id WHERE p.organization_id=:org AND r.gtin=:gtin AND p.tnved<>:tnved)'),{'org':source['organization_id'],'gtin':line['gtin'],'tnved':line['tnved']}).scalar_one():raise HTTPException(409,'В каталоге есть конфликт ТН ВЭД для GTIN: '+line['gtin'])
            if not c.execute(text('SELECT 1 FROM product_pns p JOIN products r ON r.id=p.product_id WHERE p.organization_id=:org AND p.pn=:pn AND r.gtin=:gtin AND p.tnved=:tnved LIMIT 1'),{'org':source['organization_id'],'pn':line['pn'],'gtin':line['gtin'],'tnved':line['tnved']}).scalar():raise HTTPException(409,'Сначала подтвердите PN/GTIN/ТН ВЭД в НК: '+line['pn'])
        pool=[dict(r) for r in c.execute(text("SELECT * FROM business_codes WHERE organization_id=:org AND state='free' AND gtin=ANY(CAST(:gtins AS text[])) ORDER BY created_at,id FOR UPDATE"),{'org':source['organization_id'],'gtins':list({r['gtin'] for r in marked})}).mappings()]
        if pool:Live(config,organization(c,source['organization_id'])).verify(pool,'EMITTED')
        row=s.create(current,'marking',source['number'],{'bm_policy':source['policy_snapshot'],'labels_handoff_required':True,'source_revision':source['revision'],'supplier':source['supplier'],'lines':source['lines']},source['organization_id'],source['id'],body.request_id,state='ordering',owner=source['owner_id'])
        totals={}
        for line in marked:totals[line['gtin']]=totals.get(line['gtin'],0)+line['labels']
        for index,line in enumerate(source['lines']):
            if not line.get('marked',True):continue
            available=[v for v in pool if v['gtin']==line['gtin']]
            for code in available[:line['labels']]:
                payload=gs1.with_quantity(bytes(code['base']),line['quantity'])
                c.execute(text("UPDATE business_codes SET item_id=:item,line_index=:line,quantity=:qty,pn=:pn,tnved=:tnved,payload=:payload,state='emitted' WHERE id=:id AND state='free'"),{'id':code['id'],'item':row['id'],'line':index,'qty':line['quantity'],'pn':line['pn'],'tnved':line['tnved'],'payload':payload})
                registry_bridge.capture(current,row,{**code,'payload':payload},settings,quantity_change=True)
                pool.remove(code);totals[line['gtin']]-=1
        for gtin,count in totals.items():
            for offset in range(0,count,1000):s.queue(current,row,'bm.order',order_payload(gtin,min(1000,count-offset),config,uuid4()))
        if not any(totals.values()):
            file=labels(current,row,settings);return s.save(current,row,'pool_reserved',body={**row['body'],'label_file_id':str(file)},state='labels_ready')
        return row
    @app.post('/api/business/{id}/release-pool')
    def release(id:UUID,body:Step,current=dep):
        row=s.get(current,id,'marking',True,'marking.codes.reuse.approve')
        if row['revision']!=body.revision or row['state']!='archived' or not body.confirmed:raise HTTPException(409,'Нужен актуальный остановленный процесс и подтверждение возврата КМ')
        s.idle(current.connection,id);codes=rows(current.connection,id)
        if not codes or any(code['state']!='emitted' for code in codes):raise HTTPException(409,'В пул возвращаются только полученные, ещё не нанесённые КМ')
        config=load(current.connection,settings,row['organization_id']);Live(config,organization(current.connection,row['organization_id'])).verify(codes,'EMITTED')
        current.connection.execute(text("UPDATE business_codes SET item_id=NULL,line_index=NULL,state='free' WHERE item_id=:id"),{'id':id})
        return s.save(current,row,'codes_released_to_pool',body={**row['body'],'codes_released':True})
    @app.post('/api/splits')
    def split(body:Split,current=dep):
        current.require('split.operations.create',organization_id=body.organization_id,owner_id=current.id)
        try:code=gs1.parse(body.raw.encode('ascii'),scanner=True)
        except (UnicodeError,ValueError):raise HTTPException(422,'Не удалось прочитать полный КМ') from None
        from ..incidents import enforce
        enforce(current.connection,body.organization_id,'split',cises=[code.cis])
        if code.gtin.startswith('029'):raise HTTPException(422,'REAPPLY для технического GTIN 029 недоступен')
        c=current.connection;existing=c.execute(text('SELECT * FROM business_items WHERE id=:id'),{'id':body.request_id}).mappings().first()
        if existing:
            s.require(current,existing)
            if existing['kind']!='split' or existing['organization_id']!=body.organization_id or existing['body'].get('source_cis')!=code.cis or existing['body'].get('quantity')!=body.quantity:raise HTTPException(409,'Повторный запрос отличается')
            return dict(existing)
        config=load(c,settings,body.organization_id);live=Live(config,organization(c,body.organization_id));info=live.info([code.cis])[code.cis]
        if info.get('status')!='INTRODUCED' or info.get('ownerInn')!=live.inn:raise HTTPException(409,'ЧЗ не подтвердил ваш КМ со статусом «В обороте»')
        baseline=info.get('quantityInPack')
        if baseline is None or not str(baseline).isdigit():raise HTTPException(409,'ЧЗ не подтвердил текущее количество исходного КМ')
        if c.execute(text("SELECT EXISTS(SELECT 1 FROM business_items WHERE organization_id=:org AND ((kind='split' AND body->>'source_cis'=:cis AND state NOT IN ('applied','archived','cancelled')) OR (kind='remark' AND body->>'old_cis'=:cis AND state NOT IN ('done','archived'))))"),{'org':body.organization_id,'cis':code.cis}).scalar_one():raise HTTPException(409,'По этому КМ есть незавершённое разделение')
        if body.quantity>=int(baseline):raise HTTPException(422,'Отделяемое количество должно быть меньше остатка исходного КМ')
        if not c.execute(text('SELECT 1 FROM product_pns p JOIN products r ON r.id=p.product_id WHERE p.organization_id=:org AND r.gtin=:gtin AND p.pn=:pn AND p.tnved=:tnved LIMIT 1'),{'org':body.organization_id,'gtin':code.gtin,'pn':body.pn,'tnved':body.tnved}).scalar():raise HTTPException(409,'PN/GTIN/ТН ВЭД не подтверждены в каталоге')
        eligibility=check_split_eligibility(Client(config),code.gtin)
        row=s.create(current,'split',body.pn,{'catalog_eligibility':eligibility,'source_raw':code.normalized.decode(),'source_base':code.base.decode(),'source_cis':code.cis,'gtin':code.gtin,'baseline':int(baseline),'quantity':body.quantity,'pn':body.pn,'tnved':body.tnved},body.organization_id,id=body.request_id,state='ordering')
        s.queue(current,row,'split.order',order_payload(code.gtin,1,config,row['id'],True));return {'id':row['id'],'state':row['state']}
    @app.post('/api/business/{id}/labels-handoff')
    def handoff(id:UUID,body:Handoff,current=dep):
        row=s.get(current,id,'marking',True,'marking.labels.download')
        fingerprint=sha256(json.dumps(body.model_dump(mode='json'),sort_keys=True).encode()).hexdigest()
        old=current.connection.execute(text("SELECT body FROM business_events WHERE item_id=:id AND action='labels_handoff_confirmed' AND body->>'request_id'=:request"),{'id':id,'request':str(body.request_id)}).scalar_one_or_none()
        if old:
            if old['sha256']!=fingerprint:raise HTTPException(409,'Повтор подтверждения отличается')
            return {'confirmed':True,'replayed':True}
        s.version(row,body.revision);s.idle(current.connection,id);validate_source(current,row)
        if row['state']!='labels_ready' or not body.confirmed:raise HTTPException(409,'Нужны готовые этикетки и подтверждение фактической передачи')
        if str(body.file_id)!=row['body'].get('label_file_id'):raise HTTPException(409,'Версия этикеток изменилась')
        file=current.connection.execute(text("SELECT sha256,version FROM business_artifacts WHERE id=:file AND item_id=:id AND kind IN ('labels','labels_pdf')"),{'id':id,'file':body.file_id}).mappings().first()
        if not file:raise HTTPException(409,'Не найден актуальный PDF этикеток')
        evidence={'request_id':str(body.request_id),'sha256':fingerprint,'file_id':str(body.file_id),'file_sha256':file['sha256'],'version':file['version']}
        s.record(current,row,'labels_handoff_confirmed',evidence)
        s.save(current,row,'supplier_labels_transferred',body={**row['body'],'labels_handoff':evidence})
        return {'confirmed':True,'replayed':False}
    @app.post('/api/business/{id}/{action}')
    def action(id:UUID,action:str,body:Step,current=dep):
        row=s.get(current,id,lock=True);c=current.connection
        if row['kind']=='remark' and action not in ('archive','restore'):raise HTTPException(404,'Используйте действия официальной перемаркировки')
        if row['state']=='completed':raise HTTPException(409,'Накладная завершена логистом. Сначала верните её в работу.')
        if action=='restore':
            s.require(current,row,'documents.restore')
            if body.revision!=row['revision'] or row['state']!='archived':raise HTTPException(409,'Нужен актуальный архивный документ')
            s.idle(c,id)
            if body.reason!=row['title']:raise HTTPException(422,'Введите точное название/номер документа')
            data=row['body']
            if data.get('codes_released'):raise HTTPException(409,'КМ возвращены в общий пул. Создайте новый процесс из восстановленной накладной.')
            if row['kind']=='marking':
                source=c.execute(text('SELECT state,revision,lines FROM bm_documents WHERE id=:id'),{'id':row['source_id']}).mappings().one()
                if source['state']=='archived':raise HTTPException(409,'Сначала восстановите исходную накладную')
                if source['lines']!=data['lines']:raise HTTPException(409,'Состав исходной накладной изменился; требуется ручная сверка')
                data={**data,'source_revision':source['revision']}
            state=data.get('archived_from','draft')
            c.execute(text('UPDATE operation_targets SET stopped=false WHERE id=:id'),{'id':id})
            return s.save(current,row,'restored',body=data,state=state)
        s.version(row,body.revision);validate_source(current,row)
        permission={'labels':'marking.labels.download' if row['kind']=='marking' else 'split.operations.view','applied':'marking.application.confirm' if row['kind']=='marking' else 'split.application.confirm','report':'marking.reports.submit' if row['kind']=='marking' else 'split.reports.submit','retry-report':'marking.reports.submit' if row['kind']=='marking' else 'split.reports.submit','archive':'documents.archive','transferred':'logistics.documents.complete'}.get(action)
        if not permission:raise HTTPException(404,'Действие не найдено')
        s.require(current,row,permission)
        if action=='archive':
            if row['kind']=='atk' and c.execute(text("SELECT EXISTS(SELECT 1 FROM business_items f WHERE f.kind='fts' AND f.body->'atks' @> CAST(:id AS jsonb) AND f.state IN ('prepared','fts_submitted','circulation_confirmed'))"),{'id':json.dumps([str(id)])}).scalar_one():raise HTTPException(409,'АТК включён в пакет ФТС. Сначала разберите или остановите пакет.')
            from .logistics import has_completed_dependents
            if has_completed_dependents(c,row):raise HTTPException(409,'Документ связан с завершёнными накладными. Сначала верните их в работу.')
            current.require('documents.stop',**s.context(current,row))
            if body.reason!=row['title']:raise HTTPException(422,'Введите номер/название документа в поле подтверждения')
            return s.archive(current,row,body.reason)
        s.idle(c,id)
        if action=='labels':
            if row['state'] not in ('labels_ready','application_confirmed','reporting','applied'):raise HTTPException(409,'Дождитесь получения всех КМ')
            file=row['body'].get('label_file_id')
            if not file:
                file=labels(current,row,settings);s.save(current,row,'labels_generated',body={**row['body'],'label_file_id':str(file)})
            return {'file_id':file}
        if action=='applied':
            if row['kind']=='marking' and row['body'].get('labels_handoff_required') and (not row['body'].get('label_file_id') or row['body'].get('labels_handoff',{}).get('file_id')!=row['body'].get('label_file_id')):raise HTTPException(409,'Сначала подтвердите передачу актуальных этикеток поставщику')
            if row['state']!='labels_ready' or not body.confirmed:raise HTTPException(422,'Подтвердите нанесение актуальных этикеток')
            return s.save(current,row,'application_confirmed',state='application_confirmed')
        if action in ('report','retry-report'):
            if row['state'] not in ('application_confirmed','rejected'):raise HTTPException(409,'Сначала подтвердите нанесение')
            if row['state']=='rejected' and not str(row['body'].get('last_rejected_step','')).endswith('.report'):raise HTTPException(409,'Отклонена другая операция. Требуется разбор квитанции.')
            codes=[code for code in rows(c,id) if code['state']!='applied']
            if not codes or any(code['state']!='emitted' for code in codes):raise HTTPException(409,'Все КМ должны быть получены и ещё не нанесены по данным ЧЗ')
            config=load(c,settings,row['organization_id']);inn=organization(c,row['organization_id'])
            Live(config,inn).verify(codes,'EMITTED')
            if row['kind']=='split':
                source=Live(config,inn).info([row['body']['source_cis']])[row['body']['source_cis']]
                if int(source.get('quantityInPack',-1))!=row['body']['baseline']:raise HTTPException(409,'Остаток исходного КМ изменился. Нужна сверка.')
                payload={'productGroup':'radio','utilisationType':'DIVISIONMARK','sntins':[gs1.division_entry(bytes(code['payload']),row['body']['source_raw'].encode()).decode() for code in codes],'attributes':{'participantId':inn}}
            else:
                if not body.production_date or body.production_date>date.today():raise HTTPException(422,'Укажите фактическую дату производства не в будущем')
                attributes={'productionDate':body.production_date.isoformat()}
                if config.get('kpp'):attributes['kpp']=config['kpp']
                if config.get('fias_id'):attributes['fiasId']=config['fias_id']
                payload={'productGroup':'radio','sntins':[bytes(code['payload']).decode() for code in codes],'attributes':attributes}
            for offset in range(0,len(payload['sntins']),1000):s.queue(current,row,'split.report' if row['kind']=='split' else 'bm.report',{**payload,'sntins':payload['sntins'][offset:offset+1000]})
            return s.save(current,row,'report_queued',state='reporting')
        if action=='transferred':
            if row['kind']!='atk' or row['state']!='ready' or not body.confirmed:raise HTTPException(409,'Подтвердите передачу готового АТК')
            return s.save(current,row,'supplier_transfer_confirmed',state='transferred')
    @app.post('/api/atks')
    def atk(body:Atk,current=dep):
        current.require('marking.atk.create',organization_id=body.organization_id,owner_id=current.id);c=current.connection;documents=[];supplier=None;buckets={}
        existing=c.execute(text("SELECT * FROM business_items WHERE kind='atk' AND organization_id=:org AND body->>'request_id'=:request ORDER BY id"),{'org':body.organization_id,'request':str(body.request_id)}).mappings().all()
        if existing:
            for row in existing:
                s.require(current,row)
                if sorted(row['body']['documents'])!=sorted(map(str,set(body.ids))):raise HTTPException(409,'Повторная партия отличается от исходной')
            return {'ids':[r['id'] for r in existing]}
        for id in sorted(set(body.ids)):
            row=s.get(current,id,'marking',True,'marking.atk.create');s.idle(c,id);validate_source(current,row)
            if row['organization_id']!=body.organization_id or row['state']!='applied':raise HTTPException(409,'Нужны накладные одной организации с успешным отчётом')
            resolved=canonical(c,body.organization_id,row['body']['supplier'])
            if supplier and supplier!=resolved:raise HTTPException(422,'Один АТК — один поставщик')
            if c.execute(text("SELECT EXISTS(SELECT 1 FROM business_items WHERE kind='atk' AND state<>'archived' AND body->'documents' @> CAST(:id AS jsonb))"),{'id':json.dumps([str(id)])}).scalar_one():raise HTTPException(409,'Накладная уже включена в АТК')
            supplier=resolved;documents.append(str(id))
            for code in rows(c,id):buckets.setdefault(code['tnved'],[]).append(code)
        from .supplier_batches import require_ready
        sources=require_ready(c,body.organization_id,supplier,set(body.ids))
        numbers=[v['number'] for v in sources]
        created=[]
        parts=[(tnved,codes[offset:offset+1000]) for tnved,codes in buckets.items() for offset in range(0,len(codes),1000)]
        for tnved,codes in parts:
            from ..policy import snapshot
            policy=snapshot(c,body.organization_id)
            row=s.create(current,'atk',('АТК-'+supplier+'-'+','.join(numbers))[:280]+'-'+tnved,{'policy':policy,'supplier':supplier,'numbers':numbers,'tnved':tnved,'documents':sorted(documents),'request_id':str(body.request_id),'codes':[str(v['id']) for v in codes]},body.organization_id,state='awaiting_approval' if policy['values']['bm.atk.approval_required'] else 'aggregating')
            if not policy['values']['bm.atk.approval_required']:s.queue(current,row,'bm.atk',{'trade_participant_inn':organization(c,body.organization_id),'products_list':[{'ki':v['cis']} for v in codes]})
            created.append(row['id'])
        return {'ids':created}
    @app.post('/api/atks/{id}/approve')
    def approve_atk(id:UUID,body:Step,current=dep):
        row=s.get(current,id,'atk',True,'marking.atk.create');s.version(row,body.revision);s.idle(current.connection,id)
        if row['state']!='awaiting_approval' or not body.confirmed:raise HTTPException(409,'Подтвердите актуальный состав АТК')
        validate_atk_sources(current.connection,row)
        codes=current.connection.execute(text('SELECT cis,state FROM business_codes WHERE id=ANY(CAST(:ids AS uuid[]))'),{'ids':row['body']['codes']}).mappings().all()
        if len(codes)!=len(row['body']['codes']) or any(v['state']!='applied' for v in codes):raise HTTPException(409,'Состав АТК изменился')
        s.queue(current,row,'bm.atk',{'trade_participant_inn':organization(current.connection,row['organization_id']),'products_list':[{'ki':v['cis']} for v in codes]})
        return s.save(current,row,'atk_approved',state='aggregating')
    @app.get('/api/atks/{id}/candidates')
    def candidates(id:UUID,kind:str=Query(pattern='^(ADDING|REMOVING)$'),current=dep):
        row=s.get(current,id,'atk',permission='marking.atk.transform.prepare');body=row['body']
        values=current.connection.execute(text(f"SELECT c.id,c.cis,c.pn,c.gtin,c.quantity FROM business_codes c JOIN business_items i ON i.id=c.item_id WHERE c.organization_id=:org AND c.state='applied' AND c.tnved=:tnved AND {expression("i.body->>'supplier'",'i.organization_id')}=:supplier ORDER BY c.id LIMIT 1000"),{'org':row['organization_id'],'tnved':body['tnved'],'supplier':canonical(current.connection,row['organization_id'],body['supplier'])}).mappings().all()
        included=set(body['codes']);return {'codes':[dict(r) for r in values if (str(r['id']) in included)==(kind=='REMOVING')]}
    @app.post('/api/atks/{id}/transform')
    def transform(id:UUID,body:Transform,current=dep):
        parent=s.get(current,id,'atk',True,'marking.atk.transform.prepare');s.version(parent,body.revision);s.idle(current.connection,id)
        from .logistics import has_completed_dependents
        if has_completed_dependents(current.connection,parent):raise HTTPException(409,'АТК связан с завершёнными накладными. Сначала верните их в работу.')
        if parent['state'] not in ('ready','transferred') or not parent['body'].get('atk'):raise HTTPException(409,'Нужен зарегистрированный АТК')
        wanted=set(map(str,body.codes));included=set(parent['body']['codes'])
        if current.connection.execute(text("SELECT EXISTS(SELECT 1 FROM business_items WHERE kind='fts' AND state IN ('prepared','fts_submitted','circulation_confirmed') AND body->'atks' @> CAST(:id AS jsonb))"),{'id':json.dumps([str(id)])}).scalar_one():raise HTTPException(409,'АТК включён в пакет ФТС. Требуется сначала разобрать таможенные сведения.')
        if body.kind=='REMOVING' and not wanted<=included:raise HTTPException(422,'Изымать можно только КМ из состава этого АТК')
        if body.kind=='ADDING' and wanted&included:raise HTTPException(422,'КМ уже входят в АТК')
        codes=current.connection.execute(text("SELECT c.*,i.body->>'supplier' AS supplier FROM business_codes c JOIN business_items i ON i.id=c.item_id WHERE c.id=ANY(CAST(:ids AS uuid[])) AND c.organization_id=:org AND c.state='applied'"),{'ids':list(wanted),'org':parent['organization_id']}).mappings().all()
        if len(codes)!=len(wanted) or any(c['tnved']!=parent['body']['tnved'] or canonical(current.connection,parent['organization_id'],c['supplier'])!=canonical(current.connection,parent['organization_id'],parent['body']['supplier']) for c in codes):raise HTTPException(422,'КМ должны быть нанесены, с тем же поставщиком и ТН ВЭД')
        row=s.create(current,'transform','Трансформация '+parent['title'],{'atk_item':str(id),'kind':body.kind,'codes':sorted(wanted),'reason':body.reason,'base_revision':parent['revision']},parent['organization_id'],state='awaiting_approval');return row
    @app.post('/api/atks/transforms/{id}/approve')
    def approve(id:UUID,body:Step,current=dep):
        row=s.get(current,id,'transform',True,'marking.atk.transform.approve');s.version(row,body.revision);s.idle(current.connection,id)
        if row['state']!='awaiting_approval' or not body.confirmed:raise HTTPException(422,'Подтвердите согласование')
        parent=s.get(current,UUID(row['body']['atk_item']),'atk',True)
        if parent['revision']!=row['body']['base_revision']:raise HTTPException(409,'Состав АТК изменился после подготовки запроса')
        if current.connection.execute(text("SELECT EXISTS(SELECT 1 FROM business_items WHERE kind='transform' AND body->>'atk_item'=:parent AND state='processing')"),{'parent':str(parent['id'])}).scalar_one():raise HTTPException(409,'По АТК уже выполняется трансформация')
        codes=current.connection.execute(text('SELECT cis FROM business_codes WHERE id=ANY(CAST(:ids AS uuid[]))'),{'ids':row['body']['codes']}).scalars().all()
        s.queue(current,row,'bm.transform',{'trade_participant_inn':organization(current.connection,row['organization_id']),'atk':parent['body']['atk'],'transformation_type':row['body']['kind'],'products_list':[{'ki':cis} for cis in codes]})
        return s.save(current,row,'transformation_approved',state='processing')
    @app.post('/api/business-operations/{id}/resume')
    def resume(id:UUID,body:Step,current=dep):
        from .. import operations
        c=current.connection;op=c.execute(text('SELECT o.*,l.item_id,l.metadata,l.applied FROM external_operations o JOIN business_links l ON l.operation_id=o.id WHERE o.id=:id FOR UPDATE OF o,l'),{'id':id}).mappings().first()
        if not op:raise HTTPException(404,'Операция не найдена')
        row=s.get(current,op['item_id'],lock=True);s.version(row,body.revision);s.require(current,row,op['permission_id'])
        if not body.confirmed:raise HTTPException(422,'Подтвердите возобновление')
        if op['state']=='blocked' and op['reason'].startswith('before_dispatch:'):
            reason=operations.authorization(c,op)
            if reason:raise HTTPException(409,'Проверка перед отправкой не пройдена: '+reason)
            operations.transition(c,id,'queued','operator_resumed_preflight')
            result=id
        elif op['state']=='cancelled_before_dispatch' and not op['applied']:
            result=s.queue(current,row,op['kind'],bytes(op['payload']),op['metadata']);c.execute(text('UPDATE business_links SET applied=true WHERE operation_id=:id'),{'id':id})
        else:raise HTTPException(409,'Повтор неизвестных, отправленных или уже обработанных операций запрещён')
        s.save(current,row,'operation_resumed');return {'operation_id':result}
    @app.get('/api/business-files/{id}')
    def download(id:UUID,current=dep):
        file=current.connection.execute(text('SELECT * FROM business_artifacts WHERE id=:id'),{'id':id}).mappings().first()
        if not file:raise HTTPException(404,'Файл не найден')
        row=s.item(current.connection,file['item_id'])
        if file['kind']=='warehouse_invoice':
            try:s.require(current,row,'marking.documents.view')
            except HTTPException as error:
                if error.status_code!=403:raise
                s.require(current,row,'logistics.warehouse.result.view')
        elif file['kind']=='reprint':
            from .printing import reprint_access,current_payload
            if not file['snapshot'].get('request_id'):raise HTTPException(409,'Создайте задание перепечати с причиной и контролем')
            request,code,_=reprint_access(current,UUID(file['snapshot']['request_id']))
            current_payload(request,code,row)
            if request['state']=='cancelled':raise HTTPException(409,'Перепечать отменена')
        else:s.require(current,row)
        permission='nk.export' if file['kind']=='catalog_import_source' else 'labels.remarking.create' if row['kind']=='remark' else 'logistics.fts.prepare' if file['kind'].startswith('fts') else 'logistics.atk.download' if file['kind']=='atk' else 'marking.labels.download' if row['kind']=='marking' else 'split.operations.view'
        if file['kind'] not in ('reprint','warehouse_invoice'):s.require(current,row,permission)
        if row['body'].get('codes_released') and file['kind'].startswith('labels'):raise HTTPException(409,'КМ возвращены в общий пул; прежние этикетки больше не используются')
        if file['kind'].startswith('labels') and current.connection.execute(text("SELECT EXISTS(SELECT 1 FROM business_codes WHERE item_id=:id AND state='withdrawn')"),{'id':row['id']}).scalar_one():raise HTTPException(409,'В документе есть заменённый КМ. Прежний комплект этикеток использовать нельзя.')
        if file['kind'].startswith('labels') or file['kind']=='reprint':
            from ..incidents import enforce
            selected=[file['snapshot']['code_id']] if file['kind']=='reprint' else None
            cises=current.connection.execute(text('SELECT cis FROM business_codes WHERE item_id=:id AND (CAST(:selected AS uuid[]) IS NULL OR id=ANY(CAST(:selected AS uuid[])))'),{'id':row['id'],'selected':selected}).scalars().all()
            enforce(current.connection,row['organization_id'],'reprint',cises=cises,targets=[row['id'],row['source_id']])
        data=bytes(file['body'])
        if sha256(data).hexdigest()!=file['sha256']:raise HTTPException(503,'Файл повреждён')
        s.record(current,row,'file_downloaded',{'artifact':str(id),'version':file['version']})
        return Response(data,media_type=file['media_type'],headers={'Content-Disposition':'attachment; filename="'+file['filename']+'"'})
