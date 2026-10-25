"""Internal policy only. Never weakens external checks or rewrites a prepared command."""
import json
import math
from copy import deepcopy
from importlib.resources import files
from sqlalchemy import text
from fastapi import HTTPException


def rules_catalog():
    return json.loads(files('marking_erp.data').joinpath('business-rules.json').read_text())


def validate_value(rule,value):
    kind=rule['type']
    if not rule['mutable'] and value!=rule['default']:raise ValueError('Правило пока не допускает изменения')
    if kind in ('bool','constant'):
        if type(value) is not type(rule['default']):raise ValueError('Неверный тип значения')
    elif kind=='integer':
        if type(value) is not int:raise ValueError('Требуется целое число')
    elif kind=='decimal':
        if type(value) not in (int,float) or not math.isfinite(value):raise ValueError('Требуется конечное число')
    elif kind in ('enum','reference'):
        if value not in rule['choices'] or not isinstance(value,str):raise ValueError('Выберите допустимый вариант')
    elif kind=='set':
        if not isinstance(value,list) or not all(isinstance(x,str) for x in value) or len(set(value))!=len(value) or not set(value).issubset(rule['choices']) or len(value)<rule.get('min_items',0):raise ValueError('Недопустимый набор полей')
    if 'min' in rule and not rule['min']<=value<=rule['max']:raise ValueError('Значение вне допустимого диапазона')
    return value


def snapshot(connection,organization_id):
    """Single SQL statement gives a consistent values+revisions view (READ COMMITTED).

    Future commands must persist this JSON with their prepared payload, and must
    never resolve mutable policy again at dispatch in place of that saved JSON.
    """
    org=str(organization_id)
    rows=connection.execute(text('''SELECT r.scope_key,r.revision,v.rule_id,v.value
        FROM policy_revisions r LEFT JOIN policy_values v ON v.scope_key=r.scope_key
        WHERE r.scope_key IN ('system',:org)'''),{'org':org}).mappings().all()
    values={p['id']:deepcopy(p['default']) for p in rules_catalog()}
    versions={'system':1,'organization':1}
    for key in ('system',org):
        for row in rows:
            if row['scope_key']==key:
                versions['system' if key=='system' else 'organization']=row['revision']
                if row['rule_id'] in values:values[row['rule_id']]=row['value']
    return {'catalog_version':1,'organization_id':org,'versions':versions,'values':values}
