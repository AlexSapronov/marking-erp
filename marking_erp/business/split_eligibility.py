"""Read NK eligibility before preparing and dispatching a REAPPLY order."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from .errors import RemoteError


def check(client, gtin):
    raw = client.details(gtin=gtin)
    attrs = raw.get('good_attrs')
    if not isinstance(attrs, list):
        raise RemoteError('НК не подтвердил переменную характеристику количества')
    values = [a.get('attr_value') for a in attrs
              if isinstance(a, dict) and str(a.get('attr_id')) == '24003']
    # Fail closed on missing, contradictory or unsupported characteristics.
    # This is the official radioelectronics attribute already used by NK presets.
    if not values or any(not isinstance(v, str) or v.strip().upper() != 'КОЛИЧЕСТВО' for v in values):
        raise RemoteError('Разделение требует характеристики «КОЛИЧЕСТВО» (24003) в карточке НК')
    return {'good_id': raw.get('good_id'), 'gtin': gtin, 'attribute_id': '24003',
            'value': 'КОЛИЧЕСТВО', 'checked_at': datetime.now(timezone.utc).isoformat(),
            'sha256': sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True).encode()).hexdigest()}
