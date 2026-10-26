from datetime import datetime, timezone
import hmac
from uuid import UUID, uuid4
from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictBool
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from . import auth
from .seed import GROUPS


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Login(Body):
    username: str = Field(min_length=1, max_length=80)
    password: SecretStr = Field(min_length=1, max_length=1024)
    otp: str = Field(default="", max_length=6)


class Reauth(Body):
    password: SecretStr = Field(min_length=1, max_length=1024)
    otp: str = Field(default="", max_length=6)


class PasswordChange(Reauth):
    new_password: SecretStr = Field(min_length=12, max_length=1024)


class UserCreate(Body):
    username: str = Field(min_length=3, max_length=80)
    display_name: str = Field(min_length=1, max_length=120)
    group_slug: str = Field(max_length=40)
    password: SecretStr = Field(min_length=12, max_length=1024)
    organization_ids: list[UUID] = Field(min_length=1, max_length=100)
    totp_secret: SecretStr | None = None
    totp_code: str = Field(default="", max_length=6)


class UserUpdate(Body):
    auth_version: int = Field(ge=1)
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    group_slug: str | None = Field(default=None, max_length=40)
    enabled: StrictBool | None = None
    organization_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=100)


class Credentials(Body):
    auth_version: int = Field(ge=1)
    password: SecretStr = Field(min_length=12, max_length=1024)
    totp_secret: SecretStr | None = None
    totp_code: str = Field(default="", max_length=6)


