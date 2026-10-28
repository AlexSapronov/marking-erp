"""Synthetic upstream HTTP boundary for the ERP acceptance chain; never imported by runtime."""
import base64,json
from copy import deepcopy
from uuid import uuid4
import httpx
from sqlalchemy import text
from marking_erp import operations,signer,gs1
from marking_erp.business.worker import BusinessWorker
from marking_erp.business.contracts import CONTRACTS,VERSION
from test_auth_postgres import client,login
from test_business_postgres import connections,card
from test_signer_postgres import claim,result
from test_gs1 import sample

GTIN='04640638349704'
PN='00123-A'
ATK='1'*25


class SyntheticServices:
    def __init__(self):
        self.calls=[];self.orders={};self.reports={};self.documents={};self.codes={};self.good=None
        self.reject_report_once=True;self.lose_order_reply=False;self.hold_introduced=False
    def __call__(self,request):
        path=request.url.path;method=request.method;query=dict(request.url.params)
        body=json.loads(request.content) if request.content else None
        self.calls.append((method,path))
        if request.url.host.startswith('xn--') or 'каталог' in request.url.host or 'nk.sandbox' in request.url.host:
            value=self.nk(method,path,query,body)
            return httpx.Response(200,json={'result':value})
        assert request.url.host in ('markirovka.crpt.ru','suzgrid.crpt.ru'),request.url
        value=self.live(method,path,query,body,request)
        return httpx.Response(200,json=value)
    def nk(self,method,path,q,b):
        if path=='/v3/attributes':return [{'attr_id':13914,'attr_name':'Модель / PN','attr_type':'m','attr_field_type':'text'}]
        if path=='/v3/generate-gtins':return {'drafts':[{'gtin':GTIN}]}
        if path=='/v3/feed':
            self.good={**deepcopy(b),'good_id':123,'brand_name':b['brand'],'categories':[{'cat_id':1}],'good_detailed_status':['draft'],'good_signed':False}
            return {'feed_id':'synthetic-feed'}
        if path=='/v3/feed-status':return {'status':'Processed'}
        if path=='/v3/feed-product':return [deepcopy(self.good)]
        if path=='/v3/feed-moderation':self.good['good_detailed_status']=['notsigned'];return {'accepted':True}
        if path=='/v3/feed-product-document':return {'xmls':[{'goodId':123,'xml':'<SyntheticProduct id="123" />'}]}
        if path=='/v3/feed-product-sign-pkcs':
            assert b[0]['signature'];self.good.update(good_detailed_status=['published'],good_signed=True)
            return {'signed':[123]}
        raise AssertionError(('Unexpected NK boundary',method,path))
    def introduce(self):
        for code in self.codes.values():
            if code['status']=='APPLIED':code['status']='INTRODUCED'
    def live(self,method,path,q,b,request):
        if path=='/api/v3/order':
            assert request.headers.get('X-Signature');id=str(uuid4());self.orders[id]=b
            if self.lose_order_reply:raise httpx.ReadError('Synthetic lost reply after acceptance',request=request)
            return {'orderId':id}
        if path=='/api/v3/order/status':
            order=self.orders[q['orderId']];return [{'bufferStatus':'ACTIVE','availableCodes':sum(v['quantity'] for v in order['products'])}]
        if path=='/api/v3/codes':
            order=self.orders[q['orderId']];values=[]
            for _ in range(int(q['quantity'])):
                raw=sample(37,serial=(str(len(self.codes)+1).zfill(20)).encode()).decode();parsed=gs1.parse(raw.encode())
                self.codes[parsed.cis]={'cis':parsed.cis,'gtin':GTIN,'ownerInn':'7700000000','status':'EMITTED','statusEx':'EMPTY','packageType':'UNIT','quantityInPack':1,'emissionType':'REMARK' if order['attributes']['releaseMethodType']=='REMARK' else 'FOREIGN'}
                values.append(raw)
            return {'blockId':str(uuid4()),'codes':values}
        if path=='/api/v3/utilisation':
            assert request.headers.get('X-Signature');id=str(uuid4());reject=self.reject_report_once;self.reject_report_once=False
            self.reports[id]={'payload':b,'rejected':reject}
            if not reject:
                for raw in b['sntins']:
                    code=gs1.parse(raw.split('\x1e')[0].encode());self.codes[code.cis].update(status='APPLIED',quantityInPack=code.quantity or 1)
            return {'reportId':id}
        if path=='/api/v3/report/info':
            return {'reportId':q['reportId'],'reportStatus':'REJECTED' if self.reports[q['reportId']]['rejected'] else 'SUCCESS','errors':[{'code':1000,'message':'Synthetic rejection'}] if self.reports[q['reportId']]['rejected'] else []}
        if path=='/api/v3/receipts/receipt':return {'results':[{'resultDocId':q['resultDocId'],'state':'SUCCESS','code':0}]}
        if path=='/api/v3/true-api/cises/info':return [{'cisInfo':{**deepcopy(self.codes[cis]),'requestedCis':cis}} for cis in b]
        if path=='/api/v3/true-api/lk/documents/create':
            assert b['signature'];payload=json.loads(base64.b64decode(b['product_document']));id=str(uuid4());kind=b['type']
            self.documents[id]=payload
            if kind=='ATK_AGGREGATION':
                for row in payload['products_list']:self.codes[row['ki']]['parent']=ATK
            elif kind=='LP_FTS_INTRODUCE':
                if not self.hold_introduced:self.introduce()
            elif kind=='LK_REMARK':
                for row in payload['products']:
                    self.codes[row['last_uin']].update(status='WRITTEN_OFF',statusEx='REMARK_RETIRED')
                    self.codes[row['new_uin']].update(status='INTRODUCED')
            else:raise AssertionError(('Unexpected document type',kind))
            return id
        if path.startswith('/api/v4/true-api/doc/') and path.endswith('/info'):
            id=path.split('/')[-2];return [{'number':id,'status':'CHECKED_OK','body':deepcopy(self.documents[id])}]
        raise AssertionError(('Unexpected True API boundary',method,path))


