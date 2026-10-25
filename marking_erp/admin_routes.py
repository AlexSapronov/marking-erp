"""Reviewable, versioned configuration mutations; caller transaction owns commit."""
import json
from typing import Any, Literal
from uuid import UUID
from fastapi import Depends,HTTPException
from pydantic import Field,StrictInt
from sqlalchemy import text
from .auth_routes import Body
from . import auth
from .seed import GROUPS
from .permission_catalog import catalog,dependency_errors
from .policy import rules_catalog,validate_value,snapshot


class GroupChange(Body):
    revision: StrictInt=Field(ge=1)
    grants: dict[str,Literal['none','own','assigned','all']]
    reason: str=Field(min_length=8,max_length=1000)
    tsd_confirmation: str=''


class AccessCheck(Body):
    user_id: UUID
    permission: str=Field(max_length=100)
    organization_id: UUID
    owner_id: UUID|None=None
    assigned: bool=False


class RulesChange(Body):
    revision: StrictInt=Field(ge=1)
    system_revision: StrictInt=Field(ge=1)
    changes: dict[str,Any]=Field(min_length=1,max_length=40)
    reason: str=Field(min_length=8,max_length=1000)


def record(c,actor,kind,entity,revision,reason,before,after,context=None):
    c.execute(text('''INSERT INTO configuration_changes(kind,entity,revision,actor_id,reason,before_value,after_value,context)
        VALUES (:kind,:entity,:revision,:actor,:reason,CAST(:before AS jsonb),CAST(:after AS jsonb),CAST(:context AS jsonb))'''),
        {'kind':kind,'entity':entity,'revision':revision,'actor':actor.id,'reason':reason.strip(),'before':json.dumps(before),'after':json.dumps(after),'context':json.dumps(context or {})})
    auth.audit(c,actor.id,'configuration.'+kind,entity,{'revision':revision,'reason':reason.strip()})


def global_scope(current):
    ids=set(current.connection.execute(text('SELECT id FROM organizations WHERE enabled')).scalars())
    if not ids.issubset(current.organizations()):raise HTTPException(403,'Системные изменения требуют доступа ко всем действующим организациям')


def group_values(c,slug):
    return dict(c.execute(text('SELECT permission_id,scope FROM group_grants WHERE group_slug=:slug'),{'slug':slug}).tuples().all())


def group_review(current,slug,body):
    if slug not in GROUPS:raise HTTPException(404,'Группа не найдена')
    if len(body.reason.strip())<8:raise HTTPException(422,'Укажите содержательную причину изменения')
    c=current.connection
    c.execute(text('SELECT pg_advisory_xact_lock(683273529)'))
    group=c.execute(text('SELECT revision FROM groups WHERE slug=:slug FOR UPDATE'),{'slug':slug}).scalar_one()
    if group!=body.revision:raise HTTPException(409,'Группа изменена другим администратором. Обновите данные.')
    before=group_values(c,slug);after=body.grants
    if set(after)!=set(before):raise HTTPException(422,'Нужен полный зарегистрированный набор permissions')
    for p in catalog():
        scope=after[p['id']]
        if p['administrative'] and scope not in ('none','all'):raise HTTPException(422,'Административная область может быть только все или запрет')
        if slug=='specialist' and scope!=('none' if p['administrative'] else 'all'):raise HTTPException(422,'Специалист имеет все операционные права и не имеет административных')
    if slug=='administrator' and after['permissions.manage']!='all':raise HTTPException(422,'Нельзя отключить управление правами у администратора')
    errors=dependency_errors(after)
    if errors:raise HTTPException(422,errors[0])
    if any(p['administrative'] and after[p['id']]!='none' for p in catalog()):
        if c.execute(text('SELECT EXISTS(SELECT 1 FROM users WHERE group_slug=:slug AND mfa_ciphertext IS NULL)'),{'slug':slug}).scalar():raise HTTPException(422,'Сначала настройте TOTP всем пользователям этой группы')
    baseline={p['id']:('none' if p['defaults'][slug]=='-' else p['defaults'][slug]) for p in catalog()}
    expansion=slug=='tsd' and any(after[k]!='none' and (baseline[k]=='none' or after[k] not in (baseline[k],)) for k in after)
    if expansion and body.tsd_confirmation!='РАСШИРИТЬ ТСД':raise HTTPException(422,'Расширение профиля ТСД требует подтверждения: РАСШИРИТЬ ТСД')
    diff=[{'id':k,'before':before[k],'after':after[k]} for k in after if before[k]!=after[k]]
    if not diff:raise HTTPException(422,'Изменений нет')
    return before,after,diff,expansion


