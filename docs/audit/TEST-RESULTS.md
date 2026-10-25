# Фактические результаты тестов

Дата: 25.10.2026. Проектное решение, если явно не указано иное.


Среда: Python3.12 из CODEX_PRIMARY_RUNTIME_PYTHON; fastapi0.162.0, httpx0.28.1, pytest9.1.1, pytest-asyncio1.4.0.
Это доступная среда, не полностью воспроизведённые lockfiles. Установка недостающих пакетов не завершена; зависимости не подменялись.

| Проект | Команда из его каталога | Результат | Исключено / причина |
|---|---|---|---|
| gis-trueapi | python -m pytest -q tests --tb=short | 234 passed | Нет исключений Python-набора |
| nk-manager | python -m pytest -q tests --ignore=tests/test_launcher.py --tb=short | 102 passed | launcher: нет uvicorn |
| marking-bm | python -m pytest -q tests --ignore=tests/test_security_labels.py --tb=short | 115 passed | security_labels: нет zxingcpp, включая security tests в этом файле |
| km-split | python -m pytest -q tests --ignore=tests/test_workflow.py --tb=short | 13 passed | workflow: нет zxingcpp; основные сценарии этого файла не выполнены |

Всего464 прошедших теста. Первые полные запуски NK/BM/split остановились на collection error, а не прошли полностью.
Browser suites, декодирование этикеток, реальные внешние API и PostgreSQL не запускались.
Deprecation warnings FastAPI on_event и Starlette/httpx не скрывались.

Локальный repro W-002: synthetic plan.quantity=10, expected.quantity=3 и тот же полный код в scans →
receipt_results вернул ok=true, rows[0].remaining=7. Это наблюдаемое воспроизведение функции;
достижимость конкретного несогласованного документа через каждый Excel importer отдельно не утверждается.
Исправление должно проверять этот инвариант и при создании, и при финальной сверке.

## Повторяемые локальные проверки

`python scripts/reproduce_audit.py /path/to/source-snapshots` проверяет blob SHA Python-исходников и запускает три пробы в отдельных процессах с временными БД.
Результаты: [local-probes.json](local-probes.json). W-001: anonymous list200; S-001: anonymous settings GET/POST200; W-002: ok=true при remaining7.
Это воспроизведение существующих проблем, а не passing регрессии исправленной ERP. Продуктивные API не вызываются.

## Повторный полный запуск25.10.2026

Установлены uvicorn0.54.0, click8.5.0 и zxing-cpp3.1.1. Полные наборы запущены без исключения файлов.

| Проект | Команда | Результат |
|---|---|---|
| gis-trueapi | python -m pytest -q tests --tb=short | 234 passed (предыдущий полный запуск на тех же исходниках) |
| nk-manager | python -m pytest -q tests --tb=short | 104 passed |
| marking-bm | python -m pytest -q tests --tb=short | 125 passed |
| km-split | python -m pytest -q tests --tb=short | 64 passed |

Всего527 passed. Ранее исключённые тесты запуска/безопасности/GS1/PDF теперь выполнены; browser suites и физическая печать не проверены.
Source SHA не изменились. Полный набор не означает соответствия внешним контрактам: отдельная проба S-004 воспроизводит новый дефект.
