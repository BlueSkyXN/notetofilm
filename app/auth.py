import hashlib
import hmac
import secrets
import time

VISITOR_SECONDS = 30 * 24 * 3600


def new_visitor(key):
    message = f'{int(time.time()) + VISITOR_SECONDS}.{secrets.token_hex(32)}'
    signature = hmac.new(key.encode(), ('visitor:' + message).encode(), hashlib.sha256).hexdigest()
    return message + '.' + signature


def visitor_owner(key, cookie):
    try:
        expiry, nonce, signature = cookie.split('.')
        if len(nonce) != 64 or any(c not in '0123456789abcdef' for c in nonce):
            return None
        expected = hmac.new(key.encode(), f'visitor:{expiry}.{nonce}'.encode(), hashlib.sha256).hexdigest()
        if not time.time() < int(expiry) <= time.time() + VISITOR_SECONDS or not hmac.compare_digest(signature, expected):
            return None
        return hashlib.sha256(nonce.encode()).hexdigest()
    except (ValueError, AttributeError, TypeError):
        return None


SESSION_SECONDS = 12 * 3600


def signed_session(token, expiry):
    message = f'{expiry}.{secrets.token_hex(16)}'
    signature = hmac.new(token.encode(), ('admin:' + message).encode(), hashlib.sha256).hexdigest()
    return f'{message}.{signature}'


def valid_session(token, cookie):
    try:
        expiry, nonce, signature = cookie.split('.')
        expected = hmac.new(token.encode(), f'admin:{expiry}.{nonce}'.encode(), hashlib.sha256).hexdigest()
        return time.time() < int(expiry) <= time.time() + SESSION_SECONDS and hmac.compare_digest(signature, expected)
    except (ValueError, AttributeError, TypeError):
        return False
