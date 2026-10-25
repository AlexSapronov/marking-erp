# Общие компоненты и решения о переносе

Дата: 25.10.2026. Проектное решение, если явно не указано иное.


| Компонент | Источники | Решение |
|---|---|---|
| GS1 | gis datamatrix, split codes, BM codes | Общая модель и corpus; отдельные strict/scanner функции |
| Этикетки | BM labels, split labels | Общий renderer и immutable label snapshot; внешнее декодирование обязательно |
| True API | gis trueapi, BM gateway, split clients | Общий transport/token manager + operation-specific validators |
| СУЗ | BM gateway/worker, split clients/service | Общая durable queue; не копировать SQLite SQL |
| НК | nk nk/creation/presets | Доменные функции после обновления main; источник общей номенклатуры |
| Excel | Все четыре | Разные сценарии сохраняются; общие безопасное чтение и literal export |
| Подпись | nk signer и BM signer, split browser/file signing | Один Windows agent, payload bytes+hash+lease+fence |
| Auth | BM и NK sessions, gis admin, split headers | Общая авторизация; старые username-only механизмы не переносить |
| UI | Четыре оболочки | Одна дизайн-система, role navigation, отдельная компоновка ТСД |

Ключевое различие: КИ — идентификатор, полный КМ — конкретные байты с криптохвостом и характеристикой,
этикетка — сохранённая версия представления КМ. Нельзя хранить независимые копии состояния в каждом модуле.
Перераспределение AI30 не означает изменение криптографической части или возможность игнорировать уже отправленный отчёт.
