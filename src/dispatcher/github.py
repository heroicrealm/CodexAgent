"""All GitHub HTTP effects go through this injectable adapter."""
from datetime import datetime
from contextvars import ContextVar
import json
import re
import subprocess
import time
from urllib.parse import urlencode, quote
from .error_diagnostics import OperationError
from .config import Config, Project
from .input import INPUT_MARKER, OUTPUT_MARKER
from .models import InputComment, IssueObservation, GitHubState, PRObservation, PRStatus


POLL_ID = ContextVar('github_poll_id', default=None)


class GitHubError(OperationError):
    def __init__(self, status: int | None = None, **diagnostics):
        self.status = status
        super().__init__(f'GitHub request failed (status={status})', code='github_http_error',
                         operation='github.request', **diagnostics)


class GhTransport:
    def __call__(self, method: str, endpoint: str, payload: dict | None):
        args = ['gh', 'api', '--method', method, endpoint]
        if payload is not None:
            args += ['--input', '-']
        try:
            result = subprocess.run(args, input=json.dumps(payload) if payload is not None else None,
                                    capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            raise GitHubError(stdout=getattr(exc, 'stdout', None), stderr=getattr(exc, 'stderr', None)) from exc
        if result.returncode:
            status = re.search(r'HTTP (\d{3})', result.stderr)
            raise GitHubError(int(status[1]) if status else None, stdout=result.stdout,
                              stderr=result.stderr, returncode=result.returncode)
        try:
            return json.loads(result.stdout) if result.stdout.strip() else None
        except ValueError as exc:
            raise GitHubError(stdout=result.stdout, stderr=result.stderr, returncode=result.returncode) from exc


class GitHubAdapter:
    def __init__(self, config: Config, logger, transport=None, retries: int = 2):
        self.config, self.logger = config, logger
        self.transport = transport or GhTransport()
        self.retries = retries

    def request(self, method, endpoint, *, payload=None, poll_id=None, route='/repos/{owner}/{repo}'):
        poll_id = poll_id or POLL_ID.get()
        for attempt in range(self.retries + 1):
            start = time.monotonic()
            status = None
            result = 'ok'
            try:
                return self.transport(method, endpoint, payload)
            except GitHubError as exc:
                status, result = exc.status, 'failed'
                # Never retry an uncertain POST; publisher reconciles first.
                if method != 'GET' or status not in {429, 502, 503, 504} or attempt == self.retries:
                    raise
            except Exception:
                result = 'failed'
                raise
            finally:
                self.logger.record('api', 'GitHubAdapter', 'request', {
                    'poll_id': poll_id, 'method': method, 'endpoint': route,
                    'duration_ms': int((time.monotonic() - start) * 1000), 'http_status': status}, result)
            time.sleep(min(2 ** attempt, 4))

    def pages(self, endpoint: str, *, poll_id=None, route='/repos/{owner}/{repo}') -> list:
        result, page = [], 1
        while True:
            sep = '&' if '?' in endpoint else '?'
            values = self.request('GET', f'{endpoint}{sep}per_page=100&page={page}', poll_id=poll_id, route=route)
            if not isinstance(values, list):
                raise GitHubError()
            result.extend(values)
            if len(values) < 100:
                return result
            page += 1

    @staticmethod
    def issue_observation(data, repository):
        return IssueObservation(data['id'], repository, data['number'], data['title'],
            GitHubState(data['state']), frozenset(label['name'] for label in data.get('labels', [])), data.get('body') or '')

    def issues(self, project: Project, poll_id):
        return tuple(self.issue_observation(item, project.repository) for item in
            self.pages(f'repos/{project.repository}/issues?state=open', poll_id=poll_id, route='/repos/{owner}/{repo}/issues')
            if 'pull_request' not in item)

    def issue(self, project: Project, number: int, poll_id=None):
        value = self.request('GET', f'repos/{project.repository}/issues/{number}', poll_id=poll_id,
                             route='/repos/{owner}/{repo}/issues/{number}')
        repository = value.get('repository_url', '').split('/repos/')[-1]
        if repository and repository.casefold() != project.repository.casefold():
            raise ValueError('GitHub repository identity mismatch')
        if 'pull_request' in value:
            raise ValueError('correction_for must reference an Issue, not a PR')
        return self.issue_observation(value, project.repository)

    def parent(self, project: Project, number: int, poll_id):
        try:
            value = self.request('GET', f'repos/{project.repository}/issues/{number}/parent', poll_id=poll_id,
                                 route='/repos/{owner}/{repo}/issues/{number}/parent')
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise
        repository = value.get('repository_url', '').split('/repos/')[-1]
        if repository.lower() != project.repository.lower():
            raise ValueError('Cross-repository Sub-issues require explicit project mapping')
        return self.issue_observation(value, repository)

    def children(self, project, number, poll_id):
        return tuple(self.issue_observation(item, project.repository) for item in self.pages(
            f'repos/{project.repository}/issues/{number}/sub_issues', poll_id=poll_id,
            route='/repos/{owner}/{repo}/issues/{number}/sub_issues'))

    def input_comment(self, project, number, poll_id=None):
        comments = self.pages(f'repos/{project.repository}/issues/{number}/comments', poll_id=poll_id,
                              route='/repos/{owner}/{repo}/issues/{number}/comments')
        matches = [c for c in comments if INPUT_MARKER in (c.get('body') or '')]
        if len(matches) > 1:
            raise ValueError('Multiple INPUT comments; keep one marked INPUT')
        return InputComment(matches[0]['id'], matches[0]['body']) if matches else None

    @staticmethod
    def pr_observation(value, issue_id, repository):
        status = 'merged' if value.get('merged_at') else value['state']
        return PRObservation(value['id'], issue_id, repository, value['number'],
            value['head']['ref'], value['base']['ref'], PRStatus(status), value.get('merged_at'), value['head'].get('sha'))

    def pr(self, project, number, issue_id, poll_id=None):
        return self.pr_observation(self.request('GET', f'repos/{project.repository}/pulls/{number}',
            poll_id=poll_id, route='/repos/{owner}/{repo}/pulls/{number}'), issue_id, project.repository)

    def find_pr(self, project, branch, issue_id, poll_id=None, *, since=None, excluded=frozenset()):
        query = urlencode({'state': 'all', 'head': project.repository.split('/')[0] + ':' + branch})
        values = self.pages(f'repos/{project.repository}/pulls?{query}', poll_id=poll_id,
                            route='/repos/{owner}/{repo}/pulls')
        matches = [v for v in values if v['id'] not in excluded
                   and (not since or not v.get('created_at') or datetime.fromisoformat(v['created_at']) >= datetime.fromisoformat(since))
                   and v['head']['ref'] == branch
                   and (v['head'].get('repo') or {}).get('full_name', '').lower() == project.repository.lower()]
        if len(matches) > 1:
            raise ValueError('Multiple candidate PRs for this execution')
        return self.pr_observation(matches[0], issue_id, project.repository) if matches else None

    def create_pr(self, project, issue, branch, base):
        value = self.request('POST', f'repos/{project.repository}/pulls', payload={
            'title': issue['approved_title'], 'body': f"Dispatcher result for #{issue['issue_number']}",
            'head': branch, 'base': base}, route='/repos/{owner}/{repo}/pulls')
        return self.pr_observation(value, issue['issue_id'], project.repository)

    def output(self, project, issue, body):
        body = OUTPUT_MARKER + '\n' + body
        comment_id = issue['output_comment_id']
        endpoint = (f'repos/{project.repository}/issues/comments/{comment_id}' if comment_id else
                    f"repos/{project.repository}/issues/{issue['issue_number']}/comments")
        value = self.request('PATCH' if comment_id else 'POST', endpoint, payload={'body': body},
                             route='/repos/{owner}/{repo}/issues/comments')
        return value['id'], body

    def comment(self, project, number, body):
        return self.request('POST', f'repos/{project.repository}/issues/{number}/comments',
                            payload={'body': body}, route='/repos/{owner}/{repo}/issues/comments')

    def remove_label(self, project, number, label, poll_id=None):
        try:
            self.request('DELETE', f'repos/{project.repository}/issues/{number}/labels/{quote(label, safe="")}',
                         poll_id=poll_id, route='/repos/{owner}/{repo}/issues/{number}/labels')
        except GitHubError as exc:
            if exc.status != 404:
                raise

    def sync_labels(self, project, number, state, observed=None, poll_id=None):
        labels = self.config.effective_labels(project)
        if observed is None:
            observed = frozenset(v['name'] for v in self.pages(f'repos/{project.repository}/issues/{number}/labels',
                poll_id=poll_id, route='/repos/{owner}/{repo}/issues/{number}/labels'))
        statuses = set(labels.values()) - {labels['codex-ready'], labels['approve']}
        target = self.config.status_label(project, state)
        for label in sorted(statuses & set(observed) - ({target} if target else set())):
            self.remove_label(project, number, label, poll_id)
        if target and target not in observed:
            self.request('POST', f'repos/{project.repository}/issues/{number}/labels', payload={'labels': [target]},
                         poll_id=poll_id, route='/repos/{owner}/{repo}/issues/{number}/labels')

    def close_issue(self, project, number, state):
        self.request('PATCH', f'repos/{project.repository}/issues/{number}', payload={
            'state': 'closed', 'state_reason': 'completed' if state == 'done' else 'not_planned'},
            route='/repos/{owner}/{repo}/issues/{number}')

    def initialize_labels(self, project):
        existing = {x['name'] for x in self.pages(f'repos/{project.repository}/labels')}
        for name in sorted(set(self.config.effective_labels(project).values()) - existing):
            self.request('POST', f'repos/{project.repository}/labels', payload={'name': name, 'color': 'ededed'},
                         route='/repos/{owner}/{repo}/labels')
