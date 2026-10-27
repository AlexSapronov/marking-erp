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
def live_app(engine,fresh_schema,tmp_path):
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    base=f'http://127.0.0.1:{port}'
    settings=Settings(database_url='postgresql+psycopg://unused@localhost/test',public_url=base,secure_cookie=False,
                      file_storage_path=tmp_path/'files',mfa_encryption_key=Fernet.generate_key().decode(),build_sha=os.environ.get('GITHUB_SHA','browser-local'))
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
    try:yield {'base':base,'users':users,'org':org,'file_storage_path':settings.file_storage_path}
    finally:
        server.should_exit=True;thread.join(10);sock.close()
        assert not thread.is_alive(),'Browser fixture server did not stop'


def capture(page,name):
    output=Path('test-results/ui');output.mkdir(parents=True,exist_ok=True)
    # CSP can block Playwright's injected overlay mask. Remove values before
    # capture, then restore the same DOM controls without emitting input events.
    secrets=page.locator('input[type=password],input[name=totp_secret],input[name=otp],input[name=totp_code]')
    saved=secrets.evaluate_all('(inputs)=>inputs.map(i=>{const old={value:i.value,placeholder:i.placeholder};i.value="";i.placeholder="Скрыто";return old;})')
    try:
        assert secrets.evaluate_all('(inputs)=>inputs.every(i=>i.value==="")')
        page.screenshot(path=str(output/(name+'.png')),full_page=True,animations='disabled')
    finally:
        secrets.evaluate_all('(inputs,saved)=>inputs.forEach((i,n)=>{i.value=saved[n].value;i.placeholder=saved[n].placeholder;})',saved)
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
        expect(page.locator('#audit-link')).to_be_hidden()
        expect(page.locator('#system-link')).to_be_hidden()
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


def test_group_editor_preview_history_and_live_session_revocation(page,live_app,engine,browser):
    from playwright.sync_api import expect
    context=browser.new_context();employee=context.new_page();employee.goto(live_app['base']);login(employee,'bm')
    try:
        login(page);page.locator('#groups-link').click();expect(page.get_by_role('heading',name='Группы и права',exact=True)).to_be_visible()
        page.get_by_label('Поиск действий',exact=True).fill('nk.cards.publish')
        page.get_by_label('nk.cards.publish',exact=True).select_option('none')
        page.locator('#configuration-reason').fill('Убираем публикацию у группы БМ')
        page.get_by_role('button',name='Предпросмотр изменений',exact=True).click()
        expect(page.locator('#configuration-dialog')).to_be_visible();expect(page.locator('#configuration-content')).to_contain_text('nk.cards.publish')
        capture(page,'groups-preview');page.get_by_role('button',name='Применить изменения',exact=True).click()
        expect(page.locator('#configuration-dialog')).to_be_hidden();expect(page.locator('#configuration-form')).to_be_visible()
        employee.evaluate("window.dispatchEvent(new Event('focus'))");expect(employee.locator('#login-screen')).to_be_visible()
        page.get_by_role('button',name='История изменений',exact=True).click();expect(page.locator('#configuration-content')).to_contain_text('Убираем публикацию у группы БМ');capture(page,'groups-history')
        with engine.connect() as c:assert c.execute(text("SELECT revision FROM groups WHERE slug='bm'")).scalar_one()==2
    finally:context.close()


def test_business_rule_editor_preview_validation_history_and_mobile(page,live_app,engine):
    from playwright.sync_api import expect
    login(page);page.locator('#rules-link').click();expect(page.get_by_role('heading',name='Бизнес-правила',exact=True)).to_be_visible()
    page.get_by_label('Поиск правил',exact=True).fill('archive.retention_months')
    page.get_by_label('archive.retention_months',exact=True).fill('5');page.get_by_label('archive.retention_months',exact=True).press('Tab')
    page.locator('#configuration-reason').fill('Новая политика хранения архива')
    page.get_by_role('button',name='Предпросмотр изменений',exact=True).click();expect(page.locator('#notice')).to_contain_text('Недопустимое значение')
    page.get_by_label('archive.retention_months',exact=True).fill('12');page.get_by_label('archive.retention_months',exact=True).press('Tab')
    page.get_by_role('button',name='Предпросмотр изменений',exact=True).click();expect(page.locator('#configuration-dialog')).to_be_visible();capture(page,'rules-preview')
    page.get_by_role('button',name='Применить изменения',exact=True).click();expect(page.locator('#configuration-dialog')).to_be_hidden()
    page.get_by_label('Поиск правил',exact=True).fill('archive.retention_months');expect(page.get_by_label('archive.retention_months',exact=True)).to_have_value('12')
    page.locator('#theme').click();page.set_viewport_size({'width':320,'height':740});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth');capture(page,'rules-320-dark')
    page.get_by_role('button',name='История изменений',exact=True).click();expect(page.locator('#configuration-content')).to_contain_text('Новая политика хранения архива')
    with engine.connect() as c:assert c.execute(text("SELECT value FROM policy_values WHERE scope_key=:org AND rule_id='archive.retention_months'"),{'org':str(live_app['org'])}).scalar_one()==12


