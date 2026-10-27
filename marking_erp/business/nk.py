"""National Catalogue workspace. Official statuses are written only by live readback."""
from datetime import datetime,timezone
from difflib import SequenceMatcher
from io import BytesIO
import json,re,time,threading
from hashlib import sha256
from uuid import UUID,uuid4
import httpx
from fastapi import Depends,HTTPException,Request,Response,Query
from pydantic import Field,StrictInt
from sqlalchemy import text
from . import catalog_excel
import xlsxwriter
from ..auth_routes import Body
from ..gs1 import valid_gtin
from . import store as s
from .connections import load
from .catalog_worklist import CATALOG_CTE,DUPLICATE_FILTER
from .errors import RemoteError
from .catalog_policy import rules,duplicate_filter as resolve_duplicate_filter

BASES={'production':'https://апи.национальный-каталог.рф','sandbox':'https://api.nk.sandbox.crptech.ru'}

_throttle_guard=threading.Lock()
_throttle={}

class Client:
    def __init__(self,config):self.config=config
    def call(self,method,path,params=None,body=None):
        key=self.config.get('nk_key')
        if not key:raise RemoteError('Настройте API KEY Национального каталога')
        key_id=sha256(key.encode()).hexdigest()
        with _throttle_guard:
            lock,next_at=_throttle.setdefault(key_id,(threading.Lock(),0))
        with lock:
            now=time.monotonic();next_at=_throttle[key_id][1]
            if next_at-now>3:raise RemoteError('НК ограничил частоту; дождитесь следующего запроса',status=429,retry_after=int(next_at-now)+1)
            if next_at>now:time.sleep(next_at-now)
            _throttle[key_id]=(lock,time.monotonic()+2.1)
        try:
            with httpx.Client(timeout=httpx.Timeout(40,connect=8),follow_redirects=False,trust_env=False) as c:
                r=c.request(method,BASES[self.config['environment']]+path,params={**(params or {}),'apikey':key,'format':'json'},json=body)
        except httpx.HTTPError:raise RemoteError('НК недоступен. Результат отправленного действия требует сверки.') from None
        if r.status_code!=200:
            try:retry=min(3600,max(1,int(r.headers.get('Retry-After','60'))))
            except ValueError:retry=60
            if r.status_code==429:_throttle[key_id]=(lock,time.monotonic()+retry)
            raise RemoteError('НК: HTTP '+str(r.status_code)+'. Проверьте права и статус операции.',status=r.status_code,retry_after=retry)
        try:data=json.loads(r.content)
        except ValueError:raise RemoteError('НК вернул некорректный JSON') from None
        if not isinstance(data,dict) or 'result' not in data or data.get('error') or data.get('errors'):raise RemoteError('НК вернул ошибку. Проверьте карточку в личном кабинете.')
        return json.loads(json.dumps(data['result'],ensure_ascii=False).replace(key,'[secret]'))
    def schema(self,category_id,raw=None):
        candidates=[a.get('cat_id') for a in (raw or {}).get('categories',[])] or [category_id]
        for id in dict.fromkeys(candidates):
            try:result=self.call('GET','/v3/attributes',{'cat_id':int(id),'attr_type':'a'})
            except RemoteError as error:
                if error.status==404:continue
                raise
            if not isinstance(result,list):raise RemoteError('НК вернул неизвестную структуру полей категории')
            return result
        raise RemoteError('НК не вернул схему категорий карточки. Обновите каталог и проверьте права.',status=404)
    def details(self,gtin=None,good_id=None):
        data=self.call('GET','/v3/feed-product',{'good_id':good_id} if good_id else {'gtin':gtin})
        if not isinstance(data,list):raise RemoteError('НК не вернул карточку')
        matched=[r for r in data if str(r.get('good_id'))==str(good_id)] if good_id else [r for r in data if any(str(x.get('value'))==gtin for x in r.get('identified_by',[]))]
        if len(matched)!=1:raise RemoteError('НК не подтвердил однозначную карточку')
        return matched[0]

class Card(Body):
    organization_id:UUID
    request_id:UUID|None=None
    revision:StrictInt=Field(default=1,ge=1)
    pn:str=Field(min_length=1,max_length=120)
    gtin:str=Field(default='',max_length=14)
    tnved:str=Field(pattern='^[0-9]{10}$')
    name:str=Field(min_length=1,max_length=2000)
    brand:str=Field(default='',max_length=200)
    category_id:StrictInt=Field(default=0,ge=0)
    attributes:dict[str,str]=Field(default_factory=dict,max_length=500)
    duplicate_reason:str=Field(default='',max_length=1000)

class Template(Body):
    organization_id:UUID
    title:str=Field(min_length=1,max_length=200)
    tnved:str=Field(pattern='^[0-9]{10}$')
    category_id:StrictInt=Field(ge=1)
    attributes:dict[str,str]=Field(max_length=500)

class Selection(Body):
    ids:list[UUID]=Field(min_length=1,max_length=1000)
    revision:StrictInt=Field(default=1,ge=1)
    reason:str=Field(default='',max_length=1000)

