# Развёртывание Marking ERP на рабочем VPS

Обновлено 27.10.2026. Выполнять после отдельного решения о развёртывании.
Цель: Ubuntu 24.04 LTS, x86_64, Python 3.12, PostgreSQL 16, systemd, Nginx и HTTPS.
Команды ниже выполняются по одному блоку; если проверка не прошла, следующий блок
не выполнять. Это установка текущего проверенного релиза, не обещание готовности
всех бизнес-модулей: [фактическая готовность](DELIVERY-STATUS.md).

## Что согласовать с ИТ заранее

1. SSH-адрес VPS, пользователь с sudo и способ входа. Пароли в эту инструкцию не вписывать.
2. Отдельное DNS-имя, например `erp.company.ru`, указывающее на VPS из корпоративной сети/Tailscale.
3. Сертификат и полная цепочка для этого имени, доверенные рабочими ПК, плюс соответствующий закрытый ключ.
4. Доступ VPS к Ubuntu-репозиторию или внутреннему apt-зеркалу. Python-пакеты уже в офлайн-пакете; PyPI и GitHub с VPS не нужны.
5. Корпоративный сетевой доступ: SSH — администраторам, HTTPS/443 — разрешённым сотрудникам. PostgreSQL/5432 и приложение/8000 наружу не открывать. Не включать UFW вслепую — можно потерять SSH.
6. Место для защищённой копии конфигурации/MFA-ключа отдельно от резервных копий БД.

Windows-подписант, CryptoPro и Рутокен для установки этого релиза не нужны: внешние
адаптеры не включаются. Для будущей продуктивной маркировки они потребуются отдельно.

## 1. Скачать проверенный пакет на Windows

В GitHub откройте **Actions → успешный Core PostgreSQL → Artifacts** и скачайте
`ubuntu24-python312-offline-<SHA>`. Выбирайте пакет нужного проверенного коммита,
после слияния — предпочтительно успешный push в main. Не брать случайный ZIP исходников
вместо артефакта. Внутри ZIP должны быть:

- `marking-erp-<40-символьный SHA>.tar.gz`;
- `SHA256SUMS`.

Распакуйте ZIP в `Загрузки\marking-erp-release`. Пакет содержит wheel приложения,
все runtime-зависимости с зафиксированными версиями, проверяющий установщик,
systemd-шаблон и эту инструкцию. Chromium/pytest на VPS не устанавливаются.
SHA в имени пакета, release.json и странице Actions должен совпадать.
Сохраните пакет: он нужен и для повторной установки/восстановления.

## 2. Подключиться и проверить VPS

На Windows откройте PowerShell. Замените только адрес и SSH-пользователя:

```powershell
$ErpVps = "АДРЕС_VPS"
$ErpSshUser = "SSH_ПОЛЬЗОВАТЕЛЬ"
ssh "${ErpSshUser}@${ErpVps}"
```

Далее команды **в SSH-окне Ubuntu**, не в PowerShell. Сначала проверьте sudo и дождитесь окончания запроса пароля:

```bash
sudo -v
```

Затем:

```bash
cat /etc/os-release
uname -m
timedatectl status
df -h /
free -h
sudo ss -ltnp
```

Ожидается Ubuntu **24.04**, архитектура **x86_64**, синхронизированное время и
рабочий sudo. Для первой установки оставьте несколько ГБ свободного места под
релиз, БД и резервные копии. Порт 8000 должен быть свободен. Если уже есть
`marking-erp` или БД `marking_erp`, перейдите к разделу обновления: ничего не удалять.
Если ОС/архитектура иные, этот пакет не устанавливать; сначала адаптируем среду.
Особенно не менять системный Python на Ubuntu 22.04 этими командами.

Подготовьте папку для передачи пакета:

```bash
mkdir -p ~/erp-deploy
chmod 700 ~/erp-deploy
exit
```

## 3. Передать пакет с Windows

Снова **PowerShell на Windows**:

