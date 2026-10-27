"""Durable source adapters. Single worker; every mutation persists its intent first."""
import base64,json,threading,time
from datetime import datetime,timezone
from hashlib import sha256
from uuid import UUID,uuid4
from sqlalchemy import text
from .. import auth,operations,gs1
from . import store as s
from .connections import load
from .contracts import VERSION,CONTRACTS
from .nk import Client,apply_remote,incoming,perform_action,Action
from .errors import RemoteError
from .live import Live


def background(c,owner,org,include_disabled=False):
    user=c.execute(text('SELECT * FROM users WHERE id=:id AND (:disabled OR enabled)'),{'id':owner,'disabled':include_disabled}).mappings().first()
    if not user:raise ValueError('Учётная запись отключена')
    current=auth.Actor(c,dict(user),'worker',None);current.org=org;return current

class BusinessWorker:
    def __init__(self,engine,settings):self.engine=engine;self.settings=settings;self.stop=threading.Event();self.thread=None
    def start(self):
        self.thread=threading.Thread(target=self.run,daemon=True,name='business-worker');self.thread.start()
    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=45)
    def run(self):
        while not self.stop.is_set():
            try:self.tick()
            except Exception:pass # Individual operations retain evidence/state; never log tokens or raw replies.
            self.stop.wait(2)
    def tick(self):
        with self.engine.begin() as c:
            if not c.execute(text('SELECT pg_try_advisory_xact_lock(683273539)')).scalar_one():return
        # Dispatch machinery prevents two workers claiming the same mutation.
        operations.dispatch_once(self.engine,'business-worker',{(kind,VERSION):self.dispatch for kind in CONTRACTS},allowed_kinds=list(CONTRACTS),lease_seconds=180)
        with self.engine.connect() as c:
            pending=c.execute(text("SELECT o.id FROM external_operations o JOIN business_links l ON l.operation_id=o.id WHERE (o.state='submitted' OR (o.state='unknown' AND o.external_id IS NOT NULL)) AND NOT l.applied ORDER BY o.updated_at LIMIT 10")).scalars().all()
            syncs=c.execute(text("SELECT id FROM business_items WHERE kind='sync' AND state='processing' ORDER BY created_at LIMIT 1")).scalars().all()
        for id in pending:
            if self.stop.is_set():break
            try:self.reconcile(id)
            except Exception:
                with self.engine.begin() as c:c.execute(text("UPDATE external_operations SET reason='Ожидается подтверждение ЧЗ/НК. Проверьте доступ и квитанцию в личном кабинете.' WHERE id=:id AND state='submitted'"),{'id':id})
        for id in syncs:
            try:self.sync(id)
            except RemoteError as error:
                if error.status!=429:
                    with self.engine.begin() as c:
                        row=s.item(c,id);current=background(c,row['owner_id'],row['organization_id']);s.save(current,row,'sync_paused',state='blocked')
            except Exception:
                with self.engine.begin() as c:
                    row=s.item(c,id);current=background(c,row['owner_id'],row['organization_id']);s.save(current,row,'sync_paused',state='blocked')
        self.pipeline_tick()
    def pipeline_tick(self):
        with self.engine.begin() as c:
            c.execute(text('SELECT pg_advisory_xact_lock(683273529)'))
            row=c.execute(text("SELECT * FROM business_items WHERE kind='import' AND state='processing' AND body->>'pipeline'='true' ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED")).mappings().first()
            if not row:return
            row=dict(row);current=background(c,UUID(row['body']['pipeline_actor']),row['organization_id']);s.require(current,row,'nk.bulk_creation.execute');results=dict(row['body'].get('results',{}))
            for id in row['body']['card_ids']:
                if id in results:continue
                card=s.get(current,UUID(id),'card',True)
                ops=c.execute(text('SELECT o.state FROM external_operations o JOIN business_links l ON l.operation_id=o.id WHERE l.item_id=:id'),{'id':id}).scalars().all()
                if any(v in ('unknown','blocked','rejected') for v in ops):results[id]='Требуется разбор карточки/квитанции';continue
                if any(v in ('awaiting_signature','queued','dispatching','submitted') for v in ops):continue
                data=card['body'];statuses=data.get('external',{}).get('statuses',[])
                if 'errors' in statuses:results[id]='НК требует исправлений';continue
                if any(v in statuses for v in ('moderation','notsigned','published')):results[id]='Передано на модерацию';continue
                action='gtin' if not data['gtin'] else 'save' if data.get('dirty') or not data.get('external') else 'moderate'
                # A savepoint keeps one bad card from rolling back the other rows.
                try:
                    with c.begin_nested():perform_action(current,UUID(id),action,Action(revision=card['revision'],reason='Подтверждённое массовое создание из Excel'),self.settings)
                except RemoteError as error:
                    if error.status==429:break
                    results[id]='Не пройдена проверка доступа или схемы НК'
                except Exception:results[id]='Не заполнены обязательные поля или не пройдена проверка НК'
                break
            done=len(results)==len(row['body']['card_ids'])
            s.save(current,row,'pipeline_progress',body={**row['body'],'results':results},state='done' if done else 'processing')

    def config(self,c,org):
        config=load(c,self.settings,org);inn=c.execute(text('SELECT inn FROM organizations WHERE id=:id'),{'id':org}).scalar_one();return config,inn
    def dispatch(self,ticket):
        with self.engine.begin() as c:
            link=c.execute(text('SELECT * FROM business_links WHERE operation_id=:id'),{'id':ticket['id']}).mappings().one();row=s.item(c,link['item_id']);config,inn=self.config(c,row['organization_id'])
            if not config.get('writes_enabled') or row['state']=='archived':raise operations.DispatchBlocked('Отправка отключена')
            if row['source_id'] and row['kind']=='marking':
                source=c.execute(text('SELECT state,revision FROM bm_documents WHERE id=:id'),{'id':row['source_id']}).mappings().one()
                if source['state']=='archived' or source['revision']!=row['body']['source_revision']:raise operations.DispatchBlocked('Исходная накладная остановлена/изменена')
        step=ticket['kind'];payload=json.loads(ticket['payload']) if step!='nk.publish' else None;external=None;result=None;nk=Client(config);live=Live(config,inn) if not step.startswith('nk.') else None
        required=('nk_key',) if step.startswith('nk.') else ('true_token',) if step in ('bm.atk','bm.transform') else ('oms_token','true_token') if step.endswith('.report') else ('oms_token',)
        if any(not config.get(key) for key in required):raise operations.DispatchBlocked('Настройте действующие токены интеграции')
        if step=='nk.gtin':
            result=nk.call('GET','/v3/generate-gtins',{'quantity':1});drafts=result.get('drafts',[])
            if len(drafts)!=1 or not gs1.valid_gtin(str(drafts[0].get('gtin',''))):raise ValueError('Неоднозначный результат выделения GTIN')
            external=str(drafts[0]['gtin'])
        elif step=='nk.save':
            entry=payload['entry']
            if payload.get('base') and nk.details(good_id=entry['good_id'])!=payload['base']:raise ValueError('Карточка НК изменилась после подготовки')
            result=nk.call('POST','/v3/feed',body=entry);external=str(result['feed_id'])
        elif step=='nk.moderate':
            raw=nk.details(good_id=payload['good_id'])
            if not any(v in raw.get('good_detailed_status',[]) for v in ('draft','errors')):raise ValueError('Карточка не допускает модерацию')
            result=nk.call('GET','/v3/feed-moderation',{'good_id':payload['good_id']})
            if result.get('error'):raise ValueError('НК вернул ошибку модерации')
            external=str(payload['good_id'])
        elif step=='nk.document':
            raw=nk.details(good_id=payload['good_id'])
            if 'notsigned' not in raw.get('good_detailed_status',[]):raise ValueError('Карточка не ожидает подписания')
            result=nk.call('POST','/v3/feed-product-document',body={'goodIds':[payload['good_id']],'publicationAgreement':payload['publicationAgreement']});xmls=result.get('xmls',[])
            if result.get('errors') or len(xmls)!=1 or str(xmls[0].get('goodId'))!=str(payload['good_id']):raise ValueError('НК не выдал однозначный документ публикации')
            external=str(payload['good_id'])
        elif step=='nk.publish':
            result=nk.call('POST','/v3/feed-product-sign-pkcs',body=[{'goodId':link['metadata']['good_id'],'base64Xml':base64.b64encode(ticket['payload']).decode(),'signature':base64.b64encode(ticket['signature']).decode()}])
            if str(link['metadata']['good_id']) not in set(map(str,result.get('signed',[]))):raise ValueError('НК не подтвердил принятие подписи')
            external=str(link['metadata']['good_id'])
        elif step.endswith('.order'):
            result=live.oms('order',body=ticket['payload'],signature=ticket['signature']);external=live.external_id(result.get('orderId'))
        elif step.endswith('.fetch'):
            # Consuming GET remains a durable mutation. Never automatically retry it.
            result=live.oms('codes',params=payload);external=live.external_id(result.get('blockId'))
            if not isinstance(result.get('codes'),list) or not result['codes']:raise ValueError('Пустой блок КМ')
            for raw in result['codes']:gs1.parse(raw.encode())
        elif step.endswith('.report'):
            codes=[{'cis':gs1.parse(x.split('\x1e')[0].encode()).cis,'gtin':gs1.parse(x.split('\x1e')[0].encode()).gtin} for x in payload['sntins']];live.verify(codes,'EMITTED')
            result=live.oms('utilisation',body=ticket['payload'],signature=ticket['signature']);external=live.external_id(result.get('reportId'))
        elif step in ('bm.atk','bm.transform'):
            checked=live.verify([{'cis':p['ki'],'gtin':p['ki'][2:16]} for p in payload['products_list']],'APPLIED',parent=payload.get('atk') if payload.get('transformation_type')=='REMOVING' else None)
            if step=='bm.transform' and payload.get('transformation_type')=='ADDING' and any(r.get('parent') for r in checked.values()):raise operations.DispatchBlocked('Добавляемые КМ уже входят в другой АТК')
            body={'document_format':'MANUAL','product_document':base64.b64encode(ticket['payload']).decode(),'type':'ATK_AGGREGATION' if step=='bm.atk' else 'ATK_TRANSFORMATION','signature':base64.b64encode(ticket['signature']).decode()}
            result=live.call('POST','/api/v3/true-api/lk/documents/create',{'pg':'radio'},json.dumps(body).encode());external=live.external_id(result)
        else:raise ValueError('Нет адаптера операции')
        evidence=json.dumps({'result':result},ensure_ascii=False).encode()
        return operations.Result(evidence,'submitted',ticket['payload_sha256'],str(ticket['organization_id']),external)
    def reconcile(self,id):
        with self.engine.connect() as c:
            op=dict(c.execute(text('SELECT * FROM external_operations WHERE id=:id'),{'id':id}).mappings().one());link=dict(c.execute(text('SELECT * FROM business_links WHERE operation_id=:id'),{'id':id}).mappings().one());row=s.item(c,link['item_id']);config,inn=self.config(c,row['organization_id']);evidence=c.execute(text("SELECT raw FROM operation_receipts WHERE operation_id=:id AND phase='dispatch' AND applied ORDER BY id DESC LIMIT 1"),{'id':id}).scalar_one();initial=json.loads(bytes(evidence))['result']
        step=op['kind'];payload=json.loads(bytes(op['payload'])) if step!='nk.publish' else None;nk=Client(config);live=Live(config,inn) if not step.startswith('nk.') else None;result=None;rejected=False
        if step in ('nk.gtin','nk.document') or step.endswith('.fetch'):result=initial # Synchronous authoritative response, already durably stored.
        elif step=='nk.save':
            state=nk.call('GET','/v3/feed-status',{'feed_id':op['external_id'],'verbose':'true'})
            if state.get('status')=='Processing':return
            if state.get('status')=='Rejected' or state.get('error_details'):result=state;rejected=True
            else:
                raw=nk.details(gtin=payload['entry'].get('gtin'),good_id=payload['entry'].get('good_id'));entry=payload['entry'];actual={str(a['attr_id']):str(a.get('attr_value','')) for a in raw.get('good_attrs',[])}
                units={str(a['attr_id']):str(a.get('attr_value_type') or '') for a in raw.get('good_attrs',[])}
                if any(actual.get(str(a['attr_id']))!=str(a['attr_value']) or ('attr_value_type' in a and units.get(str(a['attr_id']))!=a['attr_value_type']) for a in entry.get('good_attrs',[])) or ('good_name' in entry and raw.get('good_name')!=entry['good_name']):return
                result=raw
        elif step in ('nk.moderate','nk.publish'):
            result=nk.details(good_id=op['external_id']);states=result.get('good_detailed_status',[])
            if step=='nk.publish' and not ('published' in states and result.get('good_signed') is True):return
            if step=='nk.moderate' and not any(v in states for v in ('moderation','errors','notsigned','published')):return
        elif step.endswith('.order'):
            result=live.oms('order/status',params={'orderId':op['external_id']})
            if not isinstance(result,list) or not result:return
            rejected=any(r.get('bufferStatus')=='REJECTED' for r in result)
            expected=sum(p['quantity'] for p in payload['products'])
            if not rejected and sum(int(r.get('availableCodes',0)) for r in result)<expected:return
            if not rejected and not all(r.get('bufferStatus') in ('ACTIVE','EXHAUSTED') for r in result):return
        elif step.endswith('.report'):
            result=live.oms('report/info',params={'reportId':op['external_id']})
            if result.get('reportId')!=op['external_id']:raise ValueError('Ответ другого отчёта')
            rejected=result.get('reportStatus')=='REJECTED'
            if not rejected:
                if result.get('reportStatus')!='SUCCESS':return
                receipt=live.oms('receipts/receipt',params={'resultDocId':op['external_id']});results=receipt.get('results',[])
                if not results or any(r.get('resultDocId')!=op['external_id'] or r.get('state')!='SUCCESS' or r.get('code')!=0 for r in results):return
                codes=[{'cis':gs1.parse(x.split('\x1e')[0].encode()).cis,'gtin':gs1.parse(x.split('\x1e')[0].encode()).gtin} for x in payload['sntins']];live.verify(codes,'APPLIED');result={'report':result,'receipt':receipt}
        elif step in ('bm.atk','bm.transform'):
            result=live.call('GET','/api/v4/true-api/doc/'+op['external_id']+'/info',{'pg':'radio','body':'true','content':'true'})
            if not isinstance(result,list) or len(result)!=1 or result[0].get('number')!=op['external_id']:return
            d=result[0];rejected=bool(d.get('errors') or d.get('commonErrors') or d.get('status') in ('CHECKED_NOT_OK','PARSE_ERROR'))
            if not rejected and d.get('status')!='CHECKED_OK':return
            if not rejected:
                registry=live.verify([{'cis':p['ki'],'gtin':p['ki'][2:16]} for p in payload['products_list']],'APPLIED')
                if step=='bm.transform':
                    expected=payload['atk'] if payload['transformation_type']=='ADDING' else None
                    if any((r.get('parent') or None)!=expected for r in registry.values()):return
                result={'document':result,'registry':registry}
        else:return
        outcome=operations.Result(json.dumps({'result':result},ensure_ascii=False).encode(),'rejected' if rejected else 'confirmed',op['payload_sha256'],str(op['organization_id']),op['external_id'])
        with self.engine.begin() as c:
            c.execute(text('SELECT pg_advisory_xact_lock(683273529)'))
            if not operations.reconcile(c,id,outcome,expected_fence=op['fence']):return
            latest=s.item(c,row['id'],lock=True);current=background(c,op['actor_id'],op['organization_id'],include_disabled=True)
            if rejected:s.save(current,latest,'remote_rejected',body={**latest['body'],'last_rejected_step':step},state='rejected')
            elif step=='nk.gtin':
                gtin=str(initial['drafts'][0]['gtin'])
                if latest['body'].get('gtin') not in ('',gtin):raise ValueError('Поле GTIN изменилось')
                s.save(current,latest,'gtin_allocated',body={**latest['body'],'gtin':gtin,'dirty':True},state='draft')
            elif step in ('nk.save','nk.moderate','nk.publish'):
                data=incoming(result);s.save(current,latest,'nk_confirmed',body=data,state='active')
                if gs1.valid_gtin(data['gtin']) and data['pn'] and len(data['tnved'])==10:
                    from ..registry import register_product
                    register_product(c,op['organization_id'],data['gtin'],data['pn'],data['tnved'],source='nk_confirmed',source_ref='nk:'+str(result['good_id']),evidence_sha256=sha256(outcome.raw).hexdigest())
            elif step=='nk.document':
                xml=initial['xmls'][0]['xml'].encode();s.queue(current,latest,'nk.publish',xml,{'good_id':payload['good_id']})
            else:
                from .marking import apply
                apply(current,latest,op,link,payload,result,live,self.settings)
            if latest['state']=='archived':c.execute(text("UPDATE business_items SET state='archived' WHERE id=:id"),{'id':latest['id']})
            c.execute(text('UPDATE business_links SET applied=true WHERE operation_id=:id'),{'id':id})
    def sync(self,id):
        with self.engine.connect() as c:
            row=s.item(c,id);config,_=self.config(c,row['organization_id']);body=row['body']
        client=Client(config);listing=client.call('GET','/v4/product-list',{'from_date':body['from'],'to_date':body['to'],'offset':body['offset'],'limit':25});goods=listing.get('goods',[])
        if not isinstance(goods,list) or body['offset']>=10000:raise ValueError('Каталог больше окна; требуется разбить период синхронизации')
        details=client.call('GET','/v3/feed-product',{'good_ids':';'.join(str(r['good_id']) for r in goods)}) if goods else []
        if not isinstance(details,list) or {str(r['good_id']) for r in details}!={str(r['good_id']) for r in goods}:raise ValueError('Не полный ответ НК')
        with self.engine.begin() as c:
            c.execute(text('SELECT pg_advisory_xact_lock(683273529)'));row=s.item(c,id,lock=True)
            if row['body']!=body or row['state']!='processing':return
            current=background(c,row['owner_id'],row['organization_id']);current.require('nk.sync.execute',organization_id=row['organization_id'],owner_id=row['owner_id'])
            for raw in details:apply_remote(current,raw)
            count=body['count']+len(goods);offset=body['offset']+len(goods);done=not goods or offset>=int(listing.get('total',offset))
            s.save(current,row,'sync_progress',body={**body,'count':count,'offset':offset},state='done' if done else 'processing')
