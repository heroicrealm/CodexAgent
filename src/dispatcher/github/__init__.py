"""Bounded GitHub adapter. No issue text is ever used as a command."""
import json
import re
import subprocess
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode


class GitHubError(RuntimeError):
    """Only structured, allowlisted metadata may reach operator diagnostics."""
    def __init__(self, message='GitHub request failed', *, status=None, kind='request'):
        super().__init__(message)
        self.status = status if type(status) is int and 100 <= status <= 599 else None
        self.kind = kind

    def safe_reason(self):
        meanings = {401: 'authentication failed', 403: 'forbidden (permissions, policy or rate limit)',
                    404: 'resource not found or access denied', 422: 'request validation failed',
                    429: 'rate limited'}
        if self.status is not None:
            return f'HTTP {self.status}: ' + meanings.get(self.status, 'GitHub request failed')
        return {'timeout': 'request timed out', 'transport': 'GitHub transport unavailable',
                'invalid_response': 'invalid GitHub response'}.get(self.kind, 'GitHub request failed')


def valid_branch(value):
    return (isinstance(value, str) and bool(value) and not value.startswith(('-', '/', '.'))
            and not value.endswith(('/', '.', '.lock')) and '..' not in value and '@{' not in value
            and value != '@' and not re.search(r'[\s\x00-\x1f\x7f~^:?*\[\\]', value)
            and all(part and not part.startswith('.') and not part.endswith('.lock') for part in value.split('/')))