```powershell
$ErpBundleDir = "$env:USERPROFILE\Downloads\marking-erp-release"
scp "$ErpBundleDir\*.tar.gz" "$ErpBundleDir\SHA256SUMS" "${ErpSshUser}@${ErpVps}:~/erp-deploy/"
ssh "${ErpSshUser}@${ErpVps}"
```

Далее опять **Ubuntu/SSH**:

```bash
cd ~/erp-deploy
sha256sum -c SHA256SUMS
```

Ожидается `OK`. При несовпадении остановиться и повторить передачу исходного
пакета. В папке должен находиться только один выбранный релиз.

## 4. Установить системные пакеты

```bash
sudo apt-get update
sudo apt-get install -y python3.12 python3.12-venv postgresql-16 postgresql-client-16 nginx openssl ca-certificates fonts-dejavu-core
python3.12 --version
/usr/lib/postgresql/16/bin/psql --version
sudo systemctl status postgresql --no-pager
sudo -u postgres psql -X -d postgres -c 'SHOW listen_addresses;'
```

Ожидаются Python **3.12.x**, клиент PostgreSQL **16.x**, работающий PostgreSQL и
`listen_addresses = localhost`. Проверьте и сервер: `sudo -u postgres psql -X -c 'SHOW server_version;'`.
Если apt недоступен — нужен доступ к согласованному Ubuntu-зеркалу от ИТ.
Если PostgreSQL слушает внешние интерфейсы или уже обслуживает другие приложения,
не менять глобальные настройки без разбора существующей конфигурации.

## 5. Установить код без доступа к PyPI

Задайте SHA из имени скачанного пакета. Это не секрет:

```bash
ERP_RELEASE_SHA=ВСТАВЬТЕ_40_СИМВОЛОВ_SHA
ERP_RELEASE=/opt/marking-erp/releases/$ERP_RELEASE_SHA
tar -xzf "marking-erp-$ERP_RELEASE_SHA.tar.gz"
sudo install -d -o root -g root -m 0755 /opt/marking-erp /opt/marking-erp/releases
sudo python3.12 "marking-erp-$ERP_RELEASE_SHA/install-release.py" --bundle "$PWD/marking-erp-$ERP_RELEASE_SHA"
```

Установщик проверит ОС, Python, архитектуру, состав файлов и SHA256, установит
wheelhouse с `--no-index`, проверит зависимости и наличие миграций/статики.
Ожидается `Installed: /opt/marking-erp/releases/<SHA>`.
Он не создаёт БД, не запускает сервис и не меняет `current`. Существующий каталог
релиза не перезаписывается. Код/venv остаются у root, а не у сервисного пользователя.
Не устанавливайте приложение через `pip --user`, editable-режим или ветку main.

## 6. Создать сервисного пользователя и каталоги

Сначала проверьте, существует ли пользователь:

```bash
getent passwd marking-erp
```

Если вывода нет, создайте его:

```bash
sudo useradd --system --home-dir /var/lib/marking-erp --shell /usr/sbin/nologin marking-erp
```

Если пользователь уже есть, проверьте его назначение и не переиспользуйте чужую
учётную запись. Затем:

```bash
sudo install -d -o marking-erp -g marking-erp -m 0700 /var/lib/marking-erp /var/lib/marking-erp/files
sudo install -d -o root -g root -m 0700 /etc/marking-erp /etc/marking-erp/tls /var/backups/marking-erp
```

## 7. Создать отдельную БД и две роли

Откройте административный psql:

```bash
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres
```

Вставляйте SQL/psql-команды по очереди:

```sql
CREATE ROLE marking_erp_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE marking_erp_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
```

Отдельно введите команду и дождитесь двух запросов пароля владельца:

```text
\password marking_erp_owner
```

Затем так же задайте другой пароль runtime:

```text
\password marking_erp_runtime
```

После возврата к приглашению psql продолжите:

```sql
CREATE DATABASE marking_erp OWNER marking_erp_owner;
REVOKE ALL ON DATABASE marking_erp FROM PUBLIC;
GRANT CONNECT ON DATABASE marking_erp TO marking_erp_runtime;
\connect marking_erp
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
\q
```

