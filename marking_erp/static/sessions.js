export function createSessions({state,$,api,node,button,notice,report,ask}){
  let generation=0;
  function reset(){generation++;}
  function show(host){
    reset();
    const panel=node('section',undefined,'panel section-gap');panel.id='own-sessions';
    const title=node('div',undefined,'page-heading');title.append(node('h2','Действующие входы'));
    const refresh=button('Обновить входы',load),revoke=button('Завершить другие входы',async()=>{
      const seq=generation;
      if(!await ask('Завершить другие входы?','Остальные входы в вашу учётную запись будут завершены. Текущий вход останется активным.','Завершить другие'))return;
      if(seq!==generation||state.route!=='/profile')return;
      refresh.disabled=revoke.disabled=true;
      try{
        await api('/api/session/logout-others',{method:'POST'});
        if(seq!==generation||state.route!=='/profile')return;
        notice('Другие входы завершены. Текущий вход сохранён.');await load();
      }catch(error){
        if(seq!==generation||state.route!=='/profile')return;
        content.replaceChildren(node('p','Не удалось подтвердить результат. Обновите список перед повтором.','empty'));refresh.disabled=false;report(error);
      }
    });
    const actions=node('div',undefined,'actions');actions.append(refresh,revoke);title.append(actions);panel.append(title);
    panel.append(node('p','Один браузер или устройство может иметь несколько входов. Дата окончания показывает срок действия входа.','muted'));
    const content=node('div');content.id='own-session-list';content.setAttribute('aria-live','polite');panel.append(content);host.append(panel);load();
    async function load(){
      const seq=++generation;refresh.disabled=revoke.disabled=true;
      content.replaceChildren(node('p','Загружаем входы…','empty'));content.setAttribute('aria-busy','true');
      try{
        const data=await api('/api/session/sessions');
        if(seq!==generation||state.route!=='/profile')return;
        content.replaceChildren(node('p',`Действующих входов: ${data.total}. Показано: ${data.sessions.length}.`,'muted'));
        const list=node('ul',undefined,'own-session-list');
        for(const session of data.sessions){
          const row=node('li',undefined,'own-session-row');
          row.append(node('strong',session.is_current?'Текущий вход':'Другой вход'),
            node('span','Начало: '+new Date(session.created_at).toLocaleString('ru-RU')),
            node('span','Истекает: '+new Date(session.expires_at).toLocaleString('ru-RU')));list.append(row);
        }
        content.append(list);revoke.disabled=data.total<=1;
      }catch(error){
        if(seq!==generation||state.route!=='/profile')return;
        content.replaceChildren(node('p','Не удалось получить список. Нажмите «Обновить входы».','empty'));report(error);
      }finally{if(seq===generation){refresh.disabled=false;content.removeAttribute('aria-busy');}}
    }
  }
  return {show,reset};
}
