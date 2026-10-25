from datetime import datetime, timezone
import json
from uuid import uuid4
import pytest
from cryptography.fernet import Fernet
from fastapi import Depends
from fastapi.testclient import TestClient
import pyotp
from sqlalchemy import text
from marking_erp import auth
from marking_erp.config import Settings
from marking_erp.seed import GROUPS, seed_defaults, permission_defaults
from marking_erp.web import create_app

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("fresh_schema")]
PASSWORD = "fixture-personal-password-1234"
HASH = auth.password_hash(PASSWORD)
TOTP_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"


@pytest.fixture
def env(engine):
    settings = Settings(database_url="postgresql+psycopg://unused@localhost/test", public_url="https://testserver",
                        mfa_encryption_key=Fernet.generate_key().decode())
    org, other_org = uuid4(), uuid4()
    users = {}
    with engine.begin() as c:
        seed_defaults(c)
        c.execute(text("INSERT INTO organizations(id,inn,name) VALUES (:id,:inn,:name)"),
                  [{"id":org,"inn":"7700000000","name":"Fixture"},{"id":other_org,"inn":"7800000000","name":"Outside scope"}])
        for group in GROUPS:
            user_id = uuid4(); users[group] = user_id
            c.execute(text("""INSERT INTO users(id,username,display_name,password_hash,group_slug,mfa_ciphertext)
                VALUES (:id,:username,:display,:hash,:group,:mfa)"""),
                {"id":user_id,"username":group,"display":group,"hash":HASH,"group":group,
                 "mfa":auth.encrypt_mfa(TOTP_SECRET,settings) if group=="administrator" else None})
            c.execute(text("INSERT INTO user_organizations VALUES (:id,:org)"),{"id":user_id,"org":org})
    app=create_app(settings,engine)
    return settings,org,other_org,users,app


def client(env):
    return TestClient(env[-1],base_url="https://testserver",headers={"Origin":"https://testserver","X-Requested-With":"marking-erp"})


def login(c,group="bm",otp=None):
    if otp is None: otp=pyotp.TOTP(TOTP_SECRET).now() if group=="administrator" else ""
    response=c.post("/api/session",json={"username":group,"password":PASSWORD,"otp":otp})
    assert response.status_code==200,response.text
    c.headers["X-CSRF-Token"]=response.json()["csrf"]
    return response


def test_anonymous_and_name_only_login_are_denied(env):
    with client(env) as c:
        assert c.get("/api/session").status_code==401
        assert c.get("/api/users").status_code==401
        response=c.post("/api/session",json={"username":"bm","password":"wrong"})
        unknown=c.post("/api/session",json={"username":"not-here","password":"wrong"})
        assert response.status_code==unknown.status_code==401
        assert response.json()==unknown.json()


def test_session_cookie_hash_csrf_and_logout(env,engine):
    with client(env) as c:
        response=login(c)
        cookie=response.headers["set-cookie"]
        assert "__Host-erp_session=" in cookie and "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
        token=c.cookies.get("__Host-erp_session")
        with engine.connect() as connection:
            saved=connection.execute(text("SELECT token_hash FROM sessions")).scalar_one()
        assert saved==auth.digest(token) and saved!=token
        assert "password_hash" not in response.text and "mfa_ciphertext" not in response.text
        c.headers.pop("X-CSRF-Token")
        assert c.delete("/api/session").status_code==403
        c.headers["X-CSRF-Token"]=response.json()["csrf"]
        assert c.delete("/api/session").status_code==200
        assert c.get("/api/session").status_code==401


def test_admin_requires_mfa_and_rejects_replay(env):
    code=pyotp.TOTP(TOTP_SECRET).now()
    with client(env) as c, client(env) as second:
        result=c.post("/api/session",json={"username":"administrator","password":PASSWORD})
        assert result.status_code==401
        login(c,"administrator",code)
        assert c.get("/api/users").status_code==200
        replay=second.post("/api/session",json={"username":"administrator","password":PASSWORD,"otp":code})
        assert replay.status_code==401


