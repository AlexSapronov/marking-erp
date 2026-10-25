# Матрица проверки ERP

Дата: 25.10.2026. Проектное решение, если явно не указано иное.


Это будущие критерии. Здесь не заявлено их выполнение.

| Область | Позитивный сценарий | Обязательные негативные проверки |
|---|---|---|
| Аутентификация | Личный вход и отзыв сессии | Одно имя; отключённый пользователь; просроченная/чужая сессия |
| RBAC | Все permission×6 групп×scope×org | Прямые endpoint/files, снятие прав в активной сессии, specialist admin deny |
| НК | Sync/details/create/moderate/sign/publish | Пагинация>10000, partial details, конфликт, 429, unknown GTIN, duplicate PN |
| БМ | Полная выдача→PDF→report→АТК | Partial buffer, потерянный ответ, повтор block, неверный ID/GTIN, другой ТН ВЭД |
| GS1 | strict/scanner corpus, PDF decode | GS/FNC1, serial91/92/93, слеши/кавычки, crypto, checksum, layout ambiguity |
| Передача | Отдельные задания по накладным | Двойной клик; общий ATK; другой owner/org; конфликт expected revision |
| Приёмка | Точное множество и quantity | Чужой crypto, missing/extra, план10/полный состав3, изменение состава во время check |
| Отгрузка | Batch TrueAPI, верные штуки | Чужой GTIN до API, owner/status fail, missing quantity, API error/partial |
| Конкурентность | Два устройства и worker | Два резерва, два dispatch, stale fence, concurrent close+scan |
| Split | Условия S06 и final reconcile | statusEx/BOX/нет НК-характеристики, unknown с удержанным резервом, stale external facts |
| Reprint | Один saved full payload, ноль мутаций | Только КИ, изменённый PDF hash, ТСД deny, download≠print |
| Archive | Stop/restore сохраняют evidence | Уже отправленная операция, late result, shared ATK, unauthorized restore |
| Правила | Snapshot новой версии | Изменение старой подписи/документа, неверные значения, concurrent save |
| Эксплуатация | Clean install, update, restore | Нет секрета/миграций, БД down, диск full, signer offline |
| Производительность | 10000 строк/50000 сканов как начальный тестовый профиль | O(n²), блокировка UI, API quotas, очередь без starvation |

Нагрузочные цифры — предлагаемый профиль, SLO определяются по корпоративному пилоту.
Каждый finding из FINDINGS.md имеет целевую регрессию. Исправление не считается выполненным без теста на целевой PostgreSQL.
