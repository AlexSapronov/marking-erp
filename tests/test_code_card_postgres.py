"""A dossier must not turn code access into access to another employee's work."""
import json
from uuid import uuid4
import pytest
from sqlalchemy import text
from test_auth_postgres import env,client,login
from test_business_registry_postgres import allocated
from marking_erp.business import store
from marking_erp.business.worker import background
from marking_erp.registry import capture_code
from test_gs1 import sample,SERIAL

pytestmark=[pytest.mark.postgres,pytest.mark.usefixtures('fresh_schema')]
SECTIONS=('documents','processes','warehouse','printing','reprints','incidents','lineage')


def base(allocated):
    env,item,source,code=allocated
    return '/api/codes/'+str(code['registry_code_id'])


def test_card_links_preserve_scopes_and_do_not_leak_bodies(allocated,engine):
    env,item,source,code=allocated;path=base(allocated);org=str(env[1])
    with engine.begin() as db:
        actor=background(db,env[3]['specialist'],env[1])
        atk=store.create(actor,'atk','ATK-visible',{'codes':[str(code['id'])],'private_secret':'never return body'},env[1],state='ready')
        split=store.create(actor,'split','Split-parent',{'source_cis':code['cis']},env[1],state='receiving')
        child=capture_code(db,env[1],env[3]['bm'],sample(37,SERIAL[:-1]+b'Z'),actor_id=env[3]['bm'],source_ref='fixture:split')
        db.execute(text('''INSERT INTO business_codes(id,organization_id,cis,gtin,base,payload,item_id,quantity,state,tnved,pn,evidence_ref,registry_code_id,registry_payload_id)
            SELECT :new,organization_id,:cis,gtin,base,:raw,:split,1,'emitted',tnved,pn,evidence_ref,:registry,:payload FROM business_codes WHERE id=:old'''),
            {'new':uuid4(),'cis':sample(37,SERIAL[:-1]+b'Z')[:38].decode(),'raw':sample(37,SERIAL[:-1]+b'Z'),'split':split['id'],'registry':child['id'],'payload':child['payload_id'],'old':code['id']})
    with client(env) as c:
        login(c,'administrator')
        r=c.get(path+'/card',params={'organization_id':org});assert r.status_code==200,r.text
        card=r.json()['code'];assert card['allocation']['state']=='emitted' and card['last_check'] is None
        assert card['label_quantity']==10
        for section in SECTIONS:
            response=c.get(path+'/relations',params={'organization_id':org,'section':section})
            assert response.status_code==200,response.text
            assert 'never return body' not in response.text and 'lease_hash' not in response.text
            if section=='processes':assert {str(item),str(atk['id']),str(split['id'])}=={r['id'] for r in response.json()['items']}
            if section=='documents':assert [r['id'] for r in response.json()['items']]==[str(source)]
            if section=='lineage':assert [r['id'] for r in response.json()['items']]==[str(child['id'])]
    with client(env) as c:
        login(c,'bm')
        processes=c.get(path+'/relations',params={'organization_id':org,'section':'processes'}).json()['items']
        assert [r['id'] for r in processes]==[str(item)]
        with engine.begin() as db:db.execute(text("UPDATE group_grants SET scope='none' WHERE group_slug='bm' AND permission_id='marking.documents.view'"))
        assert c.get(path+'/card',params={'organization_id':org}).json()['code']['allocation'] is None
        assert c.get(path+'/relations',params={'organization_id':org,'section':'documents'}).json()['items']==[]
        assert c.get(path+'/relations',params={'organization_id':org,'section':'processes'}).json()['items']==[]
        assert c.get(path+'/card',params={'organization_id':str(env[2])}).status_code==404
    with client(env) as c:
        login(c,'tsd');assert c.get(path+'/card',params={'organization_id':org}).status_code==403