def install_auth(app, engine, settings):
    def actor(request: Request):
        token = request.cookies.get(settings.cookie_name, "")
        with engine.begin() as connection:
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                # The bounded Excel preview is read-only; do not hold the global
                # mutation lock while a client streams its file. CSRF still applies.
                if request.url.path != "/api/documents/import-preview":
                    # Independent scan transactions may proceed concurrently.
                    # RBAC/session edits, archive, close and dispatch retain the
                    # exclusive fence and wait for all active scans to finish.
                    path=request.url.path
                    warehouse_scan=(path.startswith('/api/warehouse/tasks/') and
                                    ((request.method=='POST' and path.endswith('/scans')) or
                                     (request.method=='DELETE' and '/scans/' in path)))
                    lock='pg_advisory_xact_lock_shared' if warehouse_scan else 'pg_advisory_xact_lock'
                    connection.execute(text('SELECT '+lock+'(683273529)'))
            current = auth.load_actor(connection, token)
            if request.method not in ("GET", "HEAD", "OPTIONS") and not hmac.compare_digest(
                    request.headers.get("X-CSRF-Token", ""), auth.csrf_token(token)):
                raise HTTPException(403, "Обновите страницу и повторите действие")
            yield current

    def details(current):
        organizations = current.connection.execute(text("""SELECT o.id,o.inn,o.name FROM organizations o
            JOIN user_organizations uo ON uo.organization_id=o.id WHERE uo.user_id=:id AND o.enabled ORDER BY o.name"""), {"id": current.id}).mappings().all()
        administrative = current.user["group_slug"] == "administrator" or auth.group_has_admin(current.connection, current.user["group_slug"])
        return {"user": auth.public_user(current.user), "organization_ids": sorted(map(str, current.organizations())),
                "organizations": [dict(row) for row in organizations], "group_name": GROUPS[current.user["group_slug"]],
                "administrative_access": administrative, "mfa_required": administrative or bool(current.user["mfa_ciphertext"]),
                "permissions": current.grants(), "csrf": auth.csrf_token(current.token)}

    def peer(request):
        # Forwarded headers are trusted only by the configured ASGI server/proxy.
        return request.client.host if request.client else "unknown"

    @app.post("/api/session")
    def login(body: Login, request: Request):
        try:
            username = auth.normalize_username(body.username)
        except ValueError:
            # Invalid/unknown identities take the same password work and error path.
            username = "invalid:" + auth.digest(body.username)
        with engine.begin() as connection:
            user, failure = auth.authenticate(connection, settings, username, body.password.get_secret_value(), body.otp, peer(request))
            if not failure:
                old = request.cookies.get(settings.cookie_name)
                if old:
                    connection.execute(text("DELETE FROM sessions WHERE token_hash=:hash"), {"hash": auth.digest(old)})
                token, ttl = auth.create_session(connection, user)
                payload = details(auth.load_actor(connection, token))
        if failure:
            response = JSONResponse({"detail": "Подождите перед следующей попыткой" if failure == 429 else "Неверные учётные данные"}, status_code=failure)
            if failure == 429:
                response.headers["Retry-After"] = "900"
            return response
        response = JSONResponse(content=jsonable_encoder(payload))
        response.set_cookie(settings.cookie_name, token, max_age=ttl, path="/", httponly=True,
                            secure=settings.secure_cookie, samesite="strict")
        return response

    @app.get("/api/session")
    def me(current=Depends(actor, scope="function")):
        return details(current)

    @app.delete("/api/session")
    def logout(current=Depends(actor, scope="function")):
        current.connection.execute(text("DELETE FROM sessions WHERE token_hash=:hash"), {"hash": auth.digest(current.token)})
        auth.audit(current.connection, current.id, "session.revoked", current.id)
        response = JSONResponse({"ok": True})
        response.delete_cookie(settings.cookie_name, path="/", secure=settings.secure_cookie, httponly=True, samesite="strict")
        return response

    @app.post("/api/session/logout-all")
    def logout_all(current=Depends(actor, scope="function")):
        current.connection.execute(text("UPDATE users SET auth_version=auth_version+1 WHERE id=:id"), {"id": current.id})
        current.connection.execute(text("DELETE FROM sessions WHERE user_id=:id"), {"id": current.id})
        auth.audit(current.connection, current.id, "sessions.revoked_all", current.id)
        response = JSONResponse({"ok": True})
        response.delete_cookie(settings.cookie_name, path="/", secure=settings.secure_cookie, httponly=True, samesite="strict")
        return response

    @app.get("/api/session/sessions")
    def own_sessions(current=Depends(actor, scope="function")):
        rows = current.connection.execute(text("""SELECT created_at,expires_at,
            token_hash=:hash AS is_current,count(*) OVER() AS total
            FROM sessions WHERE user_id=:user AND auth_version=:version AND expires_at>now()
            ORDER BY is_current DESC,created_at DESC,token_hash DESC LIMIT 20"""),
            {"hash": auth.digest(current.token), "user": current.id,
             "version": current.user['auth_version']}).mappings().all()
        return {"total": rows[0]['total'] if rows else 0,
                "sessions": [{key: row[key] for key in ('created_at','expires_at','is_current')} for row in rows]}

    @app.post("/api/session/logout-others")
    def logout_others(current=Depends(actor, scope="function")):
        current.connection.execute(text("DELETE FROM sessions WHERE user_id=:id AND token_hash<>:hash"),
                                   {"id": current.id, "hash": auth.digest(current.token)})
        auth.audit(current.connection, current.id, "sessions.revoked_others", current.id)
        return {"ok": True}

    @app.post("/api/session/reauth")
    def reauth(body: Reauth, request: Request, current=Depends(actor, scope="function")):
        user, failure = auth.authenticate(current.connection, settings, current.user["username"], body.password.get_secret_value(), body.otp, peer(request))
        if failure:
            # Return rather than raise: failed-login counters must commit.
            return JSONResponse({"detail": "Не удалось подтвердить учётные данные"}, status_code=failure)
        current.connection.execute(text("UPDATE sessions SET elevated_at=:at WHERE token_hash=:hash"),
                                   {"at": datetime.now(timezone.utc) if user["mfa_verified"] else None, "hash": auth.digest(current.token)})
        auth.audit(current.connection, current.id, "session.reauthenticated", current.id)
        return {"ok": True}

    @app.get("/api/users")
    def users(current=Depends(actor, scope="function")):
        current.require("users.manage", administrative=True)
        allowed = list(current.organizations())
        result = current.connection.execute(text("""SELECT u.id,u.username,u.display_name,u.group_slug,u.enabled,u.auth_version,
            u.mfa_ciphertext IS NOT NULL AS mfa_enabled,
            ARRAY(SELECT x.organization_id FROM user_organizations x WHERE x.user_id=u.id ORDER BY x.organization_id) AS organization_ids
            FROM users u WHERE EXISTS(SELECT 1 FROM user_organizations x WHERE x.user_id=u.id)
            AND NOT EXISTS(SELECT 1 FROM user_organizations x WHERE x.user_id=u.id AND NOT (x.organization_id=ANY(CAST(:orgs AS uuid[]))))
            ORDER BY u.username"""), {"orgs": allowed}).mappings().all()
        return {"users": [dict(row) for row in result]}

    @app.get("/api/users/options")
    def user_options(current=Depends(actor, scope="function")):
        current.require("users.manage", administrative=True)
        return {"groups": [{"slug": slug, "name": name, "requires_mfa": slug == "administrator" or auth.group_has_admin(current.connection, slug)}
                           for slug, name in GROUPS.items()]}

    @app.post("/api/session/password")
    def change_password(body: PasswordChange, request: Request, current=Depends(actor, scope="function")):
        user, failure = auth.authenticate(current.connection, settings, current.user["username"], body.password.get_secret_value(), body.otp, peer(request))
        if failure:
            return JSONResponse({"detail": "Не удалось подтвердить учётные данные"}, status_code=failure)
        if user["administrative"] and len(body.new_password.get_secret_value()) < 16:
            raise HTTPException(422, "Пароль администратора должен содержать от 16 символов")
        current.connection.execute(text("UPDATE users SET password_hash=:hash,auth_version=auth_version+1 WHERE id=:id"),
                                   {"id": current.id, "hash": auth.password_hash(body.new_password.get_secret_value())})
        current.connection.execute(text("DELETE FROM sessions WHERE user_id=:id"), {"id": current.id})
        auth.audit(current.connection, current.id, "user.password_changed", current.id)
        response = JSONResponse({"ok": True})
        response.delete_cookie(settings.cookie_name, path="/", secure=settings.secure_cookie, httponly=True, samesite="strict")
        return response

    def mfa_value(secret, code):
        if secret is None:
            return None, -1
        cipher = auth.encrypt_mfa(secret.get_secret_value(), settings)
        counter = auth.mfa_counter(cipher, code, settings)
        if counter is None:
            raise HTTPException(422, "Подтвердите новый секрет TOTP действующим кодом")
        return cipher, counter

    @app.post("/api/users", status_code=201)
    def create_user(body: UserCreate, current=Depends(actor, scope="function")):
        current.require("users.manage", administrative=True)
        orgs = auth.check_organization_scope(current, body.organization_ids)
        if body.group_slug not in GROUPS or not body.display_name.strip():
            raise HTTPException(422, "Проверьте группу и имя пользователя")
        cipher, counter = mfa_value(body.totp_secret, body.totp_code)
        administrative = body.group_slug == "administrator" or auth.group_has_admin(current.connection, body.group_slug)
        if administrative and (cipher is None or len(body.password.get_secret_value()) < 16):
            raise HTTPException(422, "Для административного доступа нужны TOTP и пароль от 16 символов")
        user_id = uuid4()
        try:
            current.connection.execute(text("""INSERT INTO users(id,username,display_name,password_hash,group_slug,mfa_ciphertext,mfa_last_counter)
                VALUES (:id,:username,:display,:hash,:group,:mfa,:counter)"""),
                {"id": user_id, "username": auth.normalize_username(body.username), "display": body.display_name.strip(),
                 "hash": auth.password_hash(body.password.get_secret_value()), "group": body.group_slug, "mfa": cipher, "counter": counter})
        except IntegrityError:
            raise HTTPException(409, "Логин уже занят") from None
        current.connection.execute(text("INSERT INTO user_organizations VALUES (:id,:org)"), [{"id": user_id, "org": org} for org in orgs])
        auth.audit(current.connection, current.id, "user.created", user_id, {"group": body.group_slug})
        return auth.public_user(current.connection.execute(text("SELECT * FROM users WHERE id=:id"), {"id": user_id}).mappings().one())

    @app.patch("/api/users/{user_id}")
    def edit_user(user_id: UUID, body: UserUpdate, current=Depends(actor, scope="function")):
        if body.display_name is not None and not body.display_name.strip():
            raise HTTPException(422, "Заполните имя пользователя")
        return auth.update_user(current, user_id, revision=body.auth_version, display_name=body.display_name,
                                group=body.group_slug, enabled=body.enabled, organizations=body.organization_ids)

    @app.post("/api/users/{user_id}/credentials")
    def reset_credentials(user_id: UUID, body: Credentials, current=Depends(actor, scope="function")):
        current.require("users.manage", administrative=True)
        target = auth.get_managed_user(current, user_id)
        if target["auth_version"] != body.auth_version:
            raise HTTPException(409, "Пользователь изменён другим администратором")
        if (target["group_slug"] == "administrator" or auth.group_has_admin(current.connection, target["group_slug"])) and len(body.password.get_secret_value()) < 16:
            raise HTTPException(422, "Пароль администратора должен содержать от 16 символов")
        cipher, counter = mfa_value(body.totp_secret, body.totp_code)
        current.connection.execute(text("""UPDATE users SET password_hash=:hash,auth_version=auth_version+1,
            mfa_ciphertext=COALESCE(:mfa,mfa_ciphertext),mfa_last_counter=CASE WHEN :mfa IS NULL THEN mfa_last_counter ELSE :counter END WHERE id=:id"""),
            {"id": user_id, "hash": auth.password_hash(body.password.get_secret_value()), "mfa": cipher, "counter": counter})
        current.connection.execute(text("DELETE FROM sessions WHERE user_id=:id"), {"id": user_id})
        auth.audit(current.connection, current.id, "user.credentials_reset", user_id, {"mfa_replaced": cipher is not None})
        return {"ok": True}

    @app.post("/api/users/{user_id}/revoke-sessions")
    def revoke_sessions(user_id: UUID, current=Depends(actor, scope="function")):
        current.require("sessions.revoke", administrative=True)
        auth.get_managed_user(current, user_id)
        current.connection.execute(text("UPDATE users SET auth_version=auth_version+1 WHERE id=:id"), {"id": user_id})
        current.connection.execute(text("DELETE FROM sessions WHERE user_id=:id"), {"id": user_id})
        auth.audit(current.connection, current.id, "sessions.revoked_by_administrator", user_id)
        return {"ok": True}

    return actor