class Action(Body):
    publish:bool=False
    revision:StrictInt=Field(ge=1)
    agreement:bool=False
    reason:str=Field(default='',max_length=1000)

def attrs(values):
    if any(not re.fullmatch('[0-9]+(?:::unit)?',k) or len(v)>30000 for k,v in values.items()):raise HTTPException(422,'Характеристики задаются по официальным ID НК; единицы — ID::unit')
    return {k:v.strip() for k,v in values.items() if v.strip()}

def normalize(body):
    data=body.model_dump(exclude={'organization_id','revision','request_id','duplicate_reason'})
    for k in ('pn','gtin','name','brand'):data[k]=data[k].strip()
    if not data['pn'] or not data['name'] or any(ord(c)<32 for c in data['pn']):raise HTTPException(422,'Проверьте PN и название')
    if data['gtin'] and not valid_gtin(data['gtin']):raise HTTPException(422,'GTIN: неверный формат или контрольная цифра')
    data['attributes']=attrs(body.attributes);return data

def duplicates(c,org,data,exclude=None):
    rows=c.execute(text("SELECT id,body->>'pn' AS pn,body->>'gtin' AS gtin,state FROM business_items WHERE organization_id=:org AND kind='card' AND id<>:id"),{'org':org,'id':exclude or UUID(int=0)}).mappings()
    def key(v):return re.sub(r'[^\w]','',v.casefold())
    pn=key(data['pn']);out=[];threshold=rules(c,org)['values']['nk.duplicates.similarity']
    for row in rows:
        other=key(row['pn'] or '')
        if (data['gtin'] and data['gtin']==row['gtin']) or pn==other or (min(len(pn),len(other))>=5 and SequenceMatcher(None,pn,other).ratio()>=threshold):out.append({**row,'id':str(row['id'])})
        if len(out)>=30:break
    return out

def check_duplicate(c,org,data,reason,exclude=None):
    found=duplicates(c,org,data,exclude)
    required=rules(c,org)['values']['nk.duplicates.reason_required']
    if found and (not reason.strip() or (required and len(reason.strip())<8)):raise HTTPException(409,{'code':'duplicate_card','message':('Есть похожий PN или такой GTIN. Просмотрите совпадения и укажите причину продолжения от 8 символов.' if required else 'Есть похожий PN или такой GTIN. Просмотрите совпадения и явно подтвердите продолжение.'),'candidates':found})
    return found

def incoming(raw):
    statuses=list(raw.get('good_detailed_status') or [])
    if isinstance(raw.get('good_status'),str) and raw['good_status'] not in statuses:statuses.append(raw['good_status'])
    aa={str(a['attr_id']):str(a.get('attr_value') or '') for a in raw.get('good_attrs',[]) if 'attr_id' in a}
    gtins=[str(x.get('value') or '') for x in raw.get('identified_by',[]) if x.get('level')=='trade-unit']
    for a in raw.get('good_attrs',[]):
        if a.get('attr_id') is not None and a.get('attr_value_type') is not None:aa[str(a['attr_id'])+'::unit']=str(a['attr_value_type'])
    categories=raw.get('categories',[])
    official=[a for a in raw.get('good_attrs',[]) if str(a.get('attr_id'))=='13914']
    candidates=official or [a for a in raw.get('good_attrs',[]) if re.sub(r'[^\w]','',str(a.get('attr_name','')).casefold()) in ('модель','модельартикул','артикулпроизводителя','модельартикулпроизводителя','pn')]
    pns=list(dict.fromkeys(str(a.get('attr_value') or '').strip() for a in candidates if str(a.get('attr_value') or '').strip() and str(a.get('attr_value') or '').strip().casefold()!=str(raw.get('brand_name') or '').casefold()))
    return {'pn':pns[0] if len(pns)==1 else '','gtin':gtins[0] if len(set(gtins))==1 else '', 'tnved':aa.get('13933',str(raw.get('tnved') or '')),
        'name':str(raw.get('good_name') or ''),'brand':str(raw.get('brand_name') or aa.get('2504','')),'category_id':int(categories[0]['cat_id']) if categories else 0,'attributes':aa,
        'external':{'good_id':raw['good_id'],'statuses':statuses,'signed':raw.get('good_signed') is True,'raw':raw,'checked_at':datetime.now(timezone.utc).isoformat()},'dirty':False}

def apply_remote(current,raw,id=None):
    data=incoming(raw);c=current.connection
    if valid_gtin(data['gtin']) and data['pn'] and re.fullmatch('[0-9]{10}',data['tnved']):
        from ..registry import register_product
        register_product(c,current.org,data['gtin'],data['pn'],data['tnved'],source='nk_confirmed',source_ref='nk:'+str(raw['good_id']),evidence_sha256=sha256(json.dumps(raw,ensure_ascii=False,sort_keys=True).encode()).hexdigest())
    if not id:id=c.execute(text("SELECT id FROM business_items WHERE organization_id=:org AND kind='card' AND body->'external'->>'good_id'=:good LIMIT 1"),{'org':current.org,'good':str(raw['good_id'])}).scalar_one_or_none()
    if id:
        row=s.item(c,id)
        if row['body'].get('dirty') or row['state']=='archived':return row
        return s.save(current,row,'nk_synchronized',body=data,state='active',title=data['pn'] or data['name'] or str(raw['good_id']))
    return s.create(current,'card',data['pn'] or data['name'] or str(raw['good_id']),data,current.org,state='active')

