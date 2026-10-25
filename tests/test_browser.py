"""Real browser → live FastAPI → isolated PostgreSQL. No API-response mocks."""
import json
import os
from pathlib import Path
import socket
import threading
import time
from uuid import uuid4
import pytest
from cryptography.fernet import Fernet
import pyotp
from sqlalchemy import text
import uvicorn
from marking_erp import auth
from marking_erp.config import Settings
from marking_erp.seed import GROUPS,seed_defaults
from marking_erp.web import create_app

pytestmark=[pytest.mark.browser,pytest.mark.postgres]
PASSWORD='Only-for-browser-fixture-2026'
SECRET='JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP'
HASH=auth.password_hash(PASSWORD)


@pytest.fixture(scope='session')
def browser():
    if os.environ.get('ERP_BROWSER_TESTS')!='1':
        if os.environ.get('CI'):
            pytest.fail('CI must enable ERP_BROWSER_TESTS=1')
        pytest.skip('Enable ERP_BROWSER_TESTS=1 and install Chromium for browser tests')
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def live_app(engine,fresh_schema):
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    base=f'http://127.0.0.1:{port}'
    settings=Settings(database_url='postgresql+psycopg://unused@localhost/test',public_url=base,secure_cookie=False,
                      mfa_encryption_key=Fernet.generate_key().decode(),build_sha=os.environ.get('GITHUB_SHA','browser-local'))
    org=uuid4();users={}
    names={'bm':'Анна · БМ','logistics':'Михаил · Логист','warehouse':'Владимир · Склад','tsd':'Терминал · ТСД','specialist':'Ирина · Маркировка','administrator':'Администратор системы'}
    with engine.begin() as c:
        seed_defaults(c)
        c.execute(text('INSERT INTO organizations(id,inn,name) VALUES (:id,\'7700000000\',\'Тестовая организация\')'),{'id':org})
        for group in GROUPS:
            user_id=uuid4();users[group]=user_id
            c.execute(text('''INSERT INTO users(id,username,display_name,password_hash,group_slug,mfa_ciphertext)
                VALUES (:id,:username,:name,:hash,:group,:mfa)'''),
                {'id':user_id,'username':group+'-user','name':names[group],'hash':HASH,'group':group,
                 'mfa':auth.encrypt_mfa(SECRET,settings) if group=='administrator' else None})
            c.execute(text('INSERT INTO user_organizations VALUES (:user,:org)'),{'user':user_id,'org':org})
    server=uvicorn.Server(uvicorn.Config(create_app(settings,engine),host='127.0.0.1',port=port,log_level='warning',access_log=False,timeout_graceful_shutdown=3))
    thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);thread.start()
    deadline=time.monotonic()+10
    while not server.started and thread.is_alive() and time.monotonic()<deadline:time.sleep(.01)
    assert server.started,'Browser fixture server failed to start'
    try:yield {'base':base,'users':users,'org':org}
    finally:
        server.should_exit=True;thread.join(10);sock.close()
        assert not thread.is_alive(),'Browser fixture server did not stop'


def capture(page,name):
    output=Path('test-results/ui');output.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(output/(name+'.png')),full_page=True,
                    mask=[page.locator('input[type=password]:visible,input[name=totp_secret]:visible,input[name=otp]:visible,input[name=totp_code]:visible')])
    (output/(name+'.json')).write_text(json.dumps({'sha':os.environ.get('GITHUB_SHA','local'),'viewport':page.viewport_size,'url_path':page.url.split('/',3)[-1]},ensure_ascii=False))


@pytest.fixture
def page(browser,live_app,request):
    context=browser.new_context(viewport={'width':1440,'height':900},locale='ru-RU',color_scheme='light')
    page=context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
    page.goto(live_app['base'])
    from playwright.sync_api import expect
    expect(page.locator('#login-screen')).to_be_visible()
    try:yield page
    finally:
        if getattr(request.node,'rep_call',None) and request.node.rep_call.failed:capture(page,'failure-'+request.node.name)
        context.close()
        assert not errors,errors


