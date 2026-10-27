const checks={
  non_root:['Отдельный пользователь сервиса','Рабочий процесс должен запускаться без прав root.'],
  https_cookies:['HTTPS и защищённые cookie','Проверяется конфигурация. Сертификат и доступ с рабочих мест проверяются отдельно.'],
  release_sha:['Версия релиза','В конфигурации должен быть указан SHA установленной сборки.'],
  private_file_storage:['Приватное хранилище файлов','Каталог должен существовать, быть доступен сервису и закрыт для других пользователей.'],
  mfa_key:['Ключ шифрования MFA','Для рабочего запуска требуется постоянный ключ сервера.'],
  schema_revision:['Версия схемы БД','Схема должна соответствовать установленному релизу.'],
  database_runtime_role:['Ограниченная роль БД','Рабочая роль не должна владеть схемой, наследовать другие роли или иметь лишние права.'],
  seed_initialized:['Начальные настройки','Системные группы и права должны быть инициализированы.'],
  administrator_mfa:['Администратор с MFA','Должна быть включённая административная учётная запись с настроенным MFA.'],
  mfa_decrypt:['Расшифровка сохранённых MFA','Текущий ключ должен подходить ко всем сохранённым секретам MFA.']
};
const operationStates={awaiting_signature:'Ожидают подписи',queued:'В очереди',dispatching:'Отправляются',submitted:'Ожидают квитанции',blocked:'Заблокированы',unknown:'Исход неизвестен'};
const printingStates={queued:'В очереди',held:'Приостановлены карантином',claimed:'Выданы агенту',unknown:'Исход неизвестен'};
const date=value=>value?new Date(value).toLocaleString('ru-RU'):'Нет данных';

function operationalView(data,{node,button,navigate,permissions}){
  const section=node('section',undefined,'panel section-gap');section.append(node('h2','Подключения и эксплуатация'),
    node('p','Здесь показаны сохранённые настройки и события сервера. Доступ к внешним API, работа фонового обработчика, сертификата и оборудования этим экраном не подтверждаются.','muted'));
  if(!data){section.append(node('p','Сводка недоступна: сначала приведите схему БД к версии установленного релиза.','empty'));return section;}
  const component=(host,label,status,description)=>{const row=node('article',undefined,'system-component');row.append(node('h3',label),node('span',status,'badge system-result unknown'),node('p',description,'muted'));host.append(row);};
  for(const org of data?.organizations||[]){
    const group=node('section',undefined,'section-gap');group.append(node('h3',org.name));
    const creds=org.credentials,unreadable=org.credential_state==='unreadable';
    const credential=key=>unreadable?'Не удалось расшифровать доступ':creds?(creds[key]?'Реквизиты сохранены':'Реквизиты не заданы'):org.credential_state==='restricted'?'Нет права на просмотр реквизитов':'Подключение не настроено';
    component(group,'Национальный каталог',credential('nk_key'),'Наличие ключа не подтверждает его действительность. Проверьте синхронизацию каталога.');
    component(group,'Честный Знак / СУЗ',!creds?credential('true_token'):(creds.true_token&&creds.oms_token?'Оба токена сохранены':'Токены заданы не полностью'),
      `Контур: ${org.environment==='production'?'промышленный':org.environment==='sandbox'?'тестовый':'не выбран'}. Внешние изменения: ${org.writes_enabled?'разрешены':'отключены'}. Проверки склада в ЧЗ: ${org.warehouse_live_checks?'включены':'отключены — физическая сверка'}.`);
    component(group,'ГОСТ-подписант',org.signers.trusted?`Доверенных: ${org.signers.trusted}; обращались за 5 мин: ${org.signers.recent}`:'Доверенный подписант не зарегистрирован',
      `Включённых устройств: ${org.signers.registered}. Отсутствие обращения не доказывает отказ. Проверка подписи на целевом Windows-ПК выполняется отдельно.`);
    component(group,'Принтер',org.printers.registered?`Включённых: ${org.printers.registered}; обращались за 5 мин: ${org.printers.recent}`:'Агент принтера не зарегистрирован',
      'Приём задания драйвером не подтверждает выход этикетки. Неизвестный исход сначала сверяют с принтером и журналом агента.');
    component(group,'Обновление токенов',org.auto_refresh_tokens?'Автообновление включено':'Ручное обновление',
      `Доверенных устройств с правами авторизации ЧЗ и СУЗ: ${org.signers.token_capable}.`);
    for(const item of org.token_renewal){group.append(node('p',`${item.service==='true'?'ЧЗ':'СУЗ'}: последнее обновление ${date(item.renewed_at)}; следующее ${date(item.next_refresh_at)}.${item.needs_attention?' Требует внимания: проверьте подключения и Windows-подписант.':''}`,'muted'));}
    section.append(group);
  }
  if(!data?.organizations?.length)section.append(node('p','Нет действующих организаций.','empty'));
  const queues=node('section',undefined,'section-gap');queues.append(node('h3','Требуют разбора'));
  const renderQueue=(label,items,names)=>{queues.append(node('h4',label));if(!items?.length){queues.append(node('p','Незавершённых заданий нет.','muted'));return;}for(const item of items){queues.append(node('p',`${names[item.state]||'Другое состояние'}: ${item.count}; самое раннее — ${date(item.oldest_at)}.`));}};
  renderQueue('Внешние операции',data?.operations,operationStates);renderQueue('Печать',data?.printing,printingStates);
  const incidents=data?.incidents||{active:0,quarantined:0,unassigned:0};queues.append(node('p',`Открытых обращений: ${incidents.active}; с карантином: ${incidents.quarantined}; без ответственного: ${incidents.unassigned}.`),
    node('p','При неизвестном исходе не создавайте повторный заказ или повторную печать. Сначала откройте исходную операцию, сохраните квитанцию и передайте разбор специалисту. Решение обращения не меняет статус в ЧЗ.','muted'));
  if(navigate&&permissions?.['operations.view'])queues.append(button('Открыть очередь операций',()=>navigate('/operations')));
  if(navigate&&permissions?.['incidents.create'])queues.append(button('Открыть центр проблем',()=>navigate('/incidents')));
  section.append(queues);
  component(section,'ТСД','Нужна приёмка устройств','Проверьте полный КМ, разделитель GS, повтор после потери сети и переход между S55/S45.');
  component(section,'Резервные копии','Расписание и восстановление проверяются отдельно','Проверьте свежую копию вне сервера и пробное восстановление в изолированную БД. Этот экран не читает хранилище резервных копий.');
  return section;
}

