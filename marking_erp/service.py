"""POSIX deployment entrypoint: private JSON credentials, read-only checks, loopback server."""
import argparse
import json
import os
from pathlib import Path
import re
import stat

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from .config import Settings
from .database import database_engine
from .migrate import schema_head
from .runtime_access import runtime_profile_matches
from .seed import SEED_KEY


class ServiceError(ValueError):
    pass


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ServiceError('service_config_duplicate_key')
        result[key] = value
    return result


def load_settings(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            metadata = os.fstat(stream.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077
                    or metadata.st_uid not in (0, os.getuid())):
                raise ServiceError('service_config_permissions')
            body = stream.read(65537)
        if len(body) > 65536:
            raise ServiceError('service_config_size')
        value = json.loads(body.decode('utf-8'), object_pairs_hook=_object)
        fields = {'database_url', 'mfa_encryption_key', 'public_url', 'secure_cookie', 'file_storage_path', 'build_sha'}
        if not isinstance(value, dict) or set(value) != fields:
            raise ServiceError('service_config_fields')
        return Settings(**value)
    except ServiceError:
        raise
    except (OSError, ValueError, UnicodeError, RecursionError):
        pass
    raise ServiceError('service_config_invalid') from None


def filesystem_ready(path):
    path = Path(path)
    try:
        if any(part.is_symlink() for part in (path, *path.parents)):
            return False
        metadata = path.stat()
        return (stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == os.getuid()
                and not metadata.st_mode & 0o077 and os.access(path, os.R_OK | os.W_OK | os.X_OK))
    except OSError:
        return False


def inspect_runtime(settings, engine):
    checks = {'non_root': os.getuid() != 0,
              'https_cookies': settings.public_url.startswith('https://') and settings.secure_cookie,
              'release_sha': re.fullmatch(r'[0-9a-f]{40}', settings.build_sha) is not None,
              'private_file_storage': filesystem_ready(settings.file_storage_path),
              'mfa_key': settings.mfa_encryption_key is not None}
    try:
        with engine.connect() as connection:
            connection.execute(text('SET TRANSACTION READ ONLY'))
            checks['schema_revision'] = connection.execute(text('SELECT version_num FROM alembic_version')).scalars().all() == [schema_head()]
            checks['database_runtime_role'] = runtime_profile_matches(connection)
            checks['seed_initialized'] = bool(connection.execute(text('SELECT 1 FROM seed_state WHERE key=:key'), {'key': SEED_KEY}).scalar())
            admins = connection.execute(text("SELECT mfa_ciphertext FROM users WHERE enabled AND group_slug='administrator'")).scalars().all()
            checks['administrator_mfa'] = bool(admins) and all(secret is not None for secret in admins)
            checks['mfa_decrypt'] = False
            if settings.mfa_encryption_key:
                cipher = Fernet(settings.mfa_encryption_key.get_secret_value().encode())
                secrets = connection.execute(text('SELECT mfa_ciphertext FROM users WHERE mfa_ciphertext IS NOT NULL')).scalars()
                for secret in secrets:
                    cipher.decrypt(bytes(secret))
                checks['mfa_decrypt'] = True
    except (SQLAlchemyError, InvalidToken, ValueError):
        checks['database_or_mfa'] = False
    return {'ready': all(checks.values()), 'checks': checks, 'schema_revision': schema_head(),
            'build_sha': settings.build_sha if checks['release_sha'] else None, 'business_integrations_verified': False}


def main():
    parser = argparse.ArgumentParser(description='ERP service preflight and loopback server; no migrations or seed')
    parser.add_argument('--config', type=Path)
    parser.add_argument('action', choices=('check', 'serve'))
    args = parser.parse_args()
    try:
        path = args.config
        if path is None:
            directory = os.environ.get('CREDENTIALS_DIRECTORY')
            if not directory:
                raise ServiceError('service_credentials_required')
            path = Path(directory) / 'erp-config'
        settings = load_settings(path)
    except ServiceError as error:
        parser.exit(1, str(error) + '\n')
    engine = database_engine(settings)
    try:
        report = inspect_runtime(settings, engine)
        if args.action == 'check':
            print(json.dumps(report))
        if not report['ready']:
            parser.exit(1, 'service_preflight_failed\n')
        if args.action == 'serve':
            import uvicorn
            from .web import create_app
            uvicorn.run(create_app(settings, engine), host='127.0.0.1', port=8000, workers=1,
                        proxy_headers=True, forwarded_allow_ips='127.0.0.1', access_log=False,
                        server_header=False, timeout_graceful_shutdown=30)
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