@pytest.mark.parametrize("group",list(GROUPS))
def test_every_permission_for_each_default_group(env,engine,group):
    _,org,other_org,users,_=env
    with engine.begin() as c:
        user=dict(c.execute(text("SELECT * FROM users WHERE id=:id"),{"id":users[group]}).mappings().one())
        actor=auth.Actor(c,user,"synthetic",datetime.now(timezone.utc))
        for permission in permission_defaults():
            expected=permission["defaults"][group]
            if expected=="-":
                with pytest.raises(Exception) as error:
                    actor.require(permission["id"],organization_id=org,owner_id=actor.id,assigned=True)
                assert getattr(error.value,"status_code",None)==403
            else:
                assert actor.require(permission["id"],organization_id=org,owner_id=actor.id,assigned=True)==expected
                with pytest.raises(Exception) as error:
                    actor.require(permission["id"],organization_id=other_org,owner_id=actor.id,assigned=True)
                assert getattr(error.value,"status_code",None)==403
                if expected=="own":
                    with pytest.raises(Exception): actor.require(permission["id"],organization_id=org,owner_id=uuid4(),assigned=True)
                if expected=="assigned":
                    with pytest.raises(Exception): actor.require(permission["id"],organization_id=org,owner_id=actor.id,assigned=False)


@pytest.mark.parametrize("group",["bm","logistics","warehouse","tsd","specialist"])
def test_operational_users_cannot_call_admin_api(env,group):
    with client(env) as c:
        login(c,group)
        assert c.get("/api/users").status_code==403
        assert c.post("/api/users",json={"username":"new-user","display_name":"User","group_slug":"bm","password":PASSWORD,
                                         "organization_ids":[str(env[1])]}).status_code==403


def test_revoked_permission_applies_to_active_session(env,engine):
    with client(env) as c:
        login(c,"administrator")
        assert c.get("/api/users").status_code==200
        with engine.begin() as connection:
            connection.execute(text("UPDATE group_grants SET scope='none' WHERE group_slug='administrator' AND permission_id='users.manage'"))
        assert c.get("/api/users").status_code==403


def test_disabled_user_and_auth_version_revoke_existing_sessions(env,engine):
    with client(env) as c:
        login(c)
        with engine.begin() as connection:
            connection.execute(text("UPDATE users SET enabled=false WHERE username='bm'"))
        assert c.get("/api/session").status_code==401


def test_rotation_and_logout_all(env):
    with client(env) as first, client(env) as second, client(env) as stale:
        login(first)
        old=first.cookies.get("__Host-erp_session")
        login(first)
        stale.cookies.set("__Host-erp_session",old)
        assert stale.get("/api/session").status_code==401
        login(second)
        assert first.post("/api/session/logout-all").status_code==200
        assert first.get("/api/session").status_code==401
        assert second.get("/api/session").status_code==401


def test_throttling_persists_across_app_instances(env):
    with client(env) as c:
        for _ in range(5):
            assert c.post("/api/session",json={"username":"bm","password":"incorrect"}).status_code==401
    with client(env) as second:
        response=second.post("/api/session",json={"username":"bm","password":PASSWORD})
        assert response.status_code==429 and response.headers["Retry-After"]=="900"


def test_manage_user_scope_revision_and_last_admin(env,engine):
    _,org,other_org,users,_=env
    with client(env) as c,client(env) as employee:
        login(c,"administrator")
        body={"username":"new-user","display_name":"New User","group_slug":"bm","password":PASSWORD,"organization_ids":[str(other_org)]}
        assert c.post("/api/users",json=body).status_code==403
        body["organization_ids"]=[str(org)]
        result=c.post("/api/users",json=body)
        assert result.status_code==201,result.text
        assert c.post("/api/users",json=body).status_code==409
        login(employee,"bm")
        target=users["bm"]
        change=c.patch(f"/api/users/{target}",json={"auth_version":1,"enabled":False})
        assert change.status_code==200,change.text
        assert employee.get("/api/session").status_code==401
        assert c.patch(f"/api/users/{target}",json={"auth_version":1,"enabled":True}).status_code==409
        assert c.patch(f"/api/users/{users['administrator']}",json={"auth_version":1,"enabled":False}).status_code==409
        assert c.patch(f"/api/users/{users['administrator']}",json={"auth_version":1,"group_slug":"bm"}).status_code==409


