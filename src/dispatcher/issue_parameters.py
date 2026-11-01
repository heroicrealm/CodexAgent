"""Parse dispatcher metadata while retaining unrelated Markdown verbatim."""
import re
import yaml
from dispatcher.workspace import TaskError


class UniqueLoader(yaml.SafeLoader):
    pass


def mapping(loader, node):
    result = {}
    for key, value in node.value:
        name = loader.construct_object(key)
        if not isinstance(name, str) or name in result:
            raise ValueError('ambiguous mapping')
        if name == 'run_codex' and (not isinstance(value, yaml.ScalarNode)
                                   or value.tag != 'tag:yaml.org,2002:bool'
                                   or value.value not in ('true', 'false')):
            raise ValueError('run_codex must be true or false')
        result[name] = loader.construct_object(value)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)


def parse(body):
    from dispatcher.github import valid_branch
    lines = (body or '').splitlines(keepends=True)
    removed, params, legacy = set(), {}, []
    found = False
    i = 0
    try:
        while i < len(lines):
            opening = re.match(r'^ {0,3}(`{3,}|~{3,})([^\r\n]*)\r?\n?$', lines[i])
            if opening:
                fence, info = opening.groups()
                start = i
                i += 1
                while i < len(lines) and not re.match(r'^ {0,3}' + re.escape(fence[0]) + '{' + str(len(fence)) + r',}[ \t]*(?:\r?\n)?$', lines[i]):
                    i += 1
                content = ''.join(lines[start + 1:i])
                service = info.strip() == 'codex-config'
                if info.strip() == 'yaml':
                    # Inspect parsed root keys too (quoted and flow mappings).
                    try:
                        candidate = yaml.compose(content)
                        service = isinstance(candidate, yaml.MappingNode) and any(
                            isinstance(k, yaml.ScalarNode) and k.value == 'dispatcher' for k, _ in candidate.value)
                    except yaml.YAMLError:
                        service = bool(re.search(r"(?m)(?:^|[,{])\s*['\"]?dispatcher['\"]?\s*:", content))
                if service and any(isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken))
                                   for token in yaml.scan(content)):
                    raise ValueError
                if service:
                    if found or i == len(lines):
                        raise ValueError
                    found = True
                    value = yaml.load(content, Loader=UniqueLoader)
                    if info.strip() == 'yaml':
                        if not isinstance(value, dict) or set(value) != {'dispatcher'}:
                            raise ValueError
                        value = value['dispatcher']
                        allowed = {'branch', 'base_branch', 'issue_type', 'run_codex'}
                    else:
                        allowed = {'branch_name', 'base_branch'}
                    if not isinstance(value, dict) or value.keys() - allowed:
                        raise ValueError
                    if ('issue_type' in value and value['issue_type'] not in ('issue', 'container')
                            or 'run_codex' in value and type(value['run_codex']) is not bool):
                        raise ValueError
                    if any(not isinstance(v, str) or not valid_branch(v)
                           for k, v in value.items() if k not in ('issue_type', 'run_codex')):
                        raise ValueError
                    params = {('branch_name' if k == 'branch' else k): v for k, v in value.items()}
                    removed.update(range(start, i + 1))
            else:
                match = re.match(r'^(?:Target branch|Целевая ветка):[ \t]*(.*?)[ \t]*(?:\r?\n)?$', lines[i], re.I)
                if match:
                    legacy.append(match[1])
                    removed.add(i)
            i += 1
        if len(legacy) > 1 or (legacy and 'branch_name' in params):
            raise ValueError
        if legacy:
            if not valid_branch(legacy[0]):
                raise ValueError
            params['branch_name'] = legacy[0]
        return params, ''.join(line for n, line in enumerate(lines) if n not in removed)
    except (ValueError, TypeError, RecursionError, yaml.YAMLError):
        raise TaskError('invalid_codex_config', 'GIT') from None


def with_base_branch(body, branch):
    """Build a reviewable proposal, retaining every byte outside the one key.

    This is deliberately independent from remote writes: GitHub Issue PATCH
    does not document a CAS precondition for committing the proposal safely.
    Legacy codex-config requires operator conversion, avoiding two service blocks.
    """
    import json
    from dispatcher.github import valid_branch
    params, _ = parse(body)
    if not valid_branch(branch):
        raise TaskError('invalid_branch', 'GIT')
    if params.get('base_branch') == branch:
        return body
    lines = body.splitlines(keepends=True)
    offset, i = 0, 0
    while i < len(lines):
        opening = re.match(r'^ {0,3}(`{3,}|~{3,})([^\r\n]*)\r?\n?$', lines[i])
        if not opening:
            offset += len(lines[i])
            i += 1
            continue
        fence, info = opening.groups()
        start = offset + len(lines[i])
        i += 1
        content = ''
        while i < len(lines) and not re.match(r'^ {0,3}' + re.escape(fence[0]) + '{' + str(len(fence)) + r',}[ \t]*(?:\r?\n)?$', lines[i]):
            content += lines[i]
            i += 1
        if info.strip() == 'codex-config':
            raise TaskError('child_legacy_config', 'GIT')
        try:
            node = yaml.compose(content) if info.strip() == 'yaml' else None
        except yaml.YAMLError:
            node = None  # An unrelated example is retained verbatim.
        if isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                if key.value != 'dispatcher':
                    continue
                for field, scalar in value.value:
                    if field.value == 'base_branch':
                        a, b = start + scalar.start_mark.index, start + scalar.end_mark.index
                        return body[:a] + json.dumps(branch) + body[b:]
                if value.flow_style:
                    position = start + value.end_mark.index - 1
                    insertion = (', ' if value.value else '') + 'base_branch: ' + json.dumps(branch)
                else:
                    position = start + value.end_mark.index
                    indent = value.value[0][0].start_mark.column if value.value else key.start_mark.column + 2
                    insertion = ' ' * indent + 'base_branch: ' + json.dumps(branch) + '\n'
                return body[:position] + insertion + body[position:]
        if i < len(lines):
            offset = start + len(content) + len(lines[i])
        i += 1
    return body + ('\n' if body and not body.endswith('\n') else '') + '```yaml\ndispatcher:\n  base_branch: ' + json.dumps(branch) + '\n```\n'
