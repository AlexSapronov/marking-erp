import {createAdmin} from '/assets/admin.js';
const $ = id => document.getElementById(id);
const field = (form, name) => form.elements.namedItem(name);
const state = {session:null,generation:0,route:'/',users:[],options:null,editing:null,credentials:null,query:'',group:'',status:'active',page:1};
const admin=createAdmin({state,$,api,node,button,heading,notice,report,openDialog,closeDialog});
const groupNames = {bm:'БМ',logistics:'Логист',warehouse:'Склад',tsd:'ТСД',specialist:'Специалист по маркировке',administrator:'Администратор'};
const modules = [
  ['Национальный каталог','Карточки товаров и характеристики','nk.cards.view','▦'],
  ['Накладные БМ','Подготовка маркировки и документов','marking.documents.create','▤'],
  ['Логистика','Готовые АТК и передача на склад','logistics.warehouse.transfer','⇄'],
  ['Приёмка','Сверка поступившего товара','warehouse.receipt.view','↓'],
  ['Проверка КМ','Проверка отсканированных кодов','codes.verify','⌕'],
  ['Отгрузка','Проверка товара перед передачей','warehouse.shipment.view','↑'],
  ['Разделение КМ','Работа с отделяемой частью товара','split.operations.create','⋈'],
  ['Перепечать этикеток','Повторная печать сохранённой маркировки','labels.reprint','▧']
];
function node(tag, text, className) { const n=document.createElement(tag); if(text!==undefined)n.textContent=text;if(className)n.className=className;return n; }
function button(text, action, className='') {const n=node('button',text,className);n.type='button';n.addEventListener('click',action);return n;}
function has(permission) {return Boolean(state.session?.permissions[permission]);}
function managesUsers() {return state.session?.permissions['users.manage']==='all';}
function errorText(data) {const detail=data?.detail;return typeof detail==='string'?detail:detail?.message||(Array.isArray(detail)?'Проверьте заполнение полей.':'Не удалось выполнить действие.');}
async function api(path, {method='GET',body}={}) {
  const generation=state.generation;
  const headers={'X-Requested-With':'marking-erp'};
  if(body!==undefined)headers['Content-Type']='application/json';
  if(state.session)headers['X-CSRF-Token']=state.session.csrf;
  const controller=new AbortController();const timeout=setTimeout(()=>controller.abort(),20000);
  let response;
  try {response=await fetch(path,{method,headers,body:body===undefined?undefined:JSON.stringify(body),credentials:'same-origin',signal:controller.signal});}
  catch (_) {throw Object.assign(new Error('Не удалось получить ответ. Проверьте связь и результат действия перед повтором.'),{code:'network'});}
  finally {clearTimeout(timeout);}
  if(generation!==state.generation)throw Object.assign(new Error('Устаревший ответ'),{code:'stale'});
  const data=await response.json().catch(()=>({}));
  if(!response.ok){
    const error=Object.assign(new Error(errorText(data)),{status:response.status,code:data?.detail?.code});
    if(response.status===401&&state.session){showLogin('Сессия завершена. Войдите снова.');error.code='stale';}
    throw error;
  }
  return data;
}
function notice(text,kind='success',action=null) {
  const host=$('notice');host.className='notice '+kind;host.replaceChildren(node('span',text));host.hidden=false;
  if(action)host.append(button(action.label,action.run));
}
function report(error,target) {
  if(error.code==='stale')return;
  if(target){$(target).textContent=error.message;$(target).hidden=false;}
  else notice(error.message,'error');
  if(error.code==='reauth_required')notice('Для этого действия подтвердите административный доступ.','warning',{label:'Подтвердить доступ',run:()=>openDialog('reauth-dialog')});
}
function busy(form,value,token) {
  if(!value&&token!==undefined&&form.dataset.busyToken!==String(token))return;
  if(value)form.dataset.busyToken=String(++busySequence);
  form.dataset.busy=String(value);form.setAttribute('aria-busy',String(value));const fieldset=form.querySelector('fieldset');if(fieldset)fieldset.disabled=value;
  form.querySelectorAll('button[type=submit]').forEach(b=>b.disabled=value);return form.dataset.busyToken;
}
let busySequence=0;
function resetDialog(dialog) {
  const form=dialog.querySelector('form');if(!form)return;
  form.reset();form.dataset.busyToken=String(++busySequence);busy(form,false);form.dataset.dirty='false';form.querySelectorAll('input[type=password],input[autocomplete=off],input[autocomplete=one-time-code]').forEach(i=>i.value='');
  form.querySelectorAll('input[name=totp_secret]').forEach(i=>i.type='password');
  dialog.querySelectorAll('.error-message').forEach(n=>n.hidden=true);
}
function closeDialog(id,force=false) {
  const dialog=$(id),form=dialog.querySelector('form');
  if(!force&&form?.dataset.busy==='true')return false;
  if(!force&&id==='user-dialog'&&form?.dataset.dirty==='true'&&!confirm('Закрыть окно без сохранения изменений?'))return false;
  if(dialog.open)dialog.close();resetDialog(dialog);return true;
}
function openDialog(id) {const d=$(id);d.querySelectorAll('.error-message').forEach(n=>n.hidden=true);if(!d.open)d.showModal();d.querySelector('input:not([disabled])')?.focus();}
function allDialogsClosed() {for(const d of document.querySelectorAll('dialog[open]'))if(!closeDialog(d.id))return false;return true;}
function showLogin(message='') {
  admin.reset();state.generation++;state.session=null;state.users=[];state.options=null;state.editing=null;state.credentials=null;
  document.querySelectorAll('dialog').forEach(d=>closeDialog(d.id,true));
  $('workspace').hidden=true;$('login-screen').hidden=false;$('boot').hidden=true;
  $('login-password').value='';$('login-otp').value='';$('login-error').textContent=message;$('login-error').hidden=!message;
  document.title='Marking ERP · Вход';$('login-name').focus();
}
function shell() {
  $('boot').hidden=true;$('login-screen').hidden=true;$('workspace').hidden=false;
  $('workspace').classList.toggle('tsd',state.session.user.group_slug==='tsd');
  $('context-label').textContent=state.session.organizations.length===1?state.session.organizations[0].name:'Личное рабочее пространство';
  $('context-name').textContent=state.session.user.display_name;
  $('group-badge').textContent=state.session.group_name;
  $('users-link').hidden=!managesUsers();
  $('groups-link').hidden=!has('permissions.manage');$('rules-link').hidden=!has('business_rules.manage');
  document.querySelectorAll('[data-nav]').forEach(link=>{if(link.closest('nav')){if(link.getAttribute('href')===state.route)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current');}});
}
function heading(title,description,actions=[]) {
  const h=node('div',undefined,'page-heading'),text=node('div');text.append(node('h1',title),node('p',description,'muted'));h.append(text);
  if(actions.length){const a=node('div',undefined,'actions');a.append(...actions);h.append(a);}return h;
}
function organizationList(ids=state.session.organization_ids) {
  const list=node('ul',undefined,'organization-list');
  for(const id of ids){const org=state.session.organizations.find(o=>o.id===id);if(!org)continue;const item=node('li',org.name);item.append(node('small','ИНН '+org.inn));list.append(item);}
  if(!list.children.length)list.append(node('li','Организации не назначены'));return list;
}
function home() {
  const host=$('page-content');host.replaceChildren(heading(state.session.user.group_slug==='tsd'?'Рабочее место ТСД':'Рабочее место · '+state.session.group_name,'Здравствуйте, '+state.session.user.display_name));
  const card=node('section',undefined,'panel');card.append(node('h2','Ваше рабочее пространство'),node('p','Доступные разделы определяются вашей группой и назначенными организациями.','muted'));
  const meta=node('div',undefined,'welcome-meta');meta.append(node('span',state.session.group_name,'badge'));card.append(meta);host.append(card);
  if(managesUsers()){const a=node('section',undefined,'panel section-gap');a.append(node('h3','Команда и доступ'),node('p','Создание пользователей, назначение групп и управление сессиями.','muted'),button('Открыть пользователей',()=>navigate('/users'),'primary section-gap'));host.append(a);}
  const available=modules.filter(m=>has(m[2]));
  if(available.length){const grid=node('div',undefined,'module-grid');for(const [title,description,,icon] of available){const c=node('section',undefined,'module-card');c.setAttribute('aria-label',title+' — готовится к запуску');c.append(node('span',icon,'module-icon'),node('h3',title),node('p',description,'muted'),node('span','Готовится к запуску','planned'));grid.append(c);}host.append(grid);}
}
function profile() {
  const host=$('page-content');host.replaceChildren(heading('Мой профиль','Личная учётная запись и управление доступом.'));
  const card=node('section',undefined,'panel');card.append(node('h2',state.session.user.display_name));const dl=node('dl',undefined,'profile-list');
  for(const [label,value] of [['Логин',state.session.user.username],['Группа',state.session.group_name],['Организации',organizationList()],['Подтверждение входа',state.session.mfa_required?'Пароль и TOTP':'Личный пароль']]){dl.append(node('dt',label));const dd=node('dd');dd.append(typeof value==='string'?document.createTextNode(value):value);dl.append(dd);}card.append(dl);
  const actions=node('div',undefined,'actions');actions.append(button('Сменить пароль',()=>{
    const f=$('password-form');f.reset();field(f,'new_password').minLength=state.session.administrative_access?16:12;$('own-otp-label').hidden=!state.session.mfa_required;field(f,'otp').required=state.session.mfa_required;openDialog('password-dialog');
  }),button('Выйти на всех устройствах',async()=>{if(await ask('Завершить все сессии?','Потребуется войти заново на каждом устройстве.','Выйти везде'))try{await api('/api/session/logout-all',{method:'POST'});showLogin('Все ваши сессии завершены.');}catch(e){report(e);}},'quiet'));
  if(state.session.administrative_access)actions.append(button('Подтвердить доступ',()=>openDialog('reauth-dialog'),'quiet'));card.append(actions);host.append(card);
}
function forbidden() {const p=node('section',undefined,'panel empty');p.append(node('h2','Раздел недоступен'),node('p','У вашей группы нет разрешения на управление пользователями.'),button('На рабочее место',()=>navigate('/'),'section-gap'));$('page-content').replaceChildren(p);}
function navigate(path,replace=false) {
  if(!state.session||!allDialogsClosed()||!admin.canLeave())return;
  state.route=['/','/profile','/users','/groups','/rules'].includes(path)?path:'/';
  history[replace?'replaceState':'pushState']({},'',state.route);$('notice').hidden=true;shell();
  document.title='Marking ERP · '+({'/':'Рабочее место','/profile':'Мой профиль','/users':'Пользователи','/groups':'Группы и права','/rules':'Бизнес-правила'}[state.route]);
  if(state.route==='/groups'||state.route==='/rules'){admin.show(state.route);return;}
  if(state.route==='/profile')profile();else if(state.route==='/users'){if(managesUsers())usersPage();else forbidden();}else home();
}
function usersPage() {
  const host=$('page-content');host.replaceChildren(heading('Пользователи','Учётные записи в доступных вам организациях.',[button('Обновить',()=>loadUsers()),button('+ Новый пользователь',()=>openUser(null),'primary')]));
  const filters=node('div',undefined,'filters');
  const searchLabel=node('label','Поиск'),search=node('input');search.placeholder='Имя сотрудника или логин';search.setAttribute('aria-label','Поиск пользователей');search.value=state.query;search.addEventListener('input',()=>{state.query=search.value;state.page=1;userRows();});searchLabel.append(search);
  const groupLabel=node('label','Группа'),group=node('select');group.setAttribute('aria-label','Фильтр по группе');group.append(new Option('Все группы',''));for(const [value,label] of Object.entries(groupNames))group.append(new Option(label,value));group.value=state.group;group.addEventListener('change',()=>{state.group=group.value;state.page=1;userRows();});groupLabel.append(group);
  const statusLabel=node('label','Состояние'),status=node('select');status.setAttribute('aria-label','Фильтр по состоянию');for(const [value,label] of [['active','Включённые'],['all','Все'],['disabled','Отключённые']])status.append(new Option(label,value));status.value=state.status;status.addEventListener('change',()=>{state.status=status.value;state.page=1;userRows();});statusLabel.append(status);
  filters.append(searchLabel,groupLabel,statusLabel);host.append(filters);const tableHost=node('section',undefined,'panel table-panel');tableHost.id='user-table-host';host.append(tableHost);loadUsers();
}
async function loadUsers() {
  if(state.route!=='/users'||!managesUsers())return;
  const host=$('user-table-host');if(!host)return;host.replaceChildren(node('p','Загружаем пользователей…','empty'));host.setAttribute('aria-busy','true');
  try {const [users,options]=await Promise.all([api('/api/users'),api('/api/users/options')]);if(state.route!=='/users'||!state.session)return;state.users=users.users;state.options=options;userRows();}
  catch(e){if(e.code==='stale')return;host.replaceChildren(node('div',e.code==='reauth_required'?'Для просмотра пользователей подтвердите доступ.':e.message,'empty'));report(e);}
  finally {host.removeAttribute('aria-busy');}
}
function userRows() {
  const host=$('user-table-host');if(!host||state.route!=='/users')return;const q=state.query.trim().toLocaleLowerCase('ru');
  const rows=state.users.filter(u=>(!q||(u.display_name+' '+u.username).toLocaleLowerCase('ru').includes(q))&&(!state.group||u.group_slug===state.group)&&(state.status==='all'||u.enabled===(state.status==='active')));
  host.replaceChildren();if(!rows.length){const empty=node('div',undefined,'empty');empty.append(node('h2','Нет пользователей по выбранным условиям'),node('p','Измените поиск или фильтры.'));host.append(empty);return;}
  const pages=Math.max(1,Math.ceil(rows.length/25));state.page=Math.min(state.page,pages);const wrap=node('div',undefined,'table-wrap'),table=node('table'),caption=node('caption','Пользователи системы','sr-only'),thead=node('thead'),head=node('tr');
  for(const title of ['Сотрудник','Логин','Группа','Состояние','Действия'])head.append(node('th',title));thead.append(head);table.append(caption,thead);const tbody=node('tbody');
  for(const user of rows.slice((state.page-1)*25,state.page*25)){
    const row=node('tr');row.dataset.userId=user.id;const name=node('td',undefined,'name-cell');name.append(button(user.display_name,()=>openUser(user),'link-button'));name.append(node('small',user.organization_ids.map(id=>state.session.organizations.find(o=>o.id===id)?.name||'').filter(Boolean).join(', ')));
    row.append(name,node('td',user.username,'login-cell'),node('td',groupNames[user.group_slug]||user.group_slug));const enabled=node('td');enabled.append(node('span',user.enabled?'Включён':'Отключён','badge '+(user.enabled?'enabled':'disabled')));row.append(enabled);
    const td=node('td'),actions=node('div',undefined,'actions');actions.append(button('Изменить',()=>openUser(user)),button('Пароль и TOTP',()=>openCredentials(user),'quiet'));
    if(has('sessions.revoke'))actions.append(button('Сессии',async()=>{if(await ask('Завершить сессии пользователя?',user.display_name+' потребуется войти заново на всех устройствах.','Завершить'))try{await api('/api/users/'+user.id+'/revoke-sessions',{method:'POST'});notice('Сессии пользователя завершены.');if(user.id===state.session?.user.id){showLogin('Ваши сессии завершены.');return;}await loadUsers();}catch(e){report(e);}},'quiet'));td.append(actions);row.append(td);tbody.append(row);
  }
  table.append(tbody);wrap.append(table);host.append(wrap);const footer=node('div',undefined,'table-footer');footer.append(node('span',`${rows.length} из ${state.users.length} · Страница ${state.page} / ${pages}`));const actions=node('div',undefined,'actions');const previous=button('Назад',()=>{state.page--;userRows();}),next=button('Далее',()=>{state.page++;userRows();});previous.disabled=state.page<=1;next.disabled=state.page>=pages;actions.append(previous,next);footer.append(actions);host.append(footer);
}
function openUser(user) {
  if(!state.options){notice('Сначала обновите список пользователей.','warning');return;}
  const form=$('user-form');resetDialog($('user-dialog'));state.editing=user;
  $('user-title').textContent=user?'Изменить пользователя':'Новый пользователь';field(form,'username').value=user?.username||'';field(form,'username').disabled=Boolean(user);field(form,'display_name').value=user?.display_name||'';
  const groups=field(form,'group_slug');groups.replaceChildren();for(const g of state.options.groups)groups.append(new Option(g.name,g.slug));groups.value=user?.group_slug||'bm';
  field(form,'enabled').checked=user?.enabled??true;$('enabled-row').hidden=!user;$('user-password-fields').hidden=Boolean(user);field(form,'password').required=!user;field(form,'password_confirm').required=!user;
  const orgs=$('user-organizations');orgs.replaceChildren();for(const org of state.session.organizations){const label=node('label',undefined,'check-row'),input=node('input');input.type='checkbox';input.name='organization_ids';input.value=org.id;input.checked=user?user.organization_ids.includes(org.id):true;label.append(input,node('span',org.name));orgs.append(label);}
  groupHelp();form.dataset.dirty='false';openDialog('user-dialog');
}
function groupHelp() {
  const form=$('user-form'),group=state.options?.groups.find(g=>g.slug===field(form,'group_slug').value),needs=Boolean(group?.requires_mfa),editing=Boolean(state.editing);
  $('group-help').textContent=needs?'Административный доступ: обязательны пароль и TOTP.':(group?.slug==='tsd'?'Базовый профиль ТСД: только приёмка, проверка КМ и отгрузка.':'Права определяются выбранной группой.');
  $('user-mfa-fields').hidden=editing||!needs;$('edit-mfa-help').hidden=!(editing&&needs&&!state.editing.mfa_enabled);
  field(form,'password').minLength=needs?16:12;field(form,'totp_secret').required=!editing&&needs;field(form,'totp_code').required=!editing&&needs;
  if(editing||!needs){field(form,'totp_secret').value='';field(form,'totp_code').value='';}
}
function openCredentials(user) {state.credentials=user;const f=$('credentials-form');resetDialog($('credentials-dialog'));$('credentials-target').textContent=user.display_name+' · '+user.username;field(f,'password').minLength=state.options.groups.find(g=>g.slug===user.group_slug)?.requires_mfa?16:12;$('credentials-mfa').hidden=true;field(f,'totp_secret').required=false;field(f,'totp_code').required=false;openDialog('credentials-dialog');}
function makeTotp(target) {
  const alphabet='ABCDEFGHIJKLMNOPQRSTUVWXYZ234567',bytes=crypto.getRandomValues(new Uint8Array(20));let bits=0,value=0,out='';
  for(const byte of bytes){value=(value<<8)|byte;bits+=8;while(bits>=5){out+=alphabet[(value>>>(bits-5))&31];bits-=5;}}
  target.value=out;target.type='text';target.form.dataset.dirty='true';target.focus();target.select();
}
async function ask(title,message,label) {
  const dialog=$('confirm-dialog');$('confirm-title').textContent=title;$('confirm-message').textContent=message;$('confirm-ok').textContent=label;dialog.returnValue='cancel';dialog.showModal();
  return await new Promise(resolve=>dialog.addEventListener('close',()=>resolve(dialog.returnValue==='ok'),{once:true}));
}
async function refreshSession() {
  if(!state.session||document.visibilityState!=='visible')return;
  try{const previous=managesUsers(),data=await api('/api/session');state.session=data;shell();if((state.route==='/groups'&&!has('permissions.manage'))||(state.route==='/rules'&&!has('business_rules.manage'))){admin.reset();closeDialog('configuration-dialog',true);navigate('/',true);notice('Ваши права изменились. Раздел недоступен.','warning');return;}if(previous&&!managesUsers()){for(const id of ['user-dialog','credentials-dialog','reauth-dialog'])closeDialog(id,true);if(state.route==='/users'){navigate('/',true);notice('Ваши права изменились. Управление пользователями недоступно.','warning');}}}
  catch(e){if(e.code!=='stale')report(e);}
}
document.querySelectorAll('[data-nav]').forEach(link=>link.addEventListener('click',event=>{if(state.session){event.preventDefault();navigate(link.getAttribute('href'));}}));
document.querySelectorAll('[data-close]').forEach(b=>b.addEventListener('click',()=>closeDialog(b.dataset.close)));
document.querySelectorAll('dialog').forEach(dialog=>{dialog.addEventListener('cancel',event=>{event.preventDefault();closeDialog(dialog.id);});dialog.addEventListener('close',()=>resetDialog(dialog));const form=dialog.querySelector('form');form?.addEventListener('input',()=>form.dataset.dirty='true');});
$('theme').addEventListener('click',()=>{const theme=document.documentElement.dataset.theme==='dark'?'light':'dark';document.documentElement.dataset.theme=theme;try{localStorage.setItem('erp-theme',theme);}catch(_){};});
$('logout').addEventListener('click',async()=>{if(!allDialogsClosed())return;try{await api('/api/session',{method:'DELETE'});showLogin();history.replaceState({},'','/login');}catch(e){report(e);}});
$('login-form').addEventListener('submit',async event=>{
  event.preventDefault();const form=event.currentTarget;if(form.dataset.busy==='true')return;const busyToken=busy(form,true);$('login-error').hidden=true;
  try{const data=await api('/api/session',{method:'POST',body:{username:$('login-name').value,password:$('login-password').value,otp:$('login-otp').value}});state.generation++;state.session=data;form.reset();state.route=location.pathname==='/users'?'/users':'/';shell();navigate(state.route,true);}
  catch(e){if(e.code!=='stale'){report(e,'login-error');$('login-password').value='';$('login-otp').value='';$('login-password').focus();}}
  finally{busy(form,false,busyToken);}
});
$('user-form').addEventListener('submit',async event=>{
  event.preventDefault();const f=event.currentTarget;if(f.dataset.busy==='true')return;$('user-error').hidden=true;const target=state.editing;
  if(!target&&field(f,'password').value!==field(f,'password_confirm').value){report(new Error('Пароли не совпадают.'),'user-error');return;}
  const organization_ids=[...f.querySelectorAll('[name=organization_ids]:checked')].map(n=>n.value);if(!organization_ids.length){report(new Error('Выберите хотя бы одну организацию.'),'user-error');return;}
  const group=field(f,'group_slug').value;if(target&&state.options.groups.find(g=>g.slug===group)?.requires_mfa&&!target.mfa_enabled){report(new Error('Сначала настройте TOTP через действие «Пароль и TOTP».'),'user-error');return;}
  if(target&&target.enabled&&!field(f,'enabled').checked&&!(await ask('Отключить пользователя?',target.display_name+' потеряет доступ к системе.','Отключить')))return;
  const body={display_name:field(f,'display_name').value,group_slug:group,organization_ids};
  if(target)Object.assign(body,{auth_version:target.auth_version,enabled:field(f,'enabled').checked});else{Object.assign(body,{username:field(f,'username').value,password:field(f,'password').value});if(!$('user-mfa-fields').hidden)Object.assign(body,{totp_secret:field(f,'totp_secret').value,totp_code:field(f,'totp_code').value});}
  const busyToken=busy(f,true);try{await api(target?'/api/users/'+target.id:'/api/users',{method:target?'PATCH':'POST',body});closeDialog('user-dialog',true);if(!state.session)return;if(target?.id===state.session.user.id){await refreshSession();if(!state.session)return;}notice(target?'Изменения сохранены.':'Пользователь создан.');await loadUsers();}catch(e){report(e,'user-error');}finally{busy(f,false,busyToken);}
});
field($('user-form'),'group_slug').addEventListener('change',groupHelp);
field($('credentials-form'),'replace_mfa').addEventListener('change',event=>{const checked=event.target.checked;$('credentials-mfa').hidden=!checked;for(const name of ['totp_secret','totp_code']){field($('credentials-form'),name).required=checked;if(!checked)field($('credentials-form'),name).value='';}});
$('generate-user-totp').addEventListener('click',()=>makeTotp(field($('user-form'),'totp_secret')));
$('generate-reset-totp').addEventListener('click',()=>makeTotp(field($('credentials-form'),'totp_secret')));
$('credentials-form').addEventListener('submit',async event=>{
  event.preventDefault();const f=event.currentTarget;if(f.dataset.busy==='true')return;if(field(f,'password').value!==field(f,'password_confirm').value){report(new Error('Пароли не совпадают.'),'credentials-error');return;}
  const target=state.credentials,body={auth_version:target.auth_version,password:field(f,'password').value};if(field(f,'replace_mfa').checked)Object.assign(body,{totp_secret:field(f,'totp_secret').value,totp_code:field(f,'totp_code').value});
  const busyToken=busy(f,true);try{await api('/api/users/'+target.id+'/credentials',{method:'POST',body});closeDialog('credentials-dialog',true);if(target.id===state.session?.user.id){showLogin('Доступ обновлён. Войдите снова.');return;}notice('Доступ пользователя обновлён. Все его сессии завершены.');await loadUsers();}catch(e){report(e,'credentials-error');}finally{busy(f,false,busyToken);}
});
$('reauth-form').addEventListener('submit',async event=>{
  event.preventDefault();const f=event.currentTarget;if(f.dataset.busy==='true')return;const busyToken=busy(f,true);try{await api('/api/session/reauth',{method:'POST',body:{password:field(f,'password').value,otp:field(f,'otp').value}});closeDialog('reauth-dialog',true);notice('Доступ подтверждён. При необходимости повторите действие.');if(state.route==='/users')await loadUsers();}catch(e){report(e,'reauth-error');field(f,'password').value='';field(f,'otp').value='';}finally{busy(f,false,busyToken);}
});
$('password-form').addEventListener('submit',async event=>{
  event.preventDefault();const f=event.currentTarget;if(f.dataset.busy==='true')return;if(field(f,'new_password').value!==field(f,'confirm').value){report(new Error('Новые пароли не совпадают.'),'password-error');return;}
  const busyToken=busy(f,true);try{await api('/api/session/password',{method:'POST',body:{password:field(f,'password').value,new_password:field(f,'new_password').value,otp:field(f,'otp').value}});closeDialog('password-dialog',true);showLogin('Пароль изменён. Войдите снова.');}catch(e){report(e,'password-error');field(f,'password').value='';field(f,'otp').value='';}finally{busy(f,false,busyToken);}
});
window.addEventListener('beforeunload',event=>{if(admin.dirty()){event.preventDefault();event.returnValue='';}});
window.addEventListener('popstate',()=>{if(state.session)navigate(location.pathname,true);});
window.addEventListener('focus',refreshSession);document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshSession();});setInterval(refreshSession,30000);
try{state.session=await api('/api/session');state.route=location.pathname;state.generation++;shell();navigate(location.pathname,true);}catch(e){showLogin(e.status===401?'':e.message);}