def workbook(rows,headers):
    out=BytesIO();book=xlsxwriter.Workbook(out,{'in_memory':True,'strings_to_formulas':False,'strings_to_urls':False});sheet=book.add_worksheet('Карточки');sheet.write_row(0,0,headers)
    for i,row in enumerate(rows,1):sheet.write_row(i,0,row)
    sheet.freeze_panes(1,0);sheet.set_column(0,len(headers)-1,24);book.close();return out.getvalue()

def build_entry(data,schema):
    from decimal import Decimal,InvalidOperation
    external=data.get('external',{});raw=external.get('raw',{})
    if raw.get('is_set') or raw.get('is_kit'):raise HTTPException(409,'Комплекты и наборы редактируются отдельным процессом')
    allowed={str(a['attr_id']):a for a in schema};values=dict(data['attributes'])
    model='13914' if '13914' in allowed else next((str(a['attr_id']) for a in schema if re.sub(r'[^\w]','',a.get('attr_name','').casefold()) in ('модель','модельартикул','артикулпроизводителя','модельартикулпроизводителя','pn')),None)
    if model:
        values[model]=data['pn']
        if not values.get(model+'::unit') and 'Артикул' in (allowed[model].get('attr_value_type') or []):values[model+'::unit']='Артикул'
    else:raise HTTPException(422,'В схеме отсутствует поле модели/PN. Проверьте категорию НК.')
    if '2478' in allowed:values['2478']=data['name']
    if '2504' in allowed:values['2504']=data['brand']
    original={str(a['attr_id']):a for a in raw.get('good_attrs',[])};attributes=[]
    if raw:
        for key,old in original.items():
            if key not in ('13933','3959') and key in allowed and allowed[key].get('attr_type')!='b' and str(old.get('attr_value') or '') and not values.get(key):raise HTTPException(422,'Удаление значения требует отдельной процедуры НК: '+key)
    for key,value in values.items():
        if key.endswith('::unit') or key in ('13933','3959'):continue
        old=original.get(key,{});unit=values.get(key+'::unit','')
        unchanged=bool(raw and value==str(old.get('attr_value') or '') and unit==str(old.get('attr_value_type') or ''))
        if unchanged:continue
        definition=allowed.get(key)
        if not definition or definition.get('attr_type')=='b':raise HTTPException(422,'Поле не разрешено схемой НК: '+key)
        if not value:raise HTTPException(422,'Пустое значение характеристики: '+key)
        if definition.get('attr_preset_only') and definition.get('attr_preset') and value not in definition['attr_preset']:raise HTTPException(422,'Значение отсутствует в справочнике НК: '+key)
        units=definition.get('attr_value_type') or []
        if units and all(isinstance(u,str) for u in units) and unit not in units:raise HTTPException(422,'Выберите единицу измерения: '+definition['attr_name'])
        if old.get('attr_value_type') and not unit:raise HTTPException(422,'Удаление единицы измерения требует отдельной процедуры: '+key)
        if definition.get('attr_field_type')=='number':
            try:
                if not Decimal(str(value)).is_finite():raise ValueError
            except (ValueError,InvalidOperation):raise HTTPException(422,'Характеристика должна быть конечным числом: '+key) from None
        attr={'attr_id':int(key),'attr_value':value}
        if unit:attr['attr_value_type']=unit
        attributes.append(attr)
    if not raw:
        present={str(a['attr_id']) for a in attributes}|{'2478','13933','3959'}
        present.update(str(a['attr_id']) for a in schema if a.get('attr_name')=='Товарный знак' and data['brand'])
        missing=[a['attr_name'] for a in schema if a.get('attr_type')=='m' and str(a['attr_id']) not in present]
        if missing:raise HTTPException(422,'Не заполнены обязательные поля: '+', '.join(missing))
    if external:return {'good_id':int(external['good_id']),'good_name':data['name'],'moderation':0,'good_attrs':attributes}
    return {'gtin':data['gtin'],'good_name':data['name'],'tnved':data['tnved'],'brand':data['brand'],'categories':[data['category_id']],'moderation':0,'is_set':False,'is_kit':False,'is_tech_gtin':False,'identified_by':[{'value':data['gtin'],'type':'gtin','level':'trade-unit','multiplier':1,'unit':'шт'}],'good_attrs':attributes}

def validate_card(data,config):
    if not data.get('brand') or not data.get('category_id') or not data.get('pn') or not data.get('name'):
        raise HTTPException(422,'Заполните PN, название, товарный знак и вид/категорию товара')
    schema=Client(config).schema(data['category_id'],data.get('external',{}).get('raw',{}))
    build_entry(data,schema)
    return {'valid':True,'message':'Обязательные поля проверены по актуальной схеме НК. Внешние операции не выполнялись.'}

