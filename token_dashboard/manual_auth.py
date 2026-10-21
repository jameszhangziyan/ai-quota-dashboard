"""手动粘贴的登录请求：用户在浏览器开发者工具里对官方额度请求「复制为 cURL」，
粘贴到本机设置页，这里解析出 URL / 请求头 / 请求体并保存，之后定时原样重放。

- 只保存在本机 .manual_auth.json（权限 600），只发给原请求的那个官方地址。
- 不会把令牌、Cookie 返回给网页或写进日志。
"""
import json
import os
import re
import shlex
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

AUTH_FILE = Path(__file__).resolve().parent / ".manual_auth.json"

PROVIDERS = {
    "workbuddy": {"name": "WorkBuddy", "must_contain": ("/billing/meter/get-user-resource",)},
    # kimi.com/settings/subscription 页面用的是 GetSubscriptionStats；GetUsages 为旧接口
    "kimiwork": {"name": "Kimi", "must_contain": ("MembershipService/GetSubscriptionStats",
                                                   "BillingService/GetUsages")},
    # gemini.google.com/usage 没有 JSON 接口，用量直接嵌在页面 HTML 里（Cookie 登录）
    "gemini": {"name": "Gemini", "must_contain": ("gemini.google.com/usage",), "raw": True},
}
KEEP_HEADERS = {"authorization", "cookie", "x-user-id", "x-enterprise-id", "x-tenant-id",
                "content-type", "accept-language", "x-msh-platform", "x-msh-device-id",
                "x-msh-session-id", "x-traffic-id", "r-timezone", "x-language", "user-agent",
                "origin", "referer"}


class AuthMissing(Exception):
    pass


class AuthExpired(Exception):
    pass


URL_RE = re.compile(r"https?://[^\s'\"^`<>]+")


def parse_fetch(text):
    """解析 Chrome「Copy as fetch」：fetch("url", { headers: {...}, body: "...", method: "POST" })。"""
    m = re.match(r'\s*(?:await\s+)?fetch\(\s*"([^"]+)"\s*,\s*(\{.*\})\s*\)\s*;?\s*$', text, re.S)
    if not m:
        raise ValueError("无法解析 fetch 格式")
    opts = json.loads(m.group(2))
    return (m.group(1), dict(opts.get("headers") or {}), opts.get("body"),
            (opts.get("method") or ("POST" if opts.get("body") is not None else "GET")).upper())


def _normalize_cmd(text):
    """Chrome「Copy as cURL (cmd)」用 ^ 转义：^" → "，^^ → ^，行尾 ^ 续行。"""
    text = re.sub(r"\^\r?\n", " ", text)
    text = text.replace('^\\^"', '\\"').replace('^"', '"').replace("^^", "\x00").replace("^", "")
    return text.replace("\x00", "^")


def parse_curl(text):
    """解析 Chrome / Safari / Edge 的「复制为 cURL」（bash 或 cmd）及「复制为 fetch」。
    返回 (url, headers, body_str, method)。"""
    text = text.strip().replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    if re.match(r"(await\s+)?fetch\(", text):
        return parse_fetch(text)
    if '^"' in text:
        text = _normalize_cmd(text)
    text = text.replace("\\\r\n", " ").replace("\\\n", " ")
    try:
        parts = shlex.split(text)
    except ValueError as e:
        raise ValueError(f"无法解析：{e}。请确认是完整复制的（右键请求 → Copy → Copy as cURL (bash)）")
    if not parts or parts[0] != "curl":
        raise ValueError("请粘贴以 curl 开头的完整内容（在开发者工具里右键请求 → Copy → Copy as cURL (bash)）")
    url, headers, body, method = None, {}, None, None
    i = 1
    while i < len(parts):
        p = parts[i]
        nxt = parts[i + 1] if i + 1 < len(parts) else None
        if p in ("-H", "--header") and nxt:
            k, _, v = nxt.partition(":")
            headers[k.strip()] = v.strip()
            i += 2
        elif p in ("-b", "--cookie") and nxt:
            headers["Cookie"] = nxt
            i += 2
        elif p in ("-d", "--data", "--data-raw", "--data-binary", "--data-ascii") and nxt is not None:
            body = nxt[1:] if nxt.startswith("$") else nxt
            i += 2
        elif p in ("-X", "--request") and nxt:
            method = nxt.upper()
            i += 2
        elif p == "--url" and nxt:
            url = nxt
            i += 2
        elif URL_RE.match(p):
            url = p
            i += 1
        elif p.startswith("-") and nxt is not None and not nxt.startswith("-") and p not in (
                "--compressed", "--insecure", "-k", "-s", "--silent", "-L", "--location", "-i"):
            i += 2  # 其他带参数的选项，忽略
        else:
            i += 1
    if not url:  # 兜底：整段文本里第一个 http(s) 地址
        m = URL_RE.search(text)
        url = m.group(0) if m else None
    if not url:
        raise ValueError("没找到请求地址。请在请求上右键 → Copy → Copy as cURL (bash) 后整段粘贴")
    method = method or ("POST" if body is not None else "GET")
    return url, headers, body, method


