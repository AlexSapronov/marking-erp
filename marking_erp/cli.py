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
            else:
                from .auth import bootstrap_admin
                bootstrap_admin(connection, settings, args.username, args.display_name, password, totp_secret, code, args.inn, args.organization_name)
                print("Администратор создан. Для входа используйте следующий код TOTP.")
    except (SQLAlchemyError, ValueError):
        parser.exit(1, "Database operation failed; inspect schema/configuration without sharing credentials.\n")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()

