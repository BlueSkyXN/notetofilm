from urllib.parse import urlsplit


def validate_gateway(base):
    if not base:
        return
    message = '网关必须是无凭据、无查询参数、无空白的 HTTPS 地址（默认或 443 端口）'
    try:
        url = urlsplit(base)
        valid = (url.scheme == 'https' and bool(url.hostname) and not url.username and not url.password
                 and not url.query and not url.fragment and url.port in (None, 443)
                 and not any(c.isspace() or ord(c) < 32 for c in base))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(message)
