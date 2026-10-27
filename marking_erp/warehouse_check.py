"""Local complete-code identity and public check-policy fingerprints."""
import json
from hashlib import sha256
from . import gs1,policy


def digest(value):
    return sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,default=str,separators=(',',':')).encode()).hexdigest()


def payload_evidence(raw,line_index=None):
    parsed=gs1.parse(raw.encode('ascii') if isinstance(raw,str) else bytes(raw),scanner=True)
    value={'sha256':sha256(parsed.normalized).hexdigest(),'quantity':parsed.quantity or 1}
    if line_index is not None:value['line_index']=line_index
    return parsed.cis,value


def level(task):
    if not any(r.get('marked',True) for r in task['lines']):return 'not_required'
    if not task['expected_codes']:return 'count_only'
    return 'full' if all(cis in task['expected_payloads'] for cis in task['expected_codes']) else 'cis'


def public_policy(c,task,config,inn):
    snapshot=policy.snapshot(c,task['organization_id'])
    rules={k:snapshot['values'][k] for k in ('warehouse.check.timeout_seconds','warehouse.external_fact_max_age_seconds')}
    return {'version':'warehouse-batch-v1','live_checks':config is not None,
        'environment':config.get('environment') if config else None,
        'ignore_legal_entity':bool(config.get('ignore_legal_entity')) if config else False,
        'inn':inn,'rules':rules}


def scan_digest(task,scans):
    return digest({'source':task['source_id'],'source_revision':task['source_revision'],'kind':task['kind'],
        'lines':task['lines'],'expected_codes':sorted(task['expected_codes']),'expected_payloads':task['expected_payloads'],
        'scans':[{'id':s['id'],'cis':s['cis'],'line_index':s['line_index'],'quantity':s['quantity'],
                  'sha256':sha256(bytes(s['payload'])).hexdigest()} for s in sorted(scans,key=lambda s:str(s['id']))]})
