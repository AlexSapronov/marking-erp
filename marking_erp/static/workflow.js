// Shared local navigation. Every destination authorizes its own fresh read.
export function createWorkflow({state,api,node,button,openTarget}){
 return function appendWorkflow(host,kind,id){
  const section=node('section',undefined,'panel workflow-panel section-gap');
  section.setAttribute('aria-label','Цепочка работы');host.append(section);
  const session=state.session;let attempt=0;
  async function load(){
   const request=++attempt;section.replaceChildren(node('p','Загружаем цепочку работы…','muted'));
   try{
    const data=await api('/api/workflow/'+kind+'/'+encodeURIComponent(id));
    if(request!==attempt||!section.isConnected||state.session?.csrf!==session?.csrf)return;
    section.replaceChildren(node('h3','Цепочка работы'),node('p','Текущий этап: '+data.stage),
     node('p','Следующее действие: '+data.next_action,'workflow-next'),
     node('p','Следующий этап ведёт: '+data.responsible_role),node('p','Владелец документа: '+data.owner_name,'muted'));
    if(data.next_target)section.append(button('Открыть следующий этап',()=>openTarget(data.next_target),'primary'));
    if(data.blockers.length){const list=node('ul',undefined,'workflow-blockers');for(const text of data.blockers)list.append(node('li',text));section.append(node('h4','Что требует внимания'),list);}
    for(const group of data.groups){const details=node('section',undefined,'workflow-group');details.append(node('h4',group.name));const list=node('ul');for(const item of group.items){const line=node('li');line.append(button(item.label+' · '+item.title,()=>openTarget(item.target),'link-button'),node('span',' — '+item.state));list.append(line);}details.append(list);section.append(details);}
    section.append(node('p','Связи НК с накладными показаны по точному PN и GTIN. Представлены только доступные документы, до 25 в каждой группе. Для полного реестра откройте соответствующее рабочее место.','muted'));
    if(data.truncated)section.append(node('p','Показана часть связанных документов. Продолжите поиск в рабочем месте.','notice warning'));
    section.append(node('p','Состояние на '+new Date(data.checked_at).toLocaleString('ru-RU')+'. Чтение цепочки не отправляет документы и не повторяет операции.','muted'),button('Обновить цепочку',load));
   }catch(e){
    if(request!==attempt||!section.isConnected||state.session?.csrf!==session?.csrf)return;
    section.replaceChildren(node('h3','Цепочка работы'),node('p',e.status===403||e.status===404?'Цепочка недоступна. Обновите права или обратитесь к ответственному.':'Не удалось загрузить цепочку. Проверьте связь и повторите чтение.','notice warning'),button('Повторить загрузку цепочки',load));
   }
  }
  load();return section;
 };
}
