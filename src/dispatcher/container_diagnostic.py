"""Durable container diagnostics, delivered only after preparation reconciliation.

The caller holds the project container lock across observation and delivery.
Only allowlisted text and numeric Issue references cross the GitHub boundary.
"""
import fcntl
import json
from pathlib import Path
from dispatcher.github import GitHubError

TRANSIENT = frozenset(('container_github_unavailable', 'fetch_failed', 'clone_url_unavailable',
                       'git_command_failed', 'git_unavailable', 'child_body_unconfirmed'))
GUIDANCE = {
    'no_diff': 'Ветка контейнера не отличается от закреплённой base. Добавьте реальные изменения; искусственный коммит не создаётся.',
    'branch_ownership_conflict': 'Ветка уже существует или закреплена за другой задачей. Изменение branch в этой Issue может быть отклонено: прежнее имя уже сохранено. Пока нет безопасного подтверждённого reset-claim, создайте новую Issue с новым именем.',
    'container_branch_conflict': 'Canonical branch этой Issue уже закреплена. Не меняйте YAML branch повторно; создайте новую Issue с новым именем. Поддерживаемого reset-claim пока нет.',
    'child_body_conflict': 'Автоматическое обновление дочерней Issue остановлено. Проверьте её изменения и параметры base_branch; не перезаписывайте body без проверки.',
    'invalid_branch': 'Проверка имени dispatcher.branch или base_branch не пройдена. Проверьте оба значения командой git check-ref-format --branch локально и исправьте недопустимое имя; для закреплённой ветки создайте новую Issue.',
}

SAFE_CODES = frozenset(('container_stale', 'origin_changed', 'remote_head_changed',
    'default_branch_protected', 'child_snapshot_unavailable', 'child_identity_conflict',
    'child_ready_removed', 'child_state_conflict', 'child_stopped', 'child_untrusted',
    'child_active_base_conflict', 'child_snapshot_stale', 'child_relation_conflict',
    'child_update_forbidden', 'child_relation_removed', 'child_collection_changed',
    'child_detached_with_open_pr', 'child_merge_unverified', 'child_merge_wrong_base',
    'child_pr_identity_conflict', 'child_cancelled_with_open_pr',
    'child_body_cas_unavailable', 'base_branch_missing', 'invalid_issue_parameters')) | TRANSIENT | GUIDANCE.keys()


def reconcile(store, adapter, row):
    db = store.db
    current = store.get(row['repository'], row['number'])
    if not current or store.stop_requested(current) or current['current_iteration_id'] != row['current_iteration_id'] or current['version'] != row['version']:
        return
    blockers = db.execute('SELECT * FROM container_blockers WHERE container_issue_id=? AND active=1 ORDER BY blocker_key', (row['issue_id'],)).fetchall()
    recovered = current['state'] == 'awaiting_children' and not blockers
    if current['state'] not in ('pending', 'awaiting_children'):
        return
    signature = json.dumps([(b['blocker_key'], b['code']) for b in blockers]) if not recovered else 'recovered'
    previous = db.execute('SELECT * FROM container_diagnostic_state WHERE issue_id=?', (row['issue_id'],)).fetchone()
    if not blockers and not recovered or recovered and previous is None:
        return
    count = previous['observations'] + 1 if previous and previous['signature'] == signature else 1
    transient = blockers and all(b['code'] in TRANSIENT for b in blockers)
    with db:
        db.execute('INSERT INTO container_diagnostic_state VALUES(?,?,?) ON CONFLICT(issue_id) DO UPDATE SET signature=excluded.signature,observations=excluded.observations', (row['issue_id'], signature, count))
        latest = db.execute('SELECT * FROM container_diagnostic_outbox WHERE issue_id=? ORDER BY id DESC LIMIT 1', (row['issue_id'],)).fetchone()
        if recovered and latest is None:
            return  # A short transient outage was never announced.
        if (not transient or count >= 3) and (latest is None or latest['signature'] != signature):
            if recovered:
                body = 'Блокировка подготовки устранена: внешние ресурсы проверены. Подготовка возобновлена, статус awaiting_children.'
            else:
                lines = [f'Проверка контейнера заблокирована. Статус: {current["state"]}.']
                for b in blockers:
                    code = b['code']
                    safe_code = code if code in SAFE_CODES else 'container_preparation_blocked'
                    stage = 'подготовка дочерних Issue' if b['child_issue_id'] else 'проверка контейнера и ветки'
                    child = db.execute('SELECT number FROM tasks WHERE issue_id=?', (b['child_issue_id'],)).fetchone()
                    context = f' Дочерняя Issue #{child[0]}.' if child else ''
                    pr = db.execute('SELECT pr_number FROM task_pull_requests WHERE issue_id=?', (b['child_issue_id'],)).fetchone()
                    if pr:
                        context += f' PR: https://github.com/{row["repository"]}/pull/{pr[0]}.'
                    action = GUIDANCE.get(code, 'Временная недоступность внешнего сервиса; повтор будет выполнен автоматически.' if code in TRANSIENT else 'Проверьте локальную диагностику через show и устраните конфликт перед повторной подготовкой.')
                    if code == 'child_merge_wrong_base' and b['details'] != code:
                        action = b['details'].replace('`', '').replace('\n', ' ')
                    lines.append(f'Код: {safe_code}. Этап: {stage}.{context} {action}')
                body = '\n\n'.join(lines)
            db.execute('INSERT INTO container_diagnostic_outbox(issue_id,signature,body) VALUES(?,?,?)', (row['issue_id'], signature, body))
    deliver(store, adapter, row)


def deliver(store, adapter, row):
    database = Path(store.db.execute('PRAGMA database_list').fetchone()[2])
    with (database.parent / 'container-diagnostic.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        if store.stop_requested(store.get(row['repository'], row['number'])):
            return
        _deliver(store, adapter, row)


def flush(store, adapter, projects):
    """Retry already reconciled, immutable events, including after transition."""
    for row in store.db.execute("SELECT DISTINCT t.* FROM tasks t JOIN container_diagnostic_outbox o ON o.issue_id=t.issue_id WHERE o.delivered=0 AND t.state!='awaiting_children'").fetchall():
        if any(p.repository.casefold() == row['repository'].casefold() for p in projects):
            deliver(store, adapter, row)


def _deliver(store, adapter, row):
    db = store.db
    endpoint = f'repos/{row["repository"]}/issues/{row["number"]}/comments'
    for event in db.execute('SELECT * FROM container_diagnostic_outbox WHERE issue_id=? AND delivered=0 ORDER BY id', (row['issue_id'],)).fetchall():
        marker = f'<!-- codex-container-diagnostic:{row["issue_id"]}:{event["id"]} -->'
        try:
            comments = adapter.pages(endpoint)
            if not isinstance(comments, list) or any(not isinstance(c, dict) or not isinstance(c.get('body', ''), str) for c in comments):
                raise GitHubError(kind='invalid_response')
            found = next((c for c in comments if marker in c.get('body', '')), None)
            if found is None:
                found = adapter.write(endpoint, 'POST', dict(body=event['body'] + '\n' + marker))
            if not isinstance(found, dict) or type(found.get('id')) is not int:
                raise GitHubError(kind='invalid_response')
            with db:
                db.execute('UPDATE container_diagnostic_outbox SET delivered=1,comment_id=? WHERE id=?', (found['id'], event['id']))
        except GitHubError:
            break
