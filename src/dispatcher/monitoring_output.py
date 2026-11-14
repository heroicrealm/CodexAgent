"""Rich human screens and lossless JSON documents."""
import json
from datetime import datetime, timezone
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


PALETTE = dict.fromkeys(('discovered', 'cancelled', 'IDLE'), 'grey50')
PALETTE.update(dict.fromkeys(('pending', 'awaiting-children', 'preparing', 'UNKNOWN'), 'yellow'))
PALETTE.update(dict.fromkeys(('running', 'manual-running'), 'cyan'))
PALETTE.update(dict.fromkeys(('review', 'done', 'RUNNING', 'OK', 'READY', 'enabled'), 'green'))
PALETTE.update(dict.fromkeys(('rejected', 'error', 'STOPPED', 'BLOCKED', 'disabled'), 'red'))
PALETTE['need-fix'] = 'dark_orange'


def colored(value):
    return Text(str(value), style=PALETTE.get(str(value), ''))


def local_time(value):
    if not value:
        return 'N/A'
    try:
        return datetime.fromisoformat(value).astimezone().strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError):
        return str(value)


def labeled(lines):
    result = Text()
    for label, value in lines:
        if result:
            result.append('\n')
        result.append(label + ': ')
        result.append(value if isinstance(value, Text) else Text(str(value)))
    return result


def human_details(value):
    if value is None or value == '':
        return 'N/A'
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return value
    if isinstance(value, dict):
        return '; '.join(f'{k}: {human_details(v)}' for k, v in value.items()) or 'N/A'
    if isinstance(value, list):
        return ', '.join(human_details(v) for v in value) or 'N/A'
    return str(value)


def timestamp_key(row):
    try:
        return datetime.fromisoformat(row['created_at']).astimezone(timezone.utc).timestamp()
    except (KeyError, ValueError, TypeError):
        return float('inf')


def age(value):
    if value is None:
        return 'N/A'
    return f'{int(value // 86400)}d' if value >= 86400 else f'{int(value // 3600)}h' if value >= 3600 else f'{int(value // 60)}m'


