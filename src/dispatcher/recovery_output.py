"""Human recovery reports and the explicit, unstyled JSON interface."""
import json
import os

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


def accent(value):
    value = str(value)
    if value in ('completed', 'done', 'NO', 'READY'):
        style = 'green'
    elif value in ('YES', 'error', 'conflict', 'FAIL') or 'conflict' in value:
        style = 'red'
    elif value in ('quarantined', 'pending', 'warning'):
        style = 'yellow'
    else:
        style = 'cyan'
    return Text(value, style=style)


def issue_key(row):
    return row['repository'], row['issue_number'], row['issue_id']


def table(title, columns):
    result = Table(title=title, expand=True, show_lines=True)
    for column in columns:
        result.add_column(column, overflow='fold')
    return result


def admission(console, required):
    console.print(Text('Recovery required: ') + accent('YES' if required else 'NO'))
    console.print('Global admission: BLOCKED' if required else 'Global admission: READY',
                  style='red' if required else 'green')


def render_recovery(result, *, mode, as_json=False, dry_run=False, status=None, console=None):
    console = console or Console(highlight=False, color_system=None if 'NO_COLOR' in os.environ else 'auto')
    if as_json:
        # Bypass Rich entirely: one document, no wrapping, markup or ANSI.
        print(json.dumps(result, ensure_ascii=False), file=console.file)
        return
    console.print(Panel(Text('CODEX DISPATCHER · RECOVERY'), expand=True))
    if mode == 'status':
        console.print(f"Database version: {result['format_version']}")
        admission(console, result['recovery_required'])
        groups = {}
        for row in sorted(result['quarantine'], key=lambda r: (*issue_key(r), r['reason_code'])):
            groups.setdefault(issue_key(row), []).append(row['reason_code'])
        console.print(f'Quarantined issues: {len(groups)}')
        console.print(f"Unfinished operations: {sum(o['phase'] != 'completed' for o in result['operations'])}")
        if result.get('upgrade_required'):
            console.print('Database upgrade required', style='yellow')
        quarantine = table('QUARANTINED ISSUES', ('Repository', 'Issue', 'Reason(s)'))
        for (repository, number, _), reasons in groups.items():
            quarantine.add_row(Text(repository), Text(f'#{number}'), Text('\n'.join(sorted(set(reasons))), style='yellow'))
        console.print(quarantine if groups else Text('No quarantined issues'))
        operations = table('RECOVERY OPERATIONS', ('Repository', 'Issue', 'Action', 'Phase'))
        for row in sorted(result['operations'], key=lambda r: (*issue_key(r), r['started_at'], r['operation_id'])):
            operations.add_row(Text(row['repository']), Text(f"#{row['issue_number']}"),
                               Text(row['action']), accent(row['phase']))
        console.print(operations if result['operations'] else Text('No recovery operations'))
    elif mode == 'recover':
        console.print('DRY RUN / NO CHANGES' if dry_run else 'RECOVERY APPLIED', style='cyan' if dry_run else 'green')
        plan = table('RECOVERY PLAN' if dry_run else 'RECOVERY RESULTS', ('Repository', 'Issue', 'Target', 'Reason(s)'))
        for row in sorted(result, key=issue_key):
            plan.add_row(Text(row['repository']), Text(f"#{row['issue_number']}"), accent(row['target']),
                         Text('\n'.join(row['reasons']) or 'None', style='yellow' if row['reasons'] else 'green'))
        console.print(plan if result else Text('No issues to reconcile'))
        if status is not None:
            admission(console, status['recovery_required'])
            console.print(f"Quarantined issues: {len({q['issue_id'] for q in status['quarantine']})}")
    else:
        summary = table('RECOVERY OPERATION', ('Repository', 'Issue', 'Action', 'Phase'))
        summary.add_row(Text(result['repository']), Text(f"#{result['issue_number']}"),
                        Text(result['action']), accent(result['phase']))
        console.print(summary)
        console.print(Text(f"Operation: {result['operation_id']}"))
        admission(console, result['recovery_required'])
        reasons = sorted({q['reason_code'] for q in result['quarantine'] if q['issue_id'] == result['issue_id']})
        console.print(Text('Remaining quarantine reasons: ' + (', '.join(reasons) or 'None')))
        if result['action'] == 'retry' and result['phase'] == 'completed':
            console.print('New GitHub approve required before execution (dispatcher:approve)', style='yellow')
