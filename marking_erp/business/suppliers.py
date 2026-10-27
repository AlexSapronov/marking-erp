"""Explicit supplier consolidation; source names and signed payloads stay intact."""
from uuid import UUID
from fastapi import Depends,HTTPException,Query
from pydantic import Field,StrictInt
from sqlalchemy import text
from ..auth_routes import Body
from .. import auth


def expression(name,org):
    # Arguments are fixed SQL expressions supplied by the application.
    return f"COALESCE((SELECT sa.canonical FROM supplier_aliases sa WHERE sa.organization_id={org} AND sa.alias={name}),{name})"


def canonical(c,org,name):
    return c.execute(text('SELECT canonical FROM supplier_aliases WHERE organization_id=:org AND alias=:name'),{'org':org,'name':name}).scalar_one_or_none() or name


def revision(c,org):
    return c.execute(text('SELECT revision FROM supplier_revisions WHERE organization_id=:org'),{'org':org}).scalar_one_or_none() or 0


def editable(c,org):
    if c.execute(text("SELECT EXISTS(SELECT 1 FROM business_items WHERE organization_id=:org AND kind='transform' AND state NOT IN ('done','rejected','archived'))"),{'org':org}).scalar_one():raise HTTPException(409,'Есть незавершённая трансформация АТК. Сначала завершите или остановите её.')
    if c.execute(text("""SELECT EXISTS(SELECT 1 FROM external_operations o JOIN business_links l ON l.operation_id=o.id JOIN business_items i ON i.id=l.item_id
        WHERE i.organization_id=:org AND i.kind IN ('atk','transform') AND (o.state NOT IN ('confirmed','rejected','cancelled_before_dispatch') OR (o.state='confirmed' AND NOT l.applied)))"""),{'org':org}).scalar_one():raise HTTPException(409,'Есть незавершённые операции АТК. Сначала проверьте их результат.')


class Merge(Body):
    organization_id:UUID
    revision:StrictInt=Field(ge=0)
    aliases:list[str]=Field(min_length=1,max_length=1000)
    canonical:str=Field(min_length=1,max_length=200)
    reason:str=Field(min_length=8,max_length=1000)


def install_suppliers(app,actor):
    dep=Depends(actor,scope='function')
    @app.get('/api/suppliers')
    def listing(organization_id:UUID,q:str=Query('',max_length=120),after:str=Query('',max_length=200),current=dep):
        current.require('integrations.manage',organization_id=organization_id,administrative=True)
        values=current.connection.execute(text('''SELECT DISTINCT name FROM (
            SELECT supplier AS name FROM bm_documents WHERE organization_id=:org
            UNION SELECT alias FROM supplier_aliases WHERE organization_id=:org
            UNION SELECT canonical FROM supplier_aliases WHERE organization_id=:org) names
            WHERE name>:after AND name ILIKE :q ESCAPE '!' ORDER BY name LIMIT 101'''),{'org':organization_id,'after':after,'q':'%'+q.replace('!','!!').replace('%','!%').replace('_','!_')+'%'}).scalars().all()
        mappings={alias:target for alias,target in current.connection.execute(text('SELECT alias,canonical FROM supplier_aliases WHERE organization_id=:org'),{'org':organization_id}).all()}
        return {'revision':revision(current.connection,organization_id),'suppliers':[{'name':v,'canonical':mappings.get(v,v)} for v in values[:100]],'next_cursor':values[99] if len(values)>100 else None}

    @app.post('/api/suppliers/merge')
    def merge(body:Merge,current=dep):
        current.require('integrations.manage',organization_id=body.organization_id,administrative=True)
        org=body.organization_id;c=current.connection
        if len(body.reason.strip())<8:raise HTTPException(422,'Укажите причину от 8 символов')
        if body.revision!=revision(c,org):raise HTTPException(409,'Справочник поставщиков изменился. Обновите список.')
        editable(c,org)
        names=set(body.aliases+[body.canonical])
        if any(not n.strip() or len(n)>200 or n!=n.strip() or any(ord(ch)<32 for ch in n) for n in names):raise HTTPException(422,'Названия должны быть непустыми, без пробелов по краям и управляющих символов')
        known=set(c.execute(text('SELECT DISTINCT supplier FROM bm_documents WHERE organization_id=:org UNION SELECT alias FROM supplier_aliases WHERE organization_id=:org UNION SELECT canonical FROM supplier_aliases WHERE organization_id=:org'),{'org':org}).scalars())
        if not set(body.aliases)<=known:raise HTTPException(422,'Выберите существующие варианты названия поставщика')
        target=canonical(c,org,body.canonical);groups={canonical(c,org,n) for n in body.aliases}
        # Flatten all aliases of the selected groups; no chains/cycles, even when
        # the chosen target was previously an alias. Global write fence serializes.
        combined=groups|{target}
        duplicates=c.execute(text(f'''SELECT owner_id,number,document_date FROM bm_documents d
            WHERE organization_id=:org AND state<>'archived' AND {expression('d.supplier','d.organization_id')}=ANY(CAST(:groups AS text[]))
            GROUP BY owner_id,number,document_date HAVING count(*)>1 LIMIT 1'''),{'org':org,'groups':list(combined)}).first()
        if duplicates:raise HTTPException(409,'После объединения получатся дубли накладных с одинаковыми номером и датой у одного владельца. Сначала разберите или архивируйте дубли.')
        old=[dict(r) for r in c.execute(text('SELECT alias,canonical FROM supplier_aliases WHERE organization_id=:org AND canonical=ANY(CAST(:groups AS text[]))'),{'org':org,'groups':list(groups)}).mappings()]
        c.execute(text('UPDATE supplier_aliases SET canonical=:target WHERE organization_id=:org AND canonical=ANY(CAST(:groups AS text[]))'),{'org':org,'target':target,'groups':list(groups)})
        for name in names|groups|{target}:
            c.execute(text('INSERT INTO supplier_aliases(organization_id,alias,canonical) VALUES (:org,:alias,:target) ON CONFLICT(organization_id,alias) DO UPDATE SET canonical=excluded.canonical'),{'org':org,'alias':name,'target':target})
        c.execute(text('INSERT INTO supplier_revisions(organization_id,revision) VALUES (:org,1) ON CONFLICT(organization_id) DO UPDATE SET revision=supplier_revisions.revision+1'),{'org':org})
        auth.audit(c,current.id,'suppliers.merged',org,{'aliases':sorted(body.aliases),'canonical':target,'previous':old,'reason':body.reason})
        return {'canonical':target,'revision':body.revision+1}

    @app.post('/api/suppliers/separate')
    def separate(body:Merge,current=dep):
        current.require('integrations.manage',organization_id=body.organization_id,administrative=True)
        c=current.connection;org=body.organization_id
        if body.revision!=revision(c,org):raise HTTPException(409,'Справочник изменился. Обновите список.')
        if len(body.aliases)!=1 or len(body.reason.strip())<8:raise HTTPException(422,'Выберите один вариант и укажите причину от 8 символов')
        editable(c,org)
        name=body.aliases[0];old=canonical(c,org,name)
        if old==name:raise HTTPException(409,'Этот вариант уже самостоятельный')
        c.execute(text('UPDATE supplier_aliases SET canonical=alias WHERE organization_id=:org AND alias=:alias'),{'org':org,'alias':name})
        c.execute(text('UPDATE supplier_revisions SET revision=revision+1 WHERE organization_id=:org'),{'org':org})
        auth.audit(c,current.id,'suppliers.separated',org,{'alias':name,'previous':old,'reason':body.reason})
        return {'canonical':name,'revision':body.revision+1}
