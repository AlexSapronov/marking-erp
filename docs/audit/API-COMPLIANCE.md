# Матрица внешних API

Дата: 25.10.2026. Проектное решение, если явно не указано иное.


Статус каждой записи ниже — UNVERIFIED полного контракта. Инвентаризация буквальных call sites не является автоматическим доказательством полноты динамических путей.
Динамическую auth simpleSignIn ветку, wrapper /api/v3 и окружение нужно проверять вручную вместе с literal inventory.

S01: выбранные методы НК существуют, product-list v4 поддерживает выдачу до10000 и пагинацию; старый v3/product-list выведен.
Откреплённая подпись feed-product-sign-pkcs заменяет устаревший feed-product-sign; текущий код использует откреплённый вариант.
S02: найденные cises/info POST, product/info v4 POST и doc/info v4 GET перечислены как альтернативы устаревшим методам.
Это подтверждает выбор endpoint, но не все обязательные поля, статусы, квоты, подпись и recovery.
Основные обнаруженные вызовы не объявляются устаревшими лишь из-за наличия v3 в URL.

Подготовлена [офлайн-проверка состава чтения НК](NK-READ-CONTRACT.md) для product-list/feed-product.
Она проверяет пагинацию и соответствие good_id на записанных телах, без сети и импорта;
полный контракт этих методов остаётся UNVERIFIED до реальных свидетельств и пилота.

| Проект | Место | HTTP | Путь / выражение | Полный контракт |
|---|---|---|---|---|
| gis-trueapi | `trueapi.py:189` | POST | `/api/v3/true-api/auth/simpleSignIn` | UNVERIFIED |
| gis-trueapi | `trueapi.py:219` | POST | `/api/v3/true-api/cises/info` | UNVERIFIED |
| gis-trueapi | `trueapi.py:255` | POST | `/api/v4/true-api/product/info` | UNVERIFIED |
| gis-trueapi | `trueapi.py:289` | POST | `/api/v4/true-api/cises/search` | UNVERIFIED |
| gis-trueapi | `trueapi.py:310` | POST | `/cises/check` | UNVERIFIED |
| km-split | `km_split/clients.py:112` | POST | `org['true_api_url'] + '/api/v3/true-api/cises/info'` | UNVERIFIED |
| km-split | `km_split/clients.py:138` | POST | `org['true_api_url'] + '/api/v4/true-api/product/info'` | UNVERIFIED |
| km-split | `km_split/clients.py:156` | GET | `org['true_api_url'] + '/api/v3/true-api/auth/key'` | UNVERIFIED |
| km-split | `km_split/service.py:201` | POST | `/api/v3/order` | UNVERIFIED |
| km-split | `km_split/service.py:201` | POST | `/api/v3/utilisation` | UNVERIFIED |
| km-split | `km_split/service.py:242` | GET | `/api/v3/order/status` | UNVERIFIED |
| km-split | `km_split/service.py:256` | GET | `/api/v3/order/status` | UNVERIFIED |
| km-split | `km_split/service.py:270` | GET | `/api/v3/codes` | UNVERIFIED |
| km-split | `km_split/service.py:306` | GET | `/api/v3/report/info` | UNVERIFIED |
| km-split | `km_split/service.py:311` | GET | `/api/v3/receipts/receipt` | UNVERIFIED |
| marking-bm | `marking_bm/gateway.py:79` | dynamic | `self.settings.oms_url + '/api/v3/' + path` | UNVERIFIED |
| marking-bm | `marking_bm/gateway.py:90` | POST | `self.settings.true_url + '/api/v3/true-api/cises/info'` | UNVERIFIED |
| marking-bm | `marking_bm/gateway.py:181` | POST | `order` | UNVERIFIED |
| marking-bm | `marking_bm/gateway.py:184` | POST | `utilisation` | UNVERIFIED |
| marking-bm | `marking_bm/gateway.py:187` | GET | `codes` | UNVERIFIED |
| marking-bm | `marking_bm/gateway.py:202` | POST | `self.settings.true_url + '/api/v3/true-api/lk/documents/create'` | UNVERIFIED |
| marking-bm | `marking_bm/gateway.py:222` | GET | `order/status` | UNVERIFIED |
| marking-bm | `marking_bm/gateway.py:229` | GET | `report/info` | UNVERIFIED |
| marking-bm | `marking_bm/gateway.py:236` | GET | `receipts/receipt` | UNVERIFIED |
| marking-bm | `marking_bm/gateway.py:247` | GET | `self.settings.true_url + f'/api/v4/true-api/doc/{ext}/info'` | UNVERIFIED |
| marking-bm | `marking_bm/gateway.py:267` | GET | `order/codes/blocks` | UNVERIFIED |
| marking-bm | `marking_bm/gateway.py:281` | GET | `order/codes/retry` | UNVERIFIED |
| marking-bm | `marking_bm/gateway.py:291` | GET | `self.settings.true_url + '/api/v3/true-api/auth/key'` | UNVERIFIED |
| nk-manager | `nk_manager/nk.py:92` | GET | `/v3/feed-product` | UNVERIFIED |
| nk-manager | `nk_manager/nk.py:264` | GET | `/v3/attributes` | UNVERIFIED |
| nk-manager | `nk_manager/nk.py:516` | GET | `/v4/product-list` | UNVERIFIED |
| nk-manager | `nk_manager/nk.py:528` | GET | `/v3/feed-product` | UNVERIFIED |
| nk-manager | `nk_manager/nk.py:558` | POST | `/v3/feed-product-document` | UNVERIFIED |
| nk-manager | `nk_manager/nk.py:577` | POST | `/v3/feed` | UNVERIFIED |
| nk-manager | `nk_manager/nk.py:581` | GET | `/v3/feed-moderation` | UNVERIFIED |
| nk-manager | `nk_manager/nk.py:587` | GET | `/v3/generate-gtins` | UNVERIFIED |
| nk-manager | `nk_manager/nk.py:599` | POST | `/v3/feed-product-sign-pkcs` | UNVERIFIED |
| nk-manager | `nk_manager/nk.py:608` | GET | `/v3/feed-status` | UNVERIFIED |