export function createSystem({state,$,api,node,button,heading,report,navigate}){
  let generation=0;
  function reset(){generation++;}
  async function show(){
    const seq=++generation,host=$('page-content');
    if(!state.session?.permissions['system.diagnostics.view']){
      host.replaceChildren(heading('Раздел недоступен','Нет разрешения на диагностику системы.'));return;
    }
    const refresh=button('Обновить проверки',show);refresh.disabled=true;
    host.replaceChildren(heading('Состояние системы','Проверки текущего сервера после развёртывания.',[refresh]));
    const content=node('div');content.id='system-status';content.setAttribute('aria-live','polite');content.append(node('p','Выполняем проверки…','panel empty'));host.append(content);
    try{
      const data=await api('/api/system/status');
      if(seq!==generation||state.route!=='/system')return;
      content.replaceChildren();
      const summary=node('section',undefined,'panel system-summary');
      summary.append(node('h2',data.technical_ready?'Проверки сервера пройдены':'Требует внимания'),
        node('p','Техническая готовность сервера не подтверждает готовность бизнес-операций.','muted'));
      const meta=node('dl',undefined,'profile-list');
      for(const [label,value] of [['Сборка',data.build_sha||'SHA релиза не указан'],['Ожидаемая схема',data.expected_schema_revision],['Проверено',new Date(data.checked_at).toLocaleString('ru-RU')]]){
        meta.append(node('dt',label),node('dd',value,'hash-text'));
      }
      summary.append(meta);content.append(summary);
      const grid=node('section',undefined,'system-check-grid');grid.setAttribute('aria-label','Проверки сервера');
      for(const [key,[label,description]] of Object.entries(checks)){
        const result=data.checks[key],item=node('article',undefined,'panel system-check');item.dataset.check=key;
        item.append(node('h3',label),node('span',result===true?'Пройдено':result===false?'Требует внимания':'Не проверено','badge system-result '+(result===true?'passed':result===false?'failed':'unknown')),node('p',description,'muted'));grid.append(item);
      }
      content.append(grid);
      content.append(operationalView(data.operational,{node,button,navigate,permissions:state.session.permissions}));
    }catch(error){
      if(seq!==generation||state.route!=='/system')return;
      content.replaceChildren(node('p',error.code==='reauth_required'?'Подтвердите административный доступ и нажмите «Обновить проверки».':'Не удалось получить состояние системы.','panel empty'));
      report(error);
    }finally{if(seq===generation)refresh.disabled=false;}
  }
  return {show,reset};
}
