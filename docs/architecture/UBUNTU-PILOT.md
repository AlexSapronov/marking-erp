# Развёртывание текущей ERP на Ubuntu

25.10.2026. Профиль для изолированной корпоративной среды: Python3.12, PostgreSQL16,
systemd с LoadCredential, Nginx/TLS. Это запуск реализованных функций ERP и инструментов
пилота, а не подтверждение продуктивных контрактов ЧЗ, подписанта или ТСД.
Шаблоны находятся в deploy/. Они не запускаются установкой Python-пакета.

## Подготовка ИТ

Нужны отдельные пользователь ОС marking-erp без login-shell, каталог релизов
/opt/marking-erp/releases, стабильная ссылка /opt/marking-erp/current, приватное
хранилище /var/lib/marking-erp/files и /etc/marking-erp для конфигурации.
Код/venv принадлежат root и недоступны сервису для записи. Данные принадлежат
marking-erp; папки данных имеют0700. Не применять chown ко всему /opt для пользователя сервиса.

Пример создания пользователя/папок на подготовленном сервере:

```bash
sudo useradd --system --home-dir /var/lib/marking-erp --shell /usr/sbin/nologin marking-erp
sudo install -d -o root -g root -m 0755 /opt/marking-erp/releases
sudo install -d -o root -g root -m 0700 /etc/marking-erp
sudo install -d -o marking-erp -g marking-erp -m 0700 /var/lib/marking-erp /var/lib/marking-erp/files
```

Если пользователь/каталоги уже существуют, сначала сверить владельцев и назначение;
не переиспользовать данные старых четырёх сервисов как новую ERP-БД.
PostgreSQL доступен только локально/в разрешённой закрытой сети, с password authentication.
Доступ клиентов к HTTPS ограничивает корпоративный firewall/Tailscale;8000 наружу не открывать.
DNS, доверенный клиентами сертификат, синхронизацию времени и доступ к резервным копиям готовит ИТ.

## Две роли PostgreSQL

В отдельной новой БД marking_erp владелец схемы — marking_erp_owner, сервис —
marking_erp_runtime. Оба LOGIN без SUPERUSER/CREATEDB/CREATEROLE/REPLICATION/BYPASSRLS.
Runtime не состоит в других ролях, включая predefined server-access roles. Пример через
административный psql, пароли вводятся интерактивно командой \password:

```sql
CREATE ROLE marking_erp_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE marking_erp_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
\password marking_erp_owner
\password marking_erp_runtime
CREATE DATABASE marking_erp OWNER marking_erp_owner;
REVOKE ALL ON DATABASE marking_erp FROM PUBLIC;
GRANT CONNECT ON DATABASE marking_erp TO marking_erp_runtime;
\connect marking_erp
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
```

Применять только к выделенной БД. PUBLIC-grants и членства складываются с прямыми
правами роли; команда grant-runtime не отнимает права у PUBLIC/других ролей.
Владелец БД не хранится в конфигурации работающего сервиса.

## Установка выбранного релиза и начальная БД

Выбрать проверенный40-символьный SHA после слияния PR, checkout/build именно этого
релиза, создать venv внутри /opt/marking-erp/releases/SHA/venv и установить пакет
обычным wheel-install без test/browser extras. Runtime не требует Chromium.
Не переключать работающий сервис на ветку main или изменяемый editable checkout.

В защищённой административной сессии установить ERP_DATABASE_URL владельца и
ERP_MFA_ENCRYPTION_KEY. Источник окружения — утверждённый менеджер секретов/скрытый
интерактивный ввод, без паролей в argv, history или присланных логах. JSON ниже не source-файл shell.
Генерация единственного MFA-ключа: marking-erp generate-mfa-key; сохранить приватно
и использовать один и тот же ключ при bootstrap, runtime и восстановлении.

```bash
marking-erp migrate
marking-erp seed
marking-erp bootstrap-admin --username admin --display-name 'Администратор' --inn 7700000000 --organization-name 'Наша организация'
marking-erp grant-runtime --role marking_erp_runtime
```

Заменить реквизиты организации реальными. Bootstrap запрашивает пароль/TOTP по
[инструкции входа](AUTHENTICATION.md). grant-runtime выполняется владельцем после
миграций: сбрасывает прямые table/sequence grants только указанной runtime-роли
в public и выдаёт профиль текущей схемы. Он не создаёт/не меняет роли и пароли,
не передаёт владение и не выполняет миграции. При owner/высоких правах/CREATE отказ.

