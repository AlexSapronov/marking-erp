"""Global administrative diagnostics; no secrets, external probes or mutations."""
from datetime import datetime, timezone
import re
from fastapi import Depends, HTTPException
from sqlalchemy import text
from .migrate import schema_head
from .service import inspect_runtime
from .readiness import operational_status

CHECKS = ('non_root', 'https_cookies', 'release_sha', 'private_file_storage', 'mfa_key',
          'schema_revision', 'database_runtime_role', 'seed_initialized', 'administrator_mfa', 'mfa_decrypt')


def public_status(report):
    values = report.get('checks', {})
    if not isinstance(values, dict):
        values = {}
    checks = {key: values.get(key) if type(values.get(key)) is bool else None for key in CHECKS}
    build = report.get('build_sha')
    if not isinstance(build, str) or re.fullmatch(r'[0-9a-f]{40}', build) is None:
        build = None
    return {'checked_at': datetime.now(timezone.utc).isoformat(), 'build_sha': build,
            'expected_schema_revision': schema_head(), 'checks': checks,
            'technical_ready': report.get('ready') is True and all(v is True for v in checks.values()),
            'business_integrations_verified': False,
            'components': {'nk': 'pilot_required', 'true_api_oms': 'pilot_required',
                           'gost_signer': 'pilot_required', 'tsd': 'pilot_required',
                           'backup_schedule': 'not_verified'}}


def install_system(app, actor, settings, engine):
    @app.get('/api/system/status')
    def status(current=Depends(actor, scope='function')):
        current.require('system.diagnostics.view', administrative=True)
        organizations = set(current.connection.execute(text('SELECT id FROM organizations WHERE enabled')).scalars())
        if not organizations.issubset(current.organizations()):
            raise HTTPException(403, 'Диагностика системы требует доступа ко всем действующим организациям')
        result = public_status(inspect_runtime(settings, engine))
        result['operational'] = operational_status(current.connection, settings,
            inspect_credentials=current.grants().get('integrations.manage') == 'all') if result['checks']['schema_revision'] is True else None
        return result
