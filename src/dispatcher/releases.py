"""Bounded, synchronous release checks; no scheduler integration."""
import json
import sys
from importlib.metadata import version
from urllib.request import Request, urlopen
from packaging.version import Version

API = 'https://api.github.com'
LATEST = API + '/repos/heroicrealm/CodexAgent/releases/latest'


def request_json(url):
    with urlopen(Request(url, headers={'Accept': 'application/vnd.github+json',
                                     'User-Agent': 'codex-dispatcher'}), timeout=3) as response:
        return json.load(response)


def stable_version(release):
    if not isinstance(release, dict):
        raise ValueError('Invalid release response')
    if release.get('draft') or release.get('prerelease'):
        raise ValueError('Not a stable release')
    tag = release.get('tag_name')
    if not isinstance(tag, str):
        raise ValueError('Invalid release tag')
    result = Version(tag.removeprefix('v'))
    if result.is_prerelease or result.is_devrelease:
        raise ValueError('Not a stable version')
    return result


def before_run():
    installed = Version(version('codex-dispatcher-v2'))
    try:
        release = request_json(LATEST)
        candidate = stable_version(release)
        if candidate > installed:
            print(f'New dispatcher version {candidate}: https://github.com/heroicrealm/CodexAgent/releases/tag/{release["tag_name"]}\n'
                  'Stop dispatcher and run install-standalone.sh to update.')
    except (OSError, ValueError, KeyError, TypeError):
        print('WARNING: Unable to check dispatcher updates.', file=sys.stderr)