def login(page,group='administrator'):
    from playwright.sync_api import expect
    page.locator('#login-name').fill(group+'-user');page.locator('#login-password').fill(PASSWORD)
    if group=='administrator':page.locator('#login-otp').fill(pyotp.TOTP(SECRET).now())
    page.locator('#login-form button[type=submit]').click()
    expect(page.locator('#workspace')).to_be_visible()


def test_login_themes_and_mobile_layout(page):
    from playwright.sync_api import expect
    capture(page,'login-light')
    page.locator('#theme').click();expect(page.locator('html')).to_have_attribute('data-theme','dark');capture(page,'login-dark')
    page.reload();expect(page.locator('html')).to_have_attribute('data-theme','dark')
    page.set_viewport_size({'width':320,'height':740})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    capture(page,'login-320-dark')
    page.locator('#login-name').fill('bm-user');page.locator('#login-password').fill('incorrect')
    page.locator('#login-form button[type=submit]').click();expect(page.locator('#login-error')).to_be_visible()
    expect(page.locator('#login-password')).to_have_value('')


@pytest.mark.parametrize('group',list(GROUPS))
def test_six_role_homes_and_tsd_restrictions(page,live_app,group):
    from playwright.sync_api import expect
    login(page,group);expect(page.locator('#group-badge')).to_have_text(GROUPS[group]);capture(page,'home-'+group)
    if group!='administrator':
        expect(page.locator('#users-link')).to_be_hidden()
        response=page.request.get(live_app['base']+'/api/users');assert response.status==403
    if group=='tsd':
        assert page.locator('.module-card h3').all_text_contents()==['Приёмка','Проверка КМ','Отгрузка']
        page.set_viewport_size({'width':320,'height':740})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        capture(page,'home-tsd-320')
        page.goto(live_app['base']+'/users');expect(page.get_by_role('heading',name='Раздел недоступен')).to_be_visible()


def test_admin_creates_edits_filters_and_revokes_user(page,live_app,engine):
    from playwright.sync_api import expect
    login(page);page.locator('#users-link').click();expect(page.locator('table')).to_be_visible();capture(page,'users-light')
    page.get_by_role('button',name='+ Новый пользователь').click();expect(page.locator('#user-dialog')).to_be_visible();capture(page,'user-create')
    f=page.locator('#user-form');f.locator('[name=display_name]').fill('Новый сотрудник');f.locator('[name=username]').fill('new-employee')
    f.locator('[name=password]').fill(PASSWORD);f.locator('[name=password_confirm]').fill(PASSWORD);f.locator('button[type=submit]').click()
    expect(page.locator('#user-dialog')).to_be_hidden();page.get_by_label('Поиск пользователей').fill('new-employee')
    row=page.locator('tbody tr').filter(has_text='new-employee');expect(row).to_have_count(1)
    row.get_by_role('button',name='Изменить',exact=True).click();f.locator('[name=display_name]').fill('Обновлённый сотрудник');f.locator('[name=group_slug]').select_option('logistics');f.locator('button[type=submit]').click()
    expect(page.locator('#user-dialog')).to_be_hidden();expect(row).to_contain_text('Обновлённый сотрудник');expect(row).to_contain_text('Логист')
    row.get_by_role('button',name='Сессии',exact=True).click();expect(page.locator('#confirm-dialog')).to_be_visible();page.locator('#confirm-ok').click();expect(page.locator('#notice')).to_contain_text('Сессии пользователя завершены')
    page.locator('#theme').click();capture(page,'users-dark')
    with engine.connect() as c:
        user=c.execute(text("SELECT display_name,group_slug,auth_version FROM users WHERE username='new-employee'")).mappings().one()
        assert user['display_name']=='Обновлённый сотрудник' and user['group_slug']=='logistics' and user['auth_version']==3


def test_password_change_and_logout_clear_session(page,live_app):
    from playwright.sync_api import expect
    login(page,'bm');page.get_by_role('link',name='Мой профиль',exact=True).click();page.get_by_role('button',name='Сменить пароль',exact=True).click()
    f=page.locator('#password-form');f.locator('[name=password]').fill(PASSWORD);f.locator('[name=new_password]').fill('Changed-browser-password-2026');f.locator('[name=confirm]').fill('Changed-browser-password-2026');f.locator('button[type=submit]').click()
    expect(page.locator('#login-screen')).to_be_visible();assert page.request.get(live_app['base']+'/api/session').status==401
    assert page.evaluate('Object.keys(localStorage)')==[]


