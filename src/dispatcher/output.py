"""Public rendering of agent output; the saved stdout remains untouched."""
from pathlib import Path
import re
from urllib.parse import quote, unquote


SHA = re.compile(r'(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z')
# Balanced parentheses in bare destinations and angle-delimited destinations.
LINK = re.compile(r'\[([^\]\n]*)\]\((<[^>\n]*>|(?:[^()\n]|\([^()\n]*\))*)\)')
LOCAL = re.compile(r'(?<![\w:/])/(?:home|Users|root|tmp|var|workspace|mnt)/[^\s`<>]*')


def confirmed_sha(execution):
    sha = execution['result_commit']
    return sha if execution['push_completed'] and sha and SHA.fullmatch(sha) else None


def public_result(text, execution, repository, committed_file):
    """Resolve only this execution's paths; committed_file does no network I/O."""
    sha = confirmed_sha(execution)
    root = Path(execution['worktree_path']) if execution['worktree_path'] else None

    def link(match):
        label, destination = match.groups()
        destination = destination.strip()
        if destination.startswith('<') and destination.endswith('>'):
            destination = destination[1:-1]
        if not destination.startswith(('/', 'file://')):
            return match.group(0)
        raw = destination.removeprefix('file://')
        # Codex uses :line[:column]; these are locations, not filename components.
        raw = re.sub(r':\d+(?::\d+)?$', '', raw)
        candidates = [raw]
        if unquote(raw) != raw:
            candidates.append(unquote(raw))
        for candidate in candidates:
            path = Path(candidate)
            try:
                if not root or not sha or not path.is_absolute():
                    continue
                relative = path.relative_to(root).as_posix()
                if not path.resolve().is_relative_to(root.resolve()):
                    continue
                if committed_file(sha, relative):
                    url = f'https://github.com/{repository}/blob/{sha}/{quote(relative, safe="/")}'
                    return f'[{label}]({url})'
            except (ValueError, OSError, RuntimeError):
                continue
        return label

    text = LINK.sub(link, text)
    # Minimal privacy fallback for bare local paths, including inline code.
    # Protect external URLs and repository-relative links from this fallback.
    protected = re.compile(r'https?://[^\s<>]+|\[[^\]\n]*\]\([^\n]*?\)')
    parts, offset = [], 0
    for match in protected.finditer(text):
        parts.append(LOCAL.sub(lambda m: m.group(0).rstrip('/').rsplit('/', 1)[-1], text[offset:match.start()]))
        parts.append(match.group(0))
        offset = match.end()
    parts.append(LOCAL.sub(lambda m: m.group(0).rstrip('/').rsplit('/', 1)[-1], text[offset:]))
    return ''.join(parts)
