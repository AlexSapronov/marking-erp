# Логическая модель PostgreSQL

Дата: 25.10.2026. Проектное решение, если явно не указано иное.


Предложение схемы, не выполненная миграция. UUID PK; timestamptz UTC; количество для AI30 — bigint с CHECK(1..99999999).
GTIN char(14) с проверкой цифр и checksum; ТН ВЭД char(10), PN text без потери регистра/символов.
Foreign keys с RESTRICT для истории. Файлы хранятся вне webroot, в БД — hash, размер, тип, относительный storage key.

| Таблицы | Ключевые поля / связи / ограничения |
|---|---|
| organizations | id, inn UNIQUE, name, active |
| users, groups, sessions | user.group_id, enabled, auth_version; hash секрета; session token_hash UNIQUE, expires, csrf, device_id |
| user_organizations, assignments | user↔organization; user/group↔warehouse_task, явное назначение |
| permissions, group_grants | id, administrative; UNIQUE(group_id,permission_id), scope own/assigned/all, revision |
| policy_versions, policy_entries | immutable version, actor, reason, parent; typed values; operation.policy_version_id |
| suppliers | id, canonical name, aliases/merge history; FK из накладных |
| products, product_pns | один product на подтверждённый GTIN; PN-алиасы с provenance; разные PN для GTIN допустимы с предупреждением |
| nk_cards, nk_snapshots | product_id nullable до GTIN, remote_good_id UNIQUE по connection, external_status/checked_at, raw snapshot, local revision/dirty |
| nk_templates, nk_batches, nk_batch_items | неизменяемые версии шаблона; построчный progress, generated_gtin, external_operation_id |
| codes | UNIQUE(product_group,cis), product_id, external status/owner/quantity/checked_at, local_state; криптохвост не ключ товара |
| code_payload_versions | code_id, version, raw bytea, normalized bytea, hashes, normalization_profile, quantity; UNIQUE(code_id,version), immutable |
| code_events | code_id, cause_operation_id, before/after, observed_at; не перезаписывает историю |
| documents, document_lines | org_id, owner_id, supplier_id, number/date, workflow_state, revision, stopped_at; line.product_id, pn snapshot, units, labels |
| code_allocations | code_id, document_line_id, quantity, revision, active; происхождение сохраняется при перераспределении |
| batches, batch_documents | общий supplier batch; готовность всех накладных поставщика |
| atks, atk_versions, atk_items | внешний ATK text case-sensitive; immutable состав версии; code_id+source_line_id; code не в двух активных ATK |
| atk_transformations, transformation_items | base_version, adding/removing, prepared_by, approved_by, operation_id; запрет устаревшей версии |
| warehouse_tasks, task_versions | document_id, kind, org_id, assignment, source_revision, expected_hash, active_version, scan_revision, state |
| warehouse_expected | task_version_id, code_id, payload_version_id, source_line_id, quantity; UNIQUE(task_version_id,code_id) |
| active_code_claims | code_id, claim_class, task_id; UNIQUE(code_id,claim_class) для несовместимых активных заданий |
| warehouse_scans | task_id, code_id, raw/normalized bytes, actor/device, request_id; UNIQUE(task_id,code_id), UNIQUE(device_id,request_id) |
| warehouse_checks, check_items | task_version_id, scan_revision, policy_version, started/finished, facts, outcome; только совпавшие ревизии завершают |
| discrepancies, incidents, comments | task/code/document, type, assignee, lifecycle, resolution; аудит всех переходов |
| split_operations, code_reservations | source_code_id, quantity, request_key UNIQUE, body_hash, new_code_id, state; unknown резерв сохраняет |
| label_versions, print_jobs, print_events | payload_version_id, template_version, PDF file_id/hash, PN/quantity snapshot; copies, reason, user; download≠printed |
| external_operations, operation_attempts | idempotency_key UNIQUE(org,kind,key), exact payload bytea/hash, state, external_id, policy_version, target_revision |
| operation_dependencies, operation_receipts | причинные связи; raw ответ, correlation id, received_at, результат интерпретации |
| signer_jobs | operation_id, payload_hash, certificate, lease_until, lease_token, fence, signature_file; один активный lease |
| files, audit_events, integration_connections | org/owner/reference ACL; immutable audit; secret reference, environment и contract version |

## Транзакционные границы

1. Передача: SELECT document FOR UPDATE → verify finalized composition → создать task/version/expected/claims/outbox event → commit.
   UNIQUE(document_id,kind,source_revision) возвращает существующее задание при повторном запросе.
2. Split: lock codes(source) → сравнить внешний baseline и активные резервы → вставить reserve и operation атомарно.
   CHECK не может проверить сумму других строк: все изменения резервов сериализуются на том же codes row.
3. Scan: lock task → проверить active_version → idempotent insert и bump scan_revision → commit.
4. Check: сохранить snapshot двух ревизий до HTTP; после ответа сохранить результат всегда, применять только при прежних версиях.
5. Complete: lock task → подтвердить успешный check на active_version+scan_revision, все количества и permissions → commit.
6. ATK transform: lock atk → base_version и внешний статус → операция; новый состав только после подтверждения результата.
7. Worker claim: FOR UPDATE SKIP LOCKED и CAS state+lease/fence. Не держать транзакцию/row lock на время HTTP.

Проекции количества не суммируют сканы как штуки: сумма quantityInPack или подтверждённых expected.quantity.
Для незнакомых сторонних КМ code.product_id допускает последующее связывание с верифицированным товаром; карточку НК не выдумывать.
Ссылки organization_id проверять и в сервисах, и составными FK для критических связей между документами/заданиями.
