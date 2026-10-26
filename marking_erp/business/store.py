from hashlib import sha256
from uuid import uuid4
import json
from fastapi import HTTPException
from sqlalchemy import text
from .. import auth,operations

VIEW={'card':'nk.cards.view','template':'nk.cards.view','import':'nk.cards.view','sync':'nk.cards.view','marking':'marking.documents.view','atk':'logistics.atk.view','transform':'logistics.atk.view','split':'split.operations.view','fts':'logistics.atk.view'}

def item(c,id,kind=None,lock=False):
    row=c.execute(text('SELECT * FROM business_items WHERE id=:id'+(' FOR UPDATE' if lock else '')),{'id':id}).mappings().first()
    if not row or (kind and row['kind']!=kind):raise HTTPException(404,'Документ не найден')
    return dict(row)

def context(current,row):
    assigned=current.connection.execute(text('SELECT EXISTS(SELECT 1 FROM operation_assignments WHERE target_id=:id AND user_id=:user)'),{'id':row['id'],'user':current.id}).scalar_one()
    return dict(organization_id=row['organization_id'],owner_id=row['owner_id'],assigned=assigned)

def require(current,row,permission=None):
    current.require(permission or VIEW[row['kind']],**context(current,row))

def get(current,id,kind=None,lock=False,permission=None):
    row=item(current.connection,id,kind,lock);require(current,row,permission);return row

def record(current,row,action,body=None):
    current.connection.execute(text('INSERT INTO business_events(item_id,actor_id,action,body) VALUES (:id,:actor,:action,CAST(:body AS jsonb))'),{'id':row['id'],'actor':current.id,'action':action,'body':json.dumps(body or {},default=str)})
    auth.audit(current.connection,current.id,'business.'+action,row['id'],{'kind':row['kind']})

def create(current,kind,title,body,org,source=None,id=None,state='draft'):
    c=current.connection;id=id or uuid4()
    if c.execute(text('SELECT EXISTS(SELECT 1 FROM operation_targets WHERE id=:id)'),{'id':id}).scalar_one():raise HTTPException(409,'Идентификатор запроса уже использован')
    c.execute(text('INSERT INTO operation_targets(id,organization_id,owner_id,revision) VALUES (:id,:org,:user,1)'),{'id':id,'org':org,'user':current.id})
    c.execute(text('''INSERT INTO business_items(id,organization_id,owner_id,kind,title,body,source_id,state)
        VALUES (:id,:org,:owner,:kind,:title,CAST(:body AS jsonb),:source,:state)'''),{'id':id,'org':org,'owner':current.id,'kind':kind,'title':title,'body':json.dumps(body,default=str),'source':source,'state':state})
    row=item(c,id);record(current,row,'created');return row

def save(current,row,action,*,body=None,state=None,title=None):
    body=row['body'] if body is None else body;state=state or row['state'];title=title or row['title']
    current.connection.execute(text('''UPDATE business_items SET body=CAST(:body AS jsonb),state=:state,title=:title,revision=revision+1,updated_at=now() WHERE id=:id'''),{'id':row['id'],'body':json.dumps(body,default=str),'state':state,'title':title})
    record(current,row,action);return item(current.connection,row['id'])

def version(row,revision):
    if row['revision']!=revision:raise HTTPException(409,'Документ изменился. Обновите данные.')
    if row['state']=='archived':raise HTTPException(409,'Документ в архиве')

def idle(c,id):
    if c.execute(text("SELECT EXISTS(SELECT 1 FROM business_links l JOIN external_operations o ON o.id=l.operation_id WHERE l.item_id=:id AND o.state NOT IN ('confirmed','rejected','cancelled_before_dispatch'))"),{'id':id}).scalar_one():raise HTTPException(409,'Есть незавершённая внешняя операция. Сначала проверьте её результат.')

def queue(current,row,step,payload,metadata=None):
    id=uuid4();c=current.connection
    c.execute(text('INSERT INTO operation_targets(id,organization_id,owner_id,revision) VALUES (:id,:org,:owner,1)'),{'id':id,'org':row['organization_id'],'owner':row['owner_id']})
    for user in c.execute(text('SELECT user_id FROM operation_assignments WHERE target_id=:id'),{'id':row['id']}).scalars().all():c.execute(text('INSERT INTO operation_assignments(target_id,user_id) VALUES (:id,:user)'),{'id':id,'user':user})
    command=operations.prepare(c,organization_id=row['organization_id'],actor_id=current.id,kind=step,target_id=id,target_revision=1,idempotency_key=str(id),payload=payload if isinstance(payload,bytes) else json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode())
    c.execute(text('INSERT INTO business_links(operation_id,item_id,step,metadata) VALUES (:op,:item,:step,CAST(:meta AS jsonb))'),{'op':command['id'],'item':row['id'],'step':step,'meta':json.dumps(metadata or {})})
    record(current,row,'queued',{'operation_id':str(command['id']),'step':step});return command['id']

def artifact(current,row,kind,filename,data,media,snapshot=None):
    if not data or len(data)>16777216:raise HTTPException(422,'Файл превышает лимит 16 МБ. Разделите документ.')
    c=current.connection;ver=c.execute(text('SELECT coalesce(max(version),0)+1 FROM business_artifacts WHERE item_id=:id AND kind=:kind'),{'id':row['id'],'kind':kind}).scalar_one();id=uuid4()
    c.execute(text('''INSERT INTO business_artifacts(id,item_id,kind,version,filename,media_type,body,sha256,snapshot) VALUES
        (:id,:item,:kind,:version,:filename,:media,:body,:sha,CAST(:snapshot AS jsonb))'''),{'id':id,'item':row['id'],'kind':kind,'version':ver,'filename':filename,'media':media,'body':data,'sha':sha256(data).hexdigest(),'snapshot':json.dumps(snapshot or {},default=str)})
    record(current,row,'file_created',{'kind':kind,'version':ver});return id


def archive(current,row,reason):
    c=current.connection
    for op in c.execute(text('SELECT o.* FROM external_operations o JOIN business_links l ON l.operation_id=o.id WHERE l.item_id=:id ORDER BY o.id FOR UPDATE'),{'id':row['id']}).mappings().all():
        c.execute(text('UPDATE operation_targets SET stopped=true WHERE id=:id'),{'id':op['target_id']})
        if op['state'] in ('queued','blocked','awaiting_signature'):operations.cancel_before_dispatch(c,op['id'])
    c.execute(text('UPDATE operation_targets SET stopped=true WHERE id=:id'),{'id':row['id']})
    record(current,row,'archive_reason',{'reason':reason})
    return save(current,row,'archived',body={**row['body'],'archived_from':row['state']},state='archived')
