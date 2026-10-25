"""Reviewed permission metadata and dependency checks shared by API and editor."""
from .seed import permission_defaults

MODULES={'nk':'Национальный каталог','marking':'Маркировка БМ','logistics':'Логистика','warehouse':'Склад','codes':'Коды','split':'Разделение','labels':'Этикетки','documents':'Документы','operations':'Операции','incidents':'Проблемы','audit':'Аудит','users':'Пользователи','permissions':'Группы и права','business_rules':'Бизнес-правила','integrations':'Интеграции','organizations':'Организации','system':'Система','sessions':'Сессии'}
ACTIONS={'view':'Просмотр','create':'Создание','edit':'Изменение','moderate':'Отправка на модерацию','publish':'Публикация','generate':'Получение GTIN','use':'Использование','preview':'Предпросмотр','commit':'Применение импорта','export':'Выгрузка','execute':'Выполнение','approve':'Согласование','issue':'Эмиссия','download':'Скачивание','confirm':'Подтверждение','submit':'Отправка','prepare':'Подготовка','transfer':'Передача на склад','refresh':'Обновление','resolve':'Разбор расхождений','complete':'Завершение','scan':'Сканирование','delete':'Удаление скана','check':'Проверка','reopen':'Возврат в работу','verify':'Проверка КМ','reprint':'Перепечать','stop':'Остановка','archive':'Архивирование','restore':'Восстановление','reconcile':'Сверка','manage':'Управление','revoke':'Отзыв'}


def catalog():
    result=[]
    for p in permission_defaults():
        id=p['id'];parts=id.split('.');module=parts[0];last=parts[-1];deps=[]
        if not p['administrative']:
            if module=='nk' and id!='nk.cards.view':deps=['nk.cards.view']
            elif module=='marking' and id!='marking.documents.view':deps=['marking.documents.view']
            elif module=='logistics' and id!='logistics.atk.view':deps=['logistics.atk.view']
            elif module=='warehouse':
                if parts[1] in ('receipt','shipment') and last!='view':deps=[f'warehouse.{parts[1]}.view']
            elif module=='codes' and id in ('codes.history.view','codes.payload.export'):deps=['codes.registry.view']
            elif module=='split' and id!='split.operations.view':deps=['split.operations.view']
            elif module=='labels' and id!='labels.print.view':deps=['labels.print.view']
            elif id in ('documents.stop','documents.archive','documents.restore'):deps=['documents.archive.view']
            elif id=='operations.reconcile':deps=['operations.view']
            elif id=='incidents.manage':deps=['incidents.create']
        kind='read' if last in ('view','download','export','verify') else 'create' if last in ('create','generate') else 'edit' if last in ('edit','delete','manage') else 'execute'
        result.append({**p,'module':MODULES[module],'label':ACTIONS.get(last,last)+' · '+id,'kind':kind,'dependencies':deps,
                       'description':'Административное действие: обязательны TOTP и свежее подтверждение.' if p['administrative'] else 'Доступ только в назначенных организациях; свои — владелец, назначенные — связанные задания.'})
    return result


def dependency_errors(grants):
    errors=[]
    for p in catalog():
        scope=grants[p['id']]
        if scope=='none':continue
        for dep in p['dependencies']:
            if grants[dep] not in (scope,'all'):
                errors.append(f"{p['id']}: требуется {dep} с областью {scope} или all")
    return errors
