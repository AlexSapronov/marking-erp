"""Opt-in read-only catalogue scheduling with fresh delegated permissions."""
from datetime import datetime, timezone
from uuid import UUID
from sqlalchemy import text
from . import store as s
from .catalog_policy import rules
from .connections import load


def start(current, org, *, scheduled=False):
    c = current.connection
    if c.execute(text("SELECT EXISTS(SELECT 1 FROM business_items WHERE kind='sync' AND organization_id=:org AND state IN ('draft','processing'))"), {'org': org}).scalar_one():
        from fastapi import HTTPException
        raise HTTPException(409, 'Синхронизация уже выполняется')
    end = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    return s.create(current, 'sync', 'Синхронизация НК', {
        'from': '1980-01-01 00:00:00', 'to': end,
        'windows': [['1980-01-01 00:00:00', end, 0]],
        'sync_actor': str(current.id), 'offset': 0, 'count': 0,
        'scheduled': scheduled, 'policy': rules(c, org)}, org, state='processing')


def tick(engine, settings):
    from .worker import background
    with engine.begin() as c:
        # Same transaction fence as manual starts; multiple workers cannot create two jobs.
        if not c.execute(text('SELECT pg_try_advisory_xact_lock(683273529)')).scalar_one():return
        configs = c.execute(text("SELECT organization_id,public FROM integration_settings WHERE public->>'nk_auto_sync'='true'")).mappings().all()
        for config in configs:
            org = config['organization_id']
            try:
                with c.begin_nested():
                    actor = background(c, UUID(config['public']['nk_sync_actor']), org)
                    actor.require('nk.sync.execute', organization_id=org, owner_id=actor.id)
                    latest = c.execute(text("SELECT state,updated_at FROM business_items WHERE organization_id=:org AND kind='sync' ORDER BY created_at DESC,id DESC LIMIT 1"), {'org': org}).mappings().first()
                    # A paused traversal must be resumed explicitly, not silently replaced.
                    if latest and latest['state'] != 'done': continue
                    interval = rules(c, org)['values']['nk.sync.interval_seconds']
                    if latest and (datetime.now(timezone.utc)-latest['updated_at']).total_seconds() < interval: continue
                    if not load(c, settings, org).get('nk_key'): continue
                    start(actor, org, scheduled=True)
            except Exception:
                # No remote write is attempted. Revoked/disabled delegates cannot schedule.
                continue
