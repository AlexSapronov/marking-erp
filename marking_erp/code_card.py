"""Read-only code dossier. Every relationship retains its original access scope.

No integration calls, payloads, tokens or print leases are returned here.
"""
from datetime import datetime,timezone,timedelta
from fastapi import HTTPException
from sqlalchemy import text
from .business import store


def scope(current, permission, owner, assigned):
    row=current.connection.execute(text('''SELECT g.scope,p.administrative FROM group_grants g JOIN permissions p ON p.id=g.permission_id
        WHERE g.group_slug=:group AND g.permission_id=:permission AND g.scope<>'none' '''),
        {'group':current.user['group_slug'],'permission':permission}).mappings().first()
    if not row:return 'false'
    if row['administrative'] and (current.user['group_slug']=='specialist' or row['scope']!='all' or not current.elevated_at
        or current.elevated_at<datetime.now(timezone.utc)-timedelta(minutes=5)):return 'false'
    expression={'all':'true','own':f'({owner}=:user)','assigned':f'({assigned})'}[row['scope']]
    from .permission_catalog import catalog
    meta=next((p for p in catalog() if p['id']==permission),None)
    for dependency in meta['dependencies'] if meta else []:
        expression+=f' AND ({scope(current,dependency,owner,assigned)})'
    return '('+expression+')'


def assignment(alias):
    return f'EXISTS(SELECT 1 FROM operation_assignments oa WHERE oa.target_id={alias}.id AND oa.user_id=:user)'


def business_scope(current, alias='i'):
    return '('+' OR '.join(f"({alias}.kind='{kind}' AND {scope(current,permission,alias+'.owner_id',assignment(alias))})"
        for kind,permission in store.VIEW.items())+')'


def warehouse_scope(current, alias='w'):
    clauses=[]
    for kind in ('receipt','shipment'):
        own=scope(current,'warehouse.'+kind+'.view',alias+'.owner_id',assignment(alias))
        logistics=scope(current,'logistics.warehouse.result.view',alias+'.owner_id',assignment(alias))
        clauses.append(f"({alias}.kind='{kind}' AND ({own} OR {logistics}))")
    admin=current.user['group_slug']=='administrator'
    if not admin:
        from .auth import group_has_admin
        admin=group_has_admin(current.connection,current.user['group_slug'])
    elevated=current.elevated_at and current.elevated_at>=datetime.now(timezone.utc)-timedelta(minutes=5)
    archive=scope(current,'documents.archive.view',alias+'.owner_id',assignment(alias)) if admin and elevated else 'false'
    return '('+' OR '.join(clauses)+f') AND ({alias}.archived_at IS NULL OR {archive})'


def overview(current, id, org):
    c=current.connection
    row=c.execute(text('''SELECT c.id,c.cis,c.gtin,c.created_at,v.version,v.template,v.label_quantity,v.normalized_sha256,
        EXISTS(SELECT 1 FROM code_events e WHERE e.code_id=c.id AND e.organization_id=:org AND e.kind='payload_conflict') AS conflict
        FROM codes c JOIN LATERAL (SELECT * FROM code_payload_versions p WHERE p.code_id=c.id AND p.organization_id=:org
            AND EXISTS(SELECT 1 FROM code_events e WHERE e.payload_id=p.id AND e.kind<>'payload_conflict') ORDER BY version DESC LIMIT 1) v ON true
        WHERE c.id=:id'''),{'id':id,'org':org}).mappings().first()
    if not row:raise HTTPException(404,'Код не найден')
    code=dict(row)
    products=c.execute(text('''SELECT DISTINCT p.pn,p.tnved FROM product_pns p JOIN products pr ON pr.id=p.product_id
        WHERE p.organization_id=:org AND pr.gtin=:gtin ORDER BY p.pn,p.tnved'''),{'org':org,'gtin':row['gtin']}).mappings().all()
    code['products']=[dict(p) for p in products]
    # Local allocation state is separate from the official status in ChZ.
    allocation=c.execute(text(f'''SELECT bc.id,bc.pn,bc.tnved,bc.quantity,bc.state,bc.item_id,i.title
        FROM business_codes bc JOIN business_items i ON i.id=bc.item_id AND i.organization_id=bc.organization_id
        WHERE bc.registry_code_id=:id AND bc.organization_id=:org AND {business_scope(current)}'''),
        {'id':id,'org':org,'user':current.id}).mappings().first()
    code['allocation']=dict(allocation) if allocation else None
    # This is a historical observation, never a new online check or close permission.
    observation=c.execute(text(f'''SELECT w.id AS task_id,w.number,w.kind,w.verified_at AS checked_at,s.verification,
        s.removed_at,w.state,w.archived_at
        FROM warehouse_tasks w JOIN warehouse_scans s ON s.task_id=w.id
        WHERE w.organization_id=:org AND s.cis=:cis AND w.verified_at IS NOT NULL
        AND s.verification IS NOT NULL AND {warehouse_scope(current)}
        ORDER BY w.verified_at DESC,w.id DESC,s.id DESC LIMIT 1'''),
        {'org':org,'cis':row['cis'],'user':current.id}).mappings().first()
    if observation:
        value=dict(observation);raw=value.pop('verification')
        value['facts']={k:raw[k] for k in ('status','ownerInn','gtin','quantity','parent') if k in raw}
        code['last_check']=value
    else:code['last_check']=None
    return code