class Chain:
    def __init__(self,env,engine,certificate,remote):
        self.env=env;self.engine=engine;self.cert=certificate;self.remote=remote
        self.worker=BusinessWorker(engine,env[0]);self.signer_token=None
    def response(self,c,path,body=None,method='POST',status=200,**kw):
        r=c.request(method,path,**({'json':body} if body is not None else {}),**kw)
        assert r.status_code==status,(path,r.status_code,r.text)
        return r.json() if 'application/json' in r.headers.get('content-type','') else r.content
    def row(self,c,id):return self.response(c,'/api/business/'+str(id),method='GET')['item']
    def action(self,c,id,action,**values):
        row=self.row(c,id);return self.response(c,'/api/business/'+str(id)+'/'+action,{'revision':row['revision'],**values})
    def configure(self):
        with client(self.env) as c:login(c,'administrator');connections(c,self.env,writes_enabled=True,warehouse_live_checks=True,nk_key='synthetic-nk-only',true_token='synthetic-true-only',oms_token='synthetic-oms-only')
        with self.engine.begin() as c:
            from marking_erp.business.contracts import register
            register(c)
            _,self.signer_token=signer.provision(c,self.env[1],'Synthetic RSA acceptance signer',self.cert[0],[kind for kind,(_,signed) in CONTRACTS.items() if signed])
    def pump(self):
        for _ in range(30):
            progressed=False
            with client(self.env) as agent:
                while True:
                    job=claim(agent,self.signer_token)
                    if not job:break
                    payload=base64.b64decode(job['payload_base64'])
                    received=self.response(agent,'/api/signer/result',result(job,self.cert[1](payload)),headers={'Authorization':'Bearer '+self.signer_token})
                    assert received['verified'] is False
                    with self.engine.begin() as c:
                        assert signer.verify_received(c,job['job_id'],verifier=self.cert[2],verification_ref='Synthetic RSA exact-byte OpenSSL test; not GOST acceptance')
                    progressed=True
            adapters={(kind,VERSION):self.worker.dispatch for kind in CONTRACTS}
            while operations.dispatch_once(self.engine,'synthetic-acceptance',adapters,allowed_kinds=list(CONTRACTS)):progressed=True
            with self.engine.connect() as c:pending=list(c.execute(text("SELECT id FROM external_operations WHERE state='submitted' ORDER BY created_at,id")).scalars())
            for id in pending:self.worker.reconcile(id);progressed=True
            if not progressed:return
        raise AssertionError('Chain did not settle within 30 local passes')
    def publish(self,c):
        value=card(self.env,pn=PN,gtin='');id=self.response(c,'/api/catalog',value)['id']
        for action in ['validate','gtin','save','moderate','publish']:
            row=self.row(c,id)
            self.response(c,'/api/catalog/'+id+'/'+action,{'revision':row['revision'],'agreement':action=='publish'})
            self.pump()
        row=self.row(c,id);assert row['body']['external']['statuses']==['published'] and row['body']['external']['signed']
        return id
