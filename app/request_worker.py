"""Bounded HTTP worker; credentials enter through stdin, never command arguments."""

import base64
import ipaddress
import json
import socket
import sys
import urllib.request
from urllib.parse import urlsplit
from contextlib import contextmanager, nullcontext

LIMIT = 32 * 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Upstream redirects are not supported')


def public_download(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Image URL must be credential-free HTTPS')
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError('Image URL must resolve to public addresses')
    return parsed.hostname, parsed.port or 443, addresses


@contextmanager
def pinned_download(url):
    host, port, addresses = public_download(url)
    original = socket.getaddrinfo
    def pinned(name, requested_port, *args, **kwargs):
        if name == host and requested_port == port:
            return addresses
        raise ValueError('Unexpected download connection target')
    socket.getaddrinfo = pinned
    try:
        yield
    finally:
        socket.getaddrinfo = original


def main():
    try:
        options = json.load(sys.stdin)
        download = options.get('download')
        req = urllib.request.Request(options['url'], data=base64.b64decode(options['data']) if options.get('data') else None,
                                     headers=options.get('headers', {}))
        handlers = [NoRedirect()]
        if download:
            handlers.append(urllib.request.ProxyHandler({}))
        with pinned_download(options['url']) if download else nullcontext():
            with urllib.request.build_opener(*handlers).open(req, timeout=options['timeout']) as response:
                raw = response.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError('Response exceeds limit')
        sys.stdout.write(base64.b64encode(raw).decode())
        return 0
    except Exception:
        # Upstream error bodies and URLs may contain credentials or customer content.
        sys.stderr.write('upstream request failed')
        return 1


if __name__ == '__main__':
    sys.exit(main())
