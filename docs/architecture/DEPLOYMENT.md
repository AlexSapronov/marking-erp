# Развёртывание и эксплуатация

Дата: 25.10.2026. Проектное решение, если явно не указано иное.


Целевая схема: Ubuntu, PostgreSQL, Nginx HTTPS, FastAPI и worker как два systemd unit, user без sudo для runtime.
Это проект deployment, готовых unit/migrations в этом PR нет.
Перед первым стартом: секреты с правами0600, чистая БД, versioned migrations, seed шести групп и первого администратора через одноразовую CLI-процедуру.
Демо не подменяет live: mock-адаптеры только в тестах; production при отсутствии подключения показывает блокер.
Readiness различает API/DB/migration/worker; интеграции и подписант — отдельные состояния. Диагностика показывает build SHA и schema version.
Обновление: backup→остановка dispatch→миграции→API/worker→health+smoke. Downgrade с несовместимой схемой запрещён без восстановленного backup.
Backup: pg_dump/physical policy + immutable files + encrypted config отдельно; хранить manifest SHA и восстанавливать согласованный набор.
Ротация логов, контроль disk space, unknown queue, возраста очереди, heartbeat, lease expiry, HTTP429 и сроков сертификата.
Пилот должен проверить restore на отдельной БД; цель RPO/RTO согласовать с ИТ, не выдавать предлагаемую цифру за обеспеченную.
У Windows signer нет входящего публичного порта; настроить доверенный TLS и доступ только к ERP.
Реальные S55 и S45 Grip: проверить GS, AIM prefix, Enter suffix, русскую раскладку, Chrome/kiosk и восстановление после сна.
