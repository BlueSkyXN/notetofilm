"""Only explicitly classified, locally authored errors may reach task owners."""


class UserFacingError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class MediaError(UserFacingError, ValueError):
    pass


class ProviderError(UserFacingError, RuntimeError):
    pass


def upstream_error(kind, status=None):
    if kind == 'http' and type(status) is int and 400 <= status <= 599:
        hint = {400: '请检查所选协议、模型及请求参数',
                401: '请管理员检查模型密钥', 403: '请管理员检查模型访问权限',
                404: '请管理员检查网关地址、接口路径及模型',
                429: '请检查上游额度或限流，稍后再由人工决定是否重试'}.get(
                    status, '上游服务暂时不可用，请稍后检查' if status >= 500 else '请检查上游配置')
        return ProviderError('upstream_http', f'上游返回 HTTP {status}；{hint}。未自动重试，请求可能已计费。')
    messages = {
        'timeout': '上游请求超时；未自动重试，结果可能未知，请核对后再决定是否重试。',
        'connection': '无法连接上游服务，请检查网络和网关可达性；未自动重试。',
        'tls': '上游 TLS 证书校验失败，请检查本机证书与代理配置；未自动重试。',
        'redirect': '上游返回重定向，当前接口不跟随跳转，请检查网关地址；未自动重试。',
        'response_limit': '上游响应超过大小限制，未发布素材；未自动重试。',
    }
    return ProviderError('upstream_' + (kind if kind in messages else 'failed'),
                         messages.get(kind, '上游请求失败；未自动重试，请管理员检查连接及配置。'))
