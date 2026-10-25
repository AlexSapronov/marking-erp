"""Radio profile v1 (templates 36/37), bytes first; not cryptographic verification."""
from dataclasses import dataclass
import re

GS=b'\x1d'
RS=b'\x1e'


def valid_gtin(value):
    return isinstance(value,str) and bool(re.fullmatch(r'[0-9]{14}',value)) and int(value[-1])==(10-sum(int(c)*(3 if i%2==0 else 1) for i,c in enumerate(value[:13]))%10)%10


@dataclass(frozen=True)
class Payload:
    raw:bytes
    normalized:bytes
    profile:str
    corrections:tuple[str,...]
    gtin:str
    cis:str
    template:int
    quantity:int|None
    base:bytes


def parse(raw:bytes,*,scanner=False):
    if type(raw) is not bytes or not 1<=len(raw)<=1024:raise ValueError('Ожидаются байты полного КМ, до 1024 байт')
    data=raw;corrections=[]
    if scanner:
        if data.startswith(b']d2'):data=data[3:];corrections.append('aim-prefix-removed')
        if data.endswith(b'\r\n'):data=data[:-2];corrections.append('crlf-terminator-removed')
        elif data.endswith((b'\r',b'\n')):data=data[:-1];corrections.append('terminator-removed')
    if not re.match(rb'^01[0-9]{14}21',data):raise ValueError('Неизвестный формат КМ радиоэлектроники')
    gtin=data[2:16].decode('ascii')
    if not valid_gtin(gtin):raise ValueError('Неверная контрольная цифра GTIN')
    if len(data)<45 or any(not 33<=c<=126 for c in data[18:38]):raise ValueError('Нужен ИСН из 20 печатных ASCII-символов')
    if data[38:41]==GS+b'91':
        if data[45:48]!=GS+b'92' or len(data)<92:raise ValueError('Неполный криптохвост 91/92')
        end=92;template=36
        crypto=data[41:45]+data[48:92]
    elif data[38:41]==GS+b'93':
        end=45;template=37;crypto=data[41:45]
    else:raise ValueError('Нужен криптохвост с обязательными GS-разделителями')
    if any(not 33<=c<=126 for c in crypto):raise ValueError('Некорректные байты криптохвоста')
    remainder=data[end:];quantity=None
    if remainder:
        match=re.fullmatch(GS+rb'30([0-9]{1,8})',remainder)
        if not match or not 1<=int(match[1])<=99999999:raise ValueError('Неподдерживаемая переменная характеристика или количество AI30')
        quantity=int(match[1])
    return Payload(raw,data,'radio-scanner-v1' if scanner else 'radio-strict-v1',tuple(corrections),gtin,data[:38].decode('ascii'),template,quantity,data[:end])


def with_quantity(raw:bytes,quantity:int):
    value=parse(raw)
    if value.quantity is not None or type(quantity) is not int or not 1<=quantity<=99999999:raise ValueError('Недопустимое количество или уже заполненный AI30')
    return value.normalized+GS+b'30'+str(quantity).encode('ascii')


def division_entry(new_raw:bytes,source_raw:bytes):
    """S-004 transport helper only: source omits AI30; original bytes remain intact."""
    new,source=parse(new_raw),parse(source_raw)
    if new.gtin!=source.gtin or new.cis==source.cis or new.quantity is None:raise ValueError('Коды не соответствуют разделению')
    return new.normalized+RS+source.base
