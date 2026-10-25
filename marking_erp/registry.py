"""Internal ingestion primitives; not a public upload/import or emission endpoint."""
from hashlib import sha256
import json
import re
from uuid import uuid4
from sqlalchemy import text
from .gs1 import parse,valid_gtin


def register_product(c,organization_id,gtin,pn,tnved,*,source,source_ref,evidence_sha256):
    if not valid_gtin(gtin) or not isinstance(pn,str) or not 1<=len(pn.strip())<=120 or not re.fullmatch('[0-9]{10}',tnved):raise ValueError('Недопустимая номенклатура')
    if source not in ('nk_confirmed','external_verified') or not source_ref.strip() or not re.fullmatch('[a-f0-9]{64}',evidence_sha256):raise ValueError('Нужен подтверждённый источник товара')
    id=c.execute(text('''INSERT INTO products(id,gtin) VALUES (:id,:gtin) ON CONFLICT(gtin) DO NOTHING RETURNING id'''),{'id':uuid4(),'gtin':gtin}).scalar()
    if id is None:id=c.execute(text('SELECT id FROM products WHERE gtin=:gtin'),{'gtin':gtin}).scalar_one()
    c.execute(text('''INSERT INTO product_pns(id,product_id,organization_id,pn,tnved,source,source_ref,evidence_sha256)
        VALUES (:id,:product,:org,:pn,:tnved,:source,:ref,:evidence) ON CONFLICT DO NOTHING'''),
        {'id':uuid4(),'product':id,'org':organization_id,'pn':pn,'tnved':tnved,'source':source,'ref':source_ref,'evidence':evidence_sha256})
    conflicts=c.execute(text('''SELECT count(DISTINCT tnved)>1 FROM product_pns WHERE product_id=:id AND organization_id=:org'''),{'id':id,'org':organization_id}).scalar()
    return {'id':id,'tnved_conflict':conflicts}


def capture_code(c,organization_id,owner_id,raw,*,actor_id,source_ref,scanner=False):
    value=parse(raw,scanner=scanner)
    if not source_ref.strip():raise ValueError('Укажите происхождение КМ')
    for user in {owner_id,actor_id}:
        if not c.execute(text('SELECT 1 FROM user_organizations WHERE user_id=:user AND organization_id=:org'),{'user':user,'org':organization_id}).scalar():raise ValueError('Пользователь не принадлежит организации')
    # Serialize creation and conflict classification for this identity across workers.
    c.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:cis,683273530))'),{'cis':value.cis})
    id=c.execute(text("SELECT id FROM codes WHERE product_group='radio' AND cis=:cis"),{'cis':value.cis}).scalar()
    if id is None:
        id=uuid4();product=c.execute(text('SELECT id FROM products WHERE gtin=:gtin'),{'gtin':value.gtin}).scalar()
        c.execute(text("INSERT INTO codes(id,product_group,cis,gtin,product_id) VALUES (:id,'radio',:cis,:gtin,:product)"),{'id':id,'cis':value.cis,'gtin':value.gtin,'product':product})
    c.execute(text('''INSERT INTO code_access(code_id,organization_id,owner_id) VALUES (:id,:org,:owner) ON CONFLICT DO NOTHING'''),{'id':id,'org':organization_id,'owner':owner_id})
    last=c.execute(text('''SELECT * FROM code_payload_versions WHERE code_id=:id AND organization_id=:org ORDER BY version DESC LIMIT 1'''),{'id':id,'org':organization_id}).mappings().first()
    canonical=c.execute(text("SELECT normalized FROM code_payload_versions v JOIN code_events e ON e.payload_id=v.id WHERE v.code_id=:id AND v.organization_id=:org AND e.kind='captured' ORDER BY e.id LIMIT 1"),{'id':id,'org':organization_id}).scalar()
    kind='captured' if canonical is None else 'reobserved' if bytes(canonical)==value.normalized else 'payload_conflict'
    payload=uuid4();version=last['version']+1 if last else 1
    c.execute(text('''INSERT INTO code_payload_versions(id,code_id,organization_id,version,raw,normalized,raw_sha256,normalized_sha256,profile,corrections,template,label_quantity,actor_id,source_ref)
        VALUES (:payload,:id,:org,:version,:raw,:normalized,:raw_hash,:normalized_hash,:profile,CAST(:corrections AS jsonb),:template,:quantity,:actor,:ref)'''),
        {'payload':payload,'id':id,'org':organization_id,'version':version,'raw':raw,'normalized':value.normalized,'raw_hash':sha256(raw).hexdigest(),'normalized_hash':sha256(value.normalized).hexdigest(),
         'profile':value.profile,'corrections':json.dumps(value.corrections),'template':value.template,'quantity':value.quantity,'actor':actor_id,'ref':source_ref})
    c.execute(text('INSERT INTO code_events(code_id,organization_id,payload_id,kind,actor_id) VALUES (:id,:org,:payload,:kind,:actor)'),{'id':id,'org':organization_id,'payload':payload,'kind':kind,'actor':actor_id})
    return {'id':id,'payload_id':payload,'version':version,'kind':kind,'conflict':kind=='payload_conflict'}


def _persist_file(c,store,value,data,kind,media,snapshot):
    metadata=store.write(data)
    c.execute(text('''INSERT INTO files(id,organization_id,code_id,payload_id,kind,snapshot,storage_key,sha256,size,media_type)
        VALUES (:id,:org,:code,:payload,:kind,CAST(:snapshot AS jsonb),:storage_key,:sha256,:size,:media)'''),{**metadata,'org':value['organization_id'],'code':value['code_id'],'payload':value['id'],'kind':kind,'media':media,'snapshot':json.dumps(snapshot)})
    return metadata['id']


def save_payload_file(c,store,payload_id):
    value=c.execute(text('SELECT * FROM code_payload_versions WHERE id=:id'),{'id':payload_id}).mappings().one()
    return _persist_file(c,store,value,bytes(value['raw']),'payload','application/octet-stream',{})


def save_label(c,store,payload_id,pn):
    from .labels import render_label
    value=c.execute(text('SELECT * FROM code_payload_versions WHERE id=:id'),{'id':payload_id}).mappings().one()
    if c.execute(text("SELECT kind FROM code_events WHERE payload_id=:id"),{'id':payload_id}).scalar_one()=='payload_conflict':raise ValueError('Конфликтный payload нельзя использовать для этикетки')
    # PN must be backed by this organization's verified source; never infer it from a title.
    parsed=parse(bytes(value['normalized']))
    if not c.execute(text('''SELECT 1 FROM product_pns p JOIN products pr ON pr.id=p.product_id WHERE pr.gtin=:gtin AND p.organization_id=:org AND p.pn=:pn LIMIT 1'''),{'gtin':parsed.gtin,'org':value['organization_id'],'pn':pn}).scalar():raise ValueError('PN не подтверждён источником')
    quantity=parsed.quantity if parsed.quantity is not None else 1
    data=render_label(parsed.normalized,pn,quantity)
    return _persist_file(c,store,value,data,'label_pdf','application/pdf',{'pn':pn,'quantity':quantity,'template':'radio-58x40-v1','payload_sha256':value['normalized_sha256']})
