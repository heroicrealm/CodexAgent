"""Initial container preparation, without execution or PR publication.

Issue PATCH is not atomic CAS. Durable intents detect observed conflicts but
cannot exclude a lost human edit in the final GET -> PATCH window.
"""
import fcntl
import hashlib
import json
import os
from dispatcher.github import GitHubError
from dispatcher.issue_parameters import parse, with_base_branch
from dispatcher.storage import now
from dispatcher.storage.transitions import Authority, Evidence
from dispatcher.workspace import TaskError, private_dir, safe_path


def child_snapshot(raw):
    """Project shared significant fields, including for legacy durable intents."""
    try:
        user = raw['user']
        labels = tuple(sorted({label['name'].casefold() for label in raw['labels']}))
        return dict(id=raw['id'], number=raw['number'],
                    repository_url=raw['repository_url'].casefold(),
                    state=raw['state'], title=raw['title'],
                    author=user['login'].casefold(), author_id=user.get('id'),
                    labels=labels, body=raw.get('body') or '',
                    updated_at=raw['updated_at'])
    except (KeyError, TypeError, AttributeError):
        raise TaskError('child_body_conflict') from None


class ContainerService:
    def __init__(self, store, adapter, git):
        self.store, self.adapter, self.git = store, adapter, git

    def block(self, row, code, child=None):
        with self.store.db as db:
            db.execute('''INSERT INTO container_blockers VALUES(?,?,?,?,?,1,?)
                ON CONFLICT(container_issue_id,blocker_key) DO UPDATE SET
                code=excluded.code,active=1,updated_at=excluded.updated_at''',
                (row['issue_id'], str(child or 'parent'), child, code, code, now()))

    def guard(self, project, expected):
        current = self.store.get(project.repository, expected['number'])
        if (not current or self.store.stop_requested(current)
                or current['issue_type'] != 'container' or current['current_iteration_id'] is None
                or not current['available'] or current['state'] != 'pending'
                or current['version'] != expected['version']
                or current['approved_version'] != expected['version']
                or current['current_iteration_id'] != expected['current_iteration_id']):
            raise TaskError('container_stale')
        raw = self.adapter.request(f'repos/{project.repository}/issues/{current["number"]}')
        if (not isinstance(raw, dict) or not isinstance(raw.get('labels'), list)
                or any(not isinstance(label, dict) or not isinstance(label.get('name'), str)
                       for label in raw['labels'])
                or not isinstance(raw.get('user'), dict)):
            raise GitHubError(kind='invalid_response')
        issue = json.loads(current['snapshot'])
        names = {label['name'].casefold() for label in raw.get('labels', [])}
        if (raw.get('id') != current['issue_id'] or raw.get('number') != current['number']
                or raw.get('repository_url', '').casefold() != f'https://api.github.com/repos/{project.repository}'.casefold()
                or raw.get('state') != 'open' or raw.get('body', '') != issue['body']
                or raw.get('title') != issue['title']
                or raw.get('updated_at') != issue.get('updated_at')
                or raw.get('user', {}).get('login', '').casefold() != issue['author'].casefold()
                or project.task_label.casefold() not in names
                or project.label('emergency_stop').name.casefold() in names
                or issue['author'].casefold() not in {a.casefold() for a in project.allowed_authors}):
            raise TaskError('container_stale')
        return raw

    def operation(self, row, kind, payload, phase='intent', remote=None):
        key = f'container:{row["issue_id"]}:{row["current_iteration_id"]}:{kind}'
        encoded = json.dumps(payload, sort_keys=True)
        with self.store.db as db:
            db.execute('''INSERT INTO container_operations VALUES(?,?,?,?,NULL,?,?,?, ?,0,?,?)
                ON CONFLICT(operation_key) DO UPDATE SET payload_hash=excluded.payload_hash,
                payload=excluded.payload,phase=excluded.phase,outcome=excluded.outcome,
                remote_identity=excluded.remote_identity,updated_at=excluded.updated_at''',
                (key, row['issue_id'], row['current_iteration_id'], kind,
                 hashlib.sha256(encoded.encode()).hexdigest(), encoded, phase,
                 'success' if phase == 'confirmed' else 'pending', remote, now()))
        return key

    def update_child(self, project, row, managed, child, branch):
        """Reconcile a durable intent before attempting a single PATCH."""
        digest = lambda body: hashlib.sha256(body.encode()).hexdigest()
        kind = 'child:' + str(child['id'])
        key = f'container:{row["issue_id"]}:{row["current_iteration_id"]}:{kind}'
        previous = self.store.db.execute('SELECT * FROM container_operations WHERE operation_key=?', (key,)).fetchone()
        # Upgrade proposal-only intents from the previous fail-closed release.
        if previous:
            legacy = json.loads(previous['payload'])
            if ('original' not in legacy and previous['phase'] == 'intent'
                    and legacy.get('body_hash') == digest(child.get('body') or '')
                    and legacy.get('base_branch') == branch):
                previous = None
        observation = dict(self.store.guard(project.repository, child['number']))
        accepted = json.loads(managed['snapshot'])
        if not previous and (accepted['body'] != (child.get('body') or '')
                             or any(accepted.get(k) != child.get(k) for k in ('title', 'updated_at'))):
            raise TaskError('child_snapshot_stale')
        if accepted['author'].casefold() != child['user']['login'].casefold():
            raise TaskError('child_snapshot_stale')
        proof = json.loads(previous['payload']) if previous else dict(
            child_id=child['id'], iteration_id=row['current_iteration_id'],
            version=managed['version'], observation=observation, local=dict(managed), original=dict(child),
            source_hash=digest(child.get('body') or ''),
            target_hash=digest(with_base_branch(child.get('body') or '', branch)), base_branch=branch)
        if proof.get('base_branch') != branch or 'original' not in proof:
            raise TaskError('child_body_conflict')

        def local_guard():
            current = self.store.get(project.repository, child['number'])
            if (dict(self.store.guard(project.repository, child['number'])) != observation
                    or not current['available']
                    or any(current[k] != proof['local'][k] for k in proof['local']
                           if k not in ('synced_at', 'observed'))
                    or self.store.stop_requested(current)
                    or current['work_branch'] or current['state'] not in ('discovered', 'pending')
                    or self.store.db.execute('SELECT 1 FROM task_pull_requests WHERE issue_id=?', (child['id'],)).fetchone()):
                raise TaskError('child_body_conflict')

        def read():
            members = self.adapter.list_direct_children(project, row)
            if not any(c['id'] == child['id'] for c in members):
                raise TaskError('child_relation_conflict')
            fresh = self.adapter.request(f'repos/{project.repository}/issues/{child["number"]}')
            original = child_snapshot(proof['original'])
            observed = child_snapshot(fresh)
            if (any(observed[k] != original[k]
                    for k in original if k not in ('body', 'updated_at'))
                    or fresh.get('state') != 'open'
                    or fresh['user']['login'].casefold() not in {a.casefold() for a in project.allowed_authors}
                    or project.task_label.casefold() not in {v['name'].casefold() for v in fresh['labels']}
                    or project.label('emergency_stop').name.casefold() in {v['name'].casefold() for v in fresh['labels']}):
                raise TaskError('child_body_conflict')
            parse(fresh.get('body') or '')
            return fresh

        self.guard(project, row)
        fresh = read()
        body = fresh.get('body') or ''
        if digest(body) != proof['target_hash']:
            local_guard()
            if (digest(body) != proof['source_hash']
                    or fresh.get('updated_at') != proof['original'].get('updated_at')
                    or previous and previous['phase'] == 'confirmed'):
                raise TaskError('child_body_conflict')
            if not self.adapter.can_update_issue(project, child['number'], child['id']):
                raise TaskError('child_update_forbidden')
            self.operation(row, kind, proof)
            self.guard(project, row)
            fresh = read()
            if child_snapshot(fresh) != child_snapshot(proof['original']):
                raise TaskError('child_body_conflict')
            local_guard()
            target = with_base_branch(fresh.get('body') or '', branch)
            try:
                self.adapter.write(f'repos/{project.repository}/issues/{child["number"]}', 'PATCH', dict(body=target))
            except GitHubError:
                pass  # Only independent read-back can resolve an unknown outcome.
            fresh = read()
            body = fresh.get('body') or ''
        if digest(body) != proof['target_hash'] or parse(body)[0].get('base_branch') != branch:
            raise TaskError('child_body_unconfirmed')
        current = self.store.get(project.repository, child['number'])
        if json.loads(current['snapshot'])['body'] != body:
            local_guard()
        self.operation(row, kind, proof, 'confirmed', proof['target_hash'])
        accepted = json.loads(current['snapshot'])
        accepted.update(body=body, updated_at=fresh['updated_at'], server_observed_at=self.adapter.response_date)
        self.store.apply(project, [accepted], partial=True, controlled_operation=key)
        return fresh

    def prepare(self, project, row):
        root = self.git.config.workspace
        directory = private_dir(root, root / project.id)
        path = safe_path(root, directory / '.container.lock')
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            try:
                return self._prepare(project, row)
            except (TaskError, GitHubError) as exc:
                self.block(row, exc.code if isinstance(exc, TaskError) else 'container_github_unavailable')
        finally:
            self.git.operation_guard = None
            os.close(fd)

    def _prepare(self, project, row):
        if row['state'] != 'pending':
            return
        root = self.git.config.workspace
        self.guard(project, row)
        with self.store.db as db:
            db.execute("UPDATE container_blockers SET active=0 WHERE container_issue_id=? AND code IN ('container_stale','container_github_unavailable','fetch_failed','clone_url_unavailable','git_command_failed','git_unavailable')", (row['issue_id'],))
        params, _ = parse(json.loads(row['snapshot'])['body'])
        if params.get('issue_type') != 'container':
            raise TaskError('container_stale')
        issue = json.loads(row['snapshot'])
        branch = self.git.branch(project, issue)
        base = params.get('base_branch', project.base_branch)
        self.git.validate_branch(base)
        if row['work_branch'] and (row['work_branch'] != branch or row['base_branch'] != base):
            raise TaskError('container_branch_conflict')
        if not row['work_branch'] and not self.store.save_branches(project.repository, row['number'],
                expected_version=row['version'], expected_iteration_id=row['current_iteration_id'],
                expected=self.store.branches(project.repository, row['number']), base_branch=base,
                work_branch=branch, branch_source='explicit' if 'branch_name' in params else 'generated',
                branch_status='resolved'):
            raise TaskError('container_branch_conflict')
        # Persistent branch ownership is shared with ordinary task admission.
        if self.store.db.execute('SELECT 1 FROM tasks WHERE repository=? AND work_branch=? AND issue_id!=?',
                                 (row['repository'], branch, row['issue_id'])).fetchone():
            raise TaskError('branch_ownership_conflict')
        key = f'container:{row["issue_id"]}:{row["current_iteration_id"]}:branch'
        previous = self.store.db.execute('SELECT * FROM container_operations WHERE operation_key=?', (key,)).fetchone()
        payload = json.loads(previous['payload']) if previous else dict(branch=branch, base=base)
        if payload['branch'] != branch or payload['base'] != base:
            raise TaskError('container_branch_conflict')
        if not previous:
            self.operation(row, 'branch', payload)
        repo, _, _ = self.git.paths(project, row['number'])
        self.git.operation_guard = lambda: self.guard(project, row)
        if not repo.exists():
            # Clone bootstrap uses the same authenticated gh transport as workspaces.
            result = self.git.run([self.git.config.github_executable, 'repo', 'clone', project.repository,
                                   str(repo), '--', '--no-checkout'], capture_output=True, timeout=120,
                                  env=dict(os.environ, GH_PROMPT_DISABLED='1', GIT_TERMINAL_PROMPT='0'))
            if result.returncode:
                raise TaskError('clone_url_unavailable')
        safe_path(root, repo / '.git')
        if self.git.git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout.strip() != str(repo / '.git'):
            raise TaskError('container_branch_conflict')
        origin = self.git.git(repo, 'remote', 'get-url', 'origin').stdout.strip()
        origin_hash = hashlib.sha256(origin.encode()).hexdigest()
        if payload.get('origin_hash') and origin_hash != payload['origin_hash']:
            raise TaskError('origin_changed')
        self.git.git(repo, 'fetch', 'origin')
        ref = 'refs/heads/' + branch
        remote = self.git.git(repo, 'ls-remote', 'origin', ref).stdout.split()
        if branch in (project.base_branch, self.git.default_branch(project), base):
            raise TaskError('default_branch_protected')
        if 'sha' not in payload:
            if remote or not self.git.git(repo, 'show-ref', '--verify', ref, check=False).returncode:
                raise TaskError('branch_ownership_conflict')
            sha = self.git.git(repo, 'rev-parse', '--verify', f'refs/remotes/origin/{base}^{{commit}}').stdout.strip()
            payload.update(sha=sha, origin_hash=origin_hash)
            self.operation(row, 'branch', payload)
        if remote and remote != [payload['sha'], ref]:
            raise TaskError('remote_head_changed')
        if not remote:
            self.guard(project, row)
            # Create-only refs API: unlike a regular push this cannot advance
            # a foreign ref created between the absence check and publication.
            # On a lost response the durable intent is reconciled on restart.
            try:
                self.adapter.write(f'repos/{project.repository}/git/refs', 'POST',
                                   dict(ref=ref, sha=payload['sha']))
            except GitHubError as exc:
                if exc.status in (409, 422):
                    raise TaskError('branch_ownership_conflict') from None
                raise
        if self.git.git(repo, 'ls-remote', 'origin', ref).stdout.split() != [payload['sha'], ref]:
            raise TaskError('remote_head_changed')
        self.operation(row, 'branch', payload, 'confirmed', payload['sha'])
        children = self.adapter.list_direct_children(project, row)
        seen, blocked = set(), False
        for child in children:
            ready = project.task_label.casefold() in {l['name'].casefold() for l in child['labels']}
            managed = self.store.get(project.repository, child['number'])
            if not managed:
                if ready:
                    self.block(row, 'child_snapshot_unavailable')
                    blocked = True
                continue
            if managed['issue_id'] != child['id'] or managed['repository_id'] != row['repository_id']:
                raise TaskError('child_identity_conflict')
            seen.add(child['id'])
            code = None
            prepared = False
            participation = 'included' if ready else 'ignored'
            if not ready:
                participation = 'detached'
                code = 'child_ready_removed'
            elif child['state'] != 'open':
                code = 'child_state_conflict'
            elif self.store.stop_requested(managed):
                code = 'child_stopped'
            elif child['user']['login'].casefold() not in {a.casefold() for a in project.allowed_authors}:
                code = 'child_untrusted'
            else:
                accepted = json.loads(managed['snapshot'])
                if accepted['title'] != child.get('title'):
                    raise TaskError('child_snapshot_stale')
                child_params, _ = parse(child.get('body') or '')
                if (managed['work_branch'] or managed['state'] not in ('discovered', 'pending')
                        or self.store.db.execute('SELECT 1 FROM task_pull_requests WHERE issue_id=?', (child['id'],)).fetchone()):
                    if managed['base_branch'] != branch or child_params.get('base_branch') != branch:
                        code = 'child_active_base_conflict'
                elif (child_params.get('base_branch') != branch or accepted['body'] != (child.get('body') or '')
                      or self.store.db.execute(
                          "SELECT 1 FROM container_blockers WHERE container_issue_id=? AND blocker_key=? AND code='child_body_conflict' AND active=1",
                          (row['issue_id'], str(child['id']))).fetchone()):
                    try:
                        fresh = self.update_child(project, row, managed, child, branch)
                        children[children.index(child)] = fresh
                        prepared = True
                    except (TaskError, GitHubError) as exc:
                        code = exc.code if isinstance(exc, TaskError) else 'child_body_unconfirmed'
            if code:
                participation = 'blocked'
                self.block(row, code, child['id'])
                blocked = True
            with self.store.db as db:
                if not code:
                    db.execute("UPDATE container_blockers SET active=0 WHERE container_issue_id=? AND blocker_key=? AND code IN ('child_body_cas_unavailable','child_body_unconfirmed')",
                               (row['issue_id'], str(child['id'])))
                if prepared:
                    db.execute("UPDATE container_blockers SET active=0 WHERE container_issue_id=? AND blocker_key=? AND code='child_body_conflict'",
                               (row['issue_id'], str(child['id'])))
                db.execute('''INSERT INTO sub_issue_relations VALUES(?,?,?,?,?)
                    ON CONFLICT(parent_issue_id,child_issue_id) DO UPDATE SET
                    observed_at=excluded.observed_at,participation=excluded.participation,
                    conflict_code=excluded.conflict_code''',
                    (row['issue_id'], child['id'], now(), participation, code))
        for relation in self.store.db.execute('SELECT * FROM sub_issue_relations WHERE parent_issue_id=?', (row['issue_id'],)).fetchall():
            if relation['child_issue_id'] not in seen:
                self.block(row, 'child_relation_removed', relation['child_issue_id'])
                blocked = True
        if blocked:
            return
        if (sorted((child_snapshot(c) for c in self.adapter.list_direct_children(project, row)), key=lambda c: c['id'])
                != sorted((child_snapshot(c) for c in children), key=lambda c: c['id'])):
            raise TaskError('child_collection_changed')
        self.guard(project, row)
        # Blockers require explicit operator resolution in this initial stage.
        if self.store.db.execute('SELECT 1 FROM container_blockers WHERE container_issue_id=? AND active=1', (row['issue_id'],)).fetchone():
            return
        if self.git.git(repo, 'ls-remote', 'origin', ref).stdout.split() != [payload['sha'], ref]:
            raise TaskError('remote_head_changed')
        prepare_key = self.operation(row, 'prepare', dict(branch=branch, children=sorted(seen)), 'confirmed')
        stamp = self.adapter.response_date
        guard = self.store.guard(project.repository, row['number'])
        return self.store.transition(repository=project.repository, number=row['number'],
            expected_state='pending', version=row['version'], iteration_id=row['current_iteration_id'],
            target='awaiting_children', event_key=prepare_key,
            authority=Authority(issue['author'], project.allowed_authors),
            evidence=Evidence(row['repository_id'], row['issue_id'], row['number'], stamp, stamp,
                              guard['server_at'], row['content_hash'], row['current_iteration_id'],
                              'pending', container_operation=prepare_key))
