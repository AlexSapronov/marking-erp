"""Explicit non-owner DB profile. Run grants only as the migration owner."""
import re
from sqlalchemy import text
from .migrate import schema_head

READ_ONLY = frozenset(('alembic_version', 'permissions', 'seed_state', 'organizations'))
APPEND_ONLY = frozenset(('audit_events', 'configuration_changes', 'products', 'codes',
    'product_pns', 'code_payload_versions', 'code_events', 'files', 'operation_attempts',
    'operation_receipts', 'operation_events', 'signer_leases', 'signer_receipts'))


def _identifier(value):
    if not isinstance(value, str) or re.fullmatch(r'[a-z][a-z0-9_]{0,62}', value) is None:
        raise ValueError('runtime_identifier')
    return '"' + value + '"'


def _owners(connection, schema):
    return connection.execute(text('''SELECT datdba FROM pg_database WHERE datname=current_database()
        UNION SELECT nspowner FROM pg_namespace WHERE nspname=:schema
        UNION SELECT c.relowner FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=:schema
        UNION SELECT p.proowner FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=:schema'''), {'schema': schema}).scalars().all()


def role_is_restricted(connection, role, schema):
    flags = connection.execute(text('''SELECT rolsuper OR rolcreaterole OR rolcreatedb
        OR rolreplication OR rolbypassrls FROM pg_roles WHERE rolname=:role'''), {'role': role}).scalar_one_or_none()
    if flags is None or flags:
        return False
    for owner in _owners(connection, schema):
        if connection.execute(text("SELECT pg_has_role(CAST(:role AS name),CAST(:owner AS oid),'MEMBER')"), {'role': role, 'owner': owner}).scalar_one():
            return False
    return not connection.execute(text("SELECT has_database_privilege(:role,current_database(),'CREATE') OR has_schema_privilege(:role,:schema,'CREATE')"), {'role': role, 'schema': schema}).scalar_one()


def apply_runtime_grants(connection, role, schema='public'):
    role_sql, schema_sql = _identifier(role), _identifier(schema)
    if not all(connection.execute(text("SELECT pg_has_role(current_user,CAST(:owner AS oid),'USAGE')"),
                                  {'owner': owner}).scalar_one() for owner in _owners(connection, schema)):
        raise ValueError('runtime_grants_require_owner')
    if connection.execute(text(f'SELECT version_num FROM {schema_sql}.alembic_version')).scalars().all() != [schema_head()]:
        raise ValueError('runtime_schema_revision')
    if not role_is_restricted(connection, role, schema):
        raise ValueError('runtime_role_must_not_own_or_create_objects')
    connection.exec_driver_sql(f'GRANT USAGE ON SCHEMA {schema_sql} TO {role_sql}')
    connection.exec_driver_sql(f'REVOKE ALL ON ALL TABLES IN SCHEMA {schema_sql} FROM {role_sql}')
    connection.exec_driver_sql(f'GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA {schema_sql} TO {role_sql}')
    for tables, rights in ((READ_ONLY, 'INSERT,UPDATE,DELETE'), (APPEND_ONLY, 'UPDATE,DELETE')):
        names = ','.join(schema_sql + '.' + _identifier(table) for table in sorted(tables))
        connection.exec_driver_sql(f'REVOKE {rights} ON {names} FROM {role_sql}')
    connection.exec_driver_sql(f'REVOKE ALL ON ALL SEQUENCES IN SCHEMA {schema_sql} FROM {role_sql}')
    connection.exec_driver_sql(f'GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA {schema_sql} TO {role_sql}')
    connection.exec_driver_sql(f'GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA {schema_sql} TO {role_sql}')


def runtime_profile_matches(connection):
    role, schema = connection.execute(text('SELECT current_user,current_schema()')).one()
    if schema is None or not role_is_restricted(connection, role, schema):
        return False
    for table in READ_ONLY | APPEND_ONLY:
        forbidden = ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'TRIGGER') if table in READ_ONLY else ('UPDATE', 'DELETE', 'TRUNCATE', 'TRIGGER')
        for privilege in forbidden:
            if connection.execute(text('SELECT has_table_privilege(:role,:table,:privilege)'),
                    {'role': role, 'table': _identifier(schema) + '.' + _identifier(table), 'privilege': privilege}).scalar_one():
                return False
    # Detect missing DML/sequence grants before the first login request.
    tables = connection.execute(text('SELECT tablename FROM pg_tables WHERE schemaname=:schema'), {'schema': schema}).scalars().all()
    for table in tables:
        for privilege in ('TRUNCATE', 'TRIGGER'):
            if connection.execute(text('SELECT has_table_privilege(:role,:table,:privilege)'),
                    {'role': role, 'table': _identifier(schema) + '.' + _identifier(table), 'privilege': privilege}).scalar_one():
                return False
        required = ('SELECT',) if table in READ_ONLY else ('SELECT', 'INSERT') if table in APPEND_ONLY else ('SELECT', 'INSERT', 'UPDATE', 'DELETE')
        for privilege in required:
            if not connection.execute(text('SELECT has_table_privilege(:role,:table,:privilege)'),
                    {'role': role, 'table': _identifier(schema) + '.' + _identifier(table), 'privilege': privilege}).scalar_one():
                return False
    sequences = connection.execute(text('SELECT sequencename FROM pg_sequences WHERE schemaname=:schema'), {'schema': schema}).scalars().all()
    for sequence in sequences:
        name = _identifier(schema) + '.' + _identifier(sequence)
        for privilege in ('USAGE', 'SELECT', 'UPDATE'):
            allowed = connection.execute(text('SELECT has_sequence_privilege(:role,:name,:privilege)'),
                {'role': role, 'name': name, 'privilege': privilege}).scalar_one()
            if allowed != (privilege != 'UPDATE'):
                return False
    return True
