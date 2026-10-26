export function createRegistry({state,$,api,node,button,heading,notice,report,openDialog}){
  let sequence=0,organization='',query='',cursor=null,previous=[];
  const reset=()=>{sequence++;organization='';query='';cursor=null;previous=[];};
  function showError(e){if(e.code==='stale')return;if($('code-dialog').open)$('code-content').prepend(node('p',e.message,'error-message'));report(e);}
  async function download(path,name){
    const generation=state.generation,controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),20000);
    try{const response=await fetch(path,{credentials:'same-origin',signal:controller.signal});
      if(response.status===401){await api('/api/session');return;}
      if(!response.ok){const error=await response.json().catch(()=>({}));throw new Error(typeof error.detail==='string'?error.detail:'Файл недоступен.');}
      const blob=await response.blob();if(generation!==state.generation)return;
      const url=URL.createObjectURL(blob),a=node('a');a.href=url;a.download=name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
    }catch(e){if(generation===state.generation)showError(new Error(e.name==='AbortError'?'Не удалось скачать файл за отведённое время.':e.message));}finally{clearTimeout(timeout);}
  }
  async function show(){
    const host=$('page-content');if(!state.session.permissions['codes.registry.view']){host.replaceChildren(heading('Раздел недоступен','У вашей группы нет доступа к реестру КМ.'));return;}
    host.replaceChildren(heading('Реестр КМ','Сохранённые коды и история исходных данных. Проверка подлинности и статуса ЧЗ этим разделом не выполняется.'));
    if(!state.session.organizations.length){host.append(node('p','Организации не назначены.','empty'));return;}
    if(!state.session.organization_ids.includes(organization))organization=state.session.organization_ids[0];
    const filters=node('form',undefined,'filters registry-filters'),org=node('label','Организация'),s=node('select');s.setAttribute('aria-label','Организация реестра');for(const o of state.session.organizations)s.append(new Option(o.name,o.id));s.value=organization;s.addEventListener('change',()=>{organization=s.value;cursor=null;previous=[];load();});org.append(s);
    const label=node('label','Поиск'),input=node('input');input.value=query;input.placeholder='GTIN, КИ или PN';input.setAttribute('aria-label','Поиск КМ');label.append(input);const submit=node('button','Найти');submit.type='submit';filters.append(org,label,submit);filters.addEventListener('submit',event=>{event.preventDefault();query=input.value;cursor=null;previous=[];load();});host.append(filters);
    host.append(node('p','Количество AI30 относится к сохранённой этикетке. Оно не подтверждает остаток товара после разделения.','notice warning'));
    const table=node('div',undefined,'panel table-panel');table.id='registry-table';host.append(table);await load();
  }
  async function load(){
    const seq=++sequence,host=$('registry-table');if(!host)return;host.replaceChildren(node('p','Загружаем коды…','empty'));
    try{const data=await api('/api/codes?'+new URLSearchParams({organization_id:organization,q:query,...(cursor?{before_id:cursor}:{})}));if(seq!==sequence||state.route!=='/codes')return;
      host.replaceChildren();if(!data.codes.length)host.append(node('p',query?'Коды по вашему запросу не найдены.':'Сохранённых КМ пока нет. Они появятся после подключения источников и рабочих процессов.','empty'));
      else{const wrap=node('div',undefined,'table-wrap'),table=node('table'),thead=node('thead'),head=node('tr');for(const text of ['PN / GTIN','Код идентификации','AI30 этикетки','Состояние данных','Действия'])head.append(node('th',text));thead.append(head);table.append(thead);const body=node('tbody');
        for(const code of data.codes){const row=node('tr'),product=node('td');product.append(node('strong',code.pn||'PN не подтверждён'),node('small',code.gtin));row.append(product,node('td',code.cis,'cis-cell'),node('td',code.label_quantity===null?'Не указан':String(code.label_quantity)));const status=node('td');status.append(node('span',code.conflicts?'Конфликт payload':'Сохранён','badge '+(code.conflicts?'conflict':'')),node('small','Статус ЧЗ не проверен'));row.append(status);const actions=node('td');if(state.session.permissions['codes.history.view'])actions.append(button('История',()=>history(code)));row.append(actions);body.append(row);}
        table.append(body);wrap.append(table);host.append(wrap);}
      const footer=node('div',undefined,'table-footer'),back=button('Назад',()=>{cursor=previous.pop()??null;load();}),next=button('Далее',()=>{previous.push(cursor);cursor=data.next_cursor;load();});back.disabled=!previous.length;next.disabled=!data.next_cursor;footer.append(node('span','Показано: '+data.codes.length),back,next);host.append(footer);
    }catch(e){if(seq!==sequence)return;host.replaceChildren(node('p','Не удалось загрузить реестр.','empty'),button('Повторить',load));report(e);}
  }
  async function history(code,before=null){
    const seq=sequence,org=organization;
    try{const data=await api('/api/codes/'+code.id+'/history?'+new URLSearchParams({organization_id:org,...(before?{before_id:before}:{})}));if(seq!==sequence||state.route!=='/codes')return;const host=$('code-content');
      if(!before){host.replaceChildren(node('p',code.cis,'cis-cell'),node('p',code.conflicts?'Обнаружен другой полный payload для того же КИ. Автоматическая замена запрещена.':'Сохранение кода не является подтверждением его статуса или подлинности.','notice '+(code.conflicts?'error':'warning')));$('code-title').textContent='История КМ';}
      const labels={captured:'Первое сохранение',reobserved:'Повторное наблюдение',payload_conflict:'Конфликт полного КМ',quantity_adjusted:'Перераспределение количества'};
      for(const event of data.history){const card=node('section',undefined,'history-entry');card.append(node('h3',labels[event.kind]),node('p',new Date(event.created_at).toLocaleString('ru-RU')+' · '+event.actor_name,'field-help'),node('p','Версия '+event.version+' · Шаблон '+event.template+' · AI30: '+(event.label_quantity??'не указан')),node('p','Источник: '+event.source_ref,'field-help'),node('p','Профиль: '+event.profile,'field-help'),node('p','Исходный SHA256: '+event.raw_sha256,'hash-text'),node('p','Нормализованный SHA256: '+event.normalized_sha256,'hash-text'));
        if(event.corrections.length)card.append(node('p','Преобразования: '+event.corrections.map(x=>({'aim-prefix-removed':'снят префикс ]d2','crlf-terminator-removed':'снят завершающий CRLF','terminator-removed':'снят завершающий CR/LF'}[x]||x)).join(', '),'field-help'));
        if(state.session.permissions['codes.payload.export'])card.append(button('Скачать исходные байты',()=>download('/api/codes/'+code.id+'/payload/'+event.payload_id+'?organization_id='+org,'code-'+event.payload_id+'.bin')));
        for(const f of data.files.filter(f=>f.payload_id===event.payload_id&&f.kind==='label_pdf'))if(state.session.permissions['marking.labels.download']){card.append(button('Скачать сохранённый PDF',()=>download('/api/files/'+f.id,'label-'+f.id+'.pdf')),node('p','Скачивание не подтверждает печать или нанесение. SHA256: '+f.sha256,'hash-text'));}
        host.append(card);
      }
      host.querySelector('[data-code-more]')?.remove();if(data.next_cursor){const more=button('Ещё 25',()=>history(code,data.next_cursor));more.dataset.codeMore='';host.append(more);}openDialog('code-dialog');
    }catch(e){if(seq===sequence)showError(e);}
  }
  return {show,reset};
}