def detect(url):
    """按请求地址判断属于哪个账号；都不匹配返回 None。"""
    for pid, p in PROVIDERS.items():
        if any(n in url for n in p["must_contain"]):
            return pid
    return None


def save(provider, curl_text):
    """保存粘贴的请求。贴错框也没关系：按地址自动归到对应账号。返回实际保存到的账号 id。"""
    if provider not in PROVIDERS:
        raise ValueError("未知账号")
    url, headers, body, method = parse_curl(curl_text)
    actual = detect(url)
    if actual is None:
        names = "；".join(f"{p['name']} 找 {p['must_contain'][0].split('/')[-1]}" for p in PROVIDERS.values())
        raise ValueError(f"这不是额度请求（{names}）")
    provider = actual
    kept = {k: v for k, v in headers.items() if k.lower() in KEEP_HEADERS}
    if not any(k.lower() in ("authorization", "cookie") for k in kept):
        raise ValueError("这条请求里没有登录信息（Authorization 或 Cookie），请确认已登录后再复制")
    data = load_all()
    data[provider] = {"url": url, "headers": kept, "body": body, "method": method,
                      "saved_at": datetime.now().isoformat(timespec="seconds")}
    AUTH_FILE.write_text(json.dumps(data, ensure_ascii=False))
    os.chmod(AUTH_FILE, 0o600)
    return provider


def remove(provider):
    data = load_all()
    if data.pop(provider, None) is not None:
        AUTH_FILE.write_text(json.dumps(data, ensure_ascii=False))
        os.chmod(AUTH_FILE, 0o600)


def load_all():
    try:
        return json.loads(AUTH_FILE.read_text())
    except (OSError, ValueError):
        return {}


def status():
    """给设置页用：只返回是否已保存和保存时间，不含任何令牌。"""
    data = load_all()
    return {p: {"name": v["name"], "saved_at": (data.get(p) or {}).get("saved_at")} for p, v in PROVIDERS.items()}


def replay(provider, body_patch=None):
    entry = load_all().get(provider)
    if not entry:
        raise AuthMissing()
    body = entry.get("body")
    if body and body_patch:
        try:
            body = json.dumps(body_patch(json.loads(body)), ensure_ascii=False)
        except ValueError:
            pass
    raw = PROVIDERS.get(provider, {}).get("raw")
    accept = "text/html,application/xhtml+xml" if raw else "application/json"
    req = urllib.request.Request(entry["url"], method=entry.get("method") or "GET",
                                 data=body.encode() if body is not None else None,
                                 headers={"Accept": accept, **entry["headers"]})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            # 登录失效时 Google 会重定向到登录页
            if "accounts.google.com" in r.geturl():
                raise AuthExpired()
            content = r.read()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AuthExpired()
        raise
    if raw:
        return content.decode("utf-8", "ignore")
    d = json.loads(content)
    # 有些接口用 200 + 业务码表示未登录
    code = d.get("code") if isinstance(d, dict) else None
    if code in (401, 403, "unauthenticated", 10001, 11001):
        raise AuthExpired()
    return d