def test_registry_real_history_bytes_pdf_and_conflict(page,live_app,engine):
    from playwright.sync_api import expect
    from marking_erp.registry import register_product,capture_code,save_payload_file,save_label
    from marking_erp.file_store import FileStore
    from test_gs1 import GTIN,sample
    raw=sample(36,quantity=50)
    with engine.begin() as c:
        register_product(c,live_app['org'],GTIN,'PN/A-1','8542399000',source='nk_confirmed',source_ref='fixture:catalog',evidence_sha256='1'*64)
        code=capture_code(c,live_app['org'],live_app['users']['bm'],raw,actor_id=live_app['users']['bm'],source_ref='fixture:observed')
        save_payload_file(c,FileStore(live_app['file_storage_path']),code['payload_id'])
        save_label(c,FileStore(live_app['file_storage_path']),code['payload_id'],'PN/A-1')
        capture_code(c,live_app['org'],live_app['users']['bm'],sample(36,quantity=25),actor_id=live_app['users']['bm'],source_ref='fixture:conflict')
    login(page,'bm');page.locator('#codes-link').click();expect(page.locator('tbody')).to_contain_text('PN/A-1');expect(page.locator('tbody')).to_contain_text('Конфликт payload');capture(page,'registry-light')
    page.get_by_role('button',name='История',exact=True).click();expect(page.locator('#code-dialog')).to_be_visible();expect(page.locator('#code-content')).to_contain_text('Автоматическая замена запрещена')
    capture(page,'registry-history')
    with page.expect_download() as pending:page.get_by_role('button',name='Скачать исходные байты',exact=True).last.click()
    download=pending.value;assert Path(download.path()).read_bytes()==raw
    with page.expect_download() as pending:page.get_by_role('button',name='Скачать сохранённый PDF',exact=True).click()
    assert Path(pending.value.path()).read_bytes().startswith(b'%PDF')
    page.locator('#code-dialog [data-close]').click();page.locator('#theme').click();page.set_viewport_size({'width':320,'height':740})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth');capture(page,'registry-320-dark')
    page.get_by_label('Поиск КМ',exact=True).fill('missing');page.get_by_role('button',name='Найти',exact=True).click();expect(page.locator('#registry-table')).to_contain_text('не найдены')


def test_registry_tsd_direct_access_and_empty_real_database(page,live_app):
    from playwright.sync_api import expect
    login(page,'tsd');expect(page.locator('#codes-link')).to_be_hidden()
    assert page.request.get(live_app['base']+'/api/codes?organization_id='+str(live_app['org'])).status==403
    page.goto(live_app['base']+'/codes');expect(page.get_by_role('heading',name='Раздел недоступен',exact=True)).to_be_visible()


def test_operations_unknown_history_and_no_retry_controls(page,live_app,engine):
    from playwright.sync_api import expect
    from marking_erp import operations as ops
    target=uuid4()
    with engine.begin() as c:
        c.execute(text('INSERT INTO operation_targets(id,organization_id,owner_id,revision) VALUES (:id,:org,:owner,1)'),{'id':target,'org':live_app['org'],'owner':live_app['users']['bm']})
        c.execute(text("INSERT INTO operation_contracts(kind,permission_id,version,semantic,requires_signature,approved,evidence_ref) VALUES ('fixture.write','marking.codes.issue','fixture-v1','mutation',false,true,'synthetic test only')"))
        op=ops.prepare(c,organization_id=live_app['org'],actor_id=live_app['users']['bm'],kind='fixture.write',target_id=target,target_revision=1,idempotency_key='browser-command',payload='{"данные":"fixture"}'.encode())
        ticket=ops.claim(c,'browser-worker');ops.transport_failure(c,ticket)
    login(page,'bm');page.locator('#operations-link').click();expect(page.locator('#operations-table')).to_contain_text('Результат неизвестен')
    expect(page.get_by_role('button',name='Повторить отправку',exact=True)).to_have_count(0);capture(page,'operations-light')
    page.get_by_label('Состояние операции',exact=True).select_option('unknown');expect(page.locator('tbody tr')).to_have_count(1)
    page.get_by_role('button',name='Подробнее',exact=True).click();expect(page.locator('#operation-dialog')).to_be_visible();expect(page.locator('#operation-content')).to_contain_text('Нет достоверного ответа');capture(page,'operations-history')
    page.locator('#operation-dialog [data-close]').click();page.locator('#theme').click();page.set_viewport_size({'width':320,'height':740});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth');capture(page,'operations-320-dark')
    assert page.request.get(live_app['base']+'/api/operations/'+str(op['id'])).json()['operation']['state']=='unknown'