def relations(current, id, org, section, before_time=None, before_id=None):
    c=current.connection
    cis=c.execute(text('SELECT cis FROM codes WHERE id=:id'),{'id':id}).scalar_one()
    params={'id':id,'org':org,'cis':cis,'user':current.id,'time':before_time,'before':before_id}
    prefix=''
    # All projections are explicit; business JSON and file bodies stay private.
    if section=='lineage':
        from .registry_routes import assignment_sql
        alias='k';table='(SELECT c.*,a.organization_id,a.owner_id FROM codes c JOIN code_access a ON a.code_id=c.id) k'
        select="k.id,k.created_at,k.cis AS title,k.gtin"
        access=scope(current,'codes.registry.view','k.owner_id',assignment_sql('k.id','k.organization_id'))
        linked='''(EXISTS(SELECT 1 FROM code_replacements r JOIN business_codes old ON old.id=r.old_code_id
            JOIN business_codes new ON new.id=r.new_code_id WHERE old.organization_id=:org AND new.organization_id=:org
            AND ((old.registry_code_id=:id AND new.registry_code_id=k.id) OR (new.registry_code_id=:id AND old.registry_code_id=k.id)))
            OR EXISTS(SELECT 1 FROM business_items i JOIN business_codes child ON child.item_id=i.id AND child.organization_id=i.organization_id
                WHERE i.organization_id=:org AND i.kind='split' AND ((i.body->>'source_cis'=:cis AND child.registry_code_id=k.id)
                    OR (child.registry_code_id=:id AND i.body->>'source_cis'=k.cis))))'''
    elif section=='processes':
        table='business_items i';alias='i'
        select='i.id,i.created_at,i.kind,i.title,i.state,i.revision'
        linked='''(i.id IN (SELECT item_id FROM business_codes WHERE registry_code_id=:id AND organization_id=:org)
            OR EXISTS(SELECT 1 FROM code_payload_versions p WHERE p.code_id=:id AND p.organization_id=:org
                AND p.source_ref LIKE 'business:%' AND split_part(p.source_ref,':',2)=i.id::text)
            OR i.body->>'source_cis'=:cis OR i.body->>'old_cis'=:cis OR i.body->>'new_cis'=:cis
            OR EXISTS(SELECT 1 FROM business_codes bc WHERE bc.registry_code_id=:id AND bc.organization_id=:org
                AND (i.body->'codes' @> jsonb_build_array(bc.id::text) OR i.body->'codes' @> jsonb_build_array(jsonb_build_object('id',bc.id::text)))))'''
        access=business_scope(current)
    elif section=='documents':
        table='bm_documents d';alias='d';select='d.id,d.created_at,d.number AS title,d.supplier,d.document_date,d.state,d.revision'
        linked='''EXISTS(SELECT 1 FROM business_items i WHERE i.source_id=d.id AND i.organization_id=:org AND
            (EXISTS(SELECT 1 FROM business_codes bc WHERE bc.item_id=i.id AND bc.organization_id=:org AND bc.registry_code_id=:id)
            OR EXISTS(SELECT 1 FROM code_payload_versions p WHERE p.code_id=:id AND p.organization_id=:org
                AND p.source_ref LIKE 'business:%' AND split_part(p.source_ref,':',2)=i.id::text)))'''
        access=scope(current,'marking.documents.view','d.owner_id',assignment('d'))
        archive=scope(current,'documents.archive.view','d.owner_id',assignment('d'))
        access+=f" AND (d.state<>'archived' OR {archive})"
    elif section=='warehouse':
        table='warehouse_tasks w';alias='w'
        select='''w.id,w.created_at,w.kind,w.number AS title,w.state,w.archived_at,w.source_type,
            (w.state='open' AND w.expected_codes ? :cis) AS expected_reservation,
            EXISTS(SELECT 1 FROM warehouse_scans s WHERE s.task_id=w.id AND s.cis=:cis AND s.removed_at IS NULL) AS scanned,
            EXISTS(SELECT 1 FROM warehouse_scans s WHERE s.task_id=w.id AND s.cis=:cis AND s.removed_at IS NOT NULL) AS scan_removed'''
        linked='''(w.expected_codes ? :cis OR EXISTS(SELECT 1 FROM warehouse_scans s WHERE s.task_id=w.id AND s.cis=:cis))'''
        access=warehouse_scope(current)
    elif section=='incidents':
        table='incidents n';alias='n';select='n.id,n.created_at,n.category,n.source_title AS title,n.cis,n.status AS state,n.restrictions,n.revision'
        prefix='''WITH RECURSIVE ancestors(id,cis,item_id) AS (
            SELECT id,cis,item_id FROM business_codes WHERE organization_id=:org AND registry_code_id=:id
            UNION SELECT old.id,old.cis,old.item_id FROM ancestors a JOIN code_replacements r ON r.new_code_id=a.id
                JOIN business_codes old ON old.id=r.old_code_id AND old.organization_id=:org),
            targets(id) AS (SELECT item_id FROM ancestors WHERE item_id IS NOT NULL
                UNION SELECT i.source_id FROM ancestors a JOIN business_items i ON i.id=a.item_id WHERE i.source_id IS NOT NULL)'''
        linked='''(n.cis=:cis OR n.source_id IN (SELECT bc.item_id FROM business_codes bc WHERE bc.registry_code_id=:id AND bc.organization_id=:org)
            OR n.source_id IN (SELECT i.source_id FROM business_items i JOIN business_codes bc ON bc.item_id=i.id WHERE bc.registry_code_id=:id AND bc.organization_id=:org)
            OR n.source_id IN (SELECT w.id FROM warehouse_tasks w WHERE w.organization_id=:org AND (w.expected_codes ? :cis
                OR EXISTS(SELECT 1 FROM warehouse_scans s WHERE s.task_id=w.id AND s.cis=:cis))))'''
        linked=f'''({linked} OR n.cis IN (SELECT cis FROM ancestors) OR (n.cis IS NULL AND
            (n.source_id IN (SELECT id FROM targets) OR EXISTS(SELECT 1 FROM warehouse_tasks w WHERE w.id=n.source_id AND w.source_id IN (SELECT id FROM targets))
                OR EXISTS(SELECT 1 FROM business_items i WHERE i.id=n.source_id AND i.source_id IN (SELECT id FROM targets)))))'''
        assigned='n.assignee_id=:user OR EXISTS(SELECT 1 FROM operation_assignments oa WHERE oa.target_id=n.source_id AND oa.user_id=:user)'
        access=scope(current,'incidents.create','n.reporter_id',assigned)
    elif section in ('printing','reprints'):
        assigned="EXISTS(SELECT 1 FROM warehouse_tasks w JOIN operation_assignments oa ON oa.target_id=w.id WHERE w.source_id=i.source_id AND w.state<>'cancelled' AND oa.user_id=:user)"
        if section=='printing':
            alias='j';table='print_jobs j JOIN business_artifacts a ON a.id=j.artifact_id JOIN business_items i ON i.id=a.item_id'
            select='j.id,j.created_at,j.state,j.copies,j.finished_at,j.reprint_id,i.title,a.sha256 AS pdf_sha256'
            # Snapshot membership includes historical kits and excludes codes added later.
            linked="(a.snapshot->'codes' @> jsonb_build_array(jsonb_build_object('cis',CAST(:cis AS text))) OR a.snapshot->'codes' ? :cis OR EXISTS(SELECT 1 FROM reprint_requests r JOIN business_codes bc ON bc.id=r.code_id WHERE r.id=j.reprint_id AND bc.registry_code_id=:id AND bc.organization_id=:org))"
            access=scope(current,'labels.print.view','i.owner_id',assigned)
        else:
            alias='r';table='reprint_requests r JOIN business_codes bc ON bc.id=r.code_id JOIN business_items i ON i.id=bc.item_id'
            select='r.id,r.created_at,r.state,r.reason,r.copies,r.scanned_copies,r.completed_at,i.title'
            linked='bc.registry_code_id=:id AND bc.organization_id=:org'
            access=scope(current,'labels.reprint','i.owner_id',assigned)
            # Other employees' requests require the same fresh elevation as detail.
            try:current.require('integrations.manage',organization_id=org,administrative=True)
            except HTTPException as error:
                if error.status_code!=403:raise
                access+=' AND r.actor_id=:user'
    else:raise HTTPException(422,'Неизвестный раздел карточки')
    rows=c.execute(text(f'''{prefix} SELECT {select} FROM {table} WHERE {alias}.organization_id=:org AND ({linked}) AND ({access})
        AND (CAST(:time AS timestamptz) IS NULL OR ({alias}.created_at,{alias}.id)<(CAST(:time AS timestamptz),CAST(:before AS uuid)))
        ORDER BY {alias}.created_at DESC,{alias}.id DESC LIMIT 26'''),params).mappings().all()
    return {'items':[dict(r) for r in rows[:25]],
        'next_cursor':{'before_time':rows[24]['created_at'],'before_id':rows[24]['id']} if len(rows)>25 else None}
