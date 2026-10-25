# Каталог версионируемых бизнес-правил

Дата: 25.10.2026. Проектное решение, если явно не указано иное.


Значения ниже — предложения для первого seed, не настройки работающей ERP.
Область: system default → organization override → document snapshot. Для status profiles обязательна проверенная контрактная версия.
Структура: id, label, description, type, default, validator, scope, dependencies, risk text, mutable, effective_version, actor/reason.

| ID | Тип | По умолчанию | Допустимо | Значение |
|---|---|---|---|---|
| `nk.sync.interval_seconds` | integer | 900 | 60..86400 | Период фоновой актуализации |
| `nk.duplicates.similarity` | decimal | 0.90 | 0.5..1 | Порог похожести PN; точные дубли всегда показываются |
| `nk.duplicates.reason_required` | bool | true | true/false | Причина подтверждения; факт подтверждения всегда в аудите |
| `nk.bulk.batch_size` | integer | 25 | 1..100 | Ограничивается дополнительно лимитом конкретного API |
| `nk.drafts.hide_published_duplicates` | bool | true | true/false | Только фильтр UI, синхронизация и данные сохраняются |
| `bm.lines.merge` | enum | same_pn_gtin_supplier | same_pn_gtin_supplier/never | Не объединять неоднозначные PN/GTIN |
| `bm.issue.extra_confirmation` | bool | true | true/false | Внутреннее дополнительное подтверждение заказа |
| `bm.quantity.edit_until` | enum | before_report_prepare | before_emission/before_report_prepare | После подписи/отправки соответствующие данные неизменяемы |
| `bm.atk.approval_required` | bool | false | true/false | Внутреннее согласование до отправки |
| `archive.retention_months` | integer | 6 | 6..120 | Минимум из ТЗ; не удалять связанные незавершённые операции |
| `warehouse.owner.policy` | enum | strict | strict/approved_exception | Исключение требует permission и обоснования; требования ЦРПТ не отключаются |
| `warehouse.missing.warning_threshold` | integer | 1 | 0..100000 | Только индикация; не разрешает завершить неполную приёмку |
| `warehouse.duplicate.behavior` | enum | error_with_delete_action | error/error_with_delete_action | Удаление отдельной командой и permission |
| `warehouse.complete.confirm` | bool | true | true/false | Подтверждение пользователя поверх обязательных серверных checks |
| `warehouse.hide_completed` | bool | true | true/false | Предустановленный фильтр списка |
| `warehouse.devices.poll_seconds` | integer | 3 | 1..60 | Актуализация центральных сканов |
| `warehouse.check.timeout_seconds` | integer | 120 | 10..600 | Ограничение читающей проверки |
| `warehouse.external_fact_max_age_seconds` | integer | 300 | 30..3600 | Давность фактов; не даёт использовать неподтверждённые данные |
| `warehouse.status_profile` | enum | approved_default | approved_default/approved_stricter | Только проверенные профили по виду операции и товарной группе |
| `logistics.transfer.confirm` | bool | true | true/false | Передача атомарна и всегда идемпотентна |
| `logistics.receipt.required` | bool | true | true/false | Для новых документов; связанные уже переданные задания не исчезают |
| `logistics.close.required_fields` | set | supplier,number,date | allowlisted field IDs | Не заменяет обязательные реквизиты внешнего контракта |
| `split.label.format` | enum | 58x40 | 58x40 | Другие форматы только после проверки renderer |
| `split.application.extra_confirm` | bool | true | true/false | Фактическое нанесение всё равно должно быть подтверждено перед отчётом |
| `labels.reprint.reason_required` | bool | true | true/false | Аудит печати обязателен всегда |
| `labels.reprint.control_scan` | bool | true | true/false | Контроль текущей сохранённой версии |
| `labels.reprint.max_copies` | integer | 1 | 1..10 | Не снимает запрет неразрешённого дублирования |
| `labels.printer_profile` | reference | browser_pdf | active profile ID | Принтеры принадлежат разрешённой площадке |
| `workflow.stop.confirm_number` | constant | true | неизменяемо | Подтверждение номера из ТЗ |
| `bm.atk.tnved_digits` | constant | 10 | неизменяемо в baseline | Полный ТН ВЭД, пока новая политика не подтверждена |
| `bm.atk.supplier_isolation` | constant | true | неизменяемо в baseline | Нельзя смешивать поставщиков |

Политика snapshot фиксируется при подготовке критической команды; старые документы не меняются от редактирования настроек.
Изменение старой подготовленной операции требует явного reprepare с diff, новой ревизией и повторной подписью при изменённых байтах.
Правило закрытия логистом действует только при выполнении всех обязательных внешних этапов; недостаток контракта не делает этап необязательным.
Системные инварианты не представлены выключателями: RBAC, quantity bounds, резерв, целостность КМ, подтверждение внешнего успеха и idempotency.
История правил append-only; optimistic revision защищает от потери правок двух админов.
