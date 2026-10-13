"""Bounded HTTP worker; credentials enter through stdin, never command arguments."""

import base64
import ipaddress
import json
import socket
import ssl
import sys
import urllib.request
from urllib.parse import urlsplit
from contextlib import contextmanager, nullcontext

LIMIT = 32 * 1024 * 1024


class RedirectDenied(ValueError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RedirectDenied('Upstream redirects are not supported')


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


def failure(kind, status=None):
    # Never serialize upstream bodies, reason strings, URLs or headers.
    sys.stderr.write(json.dumps({'kind': kind, 'status': status}))
    return 1


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
            return failure('response_limit')
        sys.stdout.write(base64.b64encode(raw).decode())
        return 0
    except urllib.error.HTTPError as exc:
        return failure('http', exc.code)
    except RedirectDenied:
        return failure('redirect')
    except (TimeoutError, socket.timeout):
        return failure('timeout')
    except urllib.error.URLError as exc:
        return failure('tls' if isinstance(exc.reason, ssl.SSLError) else
                       'timeout' if isinstance(exc.reason, TimeoutError) else 'connection')
    except Exception:
        return failure('failed')


if __name__ == '__main__':
    sys.exit(main())