class Adapter:
    def __init__(self, executable='gh', run=subprocess.run, sleep=time.sleep, attempts=3):
        self.executable, self.run, self.sleep, self.attempts = executable, run, sleep, attempts
        self.response_date = None

    def request(self, endpoint):
        last_status, last_kind = None, "transport"
        for attempt in range(self.attempts):
            self.response_date = None
            try:
                response = self.run([self.executable, 'api', '--method', 'GET', '--include', '-H', 'Cache-Control: no-cache', endpoint],
                                    capture_output=True, text=True, timeout=30, shell=False)
                raw = response.stdout or ''
                headers, separator, body = raw.replace('\r\n', '\n').partition('\n\n')
                if not separator or not headers.startswith('HTTP/'):
                    if response.returncode:
                        raise OSError('GitHub transport failure')
                    raise GitHubError('Invalid GitHub response', kind='invalid_response')
                status = int(headers.splitlines()[0].split()[1])
                last_status, last_kind = status, 'http'
                for header in headers.splitlines()[1:]:
                    key, _, value = header.partition(':')
                    if key.casefold() == 'date':
                        try:
                            stamp = parsedate_to_datetime(value.strip())
                            if stamp.tzinfo is not None:
                                self.response_date = stamp.isoformat()
                        except (ValueError, TypeError, OverflowError):
                            pass
                if response.returncode == 0 and status == 200:
                    try:
                        return json.loads(body)
                    except ValueError:
                        raise GitHubError('Invalid GitHub JSON', kind='invalid_response') from None
                if status not in (403, 429) and status < 500:
                    raise GitHubError('GitHub authorization or request failure', status=status)
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
            except (OSError, subprocess.TimeoutExpired) as exc:
                last_status, last_kind = None, "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "transport"
                delay = 2 ** attempt
            except (ValueError, IndexError):
                raise GitHubError('Invalid GitHub headers', kind='invalid_response') from None
            if attempt + 1 < self.attempts:
                self.sleep(delay)
        raise GitHubError('GitHub unavailable after bounded retries', status=last_status, kind=last_kind)

    def repository_identity(self, project):
        from dispatcher.storage.transitions import rest_id
        raw = self.request(f'repos/{project.repository}')
        if (not isinstance(raw, dict) or not rest_id(raw.get('id'))
                or str(raw.get('full_name', '')).casefold() != project.repository.casefold()):
            raise GitHubError('Unresolved repository identity', kind='invalid_response')
        return raw['id']

    def fetch(self, project):
        """Fetch all open issues, including non-ready ones for safe revocation.

        Explicit page requests avoid trusting arbitrary Link URLs. A full page
        requires a subsequent page; any failure discards the entire project.
        """
        repository_id = self.repository_identity(project)
        issues = {}
        for page in range(1, 10001):
            data = self.request(f'repos/{project.repository}/issues?' + urlencode(dict(state='open', per_page=100, page=page)))
            observed_at = self.response_date
            if not isinstance(data, list) or len(data) > 100:
                raise GitHubError('Invalid GitHub issue page')
            for raw in data:
                try:
                    if not isinstance(raw, dict):
                        raise ValueError
                    if 'pull_request' in raw:
                        continue
                    number = raw['number']
                    if type(number) is not int or number <= 0 or type(raw['id']) is not int or not 0 < raw['id'] <= 9223372036854775807:
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
                    item = dict(id=raw['id'], repository_id=repository_id, number=number, repository=project.repository.casefold(),
                                project_id=project.id, title=title, body=body, author=author,
                                state=raw['state'], labels=labels, updated_at=raw['updated_at'],
                                url=raw['html_url'], target_branch=branch, server_observed_at=observed_at)
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

    def write(self, endpoint, method, payload):
        """Bounded write with safe HTTP metadata, never raw gh stderr/body."""
        try:
            reply = self.run([self.executable, 'api', '--method', method, '--include', endpoint, '--input', '-'],
                             input=json.dumps(payload), capture_output=True, text=True, timeout=30, shell=False)
            headers, separator, body = (reply.stdout or '').replace('\r\n', '\n').partition('\n\n')
            if not separator or not headers.startswith('HTTP/'):
                raise GitHubError(kind='transport' if reply.returncode else 'invalid_response')
            status = int(headers.splitlines()[0].split()[1])
            if reply.returncode or not 200 <= status < 300:
                raise GitHubError(status=status)
            return json.loads(body) if body.strip() else None
        except subprocess.TimeoutExpired:
            raise GitHubError(kind='timeout') from None
        except OSError:
            raise GitHubError(kind='transport') from None
        except (ValueError, IndexError):
            raise GitHubError(kind='invalid_response') from None

    def pages(self, endpoint):
        result = []
        for page in range(1, 10001):
            separator = '&' if '?' in endpoint else '?'
            data = self.request(f'{endpoint}{separator}per_page=100&page={page}')
            if not isinstance(data, list) or len(data) > 100:
                raise GitHubError('Invalid GitHub page')
            result.extend(data)
            if len(data) < 100:
                return result
        raise GitHubError('Pagination limit exceeded')

    def label_page(self, endpoint):
        data = self.pages(endpoint)
        if any(not isinstance(v, dict) or not isinstance(v.get('name'), str) for v in data):
            raise GitHubError('Invalid GitHub labels')
        return data

    def labels(self, project):
        return self.label_page(f'repos/{project.repository}/labels')

    def command_events(self, project, number):
        """REST supplies authenticated event actors; GraphQL supplies edit time.

        Without lastEditedAt we cannot bind a label to a content version safely.
        A bot is never an authorized operator, even when its login is allowlisted.
        """
        owner, repo = project.repository.split('/')
        query = ('query($owner:String!,$repo:String!,$number:Int!){repository(owner:$owner,name:$repo)'
                 '{issue(number:$number){databaseId lastEditedAt title body state author{login} labels(first:100){nodes{name} pageInfo{hasNextPage}}}}}')
        result = self.write('graphql', 'POST', dict(query=query, variables=dict(owner=owner, repo=repo, number=number)))
        try:
            if result.get('errors'):
                raise KeyError
            issue = result['data']['repository']['issue']
            if (not isinstance(issue['title'], str) or not isinstance(issue['body'], str)
                    or type(issue['databaseId']) is not int
                    or not isinstance(issue['author']['login'], str)
                    or issue['state'] not in ('OPEN', 'CLOSED')
                    or type(issue['labels']['pageInfo']['hasNextPage']) is not bool
                    or any(not isinstance(label['name'], str) for label in issue['labels']['nodes'])
                    or (issue['lastEditedAt'] is not None and not isinstance(issue['lastEditedAt'], str))):
                raise KeyError
            # Read history after the content proof, covering title/ready churn
            # between the REST snapshot and GraphQL read (including edit/revert).
            events = self.pages(f'repos/{project.repository}/issues/{number}/events')
            return events, issue
        except (KeyError, TypeError, AttributeError):
            raise GitHubError('Cannot verify command version') from None

    def reconcile(self, project, row):
        """Only touch configured indicator/command labels, preserving ready and others."""
        from urllib.parse import quote
        from dispatcher.config import COLORS
        endpoint = f'repos/{project.repository}/issues/{row["number"]}/labels'
        current = self.label_page(endpoint)
        desired = project.label(row['state']).name
        managed = {project.label(k).name.casefold() for k in COLORS if k != 'ready'}
        present = {v['name'].casefold(): v['name'] for v in current}
        for folded, name in present.items():
            if folded in managed and folded != desired.casefold():
                self.write(endpoint + '/' + quote(name, safe=''), 'DELETE', {})
        if desired.casefold() not in present:
            self.write(endpoint, 'POST', dict(labels=[desired]))


def authenticated(run=subprocess.run, executable="gh"):
    """Stage 1 authorization diagnostic, retained for compatibility."""
    return run([executable, 'auth', 'status'], capture_output=True, timeout=15).returncode == 0
