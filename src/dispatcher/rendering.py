"""Local CLI presentation. JSON preserves the public SQLite row contract."""
import json
import os
import shutil
import sys
import unicodedata


STATUS_COLORS = {
    'discovered': '36', 'pending': '33', 'running': '34', 'review': '35',
    'done': '32', 'failed': '31', 'rejected': '31', 'revoked': '90',
    'cancelled': '90', 'interrupted': '31',
}


def display_task(row):
    value = dict(row)
    for key in ('snapshot', 'observed'):
        try:
            value[key] = json.dumps(json.loads(value[key]), ensure_ascii=False)
        except (KeyError, ValueError, TypeError):
            pass
    return value


def snapshot(row):
    try:
        value = json.loads(row.get('snapshot'))
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def safe_text(value):
    """Keep body layout but neutralize terminal control sequences from stored text."""
    return ''.join(c if c in '\n\t' or unicodedata.category(c) not in ('Cc', 'Cs', 'Zl', 'Zp') else '\ufffd'
                   for c in str(value))


def width(text):
    return sum(0 if unicodedata.combining(c) else
               2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in text)


def clip(text, limit):
    if width(text) <= limit:
        return text
    result = ''
    for c in text:
        if width(result + c) > limit - 1:
            break
        result += c
    return result + '…'


def color_enabled(stream):
    return stream.isatty() and 'NO_COLOR' not in os.environ and os.environ.get('TERM') not in (None, '', 'dumb')


def border(widths, left, junction, right):
    return left + junction.join('─' * (size + 2) for size in widths) + right


def framed_row(cells):
    # Cells may already contain renderer-owned ANSI; padding is done beforehand.
    return '│ ' + ' │ '.join(cells) + ' │'


def wrap_text(text, limit):
    """Wrap by display cells without dropping whitespace or logical blank lines."""
    for logical in safe_text(text).expandtabs(4).split('\n'):
        line = ''
        used = 0
        for char in logical:
            size = width(char)
            if used + size > limit and line:
                yield line
                line, used = '', 0
            line += char
            used += size
        yield line


def render_frame(sections, stream):
    limit = max(2, shutil.get_terminal_size(fallback=(100, 24)).columns - 4)
    sections = [[line for text in section for line in wrap_text(text, limit)]
                for section in sections]
    size = max(2, max((width(line) for section in sections for line in section), default=0))
    print(border([size], '┌', '┬', '┐'), file=stream)
    for index, section in enumerate(sections):
        if index:
            print(border([size], '├', '┼', '┤'), file=stream)
        for line in section:
            print(framed_row([line + ' ' * (size - width(line))]), file=stream)
    print(border([size], '└', '┴', '┘'), file=stream)


def render_queue(rows, projects, format='table', stream=None):
    stream = stream if stream is not None else sys.stdout
    if format == 'json':
        print(json.dumps(rows, ensure_ascii=False), file=stream)
        return
    if not rows:
        render_frame([['Queue is empty.']], stream)
        return
    cells = [['Issue', 'Project', 'Title', 'Status']]
    for row in rows:
        cells.append([f'#{row["number"]}', projects[row['repository']],
                      snapshot(row).get('title', '(title unavailable)'), row['state']])
    cells = [[' '.join(safe_text(c).split()) for c in row] for row in cells]
    widths = [max(width(row[i]) for row in cells) for i in range(4)]
    available = max(17, shutil.get_terminal_size(fallback=(100, 24)).columns)
    while sum(widths) + 13 > available:
        index = max(range(4), key=lambda i: widths[i])
        if widths[index] <= 1:
            break
        widths[index] -= 1
    colors = color_enabled(stream)
    print(border(widths, '┌', '┬', '┐'), file=stream)
    for index, row in enumerate(cells):
        rendered = []
        for col, cell in enumerate(row):
            value = clip(cell, widths[col])
            padded = value + ' ' * (widths[col] - width(value))
            if index and col == 3 and colors and cell in STATUS_COLORS:
                padded = f'\033[{STATUS_COLORS[cell]}m{padded}\033[0m'
            rendered.append(padded)
        print(framed_row(rendered), file=stream)
    print(border(widths, '└', '┴', '┘'), file=stream)


# Explicit fields avoid dumping internal snapshots, remote credentials or process data.
DETAIL_FIELDS = {
    'decisions': ('id', 'version', 'action', 'source', 'result', 'event_id', 'created_at'),
    'command_diagnostics': ('action', 'event_id', 'reason'),
    'attempts': ('attempt_id', 'iteration_id', 'previous_attempt_id', 'version', 'branch', 'state', 'started_at', 'finished_at',
                 'duration', 'initial_sha', 'result_sha', 'remote_sha', 'exit_code',
                 'error_code', 'category', 'codex_started', 'pushed', 'recovery_diagnostic'),
    'pull_requests': ('pr_id', 'pr_number', 'iteration_id', 'url', 'state', 'head', 'head_sha', 'merged_at'),
    'pr_sync_operations': ('operation_key', 'iteration_id', 'phase', 'outcome', 'retry_count', 'updated_at'),
    'pr_resolutions': ('pr_id', 'target', 'metadata', 'issue_closed_at', 'comment_id', 'error'),
    'quota': ('state', 'observed_at', 'diagnostic'),
    'result_publication': ('attempt_id', 'published', 'comment_id'),
}


def render_show(value, format='table', stream=None):
    stream = stream if stream is not None else sys.stdout
    if format == 'json':
        print(json.dumps(value, ensure_ascii=False), file=stream)
        return
    issue = snapshot(value)
    header = [f'Project: {value.get("project_id", "(unavailable)")}',
              f'Repository: {value.get("repository", "(unavailable)")}']
    lines = [f'Issue: #{value.get("number", "(unavailable)")}']
    for label, data, key in [('Title', issue, 'title'), ('Status', value, 'state'),
                             ('Branch', issue, 'target_branch'), ('Author', issue, 'author'),
                             ('Version', value, 'version'), ('Current iteration', value, 'current_iteration_id'), ('Approved version', value, 'approved_version'),
                             ('Available', value, 'available'), ('Created', value, 'created_at'),
                             ('Synced', value, 'synced_at')]:
        if data.get(key) is not None:
            lines.append(f'{label}: {data[key]}')
    body = issue.get('body')
    metadata = lines
    body_lines = ['Issue body:', body if isinstance(body, str) else '(body unavailable)']
    lines = []
    for section, fields in DETAIL_FIELDS.items():
        if value.get(section):
            lines.extend(['', section.replace('_', ' ').capitalize() + ':'])
            records = value[section]
            if not isinstance(records, (list, tuple)):
                records = [records]
            for record in records:
                if not isinstance(record, dict):
                    continue
                lines.append('  ' + ', '.join(f'{key}={record[key]}' for key in fields
                                              if record.get(key) is not None))
    render_frame([header, metadata, body_lines, lines], stream)
