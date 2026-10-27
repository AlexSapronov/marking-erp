"""Live HTTP only. No mock modes, invented statuses, or replay of unknown mutations."""
import base64,json
from uuid import UUID
import httpx
from .. import gs1
from .errors import RemoteError

BASES={'production':('https://markirovka.crpt.ru','https://suzgrid.crpt.ru'),'sandbox':('https://demo.lp.crpt.ru','https://suz.sandbox.crptech.ru')}
class Live:
    def __init__(self,config,inn):
        if config.get('environment')!='production':raise RemoteError('Адреса тестового контура ЧЗ/СУЗ требуют отдельного подтверждения; НК sandbox доступен отдельно')
        self.config=config;self.inn=inn
    def call(self,method,path,params=None,body=None,oms=False,signature=None,timeout=40):
        token=self.config.get('oms_token' if oms else 'true_token')
        if not token:raise RemoteError('Настройте действующий токен '+('СУЗ' if oms else 'ЧЗ'))
        headers={'Accept':'application/json',('clientToken' if oms else 'Authorization'):token if oms else 'Bearer '+token}
        if body is not None:headers['Content-Type']='application/json;charset=UTF-8'
        if signature:headers['X-Signature']=base64.b64encode(signature).decode()
        if oms:params={'omsId':self.config.get('oms_id',''),**(params or {})}
        try:
            with httpx.Client(timeout=httpx.Timeout(timeout,connect=min(8,timeout)),follow_redirects=False,trust_env=False) as c:r=c.request(method,BASES[self.config['environment']][1 if oms else 0]+path,params=params,content=body,headers=headers)
        except httpx.HTTPError:raise RemoteError('Нет подтверждения от ЧЗ/СУЗ; требуется сверка') from None
        try:data=r.json()
        except ValueError:data=r.text.strip()
        if r.status_code not in (200,201):
            # Do not expose remote text, credentials or complete codes in browser errors.
            message=json.dumps(data,ensure_ascii=False).casefold()
            if 'не найдена карточка' in message or 'не найдена карточка товара' in message:raise RemoteError('Карточка в НК ещё не готова, попробуйте сформировать отчёт позже или свяжитесь с ответственным')
            raise RemoteError('ЧЗ/СУЗ: HTTP '+str(r.status_code)+'. Сверьте операцию в личном кабинете.')
        for key in ('oms_token','true_token','nk_key'):
            if self.config.get(key):data=json.loads(json.dumps(data,ensure_ascii=False).replace(self.config[key],'[secret]'))
        return data
    def oms(self,path,params=None,body=None,signature=None):return self.call('POST' if body is not None else 'GET','/api/v3/'+path,params,body,True,signature)
    def info(self,cises,*,timeout=None):
        out={}
        for start in range(0,len(cises),500):
            batch=cises[start:start+500];data=self.call('POST','/api/v3/true-api/cises/info',{'pg':'radio'},json.dumps(batch).encode(),**({'timeout':timeout} if timeout is not None else {}))
            if not isinstance(data,list):raise RemoteError('ЧЗ не вернул сведения о КМ')
            for record in data:
                value=record.get('cisInfo',{})
                cis=value.get('requestedCis') or value.get('cis')
                if not cis or record.get('errorCode'):raise RemoteError('ЧЗ не подтвердил сведения о КМ')
                out[cis]=value
            if any(cis not in out for cis in batch):raise RemoteError('ЧЗ не вернул часть запрошенных КМ')
        return out
    def verify(self,codes,status,*,parent=None):
        data=self.info([r['cis'] for r in codes])
        for row in codes:
            actual=data[row['cis']]
            if actual.get('status')!=status or actual.get('ownerInn')!=self.inn or actual.get('gtin')!=row['gtin']:raise RemoteError('Статус, GTIN или владелец КМ не соответствует операции')
            if actual.get('statusEx') not in (None,'EMPTY','FTS_RESPOND_NOT_OK'):raise RemoteError('Особое состояние КМ не допускает операцию')
            if parent and actual.get('parent')!=parent:raise RemoteError('КМ не входит в выбранный АТК')
        return data
    @staticmethod
    def external_id(value):
        try:return str(UUID(value))
        except (ValueError,TypeError,AttributeError):raise RemoteError('Внешняя система не подтвердила ID; повтор запрещён') from None