def perform_action(current,id,action,body,settings):
    permission={'validate':'nk.cards.view','discard':'nk.cards.edit','gtin':'nk.gtin.generate','save':'nk.cards.edit','moderate':'nk.cards.moderate','publish':'nk.cards.publish','archive':'nk.cards.edit','refresh':'nk.cards.view','restore':'documents.restore'}.get(action)
    if not permission:raise HTTPException(404,'Действие не найдено')
    row=s.get(current,id,'card',True,permission)
    if action=='restore':
        if row['state']!='archived' or row['revision']!=body.revision:raise HTTPException(409,'Нужна актуальная архивная карточка')
        s.idle(current.connection,id)
        current.connection.execute(text('UPDATE operation_targets SET stopped=false WHERE id=:id'),{'id':id})
        return s.save(current,row,'card_restored',state='active' if row['body'].get('external') else 'draft')
    s.version(row,body.revision);data=row['body']
    if action=='archive':
        if len(body.reason.strip())<8:raise HTTPException(422,'Укажите причину архивирования')
        return s.archive(current,row,body.reason)
    s.idle(current.connection,id)
    config=load(current.connection,settings,row['organization_id'])
    if action=='validate':return validate_card(data,config)
    if action=='discard':
        if not data.get('external') or not body.agreement:raise HTTPException(422,'Нужно подтверждение замены локальных изменений данными НК')
        raw=Client(config).details(good_id=data['external']['good_id'])
        s.record(current,row,'local_changes_discarded',{'previous':data,'reason':body.reason})
        current.connection.execute(text("UPDATE business_items SET body=jsonb_set(body,'{dirty}','false') WHERE id=:id"),{'id':id})
        current.org=row['organization_id'];return apply_remote(current,raw,id)
    if action=='refresh':
        if data.get('dirty'):raise HTTPException(409,'Есть локальные изменения. Сохраните их в НК перед обновлением.')
        raw=Client(config).details(gtin=data['gtin'],good_id=data.get('external',{}).get('good_id'));current.org=row['organization_id'];return apply_remote(current,raw,id)
    if not config.get('writes_enabled'):raise HTTPException(409,'Администратор не разрешил внешние отправки')
    if action=='gtin':
        if data['gtin']:raise HTTPException(409,'GTIN уже заполнен')
        payload={'quantity':1}
    elif action=='save':
        if not data['gtin'] or not data['brand'] or not data['category_id']:raise HTTPException(422,'Заполните GTIN, бренд и ID категории')
        check_duplicate(current.connection,row['organization_id'],data,body.reason,id)
        raw=data.get('external',{}).get('raw',{})
        schema=Client(config).schema(data['category_id'],raw)
        entry=build_entry(data,schema)
        payload={'entry':entry,'base':raw or None}
    else:
        if data.get('dirty') or not data.get('external'):raise HTTPException(409,'Сначала сохраните карточку в НК и дождитесь подтверждения')
        statuses=data['external'].get('statuses',[])
        if action=='moderate' and not any(k in statuses for k in ('draft','errors')):raise HTTPException(409,'Карточка не находится в редактируемом статусе НК')
        if action=='publish' and ('notsigned' not in statuses or not body.agreement):raise HTTPException(422,'Нужны статус «Ожидает подписания» и согласие на публикацию')
        payload={'good_id':data['external']['good_id'],'publicationAgreement':body.agreement}
    op=s.queue(current,row,'nk.document' if action=='publish' else 'nk.'+action,payload);s.save(current,row,'nk_queued',state='processing');return {'operation_id':op}

