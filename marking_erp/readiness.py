"""Local operational evidence only; never probes services or retries work."""
from cryptography.fernet import InvalidToken
from sqlalchemy import text
from .business.connections import load


def operational_status(c, settings, *, inspect_credentials=False):
    organizations = []
    for org in c.execute(text('SELECT id,name FROM organizations WHERE enabled ORDER BY name,id')).mappings():
        oid = org['id']
        row = c.execute(text('SELECT public FROM integration_settings WHERE organization_id=:org'), {'org': oid}).scalar_one_or_none()
        config = row or {}
        credentials = None
        credential_state = 'restricted' if not inspect_credentials else 'missing'
        if row is not None and inspect_credentials:
            try:
                secret = load(c, settings, oid)
                credentials = {key: bool(secret.get(key)) for key in ('nk_key', 'true_token', 'oms_token')}
                credential_state = 'readable'
            except (InvalidToken, ValueError, TypeError, KeyError):
                credential_state = 'unreadable'
        signers = dict(c.execute(text('''SELECT count(*) FILTER (WHERE d.enabled) AS registered,
            count(*) FILTER (WHERE d.enabled AND v.enabled) AS trusted,
            count(*) FILTER (WHERE d.enabled AND v.enabled AND d.last_seen_at>now()-interval '5 minutes') AS recent,
            count(*) FILTER (WHERE d.enabled AND v.enabled AND ARRAY['auth.true','auth.oms']::text[] <@ d.allowed_kinds) AS token_capable
            FROM signer_devices d LEFT JOIN signer_verifiers v ON v.device_id=d.id WHERE d.organization_id=:org'''), {'org': oid}).mappings().one())
        printers = dict(c.execute(text('''SELECT count(*) FILTER (WHERE enabled) AS registered,
            count(*) FILTER (WHERE enabled AND last_seen_at>now()-interval '5 minutes') AS recent
            FROM printer_devices WHERE organization_id=:org'''), {'org': oid}).mappings().one())
        renewal = [dict(r) for r in c.execute(text('''SELECT service,renewed_at,next_refresh_at,
            error<>'' AS needs_attention FROM integration_auth_state WHERE organization_id=:org ORDER BY service'''), {'org': oid}).mappings()]
        organizations.append({'organization_id': oid, 'name': org['name'], 'settings_saved': row is not None,
            'environment': config.get('environment') if config.get('environment') in ('production', 'sandbox') else None,
            'writes_enabled': config.get('writes_enabled') is True,
            'warehouse_live_checks': config.get('warehouse_live_checks', True) is True,
            'auto_refresh_tokens': config.get('auto_refresh_tokens') is True,
            'credential_state': credential_state, 'credentials': credentials,
            'signers': signers, 'printers': printers, 'token_renewal': renewal})
    # Only aggregate local states. Payloads, reasons, certificate fingerprints and device tokens never leave here.
    operations = [dict(r) for r in c.execute(text('''SELECT state,count(*) AS count,min(updated_at) AS oldest_at
        FROM external_operations WHERE organization_id IN (SELECT id FROM organizations WHERE enabled)
        AND state NOT IN ('confirmed','rejected','cancelled_before_dispatch') GROUP BY state ORDER BY state''')).mappings()]
    printing = [dict(r) for r in c.execute(text('''SELECT state,count(*) AS count,min(created_at) AS oldest_at
        FROM print_jobs WHERE organization_id IN (SELECT id FROM organizations WHERE enabled)
        AND state IN ('queued','held','claimed','unknown') GROUP BY state ORDER BY state''')).mappings()]
    incidents = dict(c.execute(text('''SELECT count(*) AS active,
        count(*) FILTER (WHERE cardinality(restrictions)>0) AS quarantined,
        count(*) FILTER (WHERE assignee_id IS NULL) AS unassigned
        FROM incidents WHERE status<>'resolved' AND organization_id IN (SELECT id FROM organizations WHERE enabled)''')).mappings().one())
    from .worker_health import health
    return {'organizations': organizations, 'operations': operations, 'printing': printing, 'incidents': incidents,
            'external_probe_performed': False, 'hardware_verified': False, **health(c)}
