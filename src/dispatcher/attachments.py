"""Bounded authenticated GitHub attachment downloads; no ambient redirects."""
import hashlib
from http.client import HTTPException
import json
import re
import subprocess
from dispatcher.process_wait import run as natural_run
import time
from urllib.parse import urlsplit, unquote, urljoin
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError

from dispatcher.workspace import TaskError, private_dir, private_file


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class IssueAttachmentManager:
    def __init__(self, config, opener=None):
        self.config = config
        self.operation_guard = None
        self.opener = opener or build_opener(NoRedirect())

    @staticmethod
    def recognized(url):
        try:
            value = urlsplit(url)
            if not IssueAttachmentManager.allowed(url):
                return False
        except ValueError:
            return False
        uuid = r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}'
        if value.hostname == 'github.com':
            # Current Issue uploads and legacy repository-scoped uploads.
            patterns = (
                rf'/user-attachments/assets/{uuid}',
                r'/user-attachments/files/[1-9][0-9]*/[^/]+',
                r'/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/files/[1-9][0-9]*/[^/]+',
                rf'/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/assets/[1-9][0-9]*/{uuid}',
            )
            return any(re.fullmatch(pattern, value.path) for pattern in patterns)
        if value.hostname in ('user-images.githubusercontent.com', 'private-user-images.githubusercontent.com'):
            return bool(re.fullmatch(r'/[1-9][0-9]*/[^/]+\.[A-Za-z0-9]+', value.path))
        # Other allowed hosts serve redirects only, including release asset CDNs.
        return False

    @staticmethod
    def allowed(url):
        value = urlsplit(url)
        return (value.scheme == 'https' and not value.username and not value.password
                and value.port in (None, 443) and value.hostname in (
                    'github.com', 'api.github.com', 'user-images.githubusercontent.com',
                    'private-user-images.githubusercontent.com', 'objects.githubusercontent.com',
                    'github-production-user-asset-6210df.s3.amazonaws.com'))

    def urls(self, body):
        # GitHub's generated Markdown, HTML images, and reference destinations.
        candidates = re.findall(r'https?://[^\s<>"\)\]]+', body)
        return list(dict.fromkeys(url.rstrip("'") for url in candidates if self.recognized(url.rstrip("'"))))

    def token(self):
        if self.operation_guard:
            self.operation_guard()
        try:
            result = natural_run([self.config.github_executable, 'auth', 'token', '--hostname', 'github.com'],
                                    capture_output=True, text=True, timeout=15)
            if result.returncode or not result.stdout.strip():
                raise ValueError
            return result.stdout.strip()
        except (ValueError, OSError, subprocess.TimeoutExpired):
            raise TaskError('attachment_download_failed', 'ATTACHMENT') from None

    def download(self, url, destination, token):
        deadline = time.monotonic() + self.config.attachment_timeout_seconds
        for _ in range(6):
            if self.operation_guard:
                self.operation_guard()
            if not self.allowed(url):
                raise ValueError
            headers = {'Accept': 'application/octet-stream', 'User-Agent': 'CodexAgent'}
            # Never forward the GitHub token to signed CDN destinations.
            if urlsplit(url).hostname in ('github.com', 'api.github.com'):
                headers['Authorization'] = f'Bearer {token}'
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError
            try:
                response = self.opener.open(Request(url, headers=headers), timeout=remaining)
            except HTTPError as exc:
                if exc.code in (301, 302, 303, 307, 308):
                    location = exc.headers.get('Location')
                    exc.close()
                    if not location:
                        raise ValueError
                    url = urljoin(url, location)
                    continue
                raise
            with response:
                if response.status != 200 or not self.allowed(response.geturl()):
                    raise ValueError
                length = response.headers.get('Content-Length')
                if length and int(length) > self.config.attachment_max_bytes:
                    raise ValueError
                # A login page is not a successfully downloaded private attachment.
                if 'text/html' in response.headers.get('Content-Type', '').lower():
                    raise ValueError
                digest, size = hashlib.sha256(), 0
                with private_file(destination, 'wb') as stream:
                    while True:
                        if time.monotonic() >= deadline:
                            raise ValueError
                        # read1 performs one underlying read, so slow streams
                        # cannot hide an unbounded series of reads inside read(n).
                        socket = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
                        if socket is not None:
                            socket.settimeout(max(.001, deadline - time.monotonic()))
                        data = response.read1(min(65536, self.config.attachment_max_bytes - size + 1))
                        if not data:
                            break
                        size += len(data)
                        if size > self.config.attachment_max_bytes:
                            raise ValueError
                        digest.update(data)
                        stream.write(data)
                if length and size != int(length):
                    raise ValueError
                return size, digest.hexdigest()
        raise ValueError

    def prepare(self, body, task):
        if self.operation_guard:
            self.operation_guard()
        try:
            directory = private_dir(self.config.workspace, task / 'attachments')
        except (OSError, TaskError):
            raise TaskError('attachment_download_failed', 'ATTACHMENT') from None
        manifest, paths = [], []
        urls = self.urls(body)
        failed = len(urls) > self.config.attachment_max_files
        for index, url in enumerate(urls[:self.config.attachment_max_files]):
            if self.operation_guard:
                self.operation_guard()
            entry = dict(source_url=url, local_path=None, size=None, sha256=None, status='failed')
            try:
                name = unquote(urlsplit(url).path.rsplit('/', 1)[-1])
                if (not name or name in ('.', '..') or '/' in name or '\\' in name
                        or any(ord(c) < 32 for c in name) or len(name) > 200):
                    raise ValueError
                name = re.sub(r'[^a-zA-Z0-9._-]', '_', name)
                destination = directory / f'{index + 1:03d}-{name}'
                entry['local_path'] = str(destination)
                size, sha = self.download(url, destination, self.token())
                entry.update(size=size, sha256=sha, status='downloaded')
                paths.append(str(destination))
            except (OSError, ValueError, TaskError, HTTPException):
                failed = True
            manifest.append(entry)
        if len(urls) > self.config.attachment_max_files:
            manifest.extend(dict(source_url=u, local_path=None, size=None, sha256=None, status='limit_exceeded')
                            for u in urls[self.config.attachment_max_files:])
        with private_file(directory / 'manifest.json') as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
        if failed:
            raise TaskError('attachment_download_failed', 'ATTACHMENT')
        return paths