def test_operations_tsd_hidden_and_direct_access_denied(page,live_app):
    from playwright.sync_api import expect
    login(page,'tsd');expect(page.locator('#operations-link')).to_be_hidden()
    page.goto(live_app['base']+'/operations');expect(page.get_by_role('heading',name='Раздел недоступен',exact=True)).to_be_visible()


def test_html_report_in_chromium_is_readable_and_does_not_execute_remote_fields(tmp_path, browser):
    from playwright.sync_api import expect
    from marking_erp.nk_pilot_review import review_pilot, write_review
    from test_nk_pilot_review import card, capture as make_pilot
    attack = '<img src="https://invalid.example/track" onerror="window.nkInjected=true">'
    report = review_pilot(make_pilot(tmp_path, [card(good_attrs=[{'attr_id': 13914, 'attr_value': attack}]), card(2)]))
    output = tmp_path / 'review'
    write_review(report, output)
    context = browser.new_context(viewport={'width': 1440, 'height': 900}, locale='ru-RU')
    page = context.new_page()
    requests = []
    page.on('request', lambda request: requests.append(request.url))
    try:
        page.goto((output / 'review.html').as_uri())
        expect(page.get_by_role('heading', name='Разбор пилота НК')).to_be_visible()
        expect(page.locator('tbody tr')).to_have_count(2)
        expect(page.locator('tbody tr').first.locator('td').nth(1)).to_have_text(attack)
        assert page.evaluate('window.nkInjected === undefined')
        assert not page.locator('img,script,iframe').count()
        assert not any(url.startswith(('http:', 'https:')) for url in requests)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert page.locator('tbody tr').first.locator('td').nth(2).evaluate(
            'el=>{const range=document.createRange();range.selectNodeContents(el);return range.getClientRects().length;}') == 1
        screenshots = Path('test-results/ui')
        screenshots.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(screenshots / 'nk-pilot-review.png'), full_page=True)
    finally:
        context.close()


def test_system_diagnostics_themes_mobile_and_network_failure(page, live_app):
    from playwright.sync_api import expect
    live_app['file_storage_path'].mkdir(mode=0o700)
    login(page)
    page.locator('#system-link').click()
    expect(page.locator('.system-check')).to_have_count(10)
    expect(page.locator('[data-check=schema_revision] .system-result')).to_have_text('Пройдено')
    expect(page.locator('[data-check=database_runtime_role] .system-result')).to_have_text('Требует внимания')
    expect(page.locator('.system-component')).to_have_count(5)
    expect(page.get_by_role('heading', name='Требует внимания', exact=True)).to_be_visible()
    capture(page, 'system-status-light')
    page.locator('#theme').click()
    expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
    capture(page, 'system-status-dark')
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    capture(page, 'system-status-mobile')
    page.context.set_offline(True)
    page.get_by_role('button', name='Обновить проверки').click()
    expect(page.locator('.system-check')).to_have_count(0)
    expect(page.locator('#notice')).to_contain_text('Не удалось получить ответ')
    page.context.set_offline(False)
    page.get_by_role('button', name='Обновить проверки').click()
    expect(page.locator('.system-check')).to_have_count(10)


def test_system_diagnostics_elevation_and_live_revocation(page, live_app, engine):
    from playwright.sync_api import expect
    login(page)
    page.locator('#system-link').click()
    expect(page.locator('.system-check')).to_have_count(10)
    with engine.begin() as c:c.execute(text("UPDATE sessions SET elevated_at=now()-interval '6 minutes'"))
    page.get_by_role('button', name='Обновить проверки').click()
    expect(page.locator('.system-check')).to_have_count(0)
    page.get_by_role('button', name='Подтвердить доступ', exact=True).click()
    form = page.locator('#reauth-form')
    form.locator('[name=password]').fill(PASSWORD)
    form.locator('[name=otp]').fill(pyotp.TOTP(SECRET).at(time.time()+30))
    form.locator('button[type=submit]').click()
    expect(page.locator('#reauth-dialog')).to_be_hidden()
    # Confirmation does not silently repeat the diagnostics request.
    expect(page.locator('.system-check')).to_have_count(0)
    page.get_by_role('button', name='Обновить проверки').click()
    expect(page.locator('.system-check')).to_have_count(10)
    with engine.begin() as c:
        c.execute(text("UPDATE group_grants SET scope='none' WHERE group_slug='administrator' AND permission_id='system.diagnostics.view'"))
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    expect(page.locator('#system-link')).to_be_hidden()
    expect(page.locator('.system-check')).to_have_count(0)
    assert page.request.get(live_app['base']+'/api/system/status').status == 403


