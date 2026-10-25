"""Personal identities and fresh, database-backed authorization; no CRPT calls."""
import base64
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from uuid import UUID, uuid4
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
import pyotp
from sqlalchemy import text
from .seed import GROUPS, seed_defaults

KDF_CAPACITY = threading.BoundedSemaphore(2)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password):
    if not isinstance(password, str) or len(password) < 12 or len(password.encode()) > 1024:
        raise ValueError("Пароль должен содержать от 12 символов и не более 1024 байт")
    salt = secrets.token_bytes(16)
    with KDF_CAPACITY:
        key = hashlib.scrypt(password.encode(), salt=salt, n=2**17, r=8, p=1, dklen=32, maxmem=256*1024*1024)
    return "scrypt-v1$" + base64.b64encode(salt+key).decode()


def password_matches(password, encoded):
    try:
        if not isinstance(password, str) or len(password.encode()) > 1024 or not encoded.startswith("scrypt-v1$"):
            return False
        raw = base64.b64decode(encoded.split("$", 1)[1], validate=True)
        if len(raw) != 48:
            return False
        with KDF_CAPACITY:
            key = hashlib.scrypt(password.encode(), salt=raw[:16], n=2**17, r=8, p=1, dklen=32, maxmem=256*1024*1024)
        return hmac.compare_digest(key, raw[16:])
    except (ValueError, TypeError):
        return False


DUMMY_HASH = password_hash("timing-only dummy identity, never an account")


def normalize_username(value):
    value = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.@-]{2,79}", value):
        raise ValueError("Логин: 3–80 латинских букв, цифр или символов _.@-")
    return value


def audit(connection, actor, action, entity, detail=None):
    connection.execute(text("INSERT INTO audit_events(actor_id,action,entity,detail) VALUES (:actor,:action,:entity,CAST(:detail AS jsonb))"),
                       {"actor": actor, "action": action, "entity": str(entity), "detail": json.dumps(detail or {}, ensure_ascii=False)})


def group_has_admin(connection, group):
    return connection.execute(text("""SELECT EXISTS(SELECT 1 FROM group_grants g JOIN permissions p ON p.id=g.permission_id
        WHERE g.group_slug=:group AND p.administrative AND g.scope<>'none')"""), {"group": group}).scalar()


def encrypt_mfa(secret, settings):
    if not settings.mfa_encryption_key or not re.fullmatch(r"[A-Z2-7]{32,128}", secret):
        raise ValueError("Нужны ключ шифрования сервера и корректный секрет TOTP")
    # Force validation of the complete base32 value before storing it.
    pyotp.TOTP(secret).now()
    return Fernet(settings.mfa_encryption_key.get_secret_value().encode()).encrypt(secret.encode())


def mfa_counter(ciphertext, code, settings, last=-1):
    if not settings.mfa_encryption_key or not ciphertext or not re.fullmatch(r"[0-9]{6}", code or ""):
        return None
    try:
        secret = Fernet(settings.mfa_encryption_key.get_secret_value().encode()).decrypt(bytes(ciphertext)).decode()
        totp = pyotp.TOTP(secret)
        current = int(time.time()) // 30
        for counter in (current, current-1, current+1):
            if counter > last and hmac.compare_digest(totp.at(counter*30), code):
                return counter
    except (InvalidToken, ValueError, UnicodeError):
        pass
    return None


def bootstrap_admin(connection, settings, username, display_name, password, totp_secret, code, inn, organization_name):
    connection.execute(text("SELECT pg_advisory_xact_lock(683273528)"))
    seed_defaults(connection)
    if connection.execute(text("SELECT count(*) FROM users")).scalar():
        raise ValueError("Первый администратор уже создан; начальная настройка недоступна")
    if len(password) < 16:
        raise ValueError("Пароль администратора должен содержать не менее 16 символов")
    username = normalize_username(username)
    cipher = encrypt_mfa(totp_secret, settings)
    counter = mfa_counter(cipher, code, settings)
    if counter is None:
        raise ValueError("Код TOTP не подтверждён")
    if not re.fullmatch(r"(?:[0-9]{10}|[0-9]{12})", inn) or not organization_name.strip() or not display_name.strip():
        raise ValueError("Заполните организацию, ИНН и имя администратора")
    org_id, user_id = uuid4(), uuid4()
    connection.execute(text("INSERT INTO organizations(id,inn,name) VALUES (:id,:inn,:name)"),
                       {"id": org_id, "inn": inn, "name": organization_name.strip()})
    connection.execute(text("""INSERT INTO users(id,username,display_name,password_hash,group_slug,mfa_ciphertext,mfa_last_counter)
        VALUES (:id,:username,:display,:hash,'administrator',:mfa,:counter)"""),
                       {"id": user_id, "username": username, "display": display_name.strip(), "hash": password_hash(password), "mfa": cipher, "counter": counter})
    connection.execute(text("INSERT INTO user_organizations VALUES (:user,:org)"), {"user": user_id, "org": org_id})
    audit(connection, user_id, "administrator.bootstrapped", user_id, {"organization_id": str(org_id)})
    return user_id