На каждой `\password` psql попросит новый пароль и повтор. Выберите два разных
пароля, сохраните в согласованном менеджере паролей; они понадобятся на следующем
шаге. Не печатайте пароли SQL-командами или в shell. При `already exists` остановиться:
эта инструкция не очищает и не перезаписывает имеющуюся БД.

## 8. Создать конфигурацию и постоянный MFA-ключ

```bash
sudo "$ERP_RELEASE/venv/bin/marking-erp-admin" configure --release-sha "$ERP_RELEASE_SHA"
```

Помощник запросит HTTPS-адрес, пароль роли owner и пароль runtime. Ввод паролей
скрыт. Используйте ровно те пароли, что задали в psql. Специальные символы
кодируются автоматически. Будут созданы root-only файлы:

- `/etc/marking-erp/runtime.json` — только ограниченная runtime-роль;
- `/etc/marking-erp/maintenance.json` — владелец для миграций/backup;
- `/etc/marking-erp/nginx.generated.conf` — конфигурация вашего hostname.

MFA-ключ генерируется один раз и одинаков в двух JSON. Его потеря лишит доступа к
зашифрованным MFA. Сохраните защищённую копию настроек в согласованном месте
отдельно от БД. Конфигурацию, пароли и MFA-ключ в чат не отправлять.
Помощник намеренно отказывается перезаписывать существующие настройки;
при ошибке ввода не удаляйте их вслепую и не генерируйте новый MFA-ключ.

## 9. Применить миграции и создать первого администратора

```bash
sudo "$ERP_RELEASE/venv/bin/marking-erp-admin" migrate
sudo "$ERP_RELEASE/venv/bin/marking-erp-admin" seed
```

Seed сообщает `initialized` либо `already_initialized`. Затем замените название,
ИНН и отображаемое имя реальными значениями вашей организации:

```bash
sudo "$ERP_RELEASE/venv/bin/marking-erp-admin" bootstrap-admin --username admin --display-name 'Александр Сапронов' --inn ВАШ_ИНН --organization-name 'НАЗВАНИЕ_ОРГАНИЗАЦИИ'
```

Bootstrap запросит пароль администратора **от 16 символов**, повтор, покажет
одноразово ключ TOTP и попросит шестизначный код из приложения-аутентификатора.
Добавьте ключ в аутентификатор на телефоне. Не копируйте этот вывод в переписку.
Для первого входа дождитесь **следующего** кода: использованный при bootstrap
повторно не принимается. `grant-runtime` должен завершиться без ошибки.
После завершения интерактивного bootstrap выполните отдельно:

```bash
sudo "$ERP_RELEASE/venv/bin/marking-erp-admin" grant-runtime --role marking_erp_runtime
```

Сервис не использует owner-пароль и не выполняет миграции сам.

## 10. Настроить HTTPS

Полученные от ИТ файлы должны соответствовать вашему DNS-имени. На VPS создайте
приватную папку для передачи:

```bash
mkdir -p ~/erp-deploy/tls
chmod 700 ~/erp-deploy/tls
```

Оставьте это SSH-окно открытым. Во втором окне PowerShell задайте те же
`$ErpVps` и `$ErpSshUser` из шага 2, затем передайте два файла, заменив пути:

```powershell
scp "C:\ПУТЬ_К_ФАЙЛАМ\fullchain.pem" "C:\ПУТЬ_К_ФАЙЛАМ\private.key" "${ErpSshUser}@${ErpVps}:~/erp-deploy/tls/"
```

Вернитесь в исходное SSH-окно Ubuntu и установите файлы:

```bash
sudo install -o root -g root -m 0644 ~/erp-deploy/tls/fullchain.pem /etc/marking-erp/tls/fullchain.pem
sudo install -o root -g root -m 0600 ~/erp-deploy/tls/private.key /etc/marking-erp/tls/private.key
sudo test ! -e /etc/nginx/sites-available/marking-erp
sudo test ! -e /etc/nginx/sites-enabled/marking-erp
```