def test_audit_filters_pagination_themes_mobile_and_network(page,live_app,engine):
    from playwright.sync_api import expect
    with engine.begin() as c:
        for _ in range(30):auth.audit(c,None,'system.restored','backup',{'secret':'do-not-display-this'})
        auth.audit(c,None,'future.<script>','private-entity')
        auth.audit(c,None,'login.failed','private-throttle-key')
    login(page)
    page.locator('#audit-link').click()
    rows=page.locator('#audit-results tbody tr')
    expect(rows).to_have_count(25)
    expect(page.locator('#audit-results')).to_contain_text('Неизвестный тип события')
    assert 'private-' not in page.locator('#audit-results').inner_text()
    assert 'do-not-display-this' not in page.locator('#audit-results').inner_text()
    capture(page,'audit-light')
    page.locator('#theme').click()
    capture(page,'audit-dark')
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    capture(page,'audit-mobile')
    wrap=page.locator('#audit-results .table-wrap')
    wrap.evaluate('el=>el.scrollLeft=el.scrollWidth')
    assert wrap.evaluate('el=>el.scrollLeft')>0
    capture(page,'audit-mobile-details')
    wrap.evaluate('el=>el.scrollLeft=0')
    page.get_by_label('Событие',exact=True).select_option('system.restored')
    page.get_by_role('button',name='Применить',exact=True).click()
    expect(rows).to_have_count(25)
    expect(page.locator('#audit-results')).not_to_contain_text('Неизвестный тип события')
    first=rows.first.get_attribute('data-event-id')
    page.get_by_role('button',name='Далее',exact=True).click()
    expect(rows).to_have_count(5)
    expect(page.get_by_role('button',name='Далее',exact=True)).to_be_disabled()
    page.get_by_role('button',name='Назад',exact=True).click()
    expect(rows).to_have_count(25)
    assert rows.first.get_attribute('data-event-id')==first
    page.get_by_label('Событие',exact=True).select_option('user.created')
    page.get_by_role('button',name='Применить',exact=True).click()
    expect(rows).to_have_count(0)
    expect(page.locator('#audit-results')).to_contain_text('Нет событий')
    page.get_by_role('button',name='Сбросить',exact=True).click()
    expect(rows).to_have_count(25)
    page.context.set_offline(True)
    page.get_by_role('button',name='Обновить журнал',exact=True).click()
    expect(rows).to_have_count(0)
    expect(page.locator('#notice')).to_contain_text('Не удалось получить ответ')
    page.context.set_offline(False)
    page.get_by_role('button',name='Обновить журнал',exact=True).click()
    expect(rows).to_have_count(25)


def test_audit_elevation_and_live_revocation(page,live_app,engine):
    from playwright.sync_api import expect
    login(page)
    page.locator('#audit-link').click()
    rows=page.locator('#audit-results tbody tr')
    expect(rows.filter(has_text='Вход выполнен')).to_have_count(1)
    initial_count=rows.count()
    with engine.begin() as c:c.execute(text("UPDATE sessions SET elevated_at=now()-interval '6 minutes'"))
    page.get_by_role('button',name='Обновить журнал').click()
    expect(rows).to_have_count(0)
    page.get_by_role('button',name='Подтвердить доступ',exact=True).click()
    form=page.locator('#reauth-form')
    form.locator('[name=password]').fill(PASSWORD)
    form.locator('[name=otp]').fill(pyotp.TOTP(SECRET).at(time.time()+30))
    form.locator('button[type=submit]').click()
    expect(page.locator('#reauth-dialog')).to_be_hidden()
    expect(rows).to_have_count(0)
    page.get_by_role('button',name='Обновить журнал').click()
    expect(rows).to_have_count(initial_count+1)
    expect(rows.filter(has_text='Подтверждён административный доступ')).to_have_count(1)
    with engine.begin() as c:
        c.execute(text("UPDATE group_grants SET scope='none' WHERE group_slug='administrator' AND permission_id='audit.view'"))
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    expect(page.locator('#audit-link')).to_be_hidden()
    expect(rows).to_have_count(0)
    assert page.request.get(live_app['base']+'/api/audit').status==403


def test_profile_sessions_themes_mobile_and_network(page,live_app,engine):
    from playwright.sync_api import expect
    # A second valid login without a bearer/identifier in the list response.
    with engine.begin() as c:
        user=dict(c.execute(text('SELECT * FROM users WHERE id=:id'),{'id':live_app['users']['bm']}).mappings().one())
        auth.create_session(c,dict(user,administrative=False,mfa_verified=False))
    login(page,'bm')
    page.get_by_role('link',name='Мой профиль',exact=True).click()
    rows=page.locator('.own-session-row')
    expect(rows).to_have_count(2)
    expect(rows.first).to_contain_text('Текущий вход')
    capture(page,'profile-sessions-light')
    page.locator('#theme').click()
    capture(page,'profile-sessions-dark')
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    capture(page,'profile-sessions-mobile')
    page.context.set_offline(True)
    page.get_by_role('button',name='Обновить входы',exact=True).click()
    expect(rows).to_have_count(0)
    expect(page.locator('#notice')).to_contain_text('Не удалось получить ответ')
    expect(page.get_by_role('button',name='Завершить другие входы',exact=True)).to_be_disabled()
    page.context.set_offline(False)
    page.get_by_role('button',name='Обновить входы',exact=True).click()
    expect(rows).to_have_count(2)