## Ручное дополнение динамических ветвей

TrueAPI auth: GET auth/key; POST auth/simpleSignIn и auth/simpleSignIn/{oms_connection}.
BM OMS wrapper добавляет /api/v3/; пути order, codes, order/status, utilisation, report/info, receipts/receipt,
order/codes/blocks и order/codes/retry требуют проверки точного HTTP и семантики повторного получения.
KM split использует те же семейства; codes GET является расходующим.
TrueAPI cises/search и product/info из gis требуют отдельной проверки лимита/состава запроса.
Метод /cises/check присутствует в gis как неподключённый метод: сам исходник предупреждает о непроверенном контракте.
Не подключать его автоматически. cises/info даёт сведения по КИ и сам по себе не доказывает криптографическую подлинность скана.
ФТС и перемаркировка не имеют подтверждённого адаптера в этом пакете; их нельзя реализовывать по догадке.

## Карточка подтверждения на каждый метод

Нужны: официальный документ/version/section; environment/base URL; HTTP/path; auth; обязательные поля и enum;
размер batch/квоты; поля ответа; промежуточные/ошибочные/терминальные статусы; correlation ID;
точные подписываемые bytes; подтверждение результата (receipt+facts); идемпотентность/unknown recovery;
файлы тестовых fixtures и результат контролируемого вызова. Пока эти графы не заполнены — не PASS.
С01/S02 подтверждены по доступным страницам; СУЗ PDF403 и TrueAPI size-limit остаются I-001.

## Обновление после первого PR

Документ S03 получен полностью, v747.0. Выбранные элементы контрактов сверены: [TRUE-API-REVIEW.md](TRUE-API-REVIEW.md).
Статус UNVERIFIED в исходной таблице сохраняется для полного контракта, но не означает отсутствие его текста.
Обнаружен S-004: source.full с AI30 не соответствует описанию формата пары DIVISIONMARK.