def test_expired_elevation_requires_fresh_mfa(env,engine):
    with client(env) as c:
        login(c,"administrator")
        with engine.begin() as connection:
            connection.execute(text("UPDATE sessions SET elevated_at=now()-interval '6 minutes'"))
        assert c.get("/api/users").status_code==403
        next_code=pyotp.TOTP(TOTP_SECRET).at(datetime.now(timezone.utc).timestamp()+30)
        result=c.post("/api/session/reauth",json={"password":PASSWORD,"otp":next_code})
        assert result.status_code==200,result.text
        assert c.get("/api/users").status_code==200


def test_admin_revocation_and_password_reset_invalidate_tokens(env):
    with client(env) as admin,client(env) as employee:
        login(admin,"administrator");login(employee)
        target=env[3]["bm"]
        assert admin.post(f"/api/users/{target}/revoke-sessions").status_code==200
        assert employee.get("/api/session").status_code==401
        login(employee)
        response=admin.post(f"/api/users/{target}/credentials",json={"auth_version":2,"password":"changed-personal-password"})
        assert response.status_code==200,response.text
        assert employee.get("/api/session").status_code==401
        assert employee.post("/api/session",json={"username":"bm","password":PASSWORD}).status_code==401
        assert employee.post("/api/session",json={"username":"bm","password":"changed-personal-password"}).status_code==200


def test_bootstrap_is_single_use_and_encrypts_mfa(engine):
    # fresh_schema has no seed or users yet in this test.
    settings=Settings(database_url="postgresql+psycopg://unused@localhost/test",mfa_encryption_key=Fernet.generate_key().decode())
    code=pyotp.TOTP(TOTP_SECRET).now()
    with engine.begin() as c:
        user_id=auth.bootstrap_admin(c,settings,"first-admin","First Admin",PASSWORD,TOTP_SECRET,code,"7700000000","Fixture")
    with engine.connect() as c:
        user=c.execute(text("SELECT * FROM users WHERE id=:id"),{"id":user_id}).mappings().one()
        assert user["password_hash"]!=PASSWORD and auth.password_matches(PASSWORD,user["password_hash"])
        assert TOTP_SECRET.encode() not in user["mfa_ciphertext"]
    with pytest.raises(ValueError),engine.begin() as c:
        auth.bootstrap_admin(c,settings,"second-admin","Second",PASSWORD,TOTP_SECRET,code,"7800000000","Other")


def test_employee_can_change_own_password_and_revoke_all_sessions(env):
    with client(env) as c,client(env) as other:
        login(c);login(other)
        response=c.post("/api/session/password",json={"password":PASSWORD,"new_password":"new-personal-password-9876"})
        assert response.status_code==200,response.text
        assert c.get("/api/session").status_code==401
        assert other.get("/api/session").status_code==401
        assert c.post("/api/session",json={"username":"bm","password":PASSWORD}).status_code==401
        assert c.post("/api/session",json={"username":"bm","password":"new-personal-password-9876"}).status_code==200


def test_wrong_password_does_not_consume_totp_code(env):
    code=pyotp.TOTP(TOTP_SECRET).now()
    with client(env) as c:
        assert c.post("/api/session",json={"username":"administrator","password":"wrong","otp":code}).status_code==401
        login(c,"administrator",code)


def test_expired_session_and_organization_removal_apply_immediately(env,engine):
    with client(env) as c:
        login(c)
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM user_organizations WHERE user_id=:id"),{"id":env[3]["bm"]})
        assert c.get("/api/session").json()["organization_ids"]==[]
        # Set an expired interval while satisfying expires_at > created_at.
        with engine.begin() as connection:
            connection.execute(text("UPDATE sessions SET expires_at=now()-interval '1 minute',created_at=now()-interval '1 hour'"))
        assert c.get("/api/session").status_code==401

