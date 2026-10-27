import argparse
import getpass
from cryptography.fernet import Fernet
import pyotp
from alembic import command
from sqlalchemy.exc import SQLAlchemyError
from .config import Settings
from .database import database_engine
from .migrate import migration_config
from .seed import seed_defaults


def main():
    parser = argparse.ArgumentParser(description="Marking ERP database infrastructure")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("migrate")
    commands.add_parser("seed")
    commands.add_parser("generate-mfa-key")
    bootstrap = commands.add_parser("bootstrap-admin")
    bootstrap.add_argument("--username", required=True)
    bootstrap.add_argument("--display-name", required=True)
    bootstrap.add_argument("--inn", required=True)
    bootstrap.add_argument("--organization-name", required=True)
    signer_parser=commands.add_parser('register-signer')
    signer_parser.add_argument('--organization-id',required=True)
    signer_parser.add_argument('--name',required=True)
    signer_parser.add_argument('--certificate-sha256',required=True)
    signer_parser.add_argument('--kind',action='append',required=True)
    printer=commands.add_parser('register-printer')
    printer.add_argument('--organization-id',required=True);printer.add_argument('--name',required=True);printer.add_argument('--printer-name',required=True)
    revoke_printer=commands.add_parser('revoke-printer');revoke_printer.add_argument('--device-id',required=True)
    verifier=commands.add_parser('trust-windows-verifier');verifier.add_argument('--device-id',required=True)
    revoke=commands.add_parser('revoke-signer')
    revoke.add_argument('--device-id',required=True)
    commands.add_parser('recover-operations')
    registry_parser=commands.add_parser('sync-code-registry')
    registry_parser.add_argument('--actor-id',required=True)
    runtime = commands.add_parser('grant-runtime')
    runtime.add_argument('--role', required=True)
    args = parser.parse_args()
    if args.action == "generate-mfa-key":
        print(Fernet.generate_key().decode())
        return
    settings = Settings()
    engine = database_engine(settings)
    try:
        if args.action == "bootstrap-admin":
            if not settings.mfa_encryption_key:
                parser.exit(1, "Configure ERP_MFA_ENCRYPTION_KEY before creating an administrator.\n")
            password = getpass.getpass("Пароль администратора (от 16 символов): ")
            if password != getpass.getpass("Повторите пароль: "):
                parser.exit(1, "Пароли не совпадают.\n")
            totp_secret = pyotp.random_base32()
            print("Добавьте этот ключ вручную в приложение-аутентификатор. Не отправляйте его другим людям:")
            print(totp_secret)
            code = getpass.getpass("Текущий шестизначный код TOTP: ")
        with engine.begin() as connection:
            if args.action == "migrate":
                # Serialize migrations across processes, not just in this CLI.
                from sqlalchemy import text
                connection.execute(text("SELECT pg_advisory_xact_lock(683273527)"))
                config = migration_config()
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
            elif args.action == "seed":
                print("initialized" if seed_defaults(connection) else "already_initialized")
            elif args.action=='trust-windows-verifier':
                from uuid import UUID
                from sqlalchemy import text
                connection.execute(text('INSERT INTO signer_verifiers(device_id) VALUES (:id) ON CONFLICT(device_id) DO UPDATE SET enabled=true'),{'id':UUID(args.device_id)})
                from .auth import audit
                audit(connection,None,'signer.verifier_provisioned',args.device_id,{})
            elif args.action=='register-printer':
                from uuid import UUID
                from .business.printing import register_printer
                id,token=register_printer(connection,UUID(args.organization_id),args.name,args.printer_name)
            elif args.action=='revoke-printer':
                from uuid import UUID
                from sqlalchemy import text
                from .auth import audit
                connection.execute(text('UPDATE printer_devices SET enabled=false WHERE id=:id'),{'id':UUID(args.device_id)})
                audit(connection,None,'printer.revoked',args.device_id)
            elif args.action=='register-signer':
                from uuid import UUID
                from .signer import provision
                from .auth import audit
                id,token=provision(connection,UUID(args.organization_id),args.name,args.certificate_sha256,args.kind)
                audit(connection,None,'signer.provisioned',id,{'organization_id':args.organization_id})
                # Print only after successful database commit below.
            elif args.action=='revoke-signer':
                from uuid import UUID
                from sqlalchemy import text
                from .auth import audit
                connection.execute(text('SELECT pg_advisory_xact_lock(683273529)'))
                connection.execute(text('UPDATE signer_devices SET enabled=false WHERE id=:id'),{'id':UUID(args.device_id)})
                audit(connection,None,'signer.revoked',args.device_id)
            elif args.action=='recover-operations':
                from .operations import recover_expired
                print('recovered:',recover_expired(connection))
            elif args.action=='sync-code-registry':
                from uuid import UUID
                from .business.registry import backfill
                count=backfill(connection,settings,UUID(args.actor_id))
            elif args.action=='grant-runtime':
                from .runtime_access import apply_runtime_grants
                apply_runtime_grants(connection,args.role)
            else:
                from .auth import bootstrap_admin
                bootstrap_admin(connection, settings, args.username, args.display_name, password, totp_secret, code, args.inn, args.organization_name)
                print("Администратор создан. Для входа используйте следующий код TOTP.")
        if args.action in ('register-signer','register-printer'):
            print('Device:',id)
            print('Bearer token (save privately; shown once):',token)
        elif args.action=='sync-code-registry':
            print('registry linked:',count)
    except (SQLAlchemyError, ValueError):
        parser.exit(1, "Database operation failed; inspect schema/configuration without sharing credentials.\n")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()