Если последние проверки вернули ненулевой код, конфигурация уже существует —
сначала выяснить, чья она; не перезаписывать. Для первой установки:

```bash
sudo install -o root -g root -m 0600 /etc/marking-erp/nginx.generated.conf /etc/nginx/sites-available/marking-erp
sudo ln -s /etc/nginx/sites-available/marking-erp /etc/nginx/sites-enabled/marking-erp
sudo nginx -t
sudo systemctl enable --now nginx
```

Ожидается `syntax is ok` и `test is successful`. Nginx не должен выдавать предупреждение
о конфликтующем server_name. DNS должен разрешаться с рабочего ПК; не рассчитывайте
на редактирование hosts без прав администратора. Сертификат должен быть доверен
браузером. Не обходите это отключением secure_cookie или публикацией порта 8000.

## 11. Запустить systemd

Проверьте, что это первая установка, и установите ссылку/юнит:

```bash
sudo test ! -e /opt/marking-erp/current
sudo ln -s "$ERP_RELEASE" /opt/marking-erp/current
sudo install -o root -g root -m 0644 "$ERP_RELEASE/deploy/marking-erp.service" /etc/systemd/system/marking-erp.service
sudo systemd-analyze verify /etc/systemd/system/marking-erp.service
sudo systemctl daemon-reload
sudo systemctl enable --now marking-erp
sudo systemctl status marking-erp --no-pager
sudo systemctl reload nginx
```

Ожидается `active (running)`. systemd передаёт runtime.json через LoadCredential;
приложение работает непривилегированно на `127.0.0.1:8000`. Доступа к maintenance.json
у сервиса нет. На новых systemd файл переданного credential может отображаться
как 0440 из-за маски ACL: приложение проверяет, что чтение разрешено только
root и UID сервиса. Исходные JSON остаются root-only 0600; chmod 0640 не нужен. Если не стартует:

```bash
sudo journalctl -u marking-erp -n 60 --no-pager
```

Не делайте repeated restart при schema/config error: сначала исправьте причину.
Никогда не запускайте Uvicorn от root для обхода preflight.

## 12. Проверить сервер и вход с рабочего ПК

На VPS:

```bash
curl --fail --silent http://127.0.0.1:8000/health/live
curl --fail --silent http://127.0.0.1:8000/health/ready
sudo ss -ltnp
```

`live` — ok, `ready` — ready, schema из `release.json` выбранного пакета (текущий код: `0021_workflow_policy`), build_sha — SHA вашего пакета.
8000 и 5432 доступны только loopback, публичный вход — HTTPS/443.

На рабочем ПК откройте **ваш HTTPS-адрес**. Войдите admin + пароль + свежий TOTP.
Откройте «Состояние системы»: десять технических проверок должны пройти.
Экран покажет сохранённые подключения и локальные очереди. Наличие реквизитов
не подтверждает успешный доступ к API; выполните отдельную приёмку оборудования
и подключений по `acceptance/PILOT-CHECKLIST.md`.
Затем:

1. Создайте личную учётную запись БМ для сотрудника.
2. В отдельном браузерном профиле войдите этим сотрудником.
3. Создайте тестовую накладную, добавьте строку, сохраните и откройте повторно.
4. Проверьте XLSX-шаблон/предпросмотр на тестовом файле. Не выполняйте реальные эмиссии.
5. Проверьте архивирование с номером и причиной; возврат — специалистом/администратором.
6. Проверьте, что БМ не видит управление пользователями, а отзыв его сессии действует.

`ready` и CI подтверждают реализованную сборку, но не завершение всех четырёх проектов.

## 13. Создать и проверить первую резервную копию

После успешного первого входа:

```bash
sudo /opt/marking-erp/current/venv/bin/marking-erp-admin backup --output /var/backups/marking-erp/initial.zip
sudo /opt/marking-erp/current/venv/bin/marking-erp-admin verify-backup --bundle /var/backups/marking-erp/initial.zip
sudo ls -lh /var/backups/marking-erp/initial.zip
```

