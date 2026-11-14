"""Rich human screens and lossless JSON documents."""
import json
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


def age(value):
    if value is None:
        return 'N/A'
    return f'{int(value // 86400)}d' if value >= 86400 else f'{int(value // 3600)}h' if value >= 3600 else f'{int(value // 60)}m'


def table(console, headings, rows):
    t = Table(expand=True)
    for h in headings:
        t.add_column(h, justify='right' if h in ('№', 'Open', 'Disc.', 'Appr.', 'Review', 'Reject', 'Error', 'Issue', 'PR', 'Age') else 'left',
                     no_wrap=True, overflow='ellipsis')
    for row in rows:
        t.add_row(*(Text(str(v if v is not None else 'N/A')) for v in row))
    console.print(t)


MESSAGES = {'discovered': 'Ожидание подтверждения', 'pending': 'В очереди',
    'awaiting-children': 'Ожидание блокирующих дочерних Issue', 'preparing': 'Подготовка worktree',
    'running': 'Исполнение Codex', 'manual-running': 'Внешняя работа',
    'need-fix': 'Требуется корректирующая Issue', 'cancelled': 'Исполнение остановлено'}


def diagnostic_message(raw):
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and 'exceptions' in parsed:
            return '\n'.join(str(e.get('message', '')) for e in parsed['exceptions'])
    except (ValueError, TypeError):
        pass
    return raw


def compact_errors(rows):
    return [{**r, 'error_message': diagnostic_message(r['error_message'])} if r.get('error_message') else r for r in rows]


def render(command, data, *, as_json=False, console=None, config=None, state=None, verbose=False):
    if as_json:
        print(json.dumps(data, ensure_ascii=False))
        return
    c = console or Console()
    if command == 'status':
        active = data['executing']
        execution = 'IDLE'
        if active:
            elapsed = active['elapsed_seconds']
            clock = 'N/A' if elapsed is None else f'{int(elapsed)//3600:02}:{int(elapsed)%3600//60:02}:{int(elapsed)%60:02}'
            execution = f"{active['project_id']} #{active['issue_number']} {clock}" + ('' if active['verified_live'] else ' (unverified)')
        c.print(Panel(Text(f"Daemon: {data['daemon']['state']}\nDatabase: {data['database']} · Recovery: {data['recovery']}\nLast poll: {data['last_poll'] or 'N/A'}\nNext poll: {data['next_poll'] or 'N/A'}\nExecuting: {execution}"), title='CODEX DISPATCHER · STATUS'))
        keys = ('open', 'discovered', 'pending', 'review', 'rejected', 'error')
        rows = [[n, p['project'] + ('' if p['enabled'] else ' (disabled)'), *(p[k] for k in keys)] for n, p in enumerate(data['projects'], 1)]
        rows.append(['', 'TOTAL', *(sum(p[k] for p in data['projects']) for k in keys)])
        table(c, ('№', 'Project', 'Open', 'Disc.', 'Appr.', 'Review', 'Reject', 'Error'), rows)
    elif command == 'queue':
        projects = list(dict.fromkeys(r['project'] for r in data['issues']))
        for p in projects:
            c.print(Panel(Text(p + (f' · {state}' if state else ''))))
            rows = []
            for r in data['issues']:
                if r['project'] != p:
                    continue
                rows.append([f"#{r['issue_number']}", r['title'], *([] if state else [r['state']]),
                             ', '.join(f"#{pr['pr_number']}" for pr in r['pull_requests']) or '—', age(r['age_seconds'])])
            table(c, ['Issue', 'Title', *([] if state else ['State']), 'PR', 'Age'], rows)
        if not projects:
            c.print('Queue is empty')
    elif command == 'show':
        d = data
        c.print(Panel(Text(f"#{d['issue_number']} {d['title']}\nState: {d['state']}"), title='Issue'))
        e = d['executions'][-1] if d['executions'] else {}
        details = f"Project: {d['project']}\nRepository: {d['repository']}\nIssue: #{d['issue_number']}\nState: {d['state']}\nBranches: {d['branches']}\nPR: " + ', '.join(f"#{pr['pr_number']} {pr['status']}" for pr in d['pull_requests'])
        details += f"\nExecution: {e.get('execution_id', 'N/A')} · {e.get('status', 'N/A')}\nStarted: {e.get('started_at') or 'N/A'}\nFinished: {e.get('finished_at') or 'N/A'}\nExit code: {e.get('exit_code', 'N/A')}\nApproved: {d['approved_at'] or 'N/A'}\nApproved hash: {d['approved_hash'] or 'N/A'}"
        c.print(Panel(Text(details), title='Project / Execution'))
        result = MESSAGES.get(d['state'], d['result'] if d['result'] is not None else 'Результат недоступен')
        if d['state'] in ('error', 'rejected'):
            result += '\n' + '\n'.join(diagnostic_message(r['error_message']) for r in d['error_log'][-1:])
        if d['state'] == 'rejected':
            reasons = [r['reason'] for r in d['state_history'] if r['new_state'] == 'rejected']
            result += '\nRejection: ' + (reasons[-1] if reasons else 'N/A')
        c.print(Panel(Text(result), title='Execution Result'))
        if d['state'] == 'awaiting-children':
            c.print('Blocking Children')
            table(c, ['Issue', 'Title', 'State'], [[f"#{r['issue_number']}", r['title'], r['state']] for r in d['blocking_children']])
        if verbose:
            timeline = sorted([dict(kind='state', **r) for r in d['state_history']] + [dict(kind='recovery', **r) for r in d['recovery_events']], key=lambda r: r['created_at'])
            for execution in d['executions']:
                for key in ('started_at', 'finished_at', 'codex_started_at', 'codex_finished_at'):
                    if execution[key]:
                        timeline.append(dict(kind='execution', event=key, created_at=execution[key], execution_id=execution['execution_id']))
            for pr in d['pull_requests']:
                for key in ('updated_at', 'merged_at'):
                    if pr[key]:
                        timeline.append(dict(kind='PR', event=key, created_at=pr[key], pr_number=pr['pr_number'], status=pr['status']))
            timeline.sort(key=lambda r: r['created_at'])
            c.print(Panel(Text(json.dumps(timeline, ensure_ascii=False, indent=2)), title='Timeline'))
            c.print(Panel(Text(json.dumps(compact_errors(d['executions']), ensure_ascii=False, indent=2)), title='Execution details'))
            c.print(Panel(Text(json.dumps({'error_log': compact_errors(d['error_log']), 'recovery_quarantine': d['recovery_quarantine']}, ensure_ascii=False, indent=2)), title='Diagnostics'))