def install_admin(app,actor):
    dependency=Depends(actor,scope='function')
    @app.get('/api/groups')
    def groups(current=dependency):
        current.require('permissions.manage',administrative=True)
        return {'catalog':catalog(),'groups':[{'slug':g,'name':n,'revision':current.connection.execute(text('SELECT revision FROM groups WHERE slug=:g'),{'g':g}).scalar_one(),'grants':group_values(current.connection,g)} for g,n in GROUPS.items()]}

    @app.post('/api/groups/{slug}/preview')
    def preview(slug:str,body:GroupChange,current=dependency):
        current.require('permissions.manage',administrative=True);global_scope(current)
        _,_,diff,expansion=group_review(current,slug,body)
        return {'diff':diff,'tsd_expansion':expansion,'notice':'Все сессии пользователей этой группы будут завершены.'}

    @app.put('/api/groups/{slug}')
    def update(slug:str,body:GroupChange,current=dependency):
        current.require('permissions.manage',administrative=True);global_scope(current)
        before,after,diff,expansion=group_review(current,slug,body);c=current.connection
        c.execute(text('UPDATE group_grants SET scope=:scope WHERE group_slug=:slug AND permission_id=:id'),[{'slug':slug,'id':k,'scope':v} for k,v in after.items()])
        c.execute(text('UPDATE groups SET revision=revision+1 WHERE slug=:slug'),{'slug':slug})
        c.execute(text('UPDATE users SET auth_version=auth_version+1 WHERE group_slug=:slug'),{'slug':slug})
        c.execute(text('DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE group_slug=:slug)'),{'slug':slug})
        record(c,current,'group',slug,body.revision+1,body.reason,before,after)
        return {'revision':body.revision+1,'diff':diff,'tsd_expansion':expansion}

    @app.post('/api/groups/check-access')
    def check_access(body:AccessCheck,current=dependency):
        current.require('permissions.manage',administrative=True)
        user=auth.get_managed_user(current,body.user_id)
        auth.check_organization_scope(current,[body.organization_id])
        from datetime import datetime,timezone
        target=auth.Actor(current.connection,dict(user),'simulation',datetime.now(timezone.utc))
        allowed=False;message='Пользователь отключён'
        if user['enabled']:
            try:
                target.require(body.permission,organization_id=body.organization_id,owner_id=body.owner_id,assigned=body.assigned)
                allowed=True;message='Permission и контекст допускают действие'
            except HTTPException as e:message=e.detail if isinstance(e.detail,str) else e.detail['message']
        return {'allowed':allowed,'message':message,'simulation':True,'notice':'Проверка по указанным условиям. Существование документа, назначение, workflow и MFA действующей сессии здесь не подтверждаются.'}

    @app.get('/api/configuration/history')
    def history(kind:Literal['group','rules'],entity:str,before_id:int|None=None,current=dependency):
        current.require('permissions.manage' if kind=='group' else 'business_rules.manage',administrative=True)
        if kind=='group':
            if entity not in GROUPS:raise HTTPException(404,'Группа не найдена')
        elif entity=='system':global_scope(current)
        else:auth.check_organization_scope(current,[entity])
        result=current.connection.execute(text('''SELECT h.*,u.display_name AS actor_name FROM configuration_changes h
            JOIN users u ON u.id=h.actor_id WHERE h.kind=:kind AND h.entity=:entity
            AND (CAST(:before AS bigint) IS NULL OR h.id<CAST(:before AS bigint)) ORDER BY h.id DESC LIMIT 25'''),{'kind':kind,'entity':entity,'before':before_id}).mappings().all()
        return {'history':[dict(r) for r in result]}

    def rule_scope(current,key,write=False):
        current.require('business_rules.manage',administrative=True)
        if key=='system':global_scope(current);return None
        try:org=UUID(key)
        except ValueError:raise HTTPException(422,'Неверная организация')
        auth.check_organization_scope(current,[org]);return org

    @app.get('/api/business-rules/{key}')
    def read_rules(key:str,current=dependency):
        org=rule_scope(current,key);c=current.connection
        revisions=dict(c.execute(text("SELECT scope_key,revision FROM policy_revisions WHERE scope_key IN ('system',:key)"),{'key':key}).tuples().all())
        overrides=dict(c.execute(text('SELECT rule_id,value FROM policy_values WHERE scope_key=:key'),{'key':key}).tuples().all())
        system=dict(c.execute(text("SELECT rule_id,value FROM policy_values WHERE scope_key='system'")).tuples().all())
        rows=[{**r,'value':overrides.get(r['id'],system.get(r['id'],r['default'])),'inherited':r['id'] not in overrides,'inherited_value':system.get(r['id'],r['default']) if org else r['default']} for r in rules_catalog()]
        return {'scope':key,'revision':revisions.get(key,1),'system_revision':revisions['system'],'rules':rows}

    def review_rules(current,key,body):
        org=rule_scope(current,key,True);c=current.connection
        if len(body.reason.strip())<8:raise HTTPException(422,'Укажите содержательную причину изменения')
        # System lock also serializes creation of a new org scope and snapshots of defaults.
        system=c.execute(text("SELECT revision FROM policy_revisions WHERE scope_key='system' FOR UPDATE")).scalar_one()
        if system!=body.system_revision:raise HTTPException(409,'Системные правила изменены. Обновите данные.')
        revision=c.execute(text('SELECT revision FROM policy_revisions WHERE scope_key=:key FOR UPDATE'),{'key':key}).scalar() or 1
        if revision!=body.revision:raise HTTPException(409,'Правила изменены другим администратором. Обновите данные.')
        before=dict(c.execute(text('SELECT rule_id,value FROM policy_values WHERE scope_key=:key'),{'key':key}).tuples().all());after=dict(before);defs={r['id']:r for r in rules_catalog()}
        for id,value in body.changes.items():
            if id not in defs:raise HTTPException(422,'Неизвестное правило')
            if not defs[id]['mutable']:raise HTTPException(422,'Это правило заблокировано: '+id)
            if value is None:after.pop(id,None)
            else:
                try:validate_value(defs[id],value)
                except (ValueError,TypeError):raise HTTPException(422,'Недопустимое значение: '+id)
                after[id]=value
        inherited={k:r['default'] for k,r in defs.items()}
        if org:inherited.update(dict(c.execute(text("SELECT rule_id,value FROM policy_values WHERE scope_key='system'")).tuples().all()))
        resolved_before={**inherited,**before};resolved_after={**inherited,**after}
        diff=[{'id':k,'before':resolved_before[k],'after':resolved_after[k],'inherit':k not in after} for k in body.changes if before.get(k)!=after.get(k) or (k in before)!=(k in after)]
        if not diff:raise HTTPException(422,'Изменений нет')
        return org,before,after,diff,{'system_revision':system,'scope_revision':revision,'resolved_before':resolved_before,'resolved_after':resolved_after}

    @app.post('/api/business-rules/{key}/preview')
    def rules_preview(key:str,body:RulesChange,current=dependency):
        _,_,_,diff,_=review_rules(current,key,body)
        return {'diff':diff,'notice':'Будущие команды используют новую версию. Уже подготовленные сохраняют прежний снимок.'}

    @app.put('/api/business-rules/{key}')
    def rules_save(key:str,body:RulesChange,current=dependency):
        org,before,after,diff,context=review_rules(current,key,body);c=current.connection
        c.execute(text('INSERT INTO policy_revisions(scope_key,organization_id) VALUES (:key,:org) ON CONFLICT DO NOTHING'),{'key':key,'org':org})
        c.execute(text('UPDATE policy_revisions SET revision=revision+1 WHERE scope_key=:key'),{'key':key})
        c.execute(text('DELETE FROM policy_values WHERE scope_key=:key'),{'key':key})
        if after:c.execute(text('INSERT INTO policy_values VALUES (:key,:id,CAST(:value AS jsonb))'),[{'key':key,'id':k,'value':json.dumps(v)} for k,v in after.items()])
        record(c,current,'rules',key,body.revision+1,body.reason,before,after,context)
        return {'revision':body.revision+1,'diff':diff}
