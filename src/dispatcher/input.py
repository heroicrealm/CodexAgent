"""Approved INPUT contract; never called during registration."""
from dataclasses import dataclass
import hashlib
import json
import re
import yaml

INPUT_MARKER = '<!-- codex-dispatcher:INPUT:v2 -->'
OUTPUT_MARKER = '<!-- codex-dispatcher:OUTPUT:v2 -->'
ERROR_MARKER = '<!-- codex-dispatcher:ERROR:v2 -->'


class InputError(ValueError):
    pass


class UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise InputError('INPUT has duplicate or invalid keys')
        result[key] = loader.construct_object(value_node)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def branch_name(value: object) -> str:
    if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./-]{0,199}', value)
            or any(part in value for part in ('..', '//', '@{')) or value.endswith(('/', '.', '.lock'))
            or any(part.startswith('.') or part.endswith('.lock') for part in value.split('/'))):
        raise InputError('Invalid Git branch name')
    return value


@dataclass(frozen=True)
class Parameters:
    base_branch: str | None = None
    branch: str | None = None
    correction_for: int | None = None
    run_codex: bool = True


def parse_input(text: str | None) -> tuple[Parameters, str | None]:
    if text is None:
        return Parameters(), None
    blocks = re.findall(r'^```(?:yaml|yml)[ \t]*\r?\n(.*?)^```[ \t]*$', text, re.M | re.S)
    if len(blocks) != 1:
        raise InputError('INPUT requires exactly one fenced YAML block')
    raw = blocks[0]
    try:
        data = yaml.load(raw, Loader=UniqueLoader)
    except (yaml.YAMLError, RecursionError) as exc:
        raise InputError('Invalid INPUT YAML') from exc
    if not isinstance(data, dict) or set(data) != {'dispatcher'}:
        raise InputError('INPUT must contain only dispatcher mapping')
    values = data['dispatcher']
    if not isinstance(values, dict) or set(values) - {'base_branch', 'branch', 'correction_for', 'run_codex'}:
        raise InputError('Unsupported dispatcher parameters')
    for key in ('base_branch', 'branch'):
        if key in values:
            branch_name(values[key])
    if 'run_codex' in values and type(values['run_codex']) is not bool:
        raise InputError('run_codex must be boolean')
    if 'correction_for' in values and (type(values['correction_for']) is not int or values['correction_for'] <= 0):
        raise InputError('correction_for must be a positive issue number')
    if 'correction_for' in values and {'base_branch', 'branch'} & values.keys():
        raise InputError('Corrections cannot override group branches')
    return Parameters(**values), raw


def snapshot_hash(title: str, body: str, text: str | None) -> str:
    return hashlib.sha256(json.dumps([title, body, text], ensure_ascii=False).encode()).hexdigest()
