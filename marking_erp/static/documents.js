const states={draft:'Черновик',prepared:'Состав проверен локально',archived:'В архиве'};
const historyNames={created:'Создана',edited:'Изменён состав',prepared:'Состав проверен локально',archived:'Остановлена и архивирована',restored:'Возвращена в черновики'};
const fields=[['pn','PN'],['gtin','GTIN'],['quantity','Штук в этикетке'],['labels','Этикеток'],['tnved','ТН ВЭД']];
export function createDocuments({state,$,api,node,button,heading,notice,report,openDialog,closeDialog}){
  let generation=0,editSeq=0,org='',query='',archived=false,cursors=[null],page=0,editor=null;
  const can=p=>Boolean(state.session?.permissions[p]);
  function reset(){generation++;editSeq++;editor=null;$('document-content').replaceChildren();}
  $('document-dialog').addEventListener('close',()=>{editSeq++;editor=null;$('document-content').replaceChildren();});
  function label(text,control){const wrap=node('label',text);control.setAttribute('aria-label',text);wrap.append(control);return wrap;}
  function input(text,value='',type='text'){const i=node('input');i.type=type;i.value=value;return [label(text,i),i];}
  function fail(error){if(error.code!=='stale'){const n=node('p',error.message,'error-message');$('document-content').prepend(n);report(error);}}
  async function show(){
    const seq=++generation,host=$('page-content');
    if(!can('marking.documents.view')){host.replaceChildren(heading('Раздел недоступен','Нет разрешения на просмотр накладных.'));return;}
    if(!state.session.organization_ids.includes(org)){org=state.session.organization_ids[0]||'';cursors=[null];page=0;}
    const actions=[button('Обновить накладные',()=>{cursors=[null];page=0;show();})];
    if(can('marking.documents.create'))actions.push(button('+ Новая накладная',()=>renderEditor(null),'primary'));
    host.replaceChildren(heading('Накладные БМ','Подготовка состава, проверка и архив. Эмиссия, отчёты и АТК ещё не подключены.',actions));
    const form=node('form',undefined,'filters document-filters'),organizations=node('select');for(const o of state.session.organizations)organizations.append(new Option(o.name,o.id));organizations.value=org;organizations.addEventListener('change',()=>{org=organizations.value;cursors=[null];page=0;show();});
    const search=node('input');search.value=query;search.placeholder='Номер, поставщик или PN';
    const scope=node('select');scope.append(new Option('В работе','active'));if(can('documents.archive.view'))scope.append(new Option('Архив','archived'));if(!can('documents.archive.view'))archived=false;scope.value=archived?'archived':'active';scope.addEventListener('change',()=>{archived=scope.value==='archived';cursors=[null];page=0;show();});
    form.append(label('Организация',organizations),label('Поиск накладных',search),label('Раздел',scope));const apply=node('button','Найти');apply.type='submit';form.append(apply);form.addEventListener('submit',e=>{e.preventDefault();query=search.value;cursors=[null];page=0;show();});host.append(form);
    const panel=node('section',undefined,'panel table-panel');panel.id='document-list';panel.append(node('p','Загружаем накладные…','empty'));host.append(panel);
    try{
      const params=new URLSearchParams({organization_id:org,archived,q:query});if(cursors[page])params.set('before_id',cursors[page]);const data=await api('/api/documents?'+params);
      if(seq!==generation||state.route!=='/documents')return;panel.replaceChildren();
      if(!data.documents.length)panel.append(node('p',archived?'Архив пуст.':'Нет накладных по выбранным условиям. Создайте накладную или измените поиск.','empty'));
      else{panel.append(node('p','Таблицу накладных можно прокрутить вправо.','document-scroll-hint'));const wrap=node('div',undefined,'table-wrap'),table=node('table');wrap.tabIndex=0;wrap.setAttribute('aria-label','Реестр накладных, прокручиваемая таблица');table.append(node('caption','Накладные','sr-only'));const head=node('thead'),tr=node('tr');for(const text of ['Номер / дата','Поставщик','Состав','Состояние','Действия'])tr.append(node('th',text));head.append(tr);table.append(head);const body=node('tbody');
        for(const d of data.documents){const row=node('tr');row.dataset.documentId=d.id;const number=node('td');number.append(node('strong',d.number),node('small',d.document_date));const actions=node('td');actions.append(button('Открыть',()=>open(d.id),'quiet'));row.append(number,node('td',d.supplier),node('td',`${d.line_count} строк · ${d.labels} этикеток`),node('td',states[d.state]),actions);body.append(row);}table.append(body);wrap.append(table);panel.append(wrap);}
      const footer=node('div',undefined,'table-footer'),buttons=node('div',undefined,'actions');const previous=button('Назад',()=>{page--;show();}),next=button('Далее',()=>{cursors[++page]=data.next_cursor;show();});previous.disabled=page===0;next.disabled=!data.next_cursor;buttons.append(previous,next);footer.append(node('span','Страница '+(page+1)),buttons);panel.append(footer);
    }catch(error){if(seq!==generation)return;panel.replaceChildren(node('p','Не удалось получить накладные. Обновите список.','empty'));report(error);}
  }
  async function open(id,editing=false){
    const seq=++editSeq;openDialog('document-dialog');$('document-title').textContent='Накладная';$('document-content').replaceChildren(node('p','Загружаем накладную…'));
    try{const data=await api('/api/documents/'+id);if(seq!==editSeq||state.route!=='/documents')return;editing?renderEditor(data.document):renderDetail(data.document,data.notice);}
    catch(error){if(seq===editSeq)fail(error);}
  }
  function renderDetail(doc,message){
    const content=$('document-content');content.replaceChildren();$('document-title').textContent='Накладная '+doc.number;openDialog('document-dialog');
    content.append(node('p',`${doc.document_date} · ${doc.supplier} · ${states[doc.state]} · Версия ${doc.revision}`),node('p',message,'notice warning'));
    const actions=node('div',undefined,'actions');
    if(doc.state!=='archived'&&can('marking.documents.edit'))actions.append(button('Редактировать состав',()=>renderEditor(doc)));
    if(doc.state==='draft'&&can('marking.documents.edit'))actions.append(button('Проверить состав',()=>change(doc,'prepare',{}),'primary'));
    if(can('audit.operational.view'))actions.append(button('История накладной',async()=>{const seq=editSeq;try{const data=await api('/api/documents/'+doc.id+'/history');if(seq!==editSeq)return;const history=node('section',undefined,'section-gap');history.id='document-history';history.append(node('h3','История версий'));for(const h of data.history)history.append(node('p',`Версия ${h.revision} · ${historyNames[h.action]||'Событие'} · ${h.actor_name} · ${new Date(h.created_at).toLocaleString('ru-RU')} ${h.reason}`));content.querySelector('#document-history')?.remove();content.append(history);}catch(error){if(seq===editSeq)fail(error);}}));content.append(actions);
    renderRows(content,doc.lines,false);
    const operation=doc.state==='archived'?'restore':'archive',permission=doc.state==='archived'?'documents.restore':'documents.archive';
    if(can(permission)&&(operation==='restore'||can('documents.stop'))){const form=node('form',undefined,'document-transition section-gap');const [nl,number]=input('Подтвердите номер накладной'),[rl,reason]=input('Причина');number.required=true;reason.required=true;reason.minLength=8;reason.maxLength=1000;const submit=node('button',operation==='restore'?'Вернуть в черновики':'Документ не нужен — в архив');submit.type='submit';submit.className=operation==='archive'?'danger':'';form.append(nl,rl,submit);form.addEventListener('submit',e=>{e.preventDefault();change(doc,operation,{confirmation:number.value,reason:reason.value});});content.append(form);}
  }
  async function change(doc,action,extra){
    const seq=editSeq,dialog=$('document-dialog'),buttons=[...$('document-content').querySelectorAll('button')].map(b=>[b,b.disabled]);dialog.dataset.saving='true';buttons.forEach(([b])=>b.disabled=true);
    try{await api('/api/documents/'+doc.id+'/'+action,{method:'POST',body:{revision:doc.revision,...extra}});if(seq!==editSeq)return;notice(action==='prepare'?'Состав проверен локально. Внешние операции не выполнялись.':action==='archive'?'Накладная в архиве.':'Накладная возвращена в черновики.');closeDialog('document-dialog',true);await show();}
    catch(error){if(seq===editSeq)fail(error);}finally{delete dialog.dataset.saving;if(seq===editSeq)buttons.forEach(([b,disabled])=>b.disabled=disabled);}
  }
  function renderRows(host,rows,editable,onDirty=()=>{}){
    const section=node('section',undefined,'section-gap');host.append(section);let page=0;
    function draw(){section.replaceChildren(node('p','Таблицу строк можно прокрутить вправо.','document-scroll-hint'));const wrap=node('div',undefined,'table-wrap'),table=node('table',undefined,'document-lines');wrap.tabIndex=0;wrap.setAttribute('aria-label','Строки накладной, прокручиваемая таблица');table.append(node('caption','Строки накладной','sr-only'));const head=node('thead'),tr=node('tr');for(const [,name] of fields)tr.append(node('th',name));if(editable)tr.append(node('th','Действия'));head.append(tr);table.append(head);const body=node('tbody');
      rows.slice(page*25,(page+1)*25).forEach((line,index)=>{const row=node('tr');for(const [key,name] of fields){const td=node('td');if(editable){const i=node('input');i.value=line[key];i.setAttribute('aria-label',`${name}, строка ${page*25+index+1}`);i.maxLength=key==='pn'?120:key==='gtin'?14:key==='tnved'?10:20;i.required=['pn','quantity','labels'].includes(key);if(['quantity','labels'].includes(key))i.inputMode='numeric';i.addEventListener('input',()=>{line[key]=i.value;onDirty();});td.append(i);}else td.textContent=line[key]||'Не заполнено';row.append(td);}if(editable){const td=node('td');td.append(button('Убрать',()=>{rows.splice(page*25+index,1);page=Math.min(page,Math.max(0,Math.ceil(rows.length/25)-1));onDirty();draw();},'quiet'));row.append(td);}body.append(row);});table.append(body);wrap.append(table);section.append(wrap);
      const footer=node('div',undefined,'table-footer'),actions=node('div',undefined,'actions');const previous=button('Предыдущие строки',()=>{page--;draw();}),next=button('Следующие строки',()=>{page++;draw();});previous.disabled=page===0;next.disabled=(page+1)*25>=rows.length;actions.append(previous,next);if(editable)actions.append(button('+ Строка',()=>{if(rows.length>=20000)return;rows.push({pn:'',gtin:'',quantity:1,labels:1,tnved:''});page=Math.floor((rows.length-1)/25);onDirty();draw();}));footer.append(node('span',`${rows.length} строк · Страница ${page+1}`),actions);section.append(footer);
    }draw();return {redraw:()=>{page=0;draw();}};
  }
  function renderEditor(doc){
    const seq=++editSeq;editor={doc,request_id:crypto.randomUUID(),rows:doc?structuredClone(doc.lines):[{pn:'',gtin:'',quantity:1,labels:1,tnved:''}],imported:false};
    const content=$('document-content');content.replaceChildren();$('document-title').textContent=doc?'Изменить накладную '+doc.number:'Новая накладная';openDialog('document-dialog');
    const form=node('form');form.id='document-form';form.dataset.dirty='false';form.addEventListener('input',()=>form.dataset.dirty='true');content.append(form);
    const controls=node('div',undefined,'document-header-fields');const [nl,number]=input('Номер накладной',doc?.number||''),[dl,date]=input('Дата накладной',doc?.document_date||new Date().toLocaleDateString('sv-SE'),'date'),[sl,supplier]=input('Поставщик',doc?.supplier||'');number.required=date.required=supplier.required=true;number.maxLength=120;supplier.maxLength=200;controls.append(nl,dl,sl);form.append(controls);
    form.append(node('p','GTIN и ТН ВЭД можно заполнить позже. Перед проверкой состава они обязательны. Изменение проверенного документа вернёт его в черновики.','muted'));
    const dirty=()=>form.dataset.dirty='true';let rowsUI=renderRows(form,editor.rows,true,dirty);
    const importPanel=node('section',undefined,'document-import section-gap');const file=node('input');file.type='file';file.accept='.xlsx';const preview=button('Предпросмотр Excel',()=>previewFile()),mappingHost=node('div'),resultHost=node('div');
    if(can('marking.import.preview')){const template=node('a','Скачать шаблон Excel');template.href='/api/documents/template?organization_id='+encodeURIComponent(doc?.organization_id||org);template.setAttribute('download','invoice-template.xlsx');importPanel.append(template);importPanel.append(node('h3','Заменить состав из Excel'),label('Файл Excel',file),preview,mappingHost,resultHost);form.append(importPanel);}
    let importSeq=0;file.addEventListener('change',()=>{importSeq++;preview.disabled=false;mappingHost.replaceChildren();resultHost.replaceChildren();});
    async function previewFile(params={}){
      const importRequest=++importSeq;
      if(!file.files?.[0]){resultHost.replaceChildren(node('p','Выберите XLSX-файл.','error-message'));return;}
      if(file.files[0].size>10*1024*1024){resultHost.replaceChildren(node('p','Размер Excel превышает 10 МБ.','error-message'));return;}
      preview.disabled=true;resultHost.replaceChildren(node('p','Разбираем файл…'));const options=new URLSearchParams({organization_id:doc?.organization_id||org,...params});
      try{const data=await api('/api/documents/import-preview?'+options,{method:'POST',raw:file.files[0]});if(seq!==editSeq||importRequest!==importSeq)return;mappingHost.replaceChildren();resultHost.replaceChildren();
        if(data.requires_mapping){const sheets=node('select');for(const s of data.sheets)sheets.append(new Option(s.name,s.name));sheets.value=data.selected_sheet;const header=node('input');header.type='number';header.min=0;const columns=node('div',undefined,'document-mapping');let choices={};
          function sheetChanged(){const s=data.sheets.find(s=>s.name===sheets.value);header.value=s.suggested_header_row;columns.replaceChildren();choices={};for(const [key,title] of fields){const select=node('select');select.append(new Option('Выберите столбец',''));for(const col of s.columns)select.append(new Option(col.letter,col.index));select.value=s.suggested_mapping[key]??'';choices[key]=select;columns.append(label(title,select));}const sample=node('pre');sample.className='document-sample';sample.textContent=s.preview.slice(0,5).map(r=>`${r.row}: ${r.values.join(' | ')}`).join('\n');columns.append(sample);}
          sheets.addEventListener('change',sheetChanged);sheetChanged();mappingHost.append(label('Лист Excel',sheets),label('Строка заголовков (0 — без заголовков)',header),columns,button('Проверить сопоставление',()=>{const mapping={};for(const [key,select] of Object.entries(choices)){if(select.value===''){notice('Сопоставьте все пять полей.','warning');return;}mapping[key]=Number(select.value);}previewFile({sheet_name:sheets.value,header_row:header.value,mapping:JSON.stringify(mapping)});}));return;
        }
        for(const error of data.errors.slice(0,50))resultHost.append(node('p',`Строка ${error.row}: ${error.message}`,'error-message'));for(const warning of data.warnings.slice(0,30))resultHost.append(node('p',warning,'notice warning'));
        resultHost.append(node('p',`${data.lines.length} строк после разбора. Ошибок: ${data.errors.length}.`));renderRows(resultHost,data.lines,false);
        if(!data.errors.length&&can('marking.import.commit'))resultHost.append(button('Применить строки к черновику',()=>{if(seq!==editSeq)return;editor.rows.splice(0,editor.rows.length,...data.lines);editor.imported=true;rowsUI.redraw();dirty();notice('Строки перенесены в редактор. Сохраните накладную.');}));
      }catch(error){if(seq===editSeq&&importRequest===importSeq){resultHost.replaceChildren(node('p','Не удалось разобрать файл.','error-message'));report(error);}}finally{if(seq===editSeq&&importRequest===importSeq)preview.disabled=false;}
    }
    const actions=node('div',undefined,'dialog-actions');const save=node('button','Сохранить черновик');save.type='submit';save.className='primary';actions.append(button('Отмена',()=>closeDialog('document-dialog')),save);form.append(actions);
    form.addEventListener('submit',async e=>{e.preventDefault();if(form.dataset.busy==='true')return;form.dataset.busy='true';const blocked=[...form.querySelectorAll('button')].map(b=>[b,b.disabled]);blocked.forEach(([b])=>b.disabled=true);
      const body={organization_id:doc?.organization_id||org,number:number.value,document_date:date.value,supplier:supplier.value,lines:editor.rows,imported:editor.imported,...(doc?{revision:doc.revision}:{request_id:editor.request_id})};
      try{const data=await api(doc?'/api/documents/'+doc.id:'/api/documents',{method:doc?'PUT':'POST',body});if(seq!==editSeq)return;closeDialog('document-dialog',true);notice('Накладная сохранена.'+(data.warnings?.length?' '+data.warnings.slice(0,5).join('; ')+(data.warnings.length>5?' · Всего предупреждений: '+data.warnings.length:''):''));await show();}
      catch(error){if(seq===editSeq)fail(error);}finally{if(seq===editSeq){form.dataset.busy='false';blocked.forEach(([b,disabled])=>b.disabled=disabled);}}
    });
  }
  return {show,reset,dirty:()=>$('document-form')?.dataset.dirty==='true'};
}