def test_profile_revokes_other_context_but_preserves_current_and_foreign(page,live_app,browser):
    from playwright.sync_api import expect
    contexts=[browser.new_context(),browser.new_context()]
    try:
        second=contexts[0].new_page();second.goto(live_app['base']);login(second,'bm')
        foreign=contexts[1].new_page();foreign.goto(live_app['base']);login(foreign,'warehouse')
        login(page,'bm')
        page.get_by_role('link',name='Мой профиль',exact=True).click()
        rows=page.locator('.own-session-row');expect(rows).to_have_count(2)
        page.get_by_role('button',name='Завершить другие входы',exact=True).click()
        page.locator('#confirm-dialog').get_by_role('button',name='Отмена',exact=True).click()
        assert second.request.get(live_app['base']+'/api/session').status==200
        page.get_by_role('button',name='Завершить другие входы',exact=True).click()
        page.locator('#confirm-ok').click()
        expect(rows).to_have_count(1)
        expect(rows.first).to_contain_text('Текущий вход')
        expect(page.get_by_role('button',name='Завершить другие входы',exact=True)).to_be_disabled()
        assert page.request.get(live_app['base']+'/api/session').status==200
        assert second.request.get(live_app['base']+'/api/session').status==401
        assert foreign.request.get(live_app['base']+'/api/session').status==200
        second.evaluate("window.dispatchEvent(new Event('focus'))")
        expect(second.locator('#login-screen')).to_be_visible()
        expect(second.locator('#page-content')).to_be_empty()
    finally:
        for context in contexts:context.close()


def test_documents_manual_prepare_archive_themes_mobile(page,live_app):
    from playwright.sync_api import expect
    login(page,'bm');page.locator('#documents-link').click()
    page.get_by_role('button',name='+ Новая накладная',exact=True).click()
    page.get_by_label('Номер накладной',exact=True).fill('INV-BROWSER-01')
    page.get_by_label('Поставщик',exact=True).fill('Поставщик А')
    for name,value in [('PN','PN/А-1'),('GTIN','04640638349704'),('Штук в этикетке','10'),('Этикеток','3'),('ТН ВЭД','8542399000')]:
        page.get_by_label(name+', строка 1',exact=True).fill(value)
    assert page.get_by_label('GTIN, строка 1',exact=True).evaluate('el=>el.scrollWidth<=el.clientWidth')
    assert page.get_by_label('ТН ВЭД, строка 1',exact=True).evaluate('el=>el.scrollWidth<=el.clientWidth')
    capture(page,'documents-editor-light')
    page.get_by_role('button',name='Сохранить черновик',exact=True).click()
    expect(page.locator('#document-dialog')).to_be_hidden()
    row=page.locator('#document-list tbody tr');expect(row).to_have_count(1)
    expect(row).to_contain_text('INV-BROWSER-01');capture(page,'documents-list-light')
    page.locator('#theme').click();capture(page,'documents-list-dark')
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    capture(page,'documents-list-mobile')
    page.get_by_role('button',name='Открыть',exact=True).click()
    expect(page.get_by_role('button',name='Проверить состав',exact=True)).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    capture(page,'documents-detail-mobile')
    page.get_by_role('button',name='Проверить состав',exact=True).click()
    expect(page.locator('#document-dialog')).to_be_hidden()
    expect(row).to_contain_text('Состав проверен локально')
    page.get_by_role('button',name='Открыть',exact=True).click()
    expect(page.locator('#document-dialog [data-close]')).to_be_enabled()
    page.get_by_role('button',name='История накладной',exact=True).click()
    expect(page.locator('#document-history')).to_contain_text('Версия 2')
    page.get_by_label('Подтвердите номер накладной',exact=True).fill('INV-BROWSER-01')
    page.get_by_label('Причина',exact=True).fill('Документ больше не нужен')
    page.get_by_role('button',name='Документ не нужен — в архив',exact=True).click()
    expect(page.locator('#document-dialog')).to_be_hidden()
    expect(row).to_have_count(0)
    page.get_by_label('Раздел',exact=True).select_option('archived')
    expect(row).to_have_count(1)
    page.get_by_role('button',name='Открыть',exact=True).click()
    expect(page.get_by_role('button',name='Вернуть в черновики',exact=True)).to_have_count(0)
    expect(page.get_by_role('button',name='Редактировать состав',exact=True)).to_have_count(0)