def authenticate(connection, settings, username, password, otp, peer):
    # Acquire per-IP then per-account locks in fixed order, including unknown accounts.
    keys = [digest("ip:"+peer), digest("account:"+username)]
    for key in keys:
        connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": int(key[:15], 16)})
    limited = connection.execute(text("SELECT EXISTS(SELECT 1 FROM login_limits WHERE key IN (:ip,:account) AND failures>=CASE WHEN key=:ip THEN 50 ELSE 5 END AND window_start>now()-interval '15 minutes')"),
                                 {"ip": keys[0], "account": keys[1]}).scalar()
    if limited:
        return None, 429
    user = connection.execute(text("SELECT * FROM users WHERE username=:username FOR UPDATE"), {"username": username}).mappings().first()
    valid_password = password_matches(password, user["password_hash"] if user else DUMMY_HASH)
    administrative = bool(user and (user["group_slug"] == "administrator" or group_has_admin(connection, user["group_slug"])))
    need_mfa = bool(user and (administrative or user["mfa_ciphertext"]))
    counter = mfa_counter(user["mfa_ciphertext"], otp, settings, user["mfa_last_counter"]) if need_mfa else None
    if not user or not user["enabled"] or not valid_password or (need_mfa and counter is None):
        for key in keys:
            connection.execute(text("""INSERT INTO login_limits(key,failures) VALUES (:key,1) ON CONFLICT(key) DO UPDATE SET
                failures=CASE WHEN login_limits.window_start<=now()-interval '15 minutes' THEN 1 ELSE login_limits.failures+1 END,
                window_start=CASE WHEN login_limits.window_start<=now()-interval '15 minutes' THEN now() ELSE login_limits.window_start END"""), {"key": key})
        audit(connection, None, "login.failed", keys[1])
        return None, 401
    if counter is not None:
        connection.execute(text("UPDATE users SET mfa_last_counter=:counter WHERE id=:id"), {"counter": counter, "id": user["id"]})
    connection.execute(text("DELETE FROM login_limits WHERE key=:key"), {"key": keys[1]})
    return dict(user, administrative=administrative, mfa_verified=need_mfa), None


def create_session(connection, user):
    token = secrets.token_urlsafe(32)
    ttl = 1800 if user["administrative"] else 28800
    now = datetime.now(timezone.utc)
    connection.execute(text("""INSERT INTO sessions(token_hash,user_id,auth_version,expires_at,elevated_at)
        VALUES (:hash,:id,:version,:expires,:elevated)"""),
                       {"hash": digest(token), "id": user["id"], "version": user["auth_version"], "expires": now+timedelta(seconds=ttl),
                        "elevated": now if user["mfa_verified"] else None})
    audit(connection, user["id"], "session.created", user["id"])
    return token, ttl


def csrf_token(token):
    return digest("csrf:"+token)


@dataclass
class Actor:
    connection: object
    user: dict
    token: str
    elevated_at: datetime | None

    @property
    def id(self):
        return self.user["id"]

    def organizations(self):
        return set(self.connection.execute(text("""SELECT o.id FROM organizations o JOIN user_organizations uo ON uo.organization_id=o.id
            WHERE uo.user_id=:id AND o.enabled"""), {"id": self.id}).scalars())

    def grants(self):
        return dict(self.connection.execute(text("SELECT permission_id,scope FROM group_grants WHERE group_slug=:group AND scope<>'none'"),
                                            {"group": self.user["group_slug"]}).tuples().all())

    def require(self, permission, *, organization_id=None, owner_id=None, assigned=False, administrative=False):
        grant = self.connection.execute(text("""SELECT g.scope,p.administrative FROM group_grants g JOIN permissions p ON p.id=g.permission_id
            WHERE g.group_slug=:group AND g.permission_id=:permission AND g.scope<>'none'"""),
                                        {"group": self.user["group_slug"], "permission": permission}).mappings().first()
        if not grant or (self.user["group_slug"] == "specialist" and grant["administrative"]):
            raise HTTPException(403, "Действие недоступно вашей группе")
        scope = grant["scope"]
        if not grant["administrative"] and organization_id is None:
            raise HTTPException(403, "Не указан контекст организации")
        if organization_id is not None and UUID(str(organization_id)) not in self.organizations():
            raise HTTPException(403, "Нет доступа к организации")
        if scope == "own" and (owner_id is None or str(owner_id) != str(self.id)):
            raise HTTPException(403, "Доступны только собственные документы")
        if scope == "assigned" and not assigned:
            raise HTTPException(403, "Задание вам не назначено")
        if administrative or grant["administrative"]:
            if scope != "all" or not self.elevated_at or self.elevated_at < datetime.now(timezone.utc)-timedelta(minutes=5):
                raise HTTPException(403, {"code": "reauth_required", "message": "Подтвердите пароль и новый код TOTP для административного действия"})
        return scope