def test_reauth_and_live_permission_revocation(page,live_app,engine):
    from playwright.sync_api import expect
    login(page)
    with engine.begin() as c:c.execute(text("UPDATE sessions SET elevated_at=now()-interval '6 minutes'"))
    page.locator('#users-link').click();expect(page.get_by_role('button',name='Подтвердить доступ',exact=True)).to_be_visible();page.get_by_role('button',name='Подтвердить доступ',exact=True).click()
    f=page.locator('#reauth-form');f.locator('[name=password]').fill(PASSWORD);f.locator('[name=otp]').fill(pyotp.TOTP(SECRET).at(time.time()+30));f.locator('button[type=submit]').click()
    expect(page.locator('#reauth-dialog')).to_be_hidden();expect(page.locator('table')).to_be_visible()
    with engine.begin() as c:c.execute(text("UPDATE group_grants SET scope='none' WHERE group_slug='administrator' AND permission_id='users.manage'"))
    # A visibility transition makes the actual session refresh immediately.
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    expect(page.locator('#users-link')).to_be_hidden();expect(page.locator('table')).to_have_count(0)
    assert page.request.get(live_app['base']+'/api/users').status==403


def test_server_data_is_text_and_network_failure_is_visible(page,engine):
    from playwright.sync_api import expect
    with engine.begin() as c:c.execute(text("UPDATE users SET display_name=:name WHERE username='bm-user'"),{'name':'<img src=x onerror=alert(1)>'})
    login(page);page.locator('#users-link').click();expect(page.locator('table')).to_be_visible()
    expect(page.get_by_role('button',name='<img src=x onerror=alert(1)>',exact=True)).to_be_visible()
    assert page.locator('img').count()==0
    page.context.set_offline(True);page.get_by_role('button',name='Обновить',exact=True).click();expect(page.locator('#notice')).to_contain_text('Не удалось получить ответ')
    page.context.set_offline(False);page.get_by_role('button',name='Обновить',exact=True).click();expect(page.locator('table')).to_be_visible()


def test_credentials_enrollment_and_disabled_user(page,live_app,engine):
    from playwright.sync_api import expect
    login(page);page.locator('#users-link').click();expect(page.locator('table')).to_be_visible()
    page.get_by_label('Поиск пользователей').fill('bm-user')
    row=page.locator('tbody tr').filter(has_text='bm-user')
    row.get_by_role('button',name='Пароль и TOTP',exact=True).click()
    f=page.locator('#credentials-form');f.locator('[name=password]').fill('Reset-browser-password-2026');f.locator('[name=password_confirm]').fill('Reset-browser-password-2026')
    f.locator('[name=replace_mfa]').check();page.locator('#generate-reset-totp').click()
    secret=f.locator('[name=totp_secret]').input_value();assert len(secret)==32
    f.locator('[name=totp_code]').fill(pyotp.TOTP(secret).now());capture(page,'credentials-mfa')
    f.locator('button[type=submit]').click();expect(page.locator('#credentials-dialog')).to_be_hidden()
    expect(page.locator('#notice')).to_contain_text('Доступ пользователя обновлён')
    # Changes are committed in the actual database, including encryption and revision.
    with engine.connect() as c:
        user=c.execute(text("SELECT password_hash,mfa_ciphertext,auth_version FROM users WHERE username='bm-user'")).mappings().one()
        assert auth.password_matches('Reset-browser-password-2026',user['password_hash'])
        assert secret.encode() not in user['mfa_ciphertext'] and user['auth_version']==2
    row.get_by_role('button',name='Изменить',exact=True).click();u=page.locator('#user-form');u.locator('[name=enabled]').uncheck();u.locator('button[type=submit]').click()
    expect(page.locator('#confirm-dialog')).to_be_visible();page.locator('#confirm-ok').click()
    expect(page.locator('#user-dialog')).to_be_hidden();page.get_by_label('Фильтр по состоянию').select_option('disabled');expect(row).to_contain_text('Отключён')
    with engine.connect() as c:assert c.execute(text("SELECT enabled FROM users WHERE username='bm-user'")).scalar_one() is False
