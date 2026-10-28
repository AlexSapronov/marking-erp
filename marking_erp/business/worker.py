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
from .nk import Client,apply_remote,incoming,perform_action,Action,validate_card
from .errors import RemoteError,report_problem
from .catalog_sync import SyncProblem,progress,split_window
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
        from .catalog_schedule import tick as schedule_tick
        schedule_tick(self.engine,self.settings)
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
                if error.status!=429:self.pause_sync(id,str(error))
            except SyncProblem as error:self.pause_sync(id,str(error))
            except Exception:self.pause_sync(id,'Синхронизация остановлена. Проверьте доступ, структуру ответа НК и настройки подключения.')
        self.pipeline_tick()
        from .token_auth import refresh_tick
        refresh_tick(self.engine,self.settings)
    def pause_sync(self,id,message):
        with self.engine.begin() as c:
            c.execute(text('SELECT pg_advisory_xact_lock(683273529)'));row=s.item(c,id,lock=True)
            if row['state']!='processing':return
            current=background(c,UUID(row['body'].get('sync_actor',str(row['owner_id']))),row['organization_id'],include_disabled=True)
            s.save(current,row,'sync_paused',body={**row['body'],'error':message},state='blocked')
    def pipeline_tick(self):
        with self.engine.begin() as c:
            c.execute(text('SELECT pg_advisory_xact_lock(683273529)'))
            row=c.execute(text("SELECT * FROM business_items WHERE kind='import' AND state='processing' AND body->>'pipeline'='true' ORDER BY updated_at LIMIT 1 FOR UPDATE SKIP LOCKED")).mappings().first()
            if not row:return
            row=dict(row)
            try:
                current=background(c,UUID(row['body']['pipeline_actor']),row['organization_id']);s.require(current,row,'nk.bulk_creation.execute')
            except Exception:
                current=background(c,UUID(row['body']['pipeline_actor']),row['organization_id'],include_disabled=True)
                s.save(current,row,'pipeline_access_revoked',body={**row['body'],'pipeline':False,'error':'Доступ инициатора отозван. Уполномоченный сотрудник может продолжить партию.'},state='blocked');return
            results=dict(row['body'].get('results',{}));polls=dict(row['body'].get('polls',{}));processed=0
            batch=row['body'].get('policy',{}).get('values',{}).get('nk.bulk.batch_size',25);deadline=time.monotonic()+5
            for id in row['body']['card_ids']:
                if id in results:continue
                if processed>=batch or time.monotonic()>=deadline:break
                try:card=s.get(current,UUID(id),'card',True)
                except Exception:results[id]='Нет доступа к карточке';continue
                ops=c.execute(text('SELECT o.state FROM external_operations o JOIN business_links l ON l.operation_id=o.id WHERE l.item_id=:id ORDER BY o.created_at DESC,o.id DESC LIMIT 1'),{'id':id}).scalars().all()
                if any(v in ('unknown','blocked','rejected') for v in ops):results[id]='Требуется разбор карточки/квитанции';continue
                if any(v in ('awaiting_signature','queued','dispatching','submitted') for v in ops):continue
                data=card['body'];statuses=data.get('external',{}).get('statuses',[])
                if 'errors' in statuses:results[id]='НК требует исправлений';continue
                if not data.get('dirty') and 'published' in statuses:results[id]='Опубликовано в НК';continue
                if not data.get('dirty') and not row['body'].get('publish') and any(v in statuses for v in ('moderation','notsigned')):results[id]='Передано на модерацию';continue
                if not data.get('dirty') and row['body'].get('publish') and 'moderation' in statuses:
                    if time.time()-polls.get(id,0)<60:continue
                    action='refresh';polls[id]=time.time()
                elif not data.get('dirty') and row['body'].get('publish') and 'notsigned' in statuses:action='publish'
                else:action='gtin' if not data['gtin'] else 'save' if data.get('dirty') or not data.get('external') else 'moderate'
                # A savepoint keeps one bad card from rolling back the other rows.
                try:
                    with c.begin_nested():
                        if action=='gtin':validate_card(data,load(c,self.settings,row['organization_id']))
                        perform_action(current,UUID(id),action,Action(revision=card['revision'],agreement=bool(row['body'].get('publish')),reason=row['body'].get('duplicate_reason','')),self.settings)
                except RemoteError as error:
                    if error.status==429:break
                    results[id]='Не пройдена проверка доступа или схемы НК'
                except Exception:results[id]='Не заполнены обязательные поля или не пройдена проверка НК'
                processed+=1
            done=len(results)==len(row['body']['card_ids'])
            successful=sum(v in ('Опубликовано в НК','Передано на модерацию') for v in results.values())
            if not processed and results==row['body'].get('results',{}) and polls==row['body'].get('polls',{}) and not done:
                c.execute(text('UPDATE business_items SET updated_at=now() WHERE id=:id'),{'id':row['id']});return
            counts={'successful':successful,'errors':len(results)-successful,'pending':len(row['body']['card_ids'])-len(results)}
            s.save(current,row,'pipeline_progress',body={**row['body'],'results':results,'polls':polls,'pipeline_counts':counts},state=('blocked' if counts['errors'] else 'done') if done else 'processing')

    def config(self,c,org):
        config=load(c,self.settings,org);inn=c.execute(text('SELECT inn FROM organizations WHERE id=:id'),{'id':org}).scalar_one();return config,inn
    def dispatch(self,ticket):
        with self.engine.begin() as c:
            link=c.execute(text('SELECT * FROM business_links WHERE operation_id=:id'),{'id':ticket['id']}).mappings().one();row=s.item(c,link['item_id']);config,inn=self.config(c,row['organization_id'])
            from ..incidents import enforce_business
            try:enforce_business(c,row,ticket['kind'])
            except Exception as error:
                from fastapi import HTTPException
                if not isinstance(error,HTTPException):raise
                raise operations.DispatchBlocked('Локальный карантин запрещает эту отправку; сначала разберите инцидент') from None
            if not config.get('writes_enabled') or row['state']=='archived':raise operations.DispatchBlocked('Отправка отключена')
            if row['source_id'] and row['kind']=='marking':
                source=c.execute(text('SELECT state,revision FROM bm_documents WHERE id=:id'),{'id':row['source_id']}).mappings().one()
                if source['state']=='archived' or source['revision']!=row['body']['source_revision']:raise operations.DispatchBlocked('Исходная накладная остановлена/изменена')
        step=ticket['kind'];payload=json.loads(ticket['payload']) if step!='nk.publish' else None;external=None;result=None;nk=Client(config);live=Live(config,inn) if not step.startswith('nk.') else None
        required=('nk_key',) if step.startswith('nk.') else ('true_token',) if step in ('bm.atk','bm.transform','fts.submit','remark.submit') else ('oms_token','true_token') if step.endswith('.report') else ('oms_token',)
        if any(not config.get(key) for key in required):raise operations.DispatchBlocked('Настройте действующие токены интеграции')
        if step=='nk.gtin':
            result=nk.call('GET','/v3/generate-gtins',{'quantity':1});drafts=result.get('drafts',[])
            if len(drafts)!=1 or not gs1.valid_gtin(str(drafts[0].get('gtin',''))):raise ValueError('Неоднозначный результат выделения GTIN')
            external=str(drafts[0]['gtin'])
        elif step=='nk.save':
            entry=payload['entry']
            if payload.get('base'):
                try:
                    if nk.details(good_id=entry['good_id'])!=payload['base']:raise ValueError('changed')
                except Exception:raise operations.DispatchBlocked('Сначала сверьте карточку НК: она изменилась или недоступна') from None
            result=nk.call('POST','/v3/feed',body=entry);external=str(result['feed_id'])
        elif step=='nk.moderate':
            try:
                raw=nk.details(good_id=payload['good_id'])
                if not any(v in raw.get('good_detailed_status',[]) for v in ('draft','errors')):raise ValueError('state')
            except Exception:raise operations.DispatchBlocked('НК не подтвердил возможность модерации') from None
            result=nk.call('GET','/v3/feed-moderation',{'good_id':payload['good_id']})
            if result.get('error'):raise ValueError('НК вернул ошибку модерации')
            external=str(payload['good_id'])
        elif step=='nk.document':
            try:
                raw=nk.details(good_id=payload['good_id'])
                if 'notsigned' not in raw.get('good_detailed_status',[]):raise ValueError('state')
            except Exception:raise operations.DispatchBlocked('НК не подтвердил готовность к подписанию') from None
            result=nk.call('POST','/v3/feed-product-document',body={'goodIds':[payload['good_id']],'publicationAgreement':payload['publicationAgreement']});xmls=result.get('xmls',[])
            if result.get('errors') or len(xmls)!=1 or str(xmls[0].get('goodId'))!=str(payload['good_id']):raise ValueError('НК не выдал однозначный документ публикации')
            external=str(payload['good_id'])
        elif step=='nk.publish':
            result=nk.call('POST','/v3/feed-product-sign-pkcs',body=[{'goodId':link['metadata']['good_id'],'base64Xml':base64.b64encode(ticket['payload']).decode(),'signature':base64.b64encode(ticket['signature']).decode()}])
            if str(link['metadata']['good_id']) not in set(map(str,result.get('signed',[]))):raise ValueError('НК не подтвердил принятие подписи')
            external=str(link['metadata']['good_id'])
        elif step.endswith('.order'):
            if step=='split.order':
                from .split_eligibility import check
                try:check(nk,row['body']['gtin'])
                except Exception:raise operations.DispatchBlocked('НК не подтвердил характеристику количества перед эмиссией разделения') from None
            result=live.oms('order',body=ticket['payload'],signature=ticket['signature']);external=live.external_id(result.get('orderId'))
        elif step.endswith('.fetch'):
            # Consuming GET remains a durable mutation. Never automatically retry it.
            result=live.oms('codes',params=payload);external=live.external_id(result.get('blockId'))
            if not isinstance(result.get('codes'),list) or not result['codes']:raise ValueError('Пустой блок КМ')
            for raw in result['codes']:gs1.parse(raw.encode())
        elif step.endswith('.report'):
            codes=[{'cis':gs1.parse(x.split('\x1e')[0].encode()).cis,'gtin':gs1.parse(x.split('\x1e')[0].encode()).gtin} for x in payload['sntins']];live.verify(codes,'EMITTED')
            result=live.oms('utilisation',body=ticket['payload'],signature=ticket['signature']);external=live.external_id(result.get('reportId'))
        elif step in ('fts.submit','remark.submit'):
            try:
                if step=='fts.submit':
                    from .fts import validate_import
                    validate_import(live,row)
                else:
                    from .remarking import validate_dispatch
                    validate_dispatch(live,payload)
            except (RemoteError,ValueError):raise operations.DispatchBlocked('Не подтверждены условия операции в ЧЗ') from None
            except Exception:raise operations.DispatchBlocked('Условия документа изменились; требуется сверка до отправки') from None
            envelope={'document_format':'MANUAL','product_document':base64.b64encode(ticket['payload']).decode(),'type':'LP_FTS_INTRODUCE' if step=='fts.submit' else 'LK_REMARK','signature':base64.b64encode(ticket['signature']).decode()}
            result=live.call('POST','/api/v3/true-api/lk/documents/create',{'pg':'radio'},json.dumps(envelope).encode());external=live.external_id(result)
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
        elif step in ('bm.atk','bm.transform','fts.submit','remark.submit'):
            result=live.call('GET','/api/v4/true-api/doc/'+op['external_id']+'/info',{'pg':'radio','body':'true','content':'true'})
            if not isinstance(result,list) or len(result)!=1 or result[0].get('number')!=op['external_id']:return
            d=result[0];rejected=bool(d.get('errors') or d.get('commonErrors') or d.get('status') in ('CHECKED_NOT_OK','PARSE_ERROR'))
            if not rejected and d.get('status')!='CHECKED_OK':return
            if not rejected and step=='fts.submit':
                from .fts import strings
                contents=strings(result)
                if row['body']['declaration_number'] not in contents:return
                expected={r['cis'] for r in row['body']['codes']}
                if not expected<={v[:38] for v in contents} and not set(row['body']['atk_codes'])<=contents:return
                registry=live.verify(row['body']['codes'],'INTRODUCED')
                if any(registry[v['cis']].get('parent')!=v['atk'] for v in row['body']['codes']):return
                result={'document':result,'registry':registry}
            elif not rejected and step=='remark.submit':
                from .remarking import verify_result
                if not verify_result(live,payload,row['body']['quantity']):return
                result={'document':result,'registry':live.info([p[k] for p in payload['products'] for k in ('last_uin','new_uin')])}
            elif not rejected:
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
            if rejected:
                data={**latest['body'],'last_rejected_step':step}
                if step.endswith('.report'):
                    code,message=report_problem(result);data.update(last_rejection_code=code,last_rejection_message=message)
                    c.execute(text('UPDATE external_operations SET reason=:reason WHERE id=:id'),{'id':id,'reason':message})
                s.save(current,latest,'remote_rejected',body=data,state='rejected')
            elif step=='nk.gtin':
                gtin=str(initial['drafts'][0]['gtin'])
                if latest['body'].get('gtin') not in ('',gtin):raise ValueError('Поле GTIN изменилось')
                s.save(current,latest,'gtin_allocated',body={**latest['body'],'gtin':gtin,'dirty':True},state='draft')
                data=latest['body']
                if data.get('pn') and len(data.get('tnved',''))==10:
                    from ..registry import register_product
                    register_product(c,op['organization_id'],gtin,data['pn'],data['tnved'],source='nk_confirmed',source_ref='nk-allocation:'+str(op['id']),evidence_sha256=sha256(outcome.raw).hexdigest())
            elif step in ('nk.save','nk.moderate','nk.publish'):
                data=incoming(result);s.save(current,latest,'nk_confirmed',body=data,state='active')
                if gs1.valid_gtin(data['gtin']) and data['pn'] and len(data['tnved'])==10:
                    from ..registry import register_product
                    register_product(c,op['organization_id'],data['gtin'],data['pn'],data['tnved'],source='nk_confirmed',source_ref='nk:'+str(result['good_id']),evidence_sha256=sha256(outcome.raw).hexdigest())
            elif step=='nk.document':
                xml=initial['xmls'][0]['xml'].encode();s.queue(current,latest,'nk.publish',xml,{'good_id':payload['good_id']})
            elif step=='fts.submit':
                s.artifact(current,latest,'fts_receipt','FTS-receipt.json',outcome.raw,'application/json',{'external_document_id':op['external_id']})
                s.save(current,latest,'circulation_confirmed',body={**latest['body'],'external_document_id':op['external_id'],'verified_at':datetime.now(timezone.utc).isoformat(),'verification':'signed_document_and_codes_introduced'},state='circulation_confirmed')
            elif step=='remark.submit':
                new_id=c.execute(text('SELECT id FROM business_codes WHERE item_id=:id AND cis=:cis'),{'id':latest['id'],'cis':latest['body']['new_cis']}).scalar_one()
                c.execute(text('INSERT INTO code_replacements(old_code_id,new_code_id,remark_id,external_document_id) VALUES (:old,:new,:remark,:external)'),{'old':latest['body']['old_code_id'],'new':new_id,'remark':latest['id'],'external':op['external_id']})
                c.execute(text("UPDATE business_codes SET state='withdrawn' WHERE id=:id AND organization_id=:org"),{'id':latest['body']['old_code_id'],'org':op['organization_id']})
                s.artifact(current,latest,'remark_receipt','remark-receipt.json',outcome.raw,'application/json',{'external_document_id':op['external_id'],'old_cis':latest['body']['old_cis'],'new_cis':latest['body']['new_cis']})
                s.save(current,latest,'official_remark_confirmed',body={**latest['body'],'external_document_id':op['external_id'],'verified_at':datetime.now(timezone.utc).isoformat()},state='done')
            else:
                from .marking import apply
                apply(current,latest,op,link,payload,result,live,self.settings)
            if latest['state']=='archived':c.execute(text("UPDATE business_items SET state='archived' WHERE id=:id"),{'id':latest['id']})
            c.execute(text('UPDATE business_links SET applied=true WHERE operation_id=:id'),{'id':id})
    def sync(self,id):
        with self.engine.connect() as c:
            row=s.item(c,id)
            if row['state']!='processing':return
            current=background(c,UUID(row['body'].get('sync_actor',str(row['owner_id']))),row['organization_id']);current.require('nk.sync.execute',organization_id=row['organization_id'],owner_id=row['owner_id'])
            config,_=self.config(c,row['organization_id']);original=row['body'];body=progress(original)
        if not body['windows']:return
        start,end,offset=body['windows'][0];client=Client(config)
        batch=min(25,body.get('policy',{}).get('values',{}).get('nk.bulk.batch_size',25))
        try:listing=client.call('GET','/v4/product-list',{'from_date':start,'to_date':end,'offset':offset,'limit':batch})
        except RemoteError as error:
            if error.status!=413:raise
            return self.save_sync_progress(id,original,split_window(body),[])
        if not isinstance(listing,dict):raise SyncProblem('НК не вернул список карточек. Обновление остановлено без отметки об успехе.')
        goods=listing.get('goods');total=listing.get('total')
        if not isinstance(goods,list) or len(goods)>batch or isinstance(total,bool) or not str(total).isdigit():raise SyncProblem('НК не подтвердил полный состав и размер каталога.')
        total=int(total)
        if total>10000 or offset>=10000:return self.save_sync_progress(id,original,split_window(body),[])
        if any(not isinstance(r,dict) or not str(r.get('good_id','')).isdigit() for r in goods):raise SyncProblem('НК вернул карточку без подтверждённого идентификатора.')
        if len({str(r['good_id']) for r in goods})!=len(goods) or offset+len(goods)>total or (not goods and offset<total):raise SyncProblem('НК вернул неполную или повторяющуюся страницу. Синхронизация остановлена.')
        details=client.call('GET','/v3/feed-product',{'good_ids':';'.join(str(r['good_id']) for r in goods)}) if goods else []
        if not isinstance(details,list) or len(details)!=len(goods) or {str(r['good_id']) for r in details}!={str(r['good_id']) for r in goods}:raise SyncProblem('НК не вернул полный состав запрошенных карточек. Проверьте права и статусы.')
        if offset+len(goods)>=total:body['windows'].pop(0)
        else:body['windows'][0][2]+=len(goods)
        self.save_sync_progress(id,original,body,details)
    def save_sync_progress(self,id,original,body,details):
        with self.engine.begin() as c:
            c.execute(text('SELECT pg_advisory_xact_lock(683273529)'));row=s.item(c,id,lock=True)
            if row['body']!=original or row['state']!='processing':return
            current=background(c,UUID(row['body'].get('sync_actor',str(row['owner_id']))),row['organization_id']);current.require('nk.sync.execute',organization_id=row['organization_id'],owner_id=row['owner_id'])
            count=body['count']
            for raw in details:
                apply_remote(current,raw)
                added=c.execute(text('INSERT INTO catalog_sync_entries(item_id,good_id) VALUES (:id,:good) ON CONFLICT DO NOTHING RETURNING good_id'),{'id':id,'good':str(raw['good_id'])}).scalar()
                if added is not None:count+=1
            s.save(current,row,'sync_progress',body={**body,'count':count,'offset':body['windows'][0][2] if body['windows'] else 0,'error':''},state='processing' if body['windows'] else 'done')
