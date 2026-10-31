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
                        allowed = {'branch', 'base_branch'}
                    else:
                        allowed = {'branch_name', 'base_branch'}
                    if not isinstance(value, dict) or value.keys() - allowed:
                        raise ValueError
                    if any(not isinstance(v, str) or not valid_branch(v) for v in value.values()):
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