def test_documents_excel_mapping_preview_then_commit(page,live_app):
    from playwright.sync_api import expect
    from marking_erp.invoice_excel import xlsx
    login(page,'bm');page.locator('#documents-link').click()
    page.get_by_role('button',name='+ Новая накладная',exact=True).click()
    page.get_by_label('Номер накладной',exact=True).fill('INV-XLSX')
    page.get_by_label('Поставщик',exact=True).fill('Поставщик Excel')
    content=xlsx({'Товары':[['A','B','C','D','E'],['PN/Excel','04640638349704',10,3,'8542399000']]})
    page.get_by_label('Файл Excel',exact=True).set_input_files({'name':'invoice.xlsx','mimeType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','buffer':content})
    page.get_by_role('button',name='Предпросмотр Excel',exact=True).click()
    expect(page.get_by_label('Лист Excel',exact=True)).to_be_visible()
    for name,value in [('PN','0'),('GTIN','1'),('Штук в этикетке','2'),('Этикеток','3'),('ТН ВЭД','4')]:
        page.get_by_label(name,exact=True).select_option(value)
    capture(page,'documents-import-mapping')
    page.get_by_role('button',name='Проверить сопоставление',exact=True).click()
    expect(page.get_by_role('button',name='Применить строки к черновику',exact=True)).to_be_visible()
    page.get_by_role('button',name='Применить строки к черновику',exact=True).click()
    expect(page.get_by_label('PN, строка 1',exact=True)).to_have_value('PN/Excel')
    page.get_by_role('button',name='Сохранить черновик',exact=True).click()
    expect(page.locator('#document-dialog')).to_be_hidden()
    row=page.locator('#document-list tbody tr');expect(row).to_have_count(1)
    result=page.request.get(live_app['base']+'/api/documents/'+row.get_attribute('data-document-id')).json()
    assert result['document']['lines'][0]['labels']==3
    assert result['document']['lines'][0]['quantity']==10
    assert result['external_dispatch_available'] is False


def test_documents_dirty_close_network_and_live_revoke(page,live_app,engine):
    from playwright.sync_api import expect
    login(page,'bm');page.locator('#documents-link').click()
    page.get_by_role('button',name='+ Новая накладная',exact=True).click()
    page.get_by_label('Номер накладной',exact=True).fill('Unsaved')
    page.once('dialog',lambda dialog:dialog.dismiss())
    page.locator('#document-dialog [data-close]').click()
    expect(page.locator('#document-dialog')).to_be_visible()
    page.once('dialog',lambda dialog:dialog.accept())
    page.locator('#document-dialog [data-close]').click()
    expect(page.locator('#document-dialog')).to_be_hidden()
    page.context.set_offline(True);page.get_by_role('button',name='Обновить накладные',exact=True).click()
    expect(page.locator('#notice')).to_contain_text('Не удалось получить ответ')
    page.context.set_offline(False);page.get_by_role('button',name='Обновить накладные',exact=True).click()
    page.get_by_role('button',name='+ Новая накладная',exact=True).click()
    page.get_by_label('Номер накладной',exact=True).fill('Secret draft')
    with engine.begin() as c:c.execute(text("UPDATE group_grants SET scope='none' WHERE group_slug='bm' AND permission_id='marking.documents.view'"))
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    expect(page.locator('#documents-link')).to_be_hidden()
    expect(page.locator('#document-dialog')).to_be_hidden()
    expect(page.locator('#document-content')).to_be_empty()


