"""SQLite repository primitives; no external effects or application orchestration."""
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path
import sqlite3
from collections.abc import Iterator
from .models import (ACTIVE, State, Execution, ExecutionStatus, IssueObservation,
                     PRObservation, utc_now)


class SchemaError(RuntimeError):
    pass


def _ddl() -> str:
    return files("dispatcher").joinpath("schema.sql").read_text()


def _signature(db: sqlite3.Connection) -> list[tuple]:
    return db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master "
                      "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()


def _validate(db: sqlite3.Connection) -> None:
    reference = sqlite3.connect(":memory:")
    try:
        reference.executescript(_ddl())
        if _signature(db) != _signature(reference):
            raise SchemaError("Incompatible V2 structure; migration is not supported")
        if db.execute("SELECT version FROM schema_migrations").fetchall() != [(1,)]:
            raise SchemaError("Unsupported V2 schema version")
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise SchemaError("Corrupt SQLite database")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise SchemaError("Broken foreign keys")
    except sqlite3.DatabaseError as exc:
        raise SchemaError("Invalid V2 database") from exc
    finally:
        reference.close()


class Storage:
    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        if self.path.exists() and self.path.stat().st_size:
            # Validate read-only BEFORE journal_mode or any other persistent change.
            try:
                with self._readonly() as db:
                    _validate(db)
            except sqlite3.DatabaseError as exc:
                raise SchemaError("Cannot open V2 database") from exc
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, isolation_level=None)
        try:
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT name FROM sqlite_master").fetchone():
                for statement in _ddl().split(';'):
                    if statement.strip():
                        db.execute(statement)
                db.execute("INSERT INTO schema_migrations VALUES (1,?)", (utc_now(),))
            _validate(db)
            db.commit()
            db.execute("PRAGMA journal_mode=WAL")
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def _readonly(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("PRAGMA journal_mode=WAL")
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def issue(self, issue_id: int) -> sqlite3.Row | None:
        with self.connection() as db:
            return db.execute("SELECT * FROM issues WHERE issue_id=?", (issue_id,)).fetchone()

    def _register(self, db: sqlite3.Connection, issue: IssueObservation, project_id: str,
                  correction_of: int | None) -> None:
        old = db.execute("SELECT * FROM issues WHERE issue_id=?", (issue.issue_id,)).fetchone()
        if old:
            if (old['repository'], old['issue_number'], old['project_id']) != (issue.repository, issue.issue_number, project_id):
                raise ValueError("Issue identity mismatch")
            if not old['is_virtual']:
                if old['correction_of_issue_id'] != correction_of:
                    raise ValueError("Correction origin is immutable")
                return
        group = issue.issue_id
        if correction_of is not None:
            source = db.execute("SELECT * FROM issues WHERE issue_id=?", (correction_of,)).fetchone()
            if (not source or source['is_virtual'] or source['repository'] != issue.repository
                    or source['state'] in (State.DONE, State.CANCELLED)):
                raise ValueError("Correction requires a real same-repository source")
            group = source['execution_group_id']
            root = db.execute("SELECT * FROM issues WHERE issue_id=?", (group,)).fetchone()
            if not root or root['repository'] != issue.repository or root['state'] in (State.DONE, State.CANCELLED):
                raise ValueError("Invalid or completed group")
            if not db.execute("SELECT 1 FROM issues WHERE execution_group_id=? AND state=?",
                              (group, State.NEED_FIX)).fetchone():
                raise ValueError("Group has no need-fix permission")
            seen = {issue.issue_id}
            cursor = source
            while cursor:
                if cursor['issue_id'] in seen:
                    raise ValueError("Correction cycle")
                seen.add(cursor['issue_id'])
                cursor = db.execute("SELECT * FROM issues WHERE issue_id=?", (cursor['correction_of_issue_id'],)).fetchone()
        now = utc_now()
        if old:
            db.execute("UPDATE issues SET is_virtual=0,state=?,is_ready=1,title=?,github_state=?,"
                       "execution_group_id=?,correction_of_issue_id=?,updated_at=?,last_seen_at=? WHERE issue_id=?",
                       (State.DISCOVERED, issue.title, issue.github_state, group, correction_of, now, now, issue.issue_id))
        else:
            db.execute("INSERT INTO issues(issue_id,repository,issue_number,project_id,execution_group_id,"
                       "correction_of_issue_id,state,title,github_state,is_ready,created_at,updated_at,last_seen_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,1,?,?,?)",
                       (issue.issue_id, issue.repository, issue.issue_number, project_id, group, correction_of,
                        State.DISCOVERED, issue.title, issue.github_state, now, now, now))
        db.execute("INSERT INTO state_history(issue_id,new_state,reason,created_at) VALUES (?,?,?,?)",
                   (issue.issue_id, State.DISCOVERED, "registration", now))

    def register_issue(self, issue: IssueObservation, project_id: str,
                       correction_of: int | None = None) -> None:
        with self.transaction() as db:
            self._register(db, issue, project_id, correction_of)

    def register_child(self, child: IssueObservation, parent: IssueObservation,
                       project_id: str, correction_of: int | None = None) -> None:
        if child.repository != parent.repository:
            raise ValueError("Parent must be in same repository")
        with self.transaction() as db:
            self._register(db, child, project_id, correction_of)
            row = db.execute("SELECT * FROM issues WHERE issue_id=?", (parent.issue_id,)).fetchone()
            if row and (row['repository'], row['issue_number'], row['project_id']) != (parent.repository, parent.issue_number, project_id):
                raise ValueError("Parent identity mismatch")
            if not row:
                now = utc_now()
                db.execute("INSERT INTO issues(issue_id,repository,issue_number,project_id,is_virtual,title,created_at,updated_at) "
                           "VALUES (?,?,?,?,1,?,?,?)", (parent.issue_id, parent.repository, parent.issue_number,
                                                      project_id, parent.title, now, now))
            db.execute("INSERT INTO issue_relations VALUES (?,?) ON CONFLICT DO NOTHING", (parent.issue_id, child.issue_id))

    def _transition(self, db: sqlite3.Connection, issue_id: int, expected: State,
                    target: State, reason: str) -> bool:
        if not isinstance(expected, State) or not isinstance(target, State):
            raise ValueError("Canonical State values required")
        if expected == target:
            return False
        row = db.execute("SELECT * FROM issues WHERE issue_id=?", (issue_id,)).fetchone()
        if not row or row['is_virtual'] or row['state'] != expected:
            return False
        if target in ACTIVE:
            others = db.execute("SELECT state FROM issues WHERE execution_group_id=? AND issue_id<>?",
                                (row['execution_group_id'], issue_id)).fetchall()
            if any(r['state'] in ACTIVE for r in others):
                raise ValueError("Group already has an active issue")
        now = utc_now()
        changed = db.execute("UPDATE issues SET state=?,updated_at=? WHERE issue_id=? AND state=?",
                             (target, now, issue_id, expected)).rowcount
        if changed:
            db.execute("INSERT INTO state_history(issue_id,old_state,new_state,reason,created_at) VALUES (?,?,?,?,?)",
                       (issue_id, expected, target, reason, now))
        return bool(changed)

    def transition(self, issue_id: int, expected: State, target: State, reason: str) -> bool:
        """CAS primitive. Caller StateService must validate business transition/approval."""
        with self.transaction() as db:
            return self._transition(db, issue_id, expected, target, reason)

    def reserve(self, issue_id: int) -> Execution | None:
        """Atomic storage reservation; application eligibility belongs to Scheduler."""
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM executions WHERE issue_id=?", (issue_id,)).fetchone():
                return None
            if not self._transition(db, issue_id, State.PENDING, State.PREPARING, "reservation"):
                return None
            cursor = db.execute("INSERT INTO executions(issue_id,status,started_at) VALUES (?,'reserved',?)", (issue_id, utc_now()))
            return Execution(cursor.lastrowid, issue_id, ExecutionStatus.RESERVED)

    def mark_codex_started(self, execution_id: int) -> bool:
        """Commit a conservative launch marker before any process creation."""
        with self.transaction() as db:
            return bool(db.execute("UPDATE executions SET codex_started_at=?,status='executing' "
                "WHERE execution_id=? AND codex_started_at IS NULL AND status IN ('reserved','preparing')",
                (utc_now(), execution_id)).rowcount)

    def record_pr(self, pr: PRObservation) -> None:
        with self.transaction() as db:
            issue = db.execute("SELECT repository FROM issues WHERE issue_id=?", (pr.issue_id,)).fetchone()
            if not issue or issue['repository'] != pr.repository:
                raise ValueError("PR repository mismatch")
            old = db.execute("SELECT issue_id,repository,pr_number FROM pull_requests WHERE pr_id=?", (pr.pr_id,)).fetchone()
            if old and tuple(old) != (pr.issue_id, pr.repository, pr.pr_number):
                raise ValueError("PR identity mismatch")
            db.execute("INSERT INTO pull_requests VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(pr_id) DO UPDATE SET "
                "head_branch=excluded.head_branch,base_branch=excluded.base_branch,status=excluded.status,"
                "merged_at=excluded.merged_at,updated_at=excluded.updated_at",
                (pr.pr_id, pr.issue_id, pr.repository, pr.pr_number, pr.head_branch, pr.base_branch,
                 pr.status, pr.merged_at, utc_now()))

    def record_error(self, issue_id: int, code: str, stage: str, safe_message: str,
                     execution_id: int | None = None) -> None:
        """Message must already be a safe application summary, never raw command output."""
        with self.transaction() as db:
            if execution_id is not None and not db.execute("SELECT 1 FROM executions WHERE execution_id=? AND issue_id=?",
                                                         (execution_id, issue_id)).fetchone():
                raise ValueError("Execution belongs to another issue")
            db.execute("INSERT INTO error_log(issue_id,execution_id,error_code,error_stage,error_message,created_at) "
                       "VALUES (?,?,?,?,?,?)", (issue_id, execution_id, code, stage, safe_message, utc_now()))

    def group_branches(self, issue_id: int) -> tuple[str | None, str | None]:
        with self.connection() as db:
            root = db.execute("SELECT root.base_branch,root.work_branch FROM issues member "
                              "JOIN issues root ON root.issue_id=member.execution_group_id "
                              "WHERE member.issue_id=?", (issue_id,)).fetchone()
            if not root:
                raise ValueError("No real execution group")
            return root['base_branch'], root['work_branch']

    def set_group_branches(self, issue_id: int, base_branch: str, work_branch: str) -> None:
        """Pin initial branch names on the group root, never on a correction."""
        if not base_branch or not work_branch:
            raise ValueError("Branch names are required")
        with self.transaction() as db:
            root = db.execute("SELECT root.* FROM issues member JOIN issues root "
                              "ON root.issue_id=member.execution_group_id WHERE member.issue_id=?",
                              (issue_id,)).fetchone()
            if not root or root['is_virtual']:
                raise ValueError("No real execution group")
            if ((root['base_branch'] is not None and root['base_branch'] != base_branch)
                    or (root['work_branch'] is not None and root['work_branch'] != work_branch)):
                raise ValueError("Group branches are immutable")
            db.execute("UPDATE issues SET base_branch=?,work_branch=?,updated_at=? WHERE issue_id=?",
                       (base_branch, work_branch, utc_now(), root['issue_id']))
