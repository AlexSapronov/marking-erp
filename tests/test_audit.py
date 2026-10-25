from datetime import datetime, timezone
from uuid import uuid4
import pytest
from marking_erp.audit_routes import public_event


def test_projection_omits_detail_and_unregistered_fields():
    event={'id':1,'created_at':datetime.now(timezone.utc),'actor_id':uuid4(),
           'action':'user.created','entity':str(uuid4()),'detail':{'secret':'private'},'token':'private'}
    result=public_event(event)
    assert set(result)=={'id','created_at','actor_id','action','label','entity'}
    assert result['entity']==event['entity']
    assert 'private' not in str(result)


@pytest.mark.parametrize('action,entity,expected',[
    ('login.failed','internal-throttle-key',None),
    ('future.private-event','private',None),
    ('user.created','secret text',None),
    ('configuration.group','administrator','administrator'),
    ('configuration.rules','system','system'),
    ('system.restored','backup','backup'),
])
def test_entity_projection(action,entity,expected):
    result=public_event({'id':1,'created_at':None,'actor_id':None,'action':action,'entity':entity})
    assert result['entity']==expected
    if action=='future.private-event':assert result['action']=='unknown'
