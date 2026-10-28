# Применение бизнес-правил

Проверено для каталога версии 2. Все 31 правило сопоставлены с рабочим кодом. Изменяемые правила применяются в указанной точке; неизменяемые отражают фиксированные серверные ограничения.

| Правило | Режим | Где применяется |
|---|---|---|
| `nk.sync.interval_seconds` | Настройка | business/catalog_schedule.py — tick |
| `nk.duplicates.similarity` | Настройка | business/nk.py — поиск дублей |
| `nk.duplicates.reason_required` | Настройка | business/nk.py — подтверждение дубля |
| `nk.bulk.batch_size` | Настройка | business/catalog_batch.py; business/worker.py — пакеты |
| `nk.drafts.hide_published_duplicates` | Настройка | business/catalog_policy.py; business/catalog_worklist.py — рабочий список |
| `bm.lines.merge` | Настройка | document_routes.py — подготовка строк |
| `bm.issue.extra_confirmation` | Настройка | business/marking.py — start |
| `bm.quantity.edit_until` | Настройка | business/marking.py — reallocate |
| `bm.atk.approval_required` | Настройка | business/marking.py — atk/approve |
| `archive.retention_months` | Настройка | workflow_policy.py — retention/retain_until |
| `warehouse.owner.policy` | Фиксированное ограничение | warehouse_routes.py — verify_batch; business/connections.py — запрет обхода |
| `warehouse.missing.warning_threshold` | Настройка | warehouse_routes.py — detail/shortage |
| `warehouse.duplicate.behavior` | Настройка | warehouse_routes.py — scan |
| `warehouse.complete.confirm` | Настройка | warehouse_routes.py — transition/close |
| `warehouse.hide_completed` | Настройка | warehouse_routes.py — listing; static/warehouse.js — начальный фильтр |
| `warehouse.devices.poll_seconds` | Настройка | workflow_policy.py — display; static/warehouse.js — queueRefresh |
| `warehouse.check.timeout_seconds` | Настройка | warehouse_routes.py — run_check/verify_batch |
| `warehouse.external_fact_max_age_seconds` | Настройка | warehouse_routes.py — fresh; warehouse_check.py — public_policy |
| `warehouse.status_profile` | Фиксированное ограничение | warehouse_routes.py — фиксированные статусы verify_batch |
| `logistics.transfer.confirm` | Настройка | warehouse_routes.py — transfer/options |
| `logistics.receipt.required` | Настройка | business/logistics.py — facts |
| `logistics.close.required_fields` | Настройка | business/logistics.py — facts/complete |
| `split.label.format` | Фиксированное ограничение | labels.py — фиксированные размеры PDF |
| `split.application.extra_confirm` | Настройка | business/marking.py — split/applied; static/business.js — processAction |
| `labels.reprint.reason_required` | Настройка | business/printing.py — prepare |
| `labels.reprint.control_scan` | Настройка | business/printing.py — printed/scan |
| `labels.reprint.max_copies` | Настройка | business/printing.py — prepare |
| `labels.printer_profile` | Фиксированное ограничение | labels.py — PDF 58×40; business/printing.py — точный сохранённый PDF |
| `workflow.stop.confirm_number` | Фиксированное ограничение | document_routes.py; warehouse_routes.py; business/marking.py — остановка/архив/отмена |
| `bm.atk.tnved_digits` | Фиксированное ограничение | business/marking.py — валидация полного ТН ВЭД и группировка АТК |
| `bm.atk.supplier_isolation` | Фиксированное ограничение | business/supplier_batches.py; business/marking.py — изоляция поставщика |

## Границы

- Складская политика фиксируется при создании задания; при обновлении старые задания получают прежние значения по умолчанию.
- Интервал ТСД и начальный фильтр — текущие настройки интерфейса. Таймаут и возраст сведений ЧЗ — текущие требования к новой/актуальной сверке.
- Подтверждение передачи берётся из снимка исходной накладной. Для нового запроса передачи API требует `confirmed: true`, если подтверждение включено. Повтор уже сохранённой передачи остаётся идемпотентным.
- При разделении `confirmed` всегда обязателен для факта нанесения; `extra_confirmed` дополнительно требуется для новых процессов с включённым соответствующим правилом.
- Минимальная дата хранения фиксируется отдельной неизменяемой записью. Повторное архивирование и сокращение нового срока не уменьшают прежний минимум. Очистка/удаление истории не реализованы и не запускаются.
- Строгий владелец, допустимые статусы, формат 58×40, изоляция поставщиков и полный ТН ВЭД не ослабляются настройками. Старый флаг игнорирования юрлица не обходит проверку; API запрещает его включение.