@pytest.mark.parametrize('group',['logistics','bm'])
def test_logistics_blockers_and_whole_supplier_batch_ui(page,live_app,engine,group):
    """Synthetic local document; UI reads real API/DB, no external success fixture."""
    from playwright.sync_api import expect
    from marking_erp.policy import snapshot
    from marking_erp.business import store
    org=live_app['org'];owner=live_app['users']['bm'];source=uuid4()
    with engine.begin() as c:
        user=dict(c.execute(text('SELECT * FROM users WHERE id=:id'),{'id':owner}).mappings().one())
        current=auth.Actor(c,user,'fixture',None)
        lines=[{'pn':'PN-UI','gtin':'04640638349704','tnved':'8542399000','quantity':10,'labels':1}]
        c.execute(text('INSERT INTO operation_targets(id,organization_id,owner_id,revision) VALUES (:id,:org,:owner,1)'),{'id':source,'org':org,'owner':owner})
        c.execute(text('''INSERT INTO bm_documents(id,organization_id,owner_id,number,document_date,supplier,revision,state,lines,policy_snapshot,request_sha256)
            VALUES (:id,:org,:owner,'UI-LOG-01','2026-10-26','Поставщик UI',1,'prepared',CAST(:lines AS jsonb),CAST(:policy AS jsonb),:sha)'''),
            {'id':source,'org':org,'owner':owner,'lines':json.dumps(lines),'policy':json.dumps(snapshot(c,org),default=str),'sha':'0'*64})
        store.create(current,'marking','UI-LOG-01',{'supplier':'Поставщик UI','lines':lines,'source_revision':1},org,source,state='labels_ready')
    login(page,group)
    if group=='logistics':
        page.locator('#logistics-link').click()
        page.get_by_role('button',name='Закрытие накладных',exact=True).click()
        page.get_by_role('button',name='Проверить этапы',exact=True).click()
        expect(page.get_by_text('До закрытия нужно выполнить',exact=True)).to_be_visible()
        expect(page.get_by_text('Нет подтверждённого отчёта о нанесении',exact=True)).to_be_visible()
        expect(page.get_by_role('button',name='Закрыть накладную',exact=True)).to_have_count(0)
    else:
        page.locator('#atks-link').click()
        page.get_by_role('button',name='+ Сформировать АТК',exact=True).click()
        page.get_by_label('Поставщик',exact=True).select_option('Поставщик UI')
        expect(page.get_by_text('UI-LOG-01 · Этикетки готовы · Ожидается отчёт',exact=True)).to_be_visible()
        expect(page.get_by_role('button',name='Сформировать АТК всей партии',exact=True)).to_be_disabled()
    capture(page,'workflow-'+group+'-desktop')
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    capture(page,'workflow-'+group+'-390')


def test_catalog_sync_progress_and_report_guidance_ui(page,live_app,engine):
    from playwright.sync_api import expect
    from marking_erp.business import store
    from marking_erp.business.errors import report_problem
    org=live_app['org'];owner=live_app['users']['specialist']
    with engine.begin() as c:
        user=dict(c.execute(text('SELECT * FROM users WHERE id=:id'),{'id':owner}).mappings().one());current=auth.Actor(c,user,'fixture',None)
        store.create(current,'sync','Sync UI fixture',{'from':'2026-01-01 00:00:00','to':'2026-01-02 00:00:00','windows':[],'count':15001,'offset':0},org,state='done')
        code,message=report_problem({'error':'Не найдена карточка товара или нет прав'})
        store.create(current,'marking','REPORT-UI-01',{'supplier':'Поставщик UI','lines':[],'last_rejected_step':'bm.report','last_rejection_code':code,'last_rejection_message':message},org,state='rejected')
    login(page,'specialist');page.locator('#catalog-link').click();page.get_by_role('button',name='Ход синхронизации',exact=True).click()
    expect(page.get_by_text('Уникальных карточек обработано: 15001',exact=True)).to_be_visible()
    capture(page,'catalog-sync-progress')
    page.locator('#business-dialog [data-close]').click()
    page.locator('#marking-link').click();page.get_by_role('button',name='Открыть',exact=True).click()
    expect(page.get_by_text(message,exact=True)).to_be_visible()
    page.get_by_role('button',name='Повторить отклонённый отчёт',exact=True).click()
    expect(page.get_by_label('Фактическая дата производства',exact=True)).to_be_visible()
    with engine.connect() as c:assert c.execute(text('SELECT count(*) FROM external_operations')).scalar_one()==0