Ожидается `valid: true`, текущая схема и file_count. Имя initial.zip должно быть
новым, существующая копия не перезаписывается. Архив содержит БД и связанные файлы,
но не сам MFA-ключ. Скопируйте архив и отдельно настройки в согласованное защищённое
резервное хранилище. Только локальный файл на этом VPS не является offsite-бэкапом.
До допуска сотрудников ИТ должен согласовать расписание, хранение и контроль
резервных копий; автоматическое удаление старых копий этой инструкцией не включается.

## Обновление существующей ERP

Не повторяйте создание ролей, bootstrap или configure. Сохраните прежний релиз
и **исходный MFA-ключ**. Подготовьте и проверьте новый пакет отдельно. Порядок:

1. Проверить новую сборку в CI и установить в новый каталог релиза.
2. Остановить `marking-erp`.
3. Старым релизом сделать и проверить новый backup БД+файлов; сохранить текущую конфигурацию.
4. Новым релизом выполнить `migrate` и `grant-runtime` через существующий maintenance.json.
5. Обновить только build_sha в двух приватных JSON до нового SHA; не менять роли/ключ/URL.
6. При остановленном сервисе переключить current на новый каталог, затем запустить сервис.
7. Повторить шаги 12–13 и зафиксировать приёмку.

Не делайте автоматический downgrade БД. Если после миграции нужен откат:
остановить сервис и восстановить проверенный архив **в новую БД и новый каталог
файлов**, следуя [BACKUP-RESTORE.md](architecture/BACKUP-RESTORE.md). Исходную БД
не удалять. После сверки настроек/прав переключить старый релиз на восстановленную
копию с соответствующей схемой. Restore сбрасывает сессии и отключает dispatch;
возобновление внешних операций требует отдельной сверки.

## Если что-то пошло не так

| Наблюдение | Что проверить |
|---|---|
| apt не скачивает пакеты | Доступ к Ubuntu-репозиторию/корпоративному зеркалу |
| Установщик отклоняет ОС | /etc/os-release, x86_64, Python 3.12; не обходить проверку |
| migrate/seed не проходят | PostgreSQL 16, owner-пароль, отдельная БД, ошибки предыдущего шага |
| preflight schema_revision=false | Миграции именно этим релизом, версия БД |
| database_runtime_role=false | grant-runtime владельцем, отсутствие владения/членств/CREATE у runtime |
| private_file_storage=false | Владелец marking-erp, режим 0700, отсутствие симлинков |
| mfa_decrypt=false | Исходный MFA-ключ; не генерировать новый |
| Nginx 502 | systemctl status, journalctl, loopback /health/ready |
| Ошибка TLS | DNS/hostname, цепочка и срок сертификата, доверие рабочего ПК |
| TOTP отвергается | Синхронизация времени и следующий, ещё не использованный код |
| Другой build_sha | current, release.json и оба JSON; нужен точный проверенный пакет |

Для совместного разбора можно прислать версию ОС, SHA, статусы и названия
непройденных проверок. Не присылайте содержимое JSON-конфигураций, пароли,
TOTP-ключи/коды, cookie или токены.

Основания: [Python в Ubuntu](https://ubuntu.com/developers/docs/reference/availability/python/),
[PostgreSQL для Ubuntu](https://www.postgresql.org/download/linux/ubuntu/),
[systemd credentials](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html),
[Nginx](https://nginx.org/en/docs/beginners_guide.html).


Миграция `0017_warehouse_import` вводит типизированные складские источники БМ/Excel.
Существующие документы БМ регистрируются без изменения их состава и ревизий.
После появления заданий из Excel откат приложения к версии без этой модели
не поддерживается: старый код не сможет открыть самостоятельный источник.
Для восстановления используйте совместимый релиз и резервную копию. `downgrade`
удаляет данные добавленного импорта и сохраняет старые внешние ключи БМ как
`NOT VALID`; это не процедура производственного отката.