def test_relationship_pagination_filters_before_limit_and_rechecks_grants(allocated,engine):
    env,item,source,code=allocated;path=base(allocated);org=str(env[1])
    with engine.begin() as db:
        actor=background(db,env[3]['specialist'],env[1])
        for n in range(28):store.create(actor,'atk',f'ATK-{n}',{'codes':[str(code['id'])]},env[1],state='ready')
    with client(env) as c:
        login(c,'administrator')
        params={'organization_id':org,'section':'processes'}
        first=c.get(path+'/relations',params=params).json()
        second=c.get(path+'/relations',params={**params,**first['next_cursor']}).json()
        assert len(first['items'])==25 and len(second['items'])==4 and second['next_cursor'] is None
        assert len({r['id'] for r in first['items']+second['items']})==29
        with engine.begin() as db:db.execute(text("UPDATE group_grants SET scope='none' WHERE group_slug='administrator' AND permission_id='logistics.atk.view'"))
        assert [r['id'] for r in c.get(path+'/relations',params=params).json()['items']]==[str(item)]
        assert c.get(path+'/relations',params={**params,'before_id':str(uuid4())}).status_code==422


def test_warehouse_expectations_scans_and_historical_check(allocated,engine):
    env,item,source,code=allocated;path=base(allocated);org=str(env[1]);task=uuid4()
    with client(env) as c:
        login(c,'logistics')
        response=c.post('/api/logistics/warehouse-transfer',json={'request_id':str(task),'source_id':str(source),'source_revision':2,'kind':'receipt','assignees':[str(env[3]['warehouse'])]})
        assert response.status_code==200,response.text
    with engine.begin() as db:
        db.execute(text("UPDATE warehouse_tasks SET expected_codes=CAST(:codes AS jsonb),verified_at=now() WHERE id=:id"),{'codes':json.dumps([code['cis']]),'id':task})
        db.execute(text('''INSERT INTO warehouse_scans(id,task_id,line_index,cis,gtin,quantity,payload,actor_id,verification)
            VALUES (:id,:task,0,:cis,:gtin,10,:payload,:actor,CAST(:verification AS jsonb))'''),
            {'id':uuid4(),'task':task,'cis':code['cis'],'gtin':code['gtin'],'payload':bytes(code['payload']),'actor':env[3]['warehouse'],
             'verification':json.dumps({'status':'INTRODUCED','ownerInn':'1234567890','gtin':code['gtin'],'token':'never show this'})})
    with client(env) as c:
        login(c,'warehouse')
        r=c.get(path+'/card',params={'organization_id':org});assert r.status_code==200,r.text
        assert r.json()['code']['last_check']['facts']['status']=='INTRODUCED' and 'never show' not in r.text
        tasks=c.get(path+'/relations',params={'organization_id':org,'section':'warehouse'}).json()['items']
        assert len(tasks)==1 and tasks[0]['expected_reservation'] and tasks[0]['scanned']
        with engine.begin() as db:db.execute(text('DELETE FROM operation_assignments WHERE target_id=:task'),{'task':task})
        assert c.get(path+'/card',params={'organization_id':org}).status_code==403


