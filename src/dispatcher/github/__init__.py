"""Read-only GitHub REST adapter. No issue text is ever used as a command."""
import json
import re
import subprocess
import time
from urllib.parse import urlencode


class GitHubError(RuntimeError):
    pass


def valid_branch(value):
    return (isinstance(value, str) and bool(value) and not value.startswith(('-', '/', '.'))
            and not value.endswith(('/', '.', '.lock')) and '..' not in value and '@{' not in value
            and value != '@' and not re.search(r'[\s\x00-\x1f\x7f~^:?*\[\\]', value)
            and all(part and not part.startswith('.') and not part.endswith('.lock') for part in value.split('/')))


class Adapter:
    def __init__(self, executable='gh', run=subprocess.run, sleep=time.sleep, attempts=3):
        self.executable, self.run, self.sleep, self.attempts = executable, run, sleep, attempts

    def request(self, endpoint):
        for attempt in range(self.attempts):
            try:
                response = self.run([self.executable, 'api', '--method', 'GET', '--include', endpoint],
                                    capture_output=True, text=True, timeout=30, shell=False)
                raw = response.stdout or ''
                headers, separator, body = raw.replace('\r\n', '\n').partition('\n\n')
                if not separator or not headers.startswith('HTTP/'):
                    if response.returncode:
                        raise OSError('GitHub transport failure')
                    raise GitHubError('Invalid GitHub response')
                status = int(headers.splitlines()[0].split()[1])
                if response.returncode == 0 and status == 200:
                    try:
                        return json.loads(body)
                    except ValueError:
                        raise GitHubError('Invalid GitHub JSON') from None
                if status not in (403, 429) and status < 500:
                    raise GitHubError('GitHub authorization or request failure')
                delay = min(2 ** attempt, 60)
                for line in headers.splitlines()[1:]:
                    key, _, value = line.partition(':')
                    if key.lower() in ('retry-after', 'x-ratelimit-reset'):
                        try:
                            seconds = float(value)
                            if key.lower() == 'x-ratelimit-reset':
                                seconds -= time.time()
                            delay = max(delay, min(max(seconds, 0), 60))
                        except ValueError:
                            pass
            except (OSError, subprocess.TimeoutExpired):
                delay = 2 ** attempt
            except (ValueError, IndexError):
                raise GitHubError('Invalid GitHub headers') from None
            if attempt + 1 < self.attempts:
                self.sleep(delay)
        raise GitHubError('GitHub unavailable after bounded retries')

    def fetch(self, project):
        """Fetch all open issues, including non-ready ones for safe revocation.

        Explicit page requests avoid trusting arbitrary Link URLs. A full page
        requires a subsequent page; any failure discards the entire project.
        """
        issues = {}
        for page in range(1, 10001):
            data = self.request(f'repos/{project.repository}/issues?' + urlencode(dict(state='open', per_page=100, page=page)))
            if not isinstance(data, list) or len(data) > 100:
                raise GitHubError('Invalid GitHub issue page')
            for raw in data:
                try:
                    if not isinstance(raw, dict):
                        raise ValueError
                    if 'pull_request' in raw:
                        continue
                    number = raw['number']
                    if type(number) is not int or number <= 0 or type(raw['id']) is not int:
                        raise ValueError
                    expected = f'https://api.github.com/repos/{project.repository}/issues/{number}'
                    if raw['url'].casefold() != expected.casefold():
                        raise GitHubError('Issue repository mismatch')
                    labels = sorted(label['name'] for label in raw['labels'])
                    author = raw['user']['login']
                    title, body = raw['title'], raw.get('body') or ''
                    if not all(isinstance(v, str) for v in (title, body, author, raw['updated_at'], raw['html_url'], raw['state'], *labels)):
                        raise ValueError
                    # A declarative field, never an instruction or shell fragment.
                    branches = re.findall(r'^(?:Target branch|Целевая ветка):[ \t]*(.*?)[ \t]*$', body, re.MULTILINE | re.IGNORECASE)
                    if len(branches) > 1:
                        raise GitHubError('Ambiguous target branch')
                    branch = branches[0] if branches else project.base_branch
                    if not valid_branch(branch):
                        raise GitHubError('Invalid target branch')
                    item = dict(id=raw['id'], number=number, repository=project.repository.casefold(),
                                project_id=project.id, title=title, body=body, author=author,
                                state=raw['state'], labels=labels, updated_at=raw['updated_at'],
                                url=raw['html_url'], target_branch=branch)
                    item['eligible'] = (raw['state'] == 'open'
                        and project.task_label.casefold() in {label.casefold() for label in labels}
                        and author.casefold() in {a.casefold() for a in project.allowed_authors})
                    if number in issues:
                        raise GitHubError('Duplicate issue during pagination')
                    issues[number] = item
                except (KeyError, TypeError, ValueError, AttributeError):
                    raise GitHubError('Invalid GitHub issue') from None
            if len(data) < 100:
                return list(issues.values())
        raise GitHubError('Pagination limit exceeded')


def authenticated(run=subprocess.run, executable="gh"):
    """Stage 1 authorization diagnostic, retained for compatibility."""
    return run([executable, 'auth', 'status'], capture_output=True, timeout=15).returncode == 0