def test_logistics_handback_full_excel_in_bm_ui(page,browser,live_app,engine):
    """Local upstream fixtures; browser performs the real handback/upload APIs."""
    from playwright.sync_api import expect
    from marking_erp.business import store
    from marking_erp.policy import snapshot
    from test_gs1 import sample
    from test_suppliers_composition_postgres import full_excel
    org=live_app['org'];owner=live_app['users']['bm'];source=uuid4();code=uuid4()
    with engine.begin() as c:
        user=dict(c.execute(text('SELECT * FROM users WHERE id=:id'),{'id':owner}).mappings().one());current=auth.Actor(c,user,'fixture',None)
        lines=[{'pn':'PN/A-1','gtin':'04640638349704','tnved':'8542399000','quantity':10,'labels':1}]
        c.execute(text('INSERT INTO operation_targets(id,organization_id,owner_id,revision) VALUES (:id,:org,:owner,2)'),{'id':source,'org':org,'owner':owner})
        c.execute(text('''INSERT INTO bm_documents(id,organization_id,owner_id,number,document_date,supplier,revision,state,lines,policy_snapshot,request_sha256)
            VALUES (:id,:org,:owner,'FULL-UI-01','2026-10-26','Поставщик UI',2,'prepared',CAST(:lines AS jsonb),CAST(:policy AS jsonb),:sha)'''),{'id':source,'org':org,'owner':owner,'lines':json.dumps(lines),'policy':json.dumps(snapshot(c,org),default=str),'sha':'0'*64})
        item=store.create(current,'marking','FULL-UI-01',{'supplier':'Поставщик UI','lines':lines,'source_revision':2},org,source,state='applied')
        c.execute(text('''INSERT INTO business_codes(id,organization_id,cis,gtin,base,payload,item_id,line_index,quantity,state,tnved,pn,evidence_ref)
            VALUES (:id,:org,:cis,:gtin,:base,:payload,:item,0,10,'applied','8542399000','PN/A-1','fixture')'''),{'id':code,'org':org,'cis':sample(37)[:38].decode(),'gtin':'04640638349704','base':sample(37),'payload':sample(37,quantity=10),'item':item['id']})
        store.create(current,'atk','ATK UI fixture',{'supplier':'Поставщик UI','tnved':'8542399000','documents':[str(item['id'])],'codes':[str(code)],'atk':'1'*25},org,state='transferred')
    login(page,'logistics');page.locator('#logistics-link').click();page.get_by_role('button',name='Закрытие накладных',exact=True).click();page.get_by_role('button',name='Проверить этапы',exact=True).click()
    page.get_by_role('button',name='Вернуть в БМ для полного Excel',exact=True).click();page.get_by_role('button',name='Вернуть в БМ',exact=True).click()
    expect(page.get_by_text('Документ возвращён в БМ. Ожидается полный Excel поставки.',exact=True)).to_be_visible()
    context=browser.new_context(viewport={'width':1440,'height':900},locale='ru-RU');bm=context.new_page();errors=[];bm.on('pageerror',lambda e:errors.append(str(e)))
    try:
        bm.goto(live_app['base']);login(bm,'bm');bm.locator('#documents-link').click();bm.get_by_label('Раздел',exact=True).select_option('composition_requested');expect(bm.get_by_text('Дополнить состав для склада',exact=True)).to_be_visible();bm.get_by_role('button',name='Открыть',exact=True).click()
        bm.get_by_label('Полный Excel поставки',exact=True).set_input_files({'name':'full.xlsx','mimeType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','buffer':full_excel()})
        bm.get_by_role('button',name='Проверить полный Excel',exact=True).click()
        expect(bm.get_by_text('Маркированных позиций сверено: 1. Немаркируемых строк: 2.',exact=True)).to_be_visible()
        capture(bm,'bm-full-invoice-preview')
        bm.get_by_role('button',name='Сохранить полный состав и вернуть логисту',exact=True).click();expect(bm.locator('#document-dialog')).to_be_hidden();bm.get_by_label('Раздел',exact=True).select_option('warehouse_ready');expect(bm.get_by_role('cell',name='Полный состав готов',exact=True)).to_be_visible()
        bm.set_viewport_size({'width':390,'height':844});assert bm.evaluate('document.documentElement.scrollWidth<=innerWidth');capture(bm,'bm-full-invoice-390')
        assert not errors,errors
    finally:context.close()
    with engine.connect() as c:assert c.execute(text('SELECT count(*) FROM external_operations')).scalar_one()==0


def test_supplier_alias_admin_ui(page,live_app,engine):
    from playwright.sync_api import expect
    from marking_erp.policy import snapshot
    org=live_app['org'];owner=live_app['users']['bm']
    with engine.begin() as c:
        for n,name in enumerate(['SUPPLIER-A','SUPPLIER-B']):
            id=uuid4();c.execute(text('INSERT INTO operation_targets(id,organization_id,owner_id,revision) VALUES (:id,:org,:owner,1)'),{'id':id,'org':org,'owner':owner})
            c.execute(text('''INSERT INTO bm_documents(id,organization_id,owner_id,number,document_date,supplier,revision,state,lines,policy_snapshot,request_sha256)
                VALUES (:id,:org,:owner,:number,'2026-10-26',:supplier,1,'draft',CAST(:lines AS jsonb),CAST(:policy AS jsonb),:sha)'''),{'id':id,'org':org,'owner':owner,'number':'SUP-'+str(n),'supplier':name,'lines':json.dumps([{'pn':'PN','gtin':'','tnved':'','quantity':1,'labels':1}]),'policy':json.dumps(snapshot(c,org),default=str),'sha':'0'*64})
    login(page);page.locator('#connections-link').click();page.get_by_role('button',name='Поставщики и алиасы',exact=True).click()
    page.get_by_label('SUPPLIER-A',exact=True).check();page.get_by_label('SUPPLIER-B',exact=True).check()
    page.get_by_label('Единое название поставщика',exact=True).fill('Единый поставщик');page.get_by_label('Причина объединения',exact=True).fill('Два написания одного поставщика')
    page.get_by_role('button',name='Объединить выбранные варианты',exact=True).click()
    expect(page.get_by_label('SUPPLIER-A → Единый поставщик',exact=True)).to_be_visible()
    capture(page,'suppliers-aliases-desktop');page.set_viewport_size({'width':390,'height':844});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth');capture(page,'suppliers-aliases-390')