def test_printing_membership_reprint_privacy_and_incident_restrictions(allocated,engine):
    from marking_erp.business.printing import register_printer
    env,item,source,code=allocated;path=base(allocated);org=str(env[1])
    with engine.begin() as db:device,_=register_printer(db,env[1],'Fixture','Local printer')
    with client(env) as c:
        login(c,'specialist')
        rid=str(uuid4());reprint=c.post('/api/business-codes/'+str(code['id'])+'/reprint',json={'request_id':rid,'reason':'Повреждена этикетка при перевозке'})
        assert reprint.status_code==200,reprint.text
        job=c.post('/api/business-files/'+reprint.json()['file_id']+'/print',json={'request_id':str(uuid4()),'printer_id':str(device),'reprint_id':rid})
        assert job.status_code==200,job.text
        with engine.connect() as db:pdf=db.execute(text("SELECT id FROM business_artifacts WHERE item_id=:id AND kind='labels_pdf'"),{'id':item}).scalar_one()
        regular=c.post('/api/business-files/'+str(pdf)+'/print',json={'request_id':str(uuid4()),'printer_id':str(device)})
        assert regular.status_code==200,regular.text
        issue=c.post('/api/incidents',json={'organization_id':org,'request_id':str(uuid4()),'source_kind':'business','source_id':str(item),'code':bytes(code['payload']).decode(),'category':'damaged_label','description':'Повреждение обнаружено на складе','restrictions':['shipment']})
        assert issue.status_code==200,issue.text
        reprints=c.get(path+'/relations',params={'organization_id':org,'section':'reprints'}).json()['items']
        assert len(reprints)==1 and reprints[0]['scanned_copies']==0 and reprints[0]['reason']=='Повреждена этикетка при перевозке'
        printing=c.get(path+'/relations',params={'organization_id':org,'section':'printing'}).json()['items']
        assert len(printing)==2 and all('receipt' not in r and 'lease_hash' not in r for r in printing)
        incidents=c.get(path+'/relations',params={'organization_id':org,'section':'incidents'}).json()['items']
        assert len(incidents)==1 and incidents[0]['restrictions']==['shipment']
        # A broad standalone grant cannot bypass the print-view dependency.
        with engine.begin() as db:db.execute(text("UPDATE group_grants SET scope='none' WHERE group_slug='specialist' AND permission_id='labels.print.view'"))
        assert c.get(path+'/relations',params={'organization_id':org,'section':'reprints'}).json()['items']==[]
    with client(env) as c:
        login(c,'administrator')
        assert len(c.get(path+'/relations',params={'organization_id':org,'section':'reprints'}).json()['items'])==1
        with engine.begin() as db:db.execute(text("UPDATE sessions SET elevated_at=now()-interval '10 minutes' WHERE user_id=:id"),{'id':env[3]['administrator']})
        assert c.get(path+'/relations',params={'organization_id':org,'section':'reprints'}).json()['items']==[]


def test_confirmed_replacement_keeps_ancestor_quarantine_visible(allocated,engine):
    env,item,source,old=allocated;org=str(env[1]);new_id=uuid4()
    with client(env) as c:
        login(c,'specialist')
        incident=c.post('/api/incidents',json={'organization_id':org,'request_id':str(uuid4()),'category':'damaged_label','description':'Ограничение сохраняется до разбора замены','code':bytes(old['payload']).decode(),'restrictions':['shipment']})
        assert incident.status_code==200,incident.text
    with engine.begin() as db:
        actor=background(db,env[3]['specialist'],env[1])
        remark=store.create(actor,'remark','Confirmed replacement',{'old_cis':old['cis']},env[1],state='done')
        raw=sample(37,SERIAL[:-1]+b'R');new=capture_code(db,env[1],env[3]['bm'],raw,actor_id=env[3]['bm'],source_ref='fixture:confirmed-replacement')
        db.execute(text('''INSERT INTO business_codes(id,organization_id,cis,gtin,base,payload,item_id,quantity,state,tnved,pn,evidence_ref,registry_code_id,registry_payload_id)
            SELECT :new,organization_id,:cis,gtin,:base,:raw,:item,quantity,'applied',tnved,pn,evidence_ref,:registry,:payload FROM business_codes WHERE id=:old'''),
            {'new':new_id,'cis':raw[:38].decode(),'base':raw,'raw':raw,'item':remark['id'],'registry':new['id'],'payload':new['payload_id'],'old':old['id']})
        db.execute(text('INSERT INTO code_replacements(old_code_id,new_code_id,remark_id,external_document_id) VALUES (:old,:new,:remark,:external)'),
            {'old':old['id'],'new':new_id,'remark':remark['id'],'external':uuid4()})
    with client(env) as c:
        login(c,'administrator')
        path='/api/codes/'+str(new['id'])+'/relations'
        links=c.get(path,params={'organization_id':org,'section':'lineage'}).json()['items']
        assert [r['id'] for r in links]==[str(old['registry_code_id'])]
        issues=c.get(path,params={'organization_id':org,'section':'incidents'}).json()['items']
        assert len(issues)==1 and issues[0]['restrictions']==['shipment']
        with engine.begin() as db:db.execute(text("UPDATE group_grants SET scope='none' WHERE group_slug='administrator' AND permission_id='incidents.create'"))
        assert c.get(path,params={'organization_id':org,'section':'incidents'}).json()['items']==[]