Профиль даёт SELECT, для обычных таблиц DML, для истории/байтов/файлов INSERT без
UPDATE/DELETE, для metadata/организаций только SELECT. Groups остаётся изменяемой
для редактора revision. TRUNCATE/TRIGGER не выдаются; sequences — USAGE/SELECT без
UPDATE, функции — EXECUTE. Это не RLS: доступ между сотрудниками проверяет API;
владелец/DBA остаётся доверенной административной границей.
Новые grants после будущих миграций выдаются явно той же командой, без автоматических default privileges.

## Конфигурация сервиса

Скопировать deploy/runtime.example.json в /etc/marking-erp/runtime.json, заполнить
все шесть полей, поставить root:root0600. Это JSON, не shell: специальные символы
пароля должны быть корректно закодированы в PostgreSQL URL, а строка — в JSON.
Образец намеренно не содержит рабочего пароля/MFA-ключа.

systemd читает root-файл через LoadCredential и предоставляет сервису read-only
копию erp-config. Entrypoint marking-erp-service берёт её из CREDENTIALS_DIRECTORY;
явный --config доступен для защищённой диагностики. Файл обычный, без symlink,
не более64KiB, без повторных/лишних ключей, не доступен группе/остальным.
Поля JSON перекрывают ERP_* environment; утилита не печатает DSN/MFA-ключ или ошибки драйвера.
Окружение не загружает .env автоматически.

Скопировать deploy/marking-erp.service в /etc/systemd/system/ и настроить ссылку
current на подготовленный релиз. Сервис имеет одну Uvicorn worker на127.0.0.1:8000,
доверяет proxy headers только127.0.0.1, access-log отключён. Перед стартом — check,
serve повторяет его, затем запускает приложение. Миграции/seed/worker внешних операций
не запускаются. ProtectSystem/ProtectHome/PrivateTmp/PrivateDevices и пустой набор
capabilities ограничивают процесс; writable state — /var/lib/marking-erp.

```bash
sudo systemd-analyze verify /etc/systemd/system/marking-erp.service
sudo systemctl daemon-reload
sudo systemctl enable --now marking-erp
sudo systemctl status marking-erp
sudo journalctl -u marking-erp -n 50 --no-pager
```

Проверки check: процесс не root, настроены HTTPS+secure cookies, SHA релиза,
приватный доступный каталог файлов, версия схемы, seed, ограниченная runtime-роль,
включённый администратор с MFA и расшифровка сохранённых MFA текущим ключом.
Проверки БД выполняются в read-only transaction; файловый probe ничего не создаёт.
Они проверяют локальную конфигурацию/доступность, не сертификат клиента, оффсайт-бэкап,
внешние контракты или полную работоспособность всех бизнес-процессов.

## HTTPS и эксплуатационная проверка

deploy/nginx.conf.example — блок для отдельного корпоративного hostname и выданного
сертификата. Заменить server_name/пути ключей, проверить nginx -t и разрешённые сети
до подключения пользователей. Не запускать приложение с secure_cookie=false через HTTP.
После старта проверить с клиентского ПК HTTPS /health/live и /health/ready, затем
вход администратора с MFA, создание тестового сотрудника и отзыв его сессии.
build_sha /health/ready должен совпадать с выбранным SHA. Health ready подтверждает
только схему; service check даёт отдельные эксплуатационные проверки.

## Обновление и откат

Подготовить новый immutable релиз отдельно. Остановить web, выполнить проверенный
[backup БД+файлов](BACKUP-RESTORE.md), затем миграции владельцем и grant-runtime.
Сверить runtime.json/MFA/SHA, переключить current при остановленном сервисе,
запустить unit и повторить проверки. Старый релиз сохраняется до приёмки.
При ошибке схемы не запускать старый код поверх новой БД и не делать blind downgrade:
использовать изолированный restore и согласованное переключение по BACKUP-RESTORE.
Этот PR не добавляет расписание бэкапов, offsite, firewall или удалённый deploy.

Основания: [systemd.exec](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html),
[PostgreSQL16 GRANT](https://www.postgresql.org/docs/16/sql-grant.html).
Тесты проверяют прикладной запуск и роль на PostgreSQL16; запуск systemd/Nginx,
сертификаты и сетевой доступ требуют проверки на целевой VM.
