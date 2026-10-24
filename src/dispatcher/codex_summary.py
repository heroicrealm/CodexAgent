"""Extract public prose from an untrusted Codex JSONL stream; never log raw text."""
import json
import re

MAX_BYTES = 8192
TRUNCATED = '\n\n[Summary truncated]'
# Redact whole token-like strings, including short/malformed credentials.
TOKENS = re.compile(r'(?i)(?:github_pat_|gh[pousr]_|sk-)[a-z0-9_-]+')
# Credentials with unrecognized formats cannot be safely selected by value.
CREDENTIAL = re.compile(
    r'(?i)(?:\b(?:api[_ -]?key|access[_ -]?token|authorization|password|secret)\b'
    r'\s*[=:]|\bbearer\s+|-----BEGIN [A-Z ]*PRIVATE KEY-----)')


def final_summary(path, fallback):
    selected = None
    try:
        with open(path, encoding='utf-8') as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if not isinstance(event, dict) or event.get('type') != 'item.completed':
                    continue
                item = event.get('item')
                if not isinstance(item, dict) or item.get('type') != 'agent_message':
                    continue
                text = item.get('text')
                if isinstance(text, str) and text.strip():
                    selected = text
        if selected is None:
            return fallback
        selected = TOKENS.sub('[REDACTED]', selected)
        if CREDENTIAL.search(selected) or any(ord(c) < 32 and c not in '\n\r\t' for c in selected):
            return fallback
        # Prevent model prose from forging ownership comments or hiding metadata.
        selected = selected.replace('<', '&lt;').replace('>', '&gt;')
        encoded = selected.encode('utf-8')
        if len(encoded) > MAX_BYTES:
            selected = encoded[:MAX_BYTES - len(TRUNCATED.encode())].decode('utf-8', errors='ignore') + TRUNCATED
        return selected
    except (OSError, UnicodeError, ValueError):
        return fallback
