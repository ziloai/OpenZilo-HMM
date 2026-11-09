"""Standard-library HTTPS only; no redirects, retries, or response-body logging."""
import json
import math
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request(settings, parameters):
    if settings.get("confirmed_target") is not True:
        raise ValueError("请先确认目标站点及发送用途")
    url = settings.get("url", "")
    try:
        if not isinstance(url, str) or any(ord(c) <= 32 or ord(c) == 127 for c in url):
            raise ValueError()
        parts = urllib.parse.urlsplit(url)
        if (parts.scheme != "https" or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.fragment or "\\" in url):
            raise ValueError()
        if parts.port is not None and not 1 <= parts.port <= 65535:
            raise ValueError()
    except Exception:
        raise ValueError("目标必须是有效 HTTPS 地址，不能包含用户信息或 fragment") from None
    token = settings.get("token", "")
    if not isinstance(token, str) or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise ValueError("认证配置无效；请检查 token 格式")
    timeout = settings.get("timeout_seconds", 5)
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or not 1 <= timeout <= 15):
        raise ValueError("HTTP 超时必须为 1–15 秒")
    try:
        payload = parameters.get("payload", '{"message":"gesture detected"}')
        if not isinstance(payload, str) or len(payload) > 65536:
            raise ValueError()
        body = json.loads(payload)
        if not isinstance(body, dict):
            raise ValueError()
        data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(data) > 65536:
            raise ValueError()
    except Exception:
        raise ValueError("payload 必须是有效 JSON 对象，编码后不超过 64 KiB") from None
    headers = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    # Request can itself reject malformed URLs: never expose its exception text.
    try:
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    except Exception:
        raise ValueError("无法构造 HTTPS 请求，请检查目标设置") from None
    return request, float(timeout)


def handle_action(context, action, event, parameters):
    if action != "post":
        raise ValueError("未知 Webhook 动作")
    request, timeout = _request(context.settings, parameters)
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            status = response.status
            if not 200 <= status < 300:
                raise ValueError("non-success status")
    except Exception:
        # URLs, headers, server bodies and underlying exceptions can contain secrets.
        raise RuntimeError("Webhook 未确认成功；请检查网络和目标设置。未重试，也未跟随重定向") from None
    context.log("Webhook 已收到成功状态；未读取响应内容")
    return {"status": status}
