# Marking ERP

Единая корпоративная система управления маркировкой радиоэлектронной продукции.

**Сейчас: PostgreSQL, личный вход и интерфейс управления пользователями. Бизнес-модули ERP пока не реализованы.**

- [Исходное ТЗ](docs/SPEC-2.0.md)
- [Контрольная точка и ограничения](docs/audit/CHECKPOINT.md)
- [Исходные SHA](docs/audit/BASELINE.md)
- [Обнаруженные проблемы](docs/audit/FINDINGS.md)
- [Фактические тесты](docs/audit/TEST-RESULTS.md)
- [Матрица API](docs/audit/API-COMPLIANCE.md)
- [Архитектура](docs/architecture/OVERVIEW.md) и [модель PostgreSQL](docs/architecture/DATABASE.md)
- [Шесть групп и permissions](docs/architecture/PERMISSIONS.md)
- [Бизнес-правила](docs/architecture/BUSINESS-RULES.md)
- [UI/UX](docs/ux/DESIGN-SYSTEM.md) и [ТСД](docs/ux/TSD.md)
- [Сквозные процессы](docs/architecture/WORKFLOWS.md)
- [План небольших PR](docs/PR-PLAN.md)

Все четыре исходных удалённых репозитория сохранены без изменений. Массовый перенос не начат.
Неподтверждённые продуктивные интеграции не представлены как успешные функции.

Дополнительная проверка контрактов и снятых ограничений: [True API и формат разделения](docs/audit/TRUE-API-REVIEW.md).

[Запуск PostgreSQL-инфраструктуры и проверки](docs/architecture/INFRASTRUCTURE-START.md): миграции, шесть групп, 81 permission и health endpoints. Личный вход и административный API реализованы; доступны экран входа, профиль и управление пользователями.

[Первый администратор, личный вход и управление сессиями](docs/architecture/AUTHENTICATION.md). Для административного доступа обязателен TOTP.

[Интерфейс и браузерные проверки](docs/ux/IMPLEMENTED-UI.md): светлая/тёмная тема, шесть групп, мобильный ТСД и административные формы.

[Редактор групп и бизнес-правил](docs/architecture/CONFIGURATION.md): предпросмотр, версии, история и защита инвариантов.

[Реестр КМ и общие данные](docs/architecture/REGISTRY-FOUNDATION.md): неизменяемые байты, provenance, защищённые файлы и проверяемый PDF renderer.

[Очередь операций и протокол подписанта](docs/architecture/DURABLE-OPERATIONS.md): сохранённые команды, lease/fence, unknown и отдельная machine identity. Продуктивные адаптеры и валидатор ГОСТ ещё не подключены.
