export function createAudit({state,$,api,node,button,heading,report}){
  let generation=0;
  function reset(){generation++;}
  function show(){
    reset();
    const host=$('page-content');
    if(!state.session?.permissions['audit.view']){
      host.replaceChildren(heading('Раздел недоступен','Нет разрешения на просмотр аудита.'));return;
    }
    let filters={},cursors=[null],page=0;
    const refresh=button('Обновить журнал',()=>{cursors=[null];page=0;load();});
    host.replaceChildren(heading('Журнал аудита','Глобальная история зарегистрированных событий. Новые события — в начале списка.',[refresh]));
    const form=node('form',undefined,'filters audit-filters');form.id='audit-filters';
    function input(label,name,type){const wrap=node('label',label),control=node(type==='select'?'select':'input');control.name=name;control.setAttribute('aria-label',label);if(type!=='select')control.type=type;wrap.append(control);form.append(wrap);return control;}
    const action=input('Событие','action','select');action.append(new Option('Все события',''));
    const actor=input('Исполнитель (UUID)','actor','text');actor.placeholder='Все исполнители';actor.maxLength=36;actor.pattern='[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}';
    const since=input('С даты (ваш часовой пояс)','since','datetime-local'),until=input('По дату (ваш часовой пояс)','until','datetime-local');
    const submit=node('button','Применить');submit.type='submit';
    const clear=button('Сбросить',()=>{form.reset();until.setCustomValidity('');filters={};cursors=[null];page=0;load();});form.append(submit,clear);host.append(form);
    host.append(node('p','Показаны только метаданные. Причины, содержимое файлов, полные КМ и служебные данные скрыты. Пустой исполнитель означает системное событие или вход без установленного пользователя.','muted'));
    const content=node('section',undefined,'panel table-panel');content.id='audit-results';content.setAttribute('aria-live','polite');host.append(content);
    form.addEventListener('submit',event=>{
      event.preventDefault();
      until.setCustomValidity('');
      if(since.value&&until.value&&since.value>until.value){until.setCustomValidity('Окончание периода раньше начала.');until.reportValidity();return;}
      filters={};if(action.value)filters.action=action.value;if(actor.value)filters.actor_id=actor.value;
      if(since.value)filters.since=new Date(since.value).toISOString();if(until.value)filters.until=new Date(until.value).toISOString();
      cursors=[null];page=0;load();
    });
    until.addEventListener('input',()=>until.setCustomValidity(''));since.addEventListener('input',()=>until.setCustomValidity(''));
    async function load(){
      const seq=++generation;
      content.replaceChildren(node('p','Загружаем события…','empty'));content.setAttribute('aria-busy','true');
      refresh.disabled=submit.disabled=clear.disabled=true;
      const params=new URLSearchParams(filters);if(cursors[page])params.set('before_id',cursors[page]);
      try{
        const data=await api('/api/audit?'+params);
        if(seq!==generation||state.route!=='/audit')return;
        if(action.options.length===1)for(const [value,label] of Object.entries(data.actions))action.append(new Option(label,value));
        content.replaceChildren();
        if(!data.events.length)content.append(node('p','Нет событий по выбранным условиям.','empty'));
        else{
          const wrap=node('div',undefined,'table-wrap'),table=node('table',undefined,'audit-table');table.append(node('caption','События аудита','sr-only'));
          const head=node('thead'),titles=node('tr');for(const title of ['№ / Время','Событие','Исполнитель (UUID)','Объект'])titles.append(node('th',title));head.append(titles);table.append(head);
          const body=node('tbody');
          for(const event of data.events){const row=node('tr');row.dataset.eventId=event.id;const stamp=node('td');stamp.append(node('strong','#'+event.id),node('small',new Date(event.created_at).toLocaleString('ru-RU')));row.append(stamp,node('td',event.label),node('td',event.actor_id||'—','hash-text'),node('td',event.entity||'Скрыт / не указан','hash-text'));body.append(row);}
          table.append(body);wrap.append(table);content.append(wrap);
        }
        const footer=node('div',undefined,'table-footer'),actions=node('div',undefined,'actions');
        const previous=button('Назад',()=>{page--;load();}),next=button('Далее',()=>{cursors[++page]=data.next_before_id;load();});previous.disabled=page===0;next.disabled=!data.next_before_id;actions.append(previous,next);footer.append(node('span',`Страница ${page+1} · Событий: ${data.events.length}`),actions);content.append(footer);
      }catch(error){
        if(seq!==generation||state.route!=='/audit')return;
        content.replaceChildren(node('p',error.code==='reauth_required'?'Подтвердите доступ и нажмите «Обновить журнал».':'Не удалось получить журнал. Нажмите «Обновить журнал» для повтора.','empty'));report(error);
      }finally{if(seq===generation){refresh.disabled=submit.disabled=clear.disabled=false;content.removeAttribute('aria-busy');}}
    }
    load();
  }
  return {show,reset};
}
