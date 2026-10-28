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

const workerStages={starting:'Запуск',cycle:'Начало цикла',schedule:'Планировщик НК',auto_print:'Автопечать',dispatch:'Отправка внешней операции',reconcile:'Проверка квитанции',catalog_sync:'Синхронизация НК',pipeline:'Партия каталога',token_refresh:'Обновление доступа ЧЗ/СУЗ',idle:'Ожидание следующего цикла',stopped:'Остановлен'};
const workerObservations={active:'Свежий сигнал',starting:'Запускается',stale:'Нет свежего сигнала потока',delayed:'Долгое выполнение этапа',error:'Цикл завершился ошибкой',historical:'Предыдущий экземпляр'};
const alertNames={worker_unavailable:'Нет свежего сигнала работающего обработчика',worker_delayed:'Задержка этапа обработчика',worker_errors:'Ошибки полного цикла обработки',unknown_operations:'Неизвестный исход внешней операции — требуется сверка',blocked_operations:'Внешние операции заблокированы',ready_queue_delayed:'Готовые операции ждут отправки более 5 минут',dispatch_lease_expired:'Истёк срок выполняемой отправки — проверьте исход',external_receipt_delayed:'Внешняя квитанция ожидается более 10 минут',signature_waiting:'Операции ждут подписи более 5 минут',print_unknown:'Неизвестный исход печати или агент не завершил задание за 5 минут',print_queue_delayed:'Печать ждёт агента более 5 минут',print_held:'Печать приостановлена карантином',catalog_processing_delayed:'Каталог не обновлял прогресс более 10 минут',catalog_blocked:'Синхронизация или партия НК остановлена',auto_print_blocked:'Автопечать остановлена до очереди',auto_print_ready_delayed:'Готовый комплект ждёт автопечати более 5 минут',authorization_expired:'Истёк срок задания авторизации ЧЗ/СУЗ'};
function workersView(data,{node,button,navigate,permissions}){
  const section=node('section',undefined,'panel section-gap');section.id='worker-health';section.append(node('h2','Фоновые обработчики и задержки'));
  const status=data.workers;
  if(!status){section.append(node('p','Нет данных наблюдения.','notice warning'));return section;}
  section.append(node('p','Состояние на момент последнего обновления страницы.','muted'));
  section.append(node('p',status.available?'Есть свежий сигнал работающего потока':status.starting?'Обработчик запускается':'Свежий сигнал работающего обработчика отсутствует',status.available?'notice success':'notice warning'),
    node('p',`Сигнал активности: каждые ${status.heartbeat_seconds} сек.; устаревание после ${status.stale_after_seconds} сек. Завершённый цикл не означает успешного завершения каждой бизнес-операции.`,'muted'));
  if(status.truncated)section.append(node('p','Показаны до 20 экземпляров; работающие экземпляры идут первыми.','muted'));
  for(const instance of status.instances){const card=node('article',undefined,'history-entry');card.append(node('h3','Экземпляр '+instance.id.slice(0,8)),node('p',workerObservations[instance.observation]||'Нет данных','badge'),
    node('p','Этап: '+workerStages[instance.stage]),node('p','Начало этапа: '+date(instance.stage_started_at)+' · порог предупреждения: '+instance.stage_limit_seconds+' сек.','muted'),
    node('p','Последний сигнал: '+date(instance.heartbeat_at)),node('p','Последний завершённый цикл: '+date(instance.last_success_at)),node('p',`Завершено циклов: ${instance.cycles}. Ошибок циклов: ${instance.errors}; подряд: ${instance.consecutive_errors}.`));section.append(card);}
  section.append(node('h3','Предупреждения'));
  if(!data.alerts?.length)section.append(node('p','По локальным данным задержек и неизвестных исходов не обнаружено.','notice success'));
  for(const alert of data.alerts||[]){const row=node('p',(alertNames[alert.code]||'Требует внимания')+' · '+alert.count+(alert.oldest_at?' · самое раннее: '+date(alert.oldest_at):''),alert.severity==='critical'?'notice error':'notice warning');row.dataset.workerAlert=alert.code;section.append(row);}
  section.append(node('p','Предупреждение о задержке не доказывает отказ внешней системы. Неизвестные отправки и печать сначала сверяют по квитанции или у принтера; этот экран не повторяет операции.','muted'));
  if(navigate&&permissions?.['operations.view'])section.append(button('Проверить внешние операции',()=>navigate('/operations')));
  if(navigate&&permissions?.['labels.print.view'])section.append(button('Перейти к печати',()=>navigate('/reprint')));
  if(navigate&&permissions?.['nk.cards.view'])section.append(button('Проверить каталог',()=>navigate('/catalog')));
  const history=node('details');history.append(node('summary','Последние события обработчиков'));
  for(const event of status.events)history.append(node('p',({started:'Запуск экземпляра',cycle_failed:'Ошибка цикла',recovered:'Цикл снова завершается успешно',stopped:'Штатная остановка',restored:'Наблюдение перенесено из резервной копии'}[event.action]||'Событие')+' · '+date(event.created_at)));
  section.append(history);return section;
}

function operationalView(data,{node,button,navigate,permissions}){
  const section=node('section',undefined,'panel section-gap');section.append(node('h2','Подключения и эксплуатация'),
    node('p','Здесь показаны сохранённые настройки и события сервера. Сигналы обработчика показываются отдельно. Доступ к внешним API, работа сертификата и оборудования требуют проверки на целевой среде.','muted'));
  if(!data){section.append(node('p','Сводка недоступна: сначала приведите схему БД к версии установленного релиза.','empty'));return section;}
  section.append(workersView(data,{node,button,navigate,permissions}));
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
      summary.append(meta);if(data.operational?.alerts?.length)summary.append(node('p','Фоновая обработка требует внимания: предупреждений '+data.operational.alerts.length+'. Подробности ниже.','notice warning'));content.append(summary);
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