def table(console, headings, rows):
    t = Table(expand=True)
    for h in headings:
        t.add_column(h, justify='right' if h in ('№', 'Open', 'Disc.', 'Appr.', 'Review', 'Reject', 'Error', 'Issue', 'PR', 'Age') else 'left',
                     overflow='fold')
    for row in rows:
        cells = []
        for heading, value in zip(headings, row):
            if not isinstance(value, Text):
                value = value if value is not None else 'N/A'
                value = colored(value) if heading in ('State', 'Status') else Text(str(value))
            cells.append(value)
        t.add_row(*cells)
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
        c.print(Panel(labeled([
            ('Daemon', colored(data['daemon']['state'])),
            ('Database', Text(data['database'], style='green' if data['database'] == 'OK' else 'red')), ('Recovery', colored(data['recovery'])),
            ('Last poll', local_time(data['last_poll'])), ('Next poll', local_time(data['next_poll'])),
            ('Executing', Text(execution, style='cyan' if active else 'grey50')),
        ]), title='CODEX DISPATCHER · STATUS'))
        keys = ('open', 'discovered', 'pending', 'review', 'rejected', 'error')
        rows = [[n, Text(p['project']).append(Text(' (disabled)', style='red') if not p['enabled'] else Text()), *(p[k] for k in keys)] for n, p in enumerate(data['projects'], 1)]
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
        heading = Text(f"#{d['issue_number']} {d['title']}\nState: ")
        heading.append(colored(d['state']))
        c.print(Panel(heading, title='Issue'))
        e = d['executions'][-1] if d['executions'] else {}
        c.print(Panel(labeled([
            ('Project', d['project']), ('Repository', d['repository']), ('Issue', f"#{d['issue_number']}"),
            ('State', colored(d['state'])), ('Base branch', d['branches'].get('base_branch') or 'N/A'),
            ('Work branch', d['branches'].get('work_branch') or 'N/A'),
            ('PR', ', '.join(f"#{pr['pr_number']} {pr['status']}" for pr in d['pull_requests']) or 'N/A'),
            ('Execution', f"{e.get('execution_id', 'N/A')} · {e.get('status', 'N/A')}"),
            ('Started', local_time(e.get('started_at'))), ('Finished', local_time(e.get('finished_at'))),
            ('Exit code', e.get('exit_code') if e.get('exit_code') is not None else 'N/A'),
            ('Approved', local_time(d['approved_at'])), ('Approved hash', d['approved_hash'] or 'N/A'),
        ]), title='Project / Execution'))
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
            timeline = [dict(kind='state', **r) for r in d['state_history']] + [dict(kind='recovery', **r) for r in d['recovery_events']]
            for execution in d['executions']:
                for key in ('started_at', 'finished_at', 'codex_started_at', 'codex_finished_at'):
                    if execution[key]:
                        timeline.append(dict(kind='execution', event=key, created_at=execution[key], execution_id=execution['execution_id']))
            for pr in d['pull_requests']:
                for key in ('updated_at', 'merged_at'):
                    if pr[key]:
                        timeline.append(dict(kind='PR', event=key, created_at=pr[key], pr_number=pr['pr_number'], status=pr['status']))
            timeline.sort(key=timestamp_key)
            c.print('Timeline')
            rows = []
            for r in timeline:
                kind = r['kind']
                event = r.get('event', r.get('event_type', r.get('action', 'recovery')))
                details = r.get('reason') or r.get('details') or ''
                if kind == 'state':
                    event = f"{r.get('old_state') or 'N/A'} → {r['new_state']}"
                elif kind == 'execution':
                    event = {'started_at': 'started', 'finished_at': 'finished', 'codex_started_at': 'codex-start', 'codex_finished_at': 'codex-finish'}[event]
                    details = f"Execution {r['execution_id']}"
                elif kind == 'PR':
                    event = event.removesuffix('_at')
                    details = f"#{r['pr_number']} {r['status']}"
                details = human_details(details)
                rows.append([local_time(r.get('created_at')), kind, event, details])
            table(c, ['Time', 'Type', 'Event / Transition', 'Details'], rows)
            c.print('Execution details')
            fields = [('execution_id', 'Execution ID'), ('status', 'Status'), ('branch_name', 'Branch'),
                      ('started_at', 'Started'), ('finished_at', 'Finished'),
                      ('codex_started_at', 'Codex started'), ('codex_finished_at', 'Codex finished'),
                      ('exit_code', 'Exit code'), ('error_code', 'Error code'), ('error_message', 'Error'),
                      ('initial_commit', 'Initial commit'), ('result_commit', 'Result commit'),
                      ('push_completed', 'Push publication'), ('pr_published', 'PR publication'),
                      ('comment_published', 'Comment publication'), ('worktree_path', 'Worktree path')]
            if not d['executions']:
                c.print('No executions.')
            for execution in compact_errors(d['executions']):
                rows = []
                for key, label in fields:
                    if key not in execution:
                        continue
                    value = execution[key]
                    if key.endswith('_at'):
                        value = local_time(value)
                    elif value is None or value == '':
                        value = 'N/A'
                    rows.append([label, value])
                table(c, ['Parameter', 'Value'], rows)
            c.print('Diagnostics')
            if not d['error_log'] and not d['recovery_quarantine']:
                c.print('No errors or recovery quarantine records.')
            if d['error_log']:
                table(c, ['Time', 'Stage', 'Code', 'Message'], [
                    [local_time(r.get('created_at')), r.get('error_stage'), r.get('error_code'),
                     diagnostic_message(r.get('error_message'))] for r in d['error_log']])
            if d['recovery_quarantine']:
                table(c, ['Time', 'Reason', 'Details'], [
                    [local_time(r.get('created_at')), r.get('reason_code'), human_details(r.get('details'))] for r in d['recovery_quarantine']])
