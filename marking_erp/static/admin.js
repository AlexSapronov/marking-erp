const scopeNames={none:'Запрет',own:'Свои',assigned:'Назначенные',all:'Все'};
const kindNames={read:'Чтение',create:'Создание',edit:'Изменение',execute:'Выполнение'};
const choiceNames={same_pn_gtin_supplier:'Совпадают PN, GTIN и поставщик',never:'Не объединять',before_emission:'До эмиссии',before_report_prepare:'До подготовки отчёта',strict:'Строгая проверка',error:'Ошибка',error_with_delete_action:'Ошибка с действием удаления',approved_default:'Базовый профиль (ожидает проверки)',browser_pdf:'PDF через браузер','58x40':'58 × 40 мм'};
const categories={nk:'Национальный каталог',bm:'БМ',archive:'Архив',warehouse:'Склад',logistics:'Логистика',split:'Разделение',labels:'Этикетки',workflow:'Процессы'};
export function createAdmin({state,$,api,node,button,heading,notice,report,openDialog,closeDialog}) {
  let changed=false,sequence=0,model=null,grants={},changes={},group='bm',scope='',query='',moduleFilter='';
  const reset=()=>{changed=false;sequence++;model=null;changes={};grants={};};
  const canLeave=()=>!changed||confirm('Есть несохранённые изменения. Покинуть раздел?');
  function select(label,items,value,onChange){const l=node('label',label),s=node('select');s.setAttribute('aria-label',label);for(const [v,n] of items)s.append(new Option(n,v));s.value=value;s.addEventListener('change',()=>onChange(s.value));l.append(s);return l;}
  function reasonForm(){const f=node('form');f.id='configuration-form';const l=node('label','Причина изменения'),i=node('textarea');i.id='configuration-reason';i.required=true;i.minLength=8;i.maxLength=1000;i.rows=2;l.append(i);f.append(l);f.append(button('Предпросмотр изменений',()=>f.requestSubmit(),'primary'));f.addEventListener('submit',event=>{event.preventDefault();preview(i.value);});return f;}
  function safeError(e){if(e.code==='stale')return;if(e.code==='reauth_required')closeDialog('configuration-dialog',true);else if($('configuration-dialog').open)$('configuration-content').prepend(node('p',e.message,'error-message'));report(e);}
  function allowed(){return state.session?.permissions[state.route==='/groups'?'permissions.manage':'business_rules.manage']==='all';}
  async function show(route){
    reset();const seq=sequence,host=$('page-content');
    if(!allowed()){host.replaceChildren(heading('Раздел недоступен','У вашей группы нет разрешения на этот раздел.'));return;}
    host.replaceChildren(node('p','Загружаем настройки…','empty'));
    try{
      const data=await api(route==='/groups'?'/api/groups':'/api/business-rules/'+(scope||state.session.organization_ids[0]||'system'));
      if(seq!==sequence||state.route!==route)return;
      model=data;changed=false;changes={};route==='/groups'?renderGroups():renderRules();
    }catch(e){if(seq!==sequence)return;if(route==='/rules'&&e.status===403)scope='';host.replaceChildren(heading('Настройки недоступны','Проверьте связь и полномочия.'),button(route==='/rules'&&e.status===403?'К настройкам своей организации':'Повторить загрузку',()=>show(route)));safeError(e);}
  }
  function tools(title,description,kind,entity){
    const host=$('page-content');host.replaceChildren(heading(title,description,[button('Обновить',()=>{if(canLeave())show(state.route);}),button('История изменений',()=>history(kind,entity))]));
    return host;
  }
  function renderGroups(){
    const current=model.groups.find(g=>g.slug===group);grants=structuredClone(current.grants);
    const host=tools('Группы и права','Изменения действуют во всех организациях. Все сессии изменённой группы завершатся.','group',group);
    const controls=node('div',undefined,'filters configuration-filters');controls.append(select('Группа',model.groups.map(g=>[g.slug,g.name]),group,value=>{if(canLeave()){group=value;changed=false;renderGroups();}else controls.querySelector('select').value=group;}));
    const search=node('label','Поиск действий'),input=node('input');input.setAttribute('aria-label','Поиск действий');input.value=query;input.placeholder='ID, описание или модуль';input.addEventListener('input',()=>{query=input.value;groupRows();});search.append(input);controls.append(search);
    controls.append(select('Модуль',[['','Все модули'],...[...new Set(model.catalog.map(p=>p.module))].map(m=>[m,m])],moduleFilter,value=>{moduleFilter=value;groupRows();}));host.append(controls);
    const bar=node('div',undefined,'actions section-gap');bar.append(button('Разрешить найденные',()=>bulk('all')),button('Запретить найденные',()=>bulk('none')),button('Только чтение',()=>bulk('read')),button('Базовый профиль',()=>{grants=Object.fromEntries(model.catalog.map(p=>[p.id,p.defaults[group]==='-'?'none':p.defaults[group]]));mark();groupRows();}));host.append(bar);
    const banner=node('div',undefined,'notice warning section-gap');banner.id='group-deviation';host.append(banner);
    host.append(node('p','Свои — владелец документа; назначенные — связанные задания; все — только разрешённые пользователю организации. Зависимые чтения должны охватывать ту же область.','field-help section-gap'));
    const table=node('div',undefined,'panel table-panel section-gap');table.id='permission-table';host.append(table);
    const footer=node('div',undefined,'policy-footer');footer.append(node('p','Ревизия '+current.revision+' · '+(group==='specialist'?'Профиль специалиста фиксирован: все операционные, без административных.':'У администратора нельзя отключить управление правами.'),'field-help'));
    if(group==='tsd'){const l=node('label','Для расширения профиля введите РАСШИРИТЬ ТСД'),i=node('input');i.id='tsd-confirmation';i.autocomplete='off';l.append(i);footer.append(l);}
    footer.append(reasonForm());host.append(footer);groupRows();
    if(state.session.permissions['users.manage']==='all')host.append(button('Проверить доступ пользователя',accessCheck,'section-gap'));
  }
  function filtered(){const q=query.trim().toLocaleLowerCase('ru');return model.catalog.filter(p=>(!moduleFilter||p.module===moduleFilter)&&(!q||(p.id+' '+p.label+' '+p.description+' '+p.module).toLocaleLowerCase('ru').includes(q)));}
  function locked(p){return group==='specialist'||(group==='administrator'&&p.id==='permissions.manage');}
  function mark(){changed=JSON.stringify(grants)!==JSON.stringify(model.groups.find(g=>g.slug===group).grants);}
  function bulk(mode){for(const p of filtered())if(!locked(p)){grants[p.id]=mode==='read'?(p.kind==='read'?'all':'none'):p.administrative&&group==='specialist'?'none':mode;}mark();groupRows();}
  function groupRows(){
    const host=$('permission-table');host.replaceChildren();const wrap=node('div',undefined,'table-wrap'),table=node('table'),thead=node('thead'),tr=node('tr');
    for(const title of ['Модуль и действие','Тип','Область','Зависимые разрешения'])tr.append(node('th',title));thead.append(tr);table.append(thead);const tbody=node('tbody');
    for(const p of filtered()){
      const row=node('tr'),name=node('td');name.append(node('strong',p.module),node('div',p.label),node('small',p.description));row.append(name,node('td',kindNames[p.kind]));
      const td=node('td'),s=node('select');s.setAttribute('aria-label',p.id);for(const [value,label] of Object.entries(scopeNames))if(!p.administrative||['none','all'].includes(value))s.append(new Option(label,value));s.value=grants[p.id];s.disabled=locked(p);s.addEventListener('change',()=>{grants[p.id]=s.value;mark();deviation();});td.append(s);row.append(td,node('td',p.dependencies.join(', ')||'—','permission-dependencies'));tbody.append(row);
    }
    table.append(tbody);wrap.append(table);host.append(wrap);if(!tbody.children.length)host.append(node('p','Ничего не найдено.','empty'));deviation();
  }
  function deviation(){const banner=$('group-deviation');if(!banner)return;const p=model.catalog;const diffs=p.filter(x=>grants[x.id]!== (x.defaults[group]==='-'?'none':x.defaults[group]));banner.hidden=!diffs.length;banner.textContent=(group==='tsd'?'Отклонение от базового профиля ТСД. ':'Отклонение от базового профиля. ')+diffs.length+' действий. Изменения требуют причины и предпросмотра.';}
  function renderRules(){
    scope=model.scope;const host=tools('Бизнес-правила','Внутренняя политика для будущих команд. Уже подготовленные документы сохраняют свою версию. Бизнес-модули ещё не подключены.','rules',scope);
    const controls=node('div',undefined,'filters configuration-filters');controls.append(select('Область настроек',[['system','Системные значения'],...state.session.organizations.map(o=>[o.id,o.name])],scope,value=>{if(canLeave()){scope=value;show('/rules');}else controls.querySelector('select').value=scope;}));
    const search=node('label','Поиск правил'),i=node('input');i.setAttribute('aria-label','Поиск правил');i.addEventListener('input',()=>ruleRows(i.value));search.append(i);controls.append(search);host.append(controls);
    const list=node('div');list.id='rule-list';host.append(list);ruleRows('');
    const footer=node('div',undefined,'policy-footer');footer.append(node('p',`Ревизия ${model.revision} · системная ${model.system_revision}. Пустое переопределение восстанавливает наследование.`,'field-help'),reasonForm());host.append(footer);
  }
  function ruleRows(q){
    const host=$('rule-list');host.replaceChildren();const groups=new Map();
    for(const r of model.rules.filter(r=>(r.id+' '+r.label).toLocaleLowerCase('ru').includes(q.toLocaleLowerCase('ru')))){
      if(!groups.has(r.category)){const section=node('section',undefined,'panel section-gap');section.append(node('h2',categories[r.category]||r.category));host.append(section);groups.set(r.category,section);}
      const row=node('div',undefined,'rule-row');row.append(node('h3',r.label),node('code',r.id),node('p',r.description,'muted'));const l=node('label','Значение');let input;
      const value=Object.hasOwn(changes,r.id)?changes[r.id]===null?r.inherited_value:changes[r.id]:r.value;
      if(r.type==='bool'){input=node('select');input.append(new Option('Да','true'),new Option('Нет','false'));input.value=String(value);}
      else if(r.choices&&r.type!=='set'){input=node('select');for(const v of r.choices)input.append(new Option(choiceNames[v]||v,v));input.value=value;}
      else{input=node('input');input.type=['integer','decimal'].includes(r.type)?'number':'text';input.value=Array.isArray(value)?value.join(', '):String(value);if(r.min!==undefined){input.min=r.min;input.max=r.max;input.step=r.type==='decimal'?'0.01':'1';}}
      input.disabled=!r.mutable;input.setAttribute('aria-label',r.id);input.addEventListener('change',()=>{
        let v=input.value;if(r.type==='bool')v=v==='true';if(['integer','decimal'].includes(r.type))v=v===''?null:Number(v);if(r.type==='set')v=v.split(',').map(x=>x.trim());
        if(v===null&&['integer','decimal'].includes(r.type)){input.setCustomValidity('Введите число.');input.reportValidity();return;}input.setCustomValidity('');changes[r.id]=v;changed=true;
      });l.append(input);row.append(l);
      row.append(node('p','По умолчанию: '+JSON.stringify(r.default)+' · '+(r.inherited?'Унаследовано':'Переопределено')+(r.min!==undefined?` · ${r.min}…${r.max}`:''),'field-help'),node('p',r.risk,'field-help'));
      if(r.mutable)row.append(button(scope==='system'?'Сбросить к исходному':'Наследовать системное',()=>{changes[r.id]=null;changed=true;ruleRows(q);},'quiet'));
      if(!r.mutable)row.append(node('span','Изменение закрыто','badge'));
      groups.get(r.category).append(row);
    }
  }
  async function preview(reason){
    if(state.route==='/rules'&&[...$('rule-list').querySelectorAll('input,select')].some(i=>!i.checkValidity())){notice('Недопустимое значение. Проверьте тип и допустимый диапазон.','error');return;}
    const isGroup=state.route==='/groups',path=isGroup?'/api/groups/'+group:'/api/business-rules/'+scope;
    const body=isGroup?{revision:model.groups.find(g=>g.slug===group).revision,grants:structuredClone(grants),reason,tsd_confirmation:$('tsd-confirmation')?.value||''}:{revision:model.revision,system_revision:model.system_revision,changes:structuredClone(changes),reason};
    const seq=sequence;const submit=$('configuration-form').querySelector('button');submit.disabled=true;
    try{const result=await api(path+'/preview',{method:'POST',body});if(seq!==sequence)return;$('notice').hidden=true;const host=$('configuration-content');host.replaceChildren(node('p',result.notice,'notice warning'));$('configuration-title').textContent='Предпросмотр изменений';
      const list=node('ul',undefined,'diff-list');for(const d of result.diff)list.append(node('li',d.id+': '+JSON.stringify(d.before)+' → '+(d.inherit?'Наследование ('+JSON.stringify(d.after)+')':JSON.stringify(d.after))));host.append(list,node('p','Причина: '+reason,'muted'));
      if(result.tsd_expansion)host.append(node('p','Профиль ТСД будет расширен.','notice warning'));
      const save=button('Применить изменения',async()=>{save.disabled=true;$('configuration-dialog').dataset.saving='true';try{await api(path,{method:'PUT',body});if(seq!==sequence)return;changed=false;closeDialog('configuration-dialog',true);notice('Настройки сохранены.');await show(state.route);}catch(e){if(seq===sequence)safeError(e);}finally{save.disabled=false;delete $('configuration-dialog').dataset.saving;}},'primary section-gap');host.append(save);openDialog('configuration-dialog');
    }catch(e){if(seq===sequence)safeError(e);}finally{submit.disabled=false;}
  }
  async function history(kind,entity,before=null){
    const seq=sequence;try{const result=await api('/api/configuration/history?'+new URLSearchParams({kind,entity,...(before?{before_id:before}:{})}));if(seq!==sequence)return;const host=$('configuration-content');if(!before){host.replaceChildren();$('configuration-title').textContent='История изменений';}
      if(!result.history.length)host.append(node('p','Изменений больше нет.','muted'));
      for(const item of result.history){const card=node('section',undefined,'history-entry');card.append(node('h3','Версия '+item.revision+' · '+item.actor_name),node('p',new Date(item.created_at).toLocaleString('ru-RU'),'field-help'),node('p',item.reason));
        const beforeValues=item.context?.resolved_before||item.before_value,afterValues=item.context?.resolved_after||item.after_value;const list=node('ul',undefined,'diff-list');for(const id of new Set([...Object.keys(item.before_value),...Object.keys(item.after_value)]))if(JSON.stringify(item.before_value[id])!==JSON.stringify(item.after_value[id]))list.append(node('li',id+': '+JSON.stringify(beforeValues[id]??null)+' → '+JSON.stringify(afterValues[id]??null)+(item.kind==='rules'&&!Object.hasOwn(item.after_value,id)?' (наследование)':'')));card.append(list);host.append(card);}
      host.querySelector('[data-more]')?.remove();if(result.history.length===25){const more=button('Ещё 25',()=>history(kind,entity,result.history.at(-1).id));more.dataset.more='';host.append(more);}openDialog('configuration-dialog');
    }catch(e){safeError(e);}
  }
  async function accessCheck(){
    const seq=sequence;try{const users=(await api('/api/users')).users;if(seq!==sequence)return;const host=$('configuration-content');host.replaceChildren();$('configuration-title').textContent='Проверка доступа пользователя';
      const f=node('form');let user=users[0]?.id,permission=model.catalog[0].id,org=state.session.organization_ids[0];if(!user)return;
      f.append(select('Пользователь',users.map(u=>[u.id,u.display_name+' · '+u.username]),user,v=>user=v),select('Действие',model.catalog.map(p=>[p.id,p.id]),permission,v=>permission=v),select('Организация',state.session.organizations.map(o=>[o.id,o.name]),org,v=>org=v));
      const owner=node('label','Собственный документ'),o=node('input');o.type='checkbox';owner.append(o);const assigned=node('label','Задание назначено'),a=node('input');a.type='checkbox';assigned.append(a);const result=node('div');const submit=node('button','Проверить','primary');submit.type='submit';f.append(owner,assigned,submit,result);
      f.addEventListener('submit',async event=>{event.preventDefault();submit.disabled=true;try{const r=await api('/api/groups/check-access',{method:'POST',body:{user_id:user,permission,organization_id:org,owner_id:o.checked?user:null,assigned:a.checked}});result.replaceChildren(node('p',(r.allowed?'Разрешено: ':'Запрещено: ')+r.message,'notice '+(r.allowed?'success':'error')),node('p',r.notice,'field-help'));}catch(e){safeError(e);}finally{submit.disabled=false;}});host.append(f);openDialog('configuration-dialog');
    }catch(e){safeError(e);}
  }
  return {show,reset,sessionReset:()=>{reset();group='bm';scope='';query='';moduleFilter='';},canLeave,dirty:()=>changed};
}
