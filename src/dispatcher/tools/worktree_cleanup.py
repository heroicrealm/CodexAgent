import argparse
from pathlib import Path
from ..cleanup import Cleanup
from ..config import load_config
from ..git import Git
from ..storage import Storage


def main():
    parser = argparse.ArgumentParser(description='Inspect V2 orphan worktrees; dry-run by default')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--project')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        config = load_config(args.config)
    except (ValueError, OSError, TypeError, __import__('yaml').YAMLError) as exc:
        parser.exit(1, f'FAIL configuration: {exc}\n')
    if not config.database.is_file() or not config.database.stat().st_size:
        parser.error('Existing V2 SQLite required')
    if args.project and args.project not in {p.id for p in config.projects}:
        parser.error('Unknown project')
    from ..db_guard import check
    check(config, execution=args.apply)
    cleanup = Cleanup(config, Storage(config.database), Git(config))
    results = cleanup.orphans(apply=args.apply, project_id=args.project)
    for result in results:
        print(f"{'REMOVED' if result.removed else 'SAFE' if result.safe else 'SKIP'} {result.path}: {result.reason}")
    print(f'found={len(results)} safe={sum(r.safe for r in results)} '
          f'skipped={sum(not r.safe for r in results)} removed={sum(r.removed for r in results)}')


if __name__ == '__main__':
    main()
