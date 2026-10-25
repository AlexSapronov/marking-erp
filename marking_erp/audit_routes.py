"""Administrative metadata view of the global append-only audit; never return detail."""
from datetime import datetime
from uuid import UUID
from fastapi import Depends, HTTPException, Query
from sqlalchemy import text

ACTION_LABELS = {
    'rbac.initialized': 'Инициализированы группы и права',
    'administrator.bootstrapped': 'Создан первый администратор',
    'login.failed': 'Неудачный вход',
    'session.created': 'Вход выполнен',
    'session.revoked': 'Выход из системы',
    'sessions.revoked_all': 'Завершены собственные сессии',
    'session.reauthenticated': 'Подтверждён административный доступ',
    'user.password_changed': 'Изменён собственный пароль',
    'user.created': 'Создан пользователь',
    'user.updated': 'Изменён пользователь',
    'user.credentials_reset': 'Обновлены данные входа',
    'sessions.revoked_by_administrator': 'Администратор завершил сессии',
    'configuration.group': 'Изменены права группы',
    'configuration.rules': 'Изменены бизнес-правила',
    'code.payload.downloaded': 'Скачан полный КМ',
    'file.downloaded': 'Скачан файл',
    'signer.provisioned': 'Зарегистрирован подписант',
    'signer.revoked': 'Отозван подписант',
    'system.restored': 'Восстановлена резервная копия',
}
GROUPS = {'bm', 'logistics', 'warehouse', 'tsd', 'specialist', 'administrator'}
MAX_ID = 2**63 - 1


def public_event(row):
    action = row['action'] if row['action'] in ACTION_LABELS else 'unknown'
    entity = None
    # login.failed contains an internal throttle key. Future event types and
    # arbitrary entity text must not acquire disclosure through this endpoint.
    if action != 'unknown' and action != 'login.failed':
        try:
            entity = str(UUID(row['entity']))
        except (ValueError, TypeError, AttributeError):
            if (action == 'configuration.group' and row['entity'] in GROUPS) or (
                    action == 'configuration.rules' and row['entity'] == 'system') or (
                    action == 'system.restored' and row['entity'] == 'backup'):
                entity = row['entity']
    return {'id': str(row['id']), 'created_at': row['created_at'], 'action': action,
            'label': ACTION_LABELS.get(action, 'Неизвестный тип события'),
            'actor_id': row['actor_id'], 'entity': entity}


def install_audit(app, actor):
    @app.get('/api/audit')
    def events(action: str | None = Query(default=None, max_length=80),
               actor_id: UUID | None = None, since: datetime | None = None,
               until: datetime | None = None,
               before_id: int | None = Query(default=None, ge=1, le=MAX_ID),
               current=Depends(actor, scope='function')):
        current.require('audit.view', administrative=True)
        organizations = set(current.connection.execute(text('SELECT id FROM organizations WHERE enabled')).scalars())
        if not organizations.issubset(current.organizations()):
            raise HTTPException(403, 'Журнал аудита требует доступа ко всем действующим организациям')
        if action is not None and action not in ACTION_LABELS:
            raise HTTPException(422, 'Неизвестный тип события')
        if any(value is not None and value.utcoffset() is None for value in (since, until)):
            raise HTTPException(422, 'Укажите часовой пояс периода')
        if since and until and since > until:
            raise HTTPException(422, 'Начало периода позже окончания')
        clauses, params = [], {}
        for name, column, operator, value in (
                ('action', 'action', '=', action), ('actor', 'actor_id', '=', actor_id),
                ('since', 'created_at', '>=', since), ('until', 'created_at', '<=', until),
                ('before', 'id', '<', before_id)):
            if value is not None:
                clauses.append(f'{column} {operator} :{name}')
                params[name] = value
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        rows = current.connection.execute(text(
            'SELECT id,created_at,action,actor_id,entity FROM audit_events' + where +
            ' ORDER BY id DESC LIMIT 26'), params).mappings().all()
        return {'events': [public_event(row) for row in rows[:25]],
                'next_before_id': str(rows[24]['id']) if len(rows) > 25 else None,
                'actions': ACTION_LABELS}