def load_actor(connection, token):
    if not token or len(token) > 128:
        raise HTTPException(401, "Войдите в приложение")
    user = connection.execute(text("""SELECT u.*,s.elevated_at FROM sessions s JOIN users u ON u.id=s.user_id
        WHERE s.token_hash=:hash AND s.expires_at>now() AND s.auth_version=u.auth_version AND u.enabled"""),
                              {"hash": digest(token)}).mappings().first()
    if not user:
        raise HTTPException(401, "Сессия завершена или пользователь отключён")
    return Actor(connection, dict(user), token, user["elevated_at"])


def public_user(user):
    return {key: user[key] for key in ("id", "username", "display_name", "group_slug", "enabled", "auth_version")}


def check_organization_scope(actor, organizations):
    ids = {UUID(str(value)) for value in organizations}
    if not ids or not ids.issubset(actor.organizations()):
        raise HTTPException(403, "Выберите доступные организации")
    return ids


def get_managed_user(actor, user_id):
    user = actor.connection.execute(text("SELECT * FROM users WHERE id=:id FOR UPDATE"), {"id": user_id}).mappings().first()
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    orgs = set(actor.connection.execute(text("SELECT organization_id FROM user_organizations WHERE user_id=:id"), {"id": user_id}).scalars())
    if not orgs or not orgs.issubset(actor.organizations()):
        raise HTTPException(403, "Нет доступа ко всем организациям пользователя")
    return user


def update_user(actor, user_id, *, revision, display_name=None, group=None, enabled=None, organizations=None):
    actor.require("users.manage", administrative=True)
    c = actor.connection
    c.execute(text("SELECT pg_advisory_xact_lock(683273529)"))
    user = get_managed_user(actor, user_id)
    if user["auth_version"] != revision:
        raise HTTPException(409, "Пользователь изменён другим администратором")
    target_group = group or user["group_slug"]
    if target_group not in GROUPS:
        raise HTTPException(422, "Неизвестная группа")
    target_enabled = user["enabled"] if enabled is None else enabled
    if user["group_slug"] == "administrator" and user["enabled"] and (target_group != "administrator" or not target_enabled):
        count = c.execute(text("SELECT count(*) FROM users WHERE group_slug='administrator' AND enabled")).scalar()
        if count <= 1:
            raise HTTPException(409, "Нельзя отключить или понизить последнего администратора")
    if (target_group == "administrator" or group_has_admin(c, target_group)) and not user["mfa_ciphertext"]:
        raise HTTPException(422, "Сначала настройте TOTP пользователя")
    org_ids = check_organization_scope(actor, organizations) if organizations is not None else None
    if user["group_slug"] == "administrator" and user["enabled"]:
        previous_orgs = set(c.execute(text("SELECT organization_id FROM user_organizations WHERE user_id=:id"), {"id": user_id}).scalars())
        removed_orgs = previous_orgs if target_group != "administrator" or not target_enabled else previous_orgs - (org_ids if org_ids is not None else previous_orgs)
        for org in removed_orgs:
            remaining = c.execute(text("""SELECT EXISTS(SELECT 1 FROM users u JOIN user_organizations uo ON uo.user_id=u.id
                WHERE u.id<>:id AND u.enabled AND u.group_slug='administrator' AND uo.organization_id=:org)"""), {"id": user_id, "org": org}).scalar()
            if not remaining:
                raise HTTPException(409, "Организация должна сохранить действующего администратора")
    c.execute(text("""UPDATE users SET display_name=:name,group_slug=:group,enabled=:enabled,auth_version=auth_version+1 WHERE id=:id"""),
              {"name": display_name.strip() if display_name is not None else user["display_name"], "group": target_group, "enabled": target_enabled, "id": user_id})
    if org_ids is not None:
        c.execute(text("DELETE FROM user_organizations WHERE user_id=:id"), {"id": user_id})
        c.execute(text("INSERT INTO user_organizations VALUES (:id,:org)"), [{"id": user_id, "org": org} for org in org_ids])
    c.execute(text("DELETE FROM sessions WHERE user_id=:id"), {"id": user_id})
    audit(c, actor.id, "user.updated", user_id, {"group": target_group, "enabled": target_enabled, "previous_auth_version": revision})
    return public_user(c.execute(text("SELECT * FROM users WHERE id=:id"), {"id": user_id}).mappings().one())