def install_nk(app,actor,settings):
    dep=Depends(actor,scope='function')
    from .catalog_batch import install
    install(app,actor,settings)
    @app.get('/api/catalog-options')
    def options(organization_id:UUID,current=dep):
        current.require('nk.cards.view',organization_id=organization_id,owner_id=current.id,assigned=True)
        from importlib.resources import files
        values=json.loads(files('marking_erp.data').joinpath('presets/product-kinds.json').read_text())['kinds']
        return {'kinds':values,'policy':rules(current.connection,organization_id)}
    @app.get('/api/catalog-schema')
    def schema(organization_id:UUID,category_id:int=Query(ge=1),card_id:UUID|None=None,current=dep):
        current.require('nk.cards.view',organization_id=organization_id,owner_id=current.id,assigned=True)
        card=s.get(current,card_id,'card') if card_id else None
        if card and card['organization_id']!=organization_id:raise HTTPException(422,'Организация карточки отличается')
        raw=card['body'].get('external',{}).get('raw') if card else None
        result=Client(load(current.connection,settings,organization_id)).schema(category_id,raw)
        return {'attributes':result}
    @app.get('/api/catalog')
    def listing(organization_id:UUID,kind:str=Query('card',pattern='^(card|template|sync|import)$'),q:str=Query('',max_length=120),archived:bool=False,before_id:UUID|None=None,draft_duplicates:str|None=Query(None,pattern='^(hide|include|only)$'),status:str=Query('',pattern='^(|local|dirty|draft|errors|moderation|notsigned|published)$'),sort:str=Query('newest',pattern='^(newest|pn)$'),current=dep):
        current.require('nk.cards.view',organization_id=organization_id,owner_id=current.id,assigned=True)
        draft_duplicates=resolve_duplicate_filter(current.connection,organization_id,draft_duplicates)
        grant=current.grants()['nk.cards.view']
        projection=CATALOG_CTE if kind=='card' else ''
        source='catalog_rows' if kind=='card' else 'business_items'
        duplicate_column='duplicate_draft' if kind=='card' else 'false AS duplicate_draft'
        duplicate_filter=DUPLICATE_FILTER if kind=='card' else ''
        order="coalesce(i.body->>'pn',''),i.id" if sort=='pn' else 'i.created_at,i.id'
        cursor_order="coalesce(body->>'pn',''),id" if sort=='pn' else 'created_at,id'
        direction='ASC' if sort=='pn' else 'DESC';comparison='>' if sort=='pn' else '<'
        rows=current.connection.execute(text(projection+f'''SELECT i.id,i.title,i.state,i.revision,i.body,i.updated_at,{duplicate_column} FROM {source} i WHERE i.organization_id=:org AND kind=:kind
            AND (state='archived')=:archived AND (CAST(:before AS uuid) IS NULL OR ({order}){comparison}(SELECT {cursor_order} FROM business_items WHERE id=CAST(:before AS uuid) AND organization_id=:org))
            AND (:grant='all' OR (:grant='own' AND owner_id=:user) OR (:grant='assigned' AND EXISTS(SELECT 1 FROM operation_assignments a WHERE a.target_id=i.id AND a.user_id=:user)))
            AND (title ILIKE :q ESCAPE '!' OR body->>'pn' ILIKE :q ESCAPE '!' OR body->>'gtin' ILIKE :q ESCAPE '!' OR body->>'tnved' ILIKE :q ESCAPE '!')
            AND (:status='' OR (:status='local' AND NOT body ? 'external') OR (:status='dirty' AND body->>'dirty'='true') OR body#>'{{external,statuses}}' ? :status)
            {duplicate_filter} ORDER BY {order.split(',i.id')[0]} {direction},i.id {direction} LIMIT 26'''),{'status':status,'org':organization_id,'kind':kind,'archived':archived,'draft_duplicates':draft_duplicates,'before':str(before_id) if before_id else None,'grant':grant,'user':current.id,'q':'%'+q.replace('!','!!').replace('%','!%').replace('_','!_')+'%'}).mappings().all()
        return {'items':[{**r,'body':{k:v for k,v in r['body'].items() if k not in ('external','rows','row_links','payloads')},'external_status':r['body'].get('external',{}).get('statuses',[])} for r in rows[:25]],'next_cursor':str(rows[24]['id']) if len(rows)>25 else None}
    @app.get('/api/catalog/export')
    def export(organization_id:UUID,q:str=Query('',max_length=120),archived:bool=False,draft_duplicates:str|None=Query(None,pattern='^(hide|include|only)$'),status:str=Query('',pattern='^(|local|dirty|draft|errors|moderation|notsigned|published)$'),sort:str=Query('newest',pattern='^(newest|pn)$'),current=dep):
        draft_duplicates=resolve_duplicate_filter(current.connection,organization_id,draft_duplicates)
        export_grant=current.require('nk.export',organization_id=organization_id,owner_id=current.id,assigned=True)
        grant=current.grants().get('nk.cards.view')
        if not grant:raise HTTPException(403,'Нет доступа к карточкам каталога')
        rows=current.connection.execute(text(CATALOG_CTE+'''SELECT i.body->>'pn' AS pn,i.body->>'gtin' AS gtin,i.body->>'tnved' AS tnved,
            i.body->>'name' AS name,i.body->>'brand' AS brand,i.body->'category_id' AS category_id,i.body->'external'->'statuses' AS statuses,duplicate_draft
            FROM catalog_rows i WHERE (state='archived')=:archived
            AND (:export_grant='all' OR (:export_grant='own' AND i.owner_id=:user)
                OR (:export_grant='assigned' AND EXISTS(SELECT 1 FROM operation_assignments a WHERE a.target_id=i.id AND a.user_id=:user)))
            AND (title ILIKE :q ESCAPE '!' OR body->>'pn' ILIKE :q ESCAPE '!' OR body->>'gtin' ILIKE :q ESCAPE '!' OR body->>'tnved' ILIKE :q ESCAPE '!')
            AND (:status='' OR (:status='local' AND NOT body ? 'external') OR (:status='dirty' AND body->>'dirty'='true') OR body#>'{external,statuses}' ? :status)
            '''+DUPLICATE_FILTER+''' ORDER BY i.created_at,i.id LIMIT 50001'''),{'status':status,'org':organization_id,'grant':grant,'export_grant':export_grant,'user':current.id,'archived':archived,'draft_duplicates':draft_duplicates,'q':'%'+q.replace('!','!!').replace('%','!%').replace('_','!_')+'%'}).mappings().all()
        if len(rows)>50000:raise HTTPException(422,'В выгрузке больше 50000 карточек. Уточните фильтр PN, GTIN или ТН ВЭД; неполный файл не формируется.')
        data=workbook([[r[k] or '' for k in ('pn','gtin','tnved','name','brand','category_id')]+[', '.join(r['statuses'] or []),'Есть опубликованная карточка с точным PN' if r['duplicate_draft'] else ''] for r in rows],['PN','GTIN','ТН ВЭД','Название','Товарный знак','ID категории','Статус НК','Совпадение PN'])
        return Response(data,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':'attachment; filename="catalog.xlsx"'})
    @app.get('/api/catalog/{id}')
    def detail(id:UUID,current=dep):
        row=s.get(current,id);events=current.connection.execute(text('SELECT e.action,e.created_at,e.body,u.display_name FROM business_events e JOIN users u ON u.id=e.actor_id WHERE e.item_id=:id ORDER BY e.id DESC LIMIT 50'),{'id':id}).mappings().all()
        ops=current.connection.execute(text('SELECT o.id,o.kind,o.state,o.reason,o.external_id FROM external_operations o JOIN business_links l ON l.operation_id=o.id WHERE l.item_id=:id ORDER BY o.created_at DESC LIMIT 30'),{'id':id}).mappings().all()
        return {'item':row,'events':[dict(r) for r in events],'operations':[dict(r) for r in ops]}
    @app.post('/api/catalog')
    def create(body:Card,current=dep):
        current.require('nk.cards.create',organization_id=body.organization_id,owner_id=current.id)
        data=normalize(body)
        if body.request_id:
            old=current.connection.execute(text('SELECT * FROM business_items WHERE id=:id'),{'id':body.request_id}).mappings().first()
            if old:
                s.require(current,old)
                if old['kind']!='card' or old['organization_id']!=body.organization_id or any(old['body'].get(k)!=v for k,v in data.items()):raise HTTPException(409,'Идентификатор запроса уже использован')
                return dict(old)
        found=check_duplicate(current.connection,body.organization_id,data,body.duplicate_reason)
        data['dirty']=True;row=s.create(current,'card',data['pn'],data,body.organization_id,id=body.request_id)
        if found:s.record(current,row,'duplicate_acknowledged',{'reason':body.duplicate_reason,'candidates':[str(r['id']) for r in found]})
        return row
    @app.put('/api/catalog/{id}')
    def edit(id:UUID,body:Card,current=dep):
        row=s.get(current,id,'card',True,'nk.cards.edit');s.version(row,body.revision);s.idle(current.connection,id)
        if body.organization_id!=row['organization_id']:raise HTTPException(422,'Организация не меняется')
        data=normalize(body);found=check_duplicate(current.connection,body.organization_id,data,body.duplicate_reason,id)
        if row['body'].get('external'):
            if data['gtin']!=row['body']['gtin'] or data['tnved']!=row['body']['tnved'] or data['category_id']!=row['body']['category_id']:raise HTTPException(422,'GTIN, ТН ВЭД и категория связанной карточки не меняются')
            data['external']=row['body']['external']
        data['dirty']=True
        if found:s.record(current,row,'duplicate_acknowledged',{'reason':body.duplicate_reason})
        return s.save(current,row,'card_edited',body=data,title=data['pn'])
    @app.post('/api/catalog/templates')
    def template(body:Template,current=dep):
        current.require('nk.templates.approve',organization_id=body.organization_id,owner_id=current.id)
        data=body.model_dump(exclude={'organization_id'});data['attributes']=attrs(body.attributes)
        return s.create(current,'template',body.title,data,body.organization_id,state='approved')
    @app.post('/api/catalog/templates/{id}/apply')
    def apply(id:UUID,body:Selection,current=dep):
        template=s.get(current,id,'template');s.version(template,body.revision);results=[]
        for card_id in sorted(set(body.ids)):
            row=s.get(current,card_id,'card',True,'nk.cards.edit');s.require(current,row,'nk.templates.use');s.idle(current.connection,card_id)
            if row['state']=='archived' or row['body']['tnved']!=template['body']['tnved'] or row['body'].get('category_id') not in (0,template['body']['category_id']) or row['organization_id']!=template['organization_id']:raise HTTPException(422,'Выберите действующие карточки с ТН ВЭД и организацией шаблона')
            data={**row['body'],'attributes':{**template['body']['attributes'],**row['body']['attributes']},'dirty':True}
            if not data.get('category_id'):data['category_id']=template['body']['category_id']
            if '13914' not in data['attributes']:data['attributes']['13914']=data['pn']
            s.save(current,row,'template_applied',body=data);results.append(str(card_id))
        return {'applied':results}
    @app.post('/api/catalog/{id}/{action}')
    def action(id:UUID,action:str,body:Action,current=dep):
        return perform_action(current,id,action,body,settings)
    @app.post('/api/catalog-sync')
    def sync(organization_id:UUID,current=dep):
        current.require('nk.sync.execute',organization_id=organization_id,owner_id=current.id)
        load(current.connection,settings,organization_id)
        from .catalog_schedule import start
        return start(current,organization_id)
    @app.post('/api/catalog-sync/{id}/resume')
    def resume_sync(id:UUID,body:Action,current=dep):
        row=s.get(current,id,'sync',True,'nk.sync.execute');s.version(row,body.revision)
        if row['state']!='blocked':raise HTTPException(409,'Продолжить можно остановленную синхронизацию')
        if current.connection.execute(text("SELECT EXISTS(SELECT 1 FROM business_items WHERE kind='sync' AND organization_id=:org AND state='processing' AND id<>:id)"),{'org':row['organization_id'],'id':id}).scalar_one():raise HTTPException(409,'Другая синхронизация уже выполняется')
        return s.save(current,row,'sync_resumed',body={**row['body'],'error':'','sync_actor':str(current.id)},state='processing')
    @app.post('/api/catalog-import/preview')
    async def preview(request:Request,organization_id:UUID,sheet_name:str|None=Query(None,max_length=120),filename:str=Query('catalog.xlsx',max_length=160),current=dep):
        current.require('nk.import.preview',organization_id=organization_id,owner_id=current.id)
        chunks=[];size=0
        async for chunk in request.stream():
            size+=len(chunk)
            if size>10*1024*1024:raise HTTPException(413,'Файл больше 10 МБ')
            chunks.append(chunk)
        content=b''.join(chunks)
        try:source=catalog_excel.read(content,sheet_name)
        except ValueError as error:raise HTTPException(422,str(error)) from None
        except Exception:raise HTTPException(422,'Не удалось прочитать XLSX') from None
        rows=[];seen={}
        for r in source['rows']:
            values=r['values'];error=r['error'];matches=[];data=values
            try:
                values['category_id']=int(values['category_id'] or 0)
                data=normalize(Card(organization_id=organization_id,**values))
                matches=duplicates(current.connection,organization_id,data)
            except Exception:error=error or 'Проверьте PN, GTIN, ТН ВЭД и ID категории'
            key=sha256(json.dumps(data,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            duplicate_row=seen.get(key);seen.setdefault(key,r['row'])
            rows.append({'row':r['row'],'data':data,'duplicates':matches,'error':error,'duplicate_row':duplicate_row})
        meta={k:v for k,v in source.items() if k!='rows'}
        result=s.create(current,'import','Импорт НК · '+filename,{'rows':rows,'source_meta':meta,'source_sha256':sha256(content).hexdigest(),'filename':filename},organization_id)
        s.artifact(current,result,'catalog_import_source','catalog-source.xlsx',content,catalog_excel.MIME,{'sheet_name':source['sheet_name']})
        return {'id':result['id'],'revision':result['revision'],'rows':rows,'sheet_name':source['sheet_name'],'sheet_names':source['sheet_names']}
    def import_source(current,row):
        file=current.connection.execute(text("SELECT * FROM business_artifacts WHERE item_id=:id AND kind='catalog_import_source' ORDER BY version LIMIT 1"),{'id':row['id']}).mappings().first()
        if not file:raise HTTPException(404,'Исходный Excel не сохранён для этой старой партии')
        data=bytes(file['body'])
        if sha256(data).hexdigest()!=file['sha256'] or file['sha256']!=row['body'].get('source_sha256'):raise HTTPException(503,'Исходный файл повреждён')
        return data
    @app.get('/api/catalog-import/{id}/source')
    def source_download(id:UUID,current=dep):
        row=s.get(current,id,'import',permission='nk.export');data=import_source(current,row)
        s.record(current,row,'import_source_downloaded')
        return Response(data,media_type=catalog_excel.MIME,headers={'Content-Disposition':'attachment; filename="catalog-source.xlsx"'})
    @app.post('/api/catalog-import/{id}/pipeline')
    def pipeline(id:UUID,body:Action,current=dep):
        row=s.get(current,id,'import',True,'nk.bulk_creation.execute')
        if row['revision']!=body.revision or row['state'] not in ('done','blocked'):raise HTTPException(409,'Нужна сохранённая партия импорта')
        if not row['body'].get('card_ids') or not body.agreement:raise HTTPException(422,'Подтвердите получение GTIN, сохранение и модерацию карточек')
        if not load(current.connection,settings,row['organization_id']).get('writes_enabled'):raise HTTPException(409,'Отправка отключена')
        for card_id in row['body']['card_ids']:
            card=s.get(current,UUID(card_id),'card')
            for permission in ['nk.gtin.generate','nk.cards.edit','nk.cards.moderate']+(['nk.cards.publish'] if body.publish else []):s.require(current,card,permission)
        return s.save(current,row,'pipeline_started',body={**row['body'],'pipeline_actor':str(current.id),'pipeline':True,'publish':body.publish,'policy':rules(current.connection,row['organization_id']),'results':{},'polls':{}},state='processing')
    @app.post('/api/catalog-import/{id}/pause')
    def pause_pipeline(id:UUID,body:Action,current=dep):
        row=s.get(current,id,'import',True,'nk.bulk_creation.execute');s.version(row,body.revision)
        if row['state']!='processing' or not row['body'].get('pipeline'):raise HTTPException(409,'Партия не выполняется')
        return s.save(current,row,'pipeline_paused',body={**row['body'],'pipeline':False,'error':'Остановлено сотрудником. Уже поставленные операции требуют отдельной сверки.'},state='blocked')
    @app.get('/api/catalog-import/{id}/export')
    def export_import(id:UUID,current=dep):
        row=s.get(current,id,'import',permission='nk.export');values=[]
        ids=[UUID(v) for v in row['body'].get('card_ids',[])];grants=current.grants()
        allowed=current.connection.execute(text('''SELECT i.* FROM business_items i WHERE i.id=ANY(CAST(:ids AS uuid[]))
            AND i.organization_id=:org AND i.kind='card'
            AND (:view='all' OR (:view='own' AND i.owner_id=:user) OR (:view='assigned' AND EXISTS(SELECT 1 FROM operation_assignments a WHERE a.target_id=i.id AND a.user_id=:user)))
            AND (:export='all' OR (:export='own' AND i.owner_id=:user) OR (:export='assigned' AND EXISTS(SELECT 1 FROM operation_assignments a WHERE a.target_id=i.id AND a.user_id=:user)))'''),
            {'ids':ids,'org':row['organization_id'],'user':current.id,'view':grants.get('nk.cards.view'),'export':grants.get('nk.export')}).mappings().all()
        cards={str(v['id']):dict(v) for v in allowed}
        if set(cards)!={str(v) for v in ids}:raise HTTPException(403,'Нет права выгрузить одну или несколько карточек партии')
        for card_id in row['body'].get('card_ids',[]):
            d=cards[card_id]['body'];values.append([d['pn'],d['tnved'],d['brand'],d['gtin'],', '.join(d.get('external',{}).get('statuses',[]))])
        if row['body'].get('source_meta'):
            if 'row_links' not in row['body']:raise HTTPException(409,'Сначала создайте карточки из предпросмотра')
            results={}
            for link in row['body']['row_links']:
                card=cards[link['card_id']];d=card['body']
                if {k:d.get(k,'') for k in ('pn','brand','tnved')}!=link['identity']:
                    results[link['row']]=['','PN, бренд или ТН ВЭД карточки изменены; требуется сверка']
                    continue
                state=', '.join(d.get('external',{}).get('statuses',[])) or 'Локальная карточка'
                if d.get('dirty') and d.get('external'):state+='; есть неподтверждённые локальные изменения'
                if card['state']=='archived':state+='; архив ERP'
                result=row['body'].get('results',{}).get(link['card_id'])
                if result:state+='; '+result
                results[link['row']]=[d.get('gtin',''),state]
            try:data=catalog_excel.append_results(import_source(current,row),row['body']['source_meta'],results)
            except ValueError as error:raise HTTPException(422,str(error)) from None
        else:data=workbook(values,['PN','ТН ВЭД','Товарный знак','GTIN','Статус НК'])
        s.record(current,row,'import_result_downloaded')
        return Response(data,media_type=catalog_excel.MIME,headers={'Content-Disposition':'attachment; filename="creation-result.xlsx"'})
    @app.post('/api/catalog-import/{id}/commit')
    def commit(id:UUID,body:Selection,current=dep):
        row=s.get(current,id,'import',True,'nk.import.commit')
        request_hash=sha256(json.dumps(body.model_dump(mode='json'),sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        if row['body'].get('commit_sha256')==request_hash:
            for card_id in row['body']['card_ids']:s.get(current,UUID(card_id),'card')
            return {'card_ids':row['body']['card_ids'],'row_links':row['body']['row_links']}
        s.version(row,body.revision)
        if row['state']!='draft':raise HTTPException(409,'Импорт уже обработан')
        if body.ids!=[id]:raise HTTPException(422,'Подтверждение должно относиться к этой партии импорта')
        ids=[];seen={};links=[]
        for r in row['body']['rows']:
            if r['error']:raise HTTPException(422,'Исправьте ошибки в Excel и загрузите заново')
            data=r['data'];key=sha256(json.dumps(data,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            if key not in seen:
                check_duplicate(current.connection,row['organization_id'],data,body.reason)
                card=s.create(current,'card',data['pn'],{**data,'dirty':True},row['organization_id']);card_id=str(card['id']);ids.append(card_id);seen[key]=card_id
                s.record(current,card,'imported',{'source':str(id),'duplicate_reason':body.reason})
            links.append({'row':r['row'],'card_id':seen[key],'identity':{k:data.get(k,'') for k in ('pn','brand','tnved')}})
        s.save(current,row,'import_committed',body={**row['body'],'card_ids':ids,'row_links':links,'commit_sha256':request_hash,'duplicate_reason':body.reason},state='done')
        return {'card_ids':ids,'row_links':links}
