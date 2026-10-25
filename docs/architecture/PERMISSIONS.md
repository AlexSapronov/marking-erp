# Реестр permissions и шесть групп

Дата: 25.10.2026. Проектное решение, если явно не указано иное.


Основной источник требований — SPEC-2.0.md §§5–7. Таблица — исходный seed; редактор /groups сохраняет отклонения с причиной и историей. См. CONFIGURATION.md.
own = владелец документа; assigned = назначенные/связанные задания; all = все в разрешённых организациях; «-» = запрет.
При grant all нельзя выходить за user_organizations. Read permission не даёт write permission.
Специалист получает все зарегистрированные operational permissions, включая новые; administrative=true всегда исключён.
Администратор имеет все разрешения, но соблюдает workflow и инварианты.

| Permission | БМ | Логист | Склад | ТСД | Специалист | Администратор |
|---|---|---|---|---|---|---|
| `nk.cards.view` | all | all | all | - | all | all |
| `nk.cards.create` | own | - | - | - | all | all |
| `nk.cards.edit` | own | - | - | - | all | all |
| `nk.cards.moderate` | own | - | - | - | all | all |
| `nk.cards.publish` | own | - | - | - | all | all |
| `nk.gtin.generate` | own | - | - | - | all | all |
| `nk.templates.use` | own | - | - | - | all | all |
| `nk.import.preview` | own | - | - | - | all | all |
| `nk.import.commit` | own | - | - | - | all | all |
| `nk.export` | own | - | - | - | all | all |
| `nk.bulk_creation.execute` | - | - | - | - | all | all |
| `nk.sync.execute` | - | - | - | - | all | all |
| `nk.templates.approve` | - | - | - | - | all | all |
| `marking.documents.view` | own | assigned | - | - | all | all |
| `marking.documents.create` | own | - | - | - | all | all |
| `marking.documents.edit` | own | - | - | - | all | all |
| `marking.import.preview` | own | - | - | - | all | all |
| `marking.import.commit` | own | - | - | - | all | all |
| `marking.codes.issue` | own | - | - | - | all | all |
| `marking.labels.download` | own | - | - | - | all | all |
| `marking.application.confirm` | own | - | - | - | all | all |
| `marking.reports.submit` | own | - | - | - | all | all |
| `marking.atk.create` | own | - | - | - | all | all |
| `marking.atk.transform.prepare` | own | - | - | - | all | all |
| `marking.atk.transform.approve` | - | - | - | - | all | all |
| `marking.codes.reuse.approve` | - | - | - | - | all | all |
| `logistics.atk.view` | own | all | - | - | all | all |
| `logistics.atk.download` | own | all | - | - | all | all |
| `logistics.warehouse.transfer` | - | all | - | - | all | all |
| `logistics.warehouse.refresh` | - | all | - | - | all | all |
| `logistics.warehouse.result.view` | - | all | - | - | all | all |
| `logistics.discrepancies.resolve` | - | all | - | - | all | all |
| `logistics.fts.prepare` | - | all | - | - | all | all |
| `logistics.fts.submit` | - | all | - | - | all | all |
| `logistics.documents.complete` | - | all | - | - | all | all |
| `warehouse.receipt.view` | - | - | assigned | assigned | all | all |
| `warehouse.receipt.scan` | - | - | assigned | assigned | all | all |
| `warehouse.receipt.scan.delete` | - | - | assigned | assigned | all | all |
| `warehouse.receipt.check` | - | - | assigned | assigned | all | all |
| `warehouse.receipt.complete` | - | - | assigned | assigned | all | all |
| `warehouse.shipment.view` | - | - | assigned | assigned | all | all |
| `warehouse.shipment.scan` | - | - | assigned | assigned | all | all |
| `warehouse.shipment.scan.delete` | - | - | assigned | assigned | all | all |
| `warehouse.shipment.check` | - | - | assigned | assigned | all | all |
| `warehouse.shipment.complete` | - | - | assigned | assigned | all | all |
| `warehouse.shipment.export` | - | - | assigned | assigned | all | all |
| `warehouse.receipt.create` | - | - | - | - | all | all |
| `warehouse.shipment.create` | - | - | - | - | all | all |
| `warehouse.import.preview` | - | - | - | - | all | all |
| `warehouse.import.commit` | - | - | - | - | all | all |
| `warehouse.documents.reopen` | - | - | - | - | all | all |
| `codes.verify` | all | all | all | all | all | all |
| `codes.registry.view` | own | assigned | assigned | - | all | all |
| `codes.history.view` | own | assigned | assigned | - | all | all |
| `codes.payload.export` | own | assigned | assigned | - | all | all |
| `split.operations.view` | - | - | assigned | - | all | all |
| `split.operations.create` | - | - | assigned | - | all | all |
| `split.application.confirm` | - | - | assigned | - | all | all |
| `split.reports.submit` | - | - | assigned | - | all | all |
| `labels.reprint` | - | - | assigned | - | all | all |
| `labels.print.view` | - | - | assigned | - | all | all |
| `labels.print.confirm` | - | - | assigned | - | all | all |
| `labels.remarking.create` | - | - | - | - | all | all |
| `labels.remarking.submit` | - | - | - | - | all | all |
| `documents.stop` | own | assigned | assigned | - | all | all |
| `documents.archive` | own | assigned | assigned | - | all | all |
| `documents.archive.view` | own | assigned | assigned | - | all | all |
| `documents.restore` | - | - | - | - | all | all |
| `operations.view` | own | assigned | assigned | - | all | all |
| `operations.reconcile` | - | assigned | assigned | - | all | all |
| `incidents.create` | own | assigned | assigned | assigned | all | all |
| `incidents.manage` | - | assigned | assigned | - | all | all |
| `audit.operational.view` | own | assigned | assigned | - | all | all |
| `users.manage` | - | - | - | - | - | all |
| `permissions.manage` | - | - | - | - | - | all |
| `business_rules.manage` | - | - | - | - | - | all |
| `integrations.manage` | - | - | - | - | - | all |
| `organizations.manage` | - | - | - | - | - | all |
| `system.diagnostics.view` | - | - | - | - | - | all |
| `audit.view` | - | - | - | - | - | all |
| `sessions.revoke` | - | - | - | - | - | all |

## Зависимости и обновление

Любая изменяющая операция требует связанного view. transfer требует logistics.atk.view; check/complete — view задания;
reprint требует доступного saved payload; transform.approve — view ATK и доступ к его организации.
Сервис проверяет dependencies целиком; редактор не сохраняет противоречивый набор.
В транзакции изменения группы: lock group revision → validate → insert immutable change → bump auth_version → commit.
Каждый запрос/dispatch сверяет актуальную версию полномочий; кеш только с проверкой версии. При снятии доступа активный UI обновляется.
Команда с ранее подготовленным payload заново проверяет actor перед dispatch. Отказ не удаляет внешние evidence.

## Неоднозначность ТЗ по ТСД

§5.4 требует исключительно три раздела; §7.4 допускает явное административное расширение как отклонение.
Решение baseline: seed и основной приёмочный профиль имеют только три раздела.
Редактор может сохранить явное расширение с причиной и предупреждением; UI показывает отклонение от профиля.
Тест запрета всех остальных API относится к базовому профилю. Дополнительные права не выдаются автоматически по типу устройства.
Чтобы сохранять физически ограниченный ТСД, администратор не должен расширять его группу; следует назначать другую подходящую группу пользователю.
