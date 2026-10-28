"""Rule bindings shared by API decisions and operator controls."""
import json
from uuid import uuid4
from sqlalchemy import text
from .policy import snapshot,rules_catalog


def saved(task):
    # Legacy tasks retain shipped defaults, never acquire a relaxed new rule.
    values={r['id']:r['default'] for r in rules_catalog()}
    values.update((task.get('policy_snapshot') or {}).get('values',{}))
    return values


def display(c,org):
    rules=snapshot(c,org)['values']
    return {key:rules[key] for key in ('warehouse.hide_completed','warehouse.devices.poll_seconds')}


def retention(current,target,org):
    policy=snapshot(current.connection,org)
    current.connection.execute(text('''INSERT INTO archive_retention(id,organization_id,target_id,actor_id,retain_until,policy_snapshot)
        VALUES (:id,:org,:target,:actor,now()+make_interval(months=>:months),CAST(:policy AS jsonb))'''),
        {'id':uuid4(),'org':org,'target':target,'actor':current.id,'months':policy['values']['archive.retention_months'],'policy':json.dumps(policy)})


def retain_until(c,target,org):
    return c.execute(text('SELECT max(retain_until) FROM archive_retention WHERE organization_id=:org AND target_id=:id'),{'org':org,'id':target}).scalar_one()
