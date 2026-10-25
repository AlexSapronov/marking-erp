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
const components=[
  ['nk','Национальный каталог','pilot_only','Инструменты пилота','Сбор и разбор ответов доступны отдельными командами. Импорт и продуктивная синхронизация не подключены.'],
  ['true_api_oms','Честный Знак / СУЗ','not_connected','Не подключены','Продуктивные адаптеры внешних операций отсутствуют.'],
  ['gost_signer','ГОСТ-подписант','verifier_not_connected','Нужен валидатор','Протокол очереди подготовлен; доверенная проверка ГОСТ-подписи и пилот Windows ещё требуются.'],
  ['tsd','ТСД','pilot_required','Нужен пилот устройств','Требуется проверка сканирования на реальных S55 и S45.'],
  ['backup_schedule','Резервные копии','not_verified','Расписание не проверено','Команды backup/restore реализованы. Автоматический запуск, offsite и фактическую копию проверяет ИТ.']
];

export function createSystem({state,$,api,node,button,heading,report}){
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
      const external=node('section',undefined,'panel section-gap');external.append(node('h2','Подключения и эксплуатация'),node('p','Эти пункты не проверяются внешними запросами с этого экрана.','muted'));
      for(const [id,label,expected,status,description] of components){const row=node('article',undefined,'system-component');row.append(node('h3',label),node('span',data.components?.[id]===expected?status:'Не проверено','badge system-result unknown'),node('p',description,'muted'));external.append(row);}content.append(external);
    }catch(error){
      if(seq!==generation||state.route!=='/system')return;
      content.replaceChildren(node('p',error.code==='reauth_required'?'Подтвердите административный доступ и нажмите «Обновить проверки».':'Не удалось получить состояние системы.','panel empty'));
      report(error);
    }finally{if(seq===generation)refresh.disabled=false;}
  }
  return {show,reset};
}
