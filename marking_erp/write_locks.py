"""Authorization fence plus audited, local business locks.

Take the authorization fence before loading the actor or locking business rows.
Shared holders never upgrade it: compound workflows and administrative writes
keep the exclusive fence until their cross-document invariants are scoped too.
Unknown routes deliberately default to exclusive coordination.
"""
import re
from hashlib import sha256
from sqlalchemy import text

FENCE = 683273529
UUID_PATH = r'[0-9a-fA-F-]{36}'
LOCAL_WRITES = (
    ('POST', re.compile(r'/api/documents')),
    ('PUT', re.compile(r'/api/documents/' + UUID_PATH)),
    ('POST', re.compile(r'/api/documents/' + UUID_PATH + r'/prepare')),
    ('POST', re.compile(r'/api/logistics/warehouse-transfer')),
    ('POST', re.compile(r'/api/warehouse/tasks/' + UUID_PATH + r'/(?:scans|check|close|reopen|cancel|archive|unarchive)')),
    ('DELETE', re.compile(r'/api/warehouse/tasks/' + UUID_PATH + r'/scans/' + UUID_PATH)),
)


def authorization_fence(c, method, path):
    # These upload previews are read-only, bounded, and still enforce CSRF/RBAC.
    if method == 'POST' and (path == '/api/documents/import-preview' or
            re.fullmatch(r'/api/documents/' + UUID_PATH + r'/warehouse-composition/preview', path)):
        return
    shared = any(method == verb and pattern.fullmatch(path) for verb, pattern in LOCAL_WRITES)
    function = 'pg_advisory_xact_lock_shared' if shared else 'pg_advisory_xact_lock'
    c.execute(text('SELECT ' + function + '(:key)'), {'key': FENCE})


def resources(c, namespace, keys, *, rows=False):
    """Transaction locks in deterministic order, including not-yet-existing rows.

    Hash collisions conservatively serialize unrelated resources. Prefixes and
    separators keep business identities distinct; lock order follows actual
    PostgreSQL signed lock keys to remain stable even in the collision case.
    """
    values = sorted({namespace + ':' + str(key) for key in keys})
    if not values:return
    if rows:
        # Persistent identities use scalable row locks, including for 100,000
        # expected CIS. Never delete these coordination rows while writers run.
        hashes=sorted({sha256(value.encode()).hexdigest() for value in values})
        c.execute(text('''INSERT INTO write_resources(key)
            SELECT value FROM unnest(CAST(:keys AS text[])) AS value ORDER BY value
            ON CONFLICT DO NOTHING'''), {'keys':hashes})
        c.execute(text('''SELECT key FROM write_resources
            WHERE key=ANY(CAST(:keys AS text[])) ORDER BY key FOR UPDATE'''), {'keys':hashes}).all()
    else:
        c.execute(text('''SELECT pg_advisory_xact_lock(key) FROM (
            SELECT DISTINCT hashtextextended(value,683273550) AS key
            FROM unnest(CAST(:values AS text[])) AS value ORDER BY key
        ) ordered_locks'''), {'values': values})
