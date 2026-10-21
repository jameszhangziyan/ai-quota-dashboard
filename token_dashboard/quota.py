"""账号额度：用本机各工具已保存的登录令牌，调用各家自己的额度接口。

- 令牌只在本机读取，只发给对应厂商的接口，不写日志、不落盘、不返回给网页。
- 从不刷新令牌（刷新会轮换 refresh token，可能把原工具登出）；
  令牌过期时显示上一次的结果，并提示打开对应工具即可恢复。
"""
import json
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import ext_bridge
import manual_auth

HOME = Path.home()
CACHE_FILE = Path(__file__).resolve().parent / ".quota_cache.json"
TTL = 300  # 秒：同一账号 5 分钟内最多查一次
_lock = threading.Lock()
_mem = {}


def _http_json(url, headers, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, headers={"Accept": "application/json", **headers},
                                 method=method, data=data)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _iso_to_epoch(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class NeedAction(Exception):
    """令牌缺失或过期：需要用户在对应工具里登录 / 打开一下。"""


# ---------- Claude（Claude Code 登录） ----------
def fetch_claude():
    raw = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                         capture_output=True, text=True, timeout=10).stdout.strip()
    if not raw:
        raise NeedAction("未找到 Claude Code 登录信息")
    o = (json.loads(raw).get("claudeAiOauth") or {})
    if o.get("expiresAt", 0) / 1000 < time.time() + 30:
        raise NeedAction("登录令牌已过期，打开一次 Claude Code 即可恢复")
    d = _http_json("https://api.anthropic.com/api/oauth/usage", {
        "Authorization": "Bearer " + o["accessToken"],
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-cli/2.1.0 (external, cli)",
    })
    windows = []
    for key, label in (("five_hour", "5 小时"), ("seven_day", "本周"),
                       ("seven_day_opus", "本周 · Opus"), ("seven_day_sonnet", "本周 · Sonnet")):
        w = d.get(key)
        if isinstance(w, dict) and isinstance(w.get("utilization"), (int, float)):
            windows.append({"label": label, "percent": round(w["utilization"]),
                            "resets_at": _iso_to_epoch(w.get("resets_at"))})
    plan = (o.get("subscriptionType") or "").capitalize() or None
    return {"plan": plan, "windows": windows}


# ---------- ChatGPT（Codex 登录） ----------
def fetch_chatgpt():
    p = HOME / ".codex" / "auth.json"
    if not p.exists():
        raise NeedAction("未找到 Codex 登录信息")
    t = (json.loads(p.read_text()).get("tokens") or {})
    if not t.get("access_token"):
        raise NeedAction("Codex 未用 ChatGPT 账号登录")
    try:
        d = _http_json("https://chatgpt.com/backend-api/wham/usage", {
            "Authorization": "Bearer " + t["access_token"],
            "ChatGPT-Account-Id": t.get("account_id") or "",
            "User-Agent": "codex-cli",
        })
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise NeedAction("登录令牌已过期，打开一次 Codex 即可恢复")
        raise
    rl = d.get("rate_limit") or {}
    windows = []
    for key, label in (("primary_window", "5 小时"), ("secondary_window", "本周")):
        w = rl.get(key)
        if isinstance(w, dict) and w.get("used_percent") is not None:
            secs = w.get("limit_window_seconds") or 0
            if key == "primary_window" and secs and secs != 18000:
                label = f"{round(secs / 3600)} 小时"
            windows.append({"label": label, "percent": round(w["used_percent"]),
                            "resets_at": w.get("reset_at")})
    for extra in d.get("additional_rate_limits") or []:
        w = (extra.get("rate_limit") or {}).get("primary_window") or {}
        if w.get("used_percent") is not None:
            windows.append({"label": extra.get("limit_name") or "附加额度",
                            "percent": round(w["used_percent"]), "resets_at": w.get("reset_at")})
    return {"plan": (d.get("plan_type") or "").capitalize() or None, "windows": windows,
            "blocked": bool(rl.get("limit_reached"))}


# ---------- Kimi（Kimi Code 登录，与 Kimi 会员共用账号） ----------
def _kimi_token():
    for p in (HOME / ".kimi-code" / "credentials" / "kimi-code.json",
              HOME / ".kimi" / "credentials" / "kimi-code.json"):
        if p.exists():
            d = json.loads(p.read_text())
            tok = d.get("access_token") or d.get("accessToken")
            exp = d.get("expires_at") or d.get("expiresAt")
            if exp and float(exp) > 1e12:
                exp = float(exp) / 1000
            if tok and (not exp or float(exp) > time.time() + 30):
                return tok
            raise NeedAction("Kimi 登录已过期，在终端运行一次 kimi 即可恢复")
    raise NeedAction("需要在终端运行 kimi 并用 /login 登录一次")


def _kimi_detail(det):
    lim, used = float(det.get("limit") or 0), float(det.get("used") or 0)
    if not lim:
        return None
    return round(used / lim * 100), _iso_to_epoch(det.get("resetTime"))


def fetch_kimi():
    tok = _kimi_token()
    try:
        d = _http_json("https://api.kimi.com/coding/v1/usages", {"Authorization": "Bearer " + tok})
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise NeedAction("Kimi 登录已过期，在终端运行一次 kimi 即可恢复")
        raise
    windows = []
    for lm in d.get("limits") or []:
        win, det = lm.get("window") or {}, lm.get("detail") or {}
        r = _kimi_detail(det)
        if r:
            mins = win.get("duration", 0) * (60 if "HOUR" in str(win.get("timeUnit")) else 1)
            label = f"{round(mins / 60)} 小时" if mins else "短期"
            windows.append({"label": label, "percent": r[0], "resets_at": r[1]})
    if isinstance(d.get("usage"), dict):
        r = _kimi_detail(d["usage"])
        if r:
            windows.append({"label": "本周", "percent": r[0], "resets_at": r[1]})
    plan = None
    u = d.get("user") or {}
    if isinstance(u, dict):
        plan = ((u.get("membership") or {}).get("level") or "").replace("LEVEL_", "").capitalize() or None
    return {"plan": plan, "windows": windows,
            "note": "这是 Kimi Code 的额度，Kimi Work 的 Agent 用量另算，不在其中"}


# ---------- WorkBuddy（本机数据库里的积分消耗，不需要登录令牌） ----------
def _workbuddy_local_stats():
    """~/.workbuddy/workbuddy.db 的 session_usage.credit_json 记录了每个会话里
    各次请求消耗的积分。只有会话最后更新时间，所以按会话更新日期归到某一天。"""
    import sqlite3
    db = HOME / ".workbuddy" / "workbuddy.db"
    if not db.exists():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        rows = con.execute("SELECT updated_at, credit_json FROM session_usage "
                           "WHERE credit_json IS NOT NULL AND credit_json != ''").fetchall()
    finally:
        con.close()
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    week_start = (now.timestamp() - 6 * 86400)
    month = now.strftime("%Y-%m")
    t = w = m = 0.0
    for updated_at, cj in rows:
        try:
            spent = sum(float(v) for v in json.loads(cj).values())
        except (ValueError, TypeError, AttributeError):
            continue
        dt = datetime.fromtimestamp(updated_at / 1000)
        if dt.strftime("%Y-%m-%d") == today:
            t += spent
        if dt.timestamp() >= datetime.fromtimestamp(week_start).replace(hour=0, minute=0, second=0).timestamp():
            w += spent
        if dt.strftime("%Y-%m") == month:
            m += spent
    return [{"label": "本机今天", "value": round(t, 1)}, {"label": "本机近 7 天", "value": round(w, 1)},
            {"label": "本机本月", "value": round(m, 1)}]


def _to_epoch(v):
    """WorkBuddy 时间可能是毫秒数字串或 'YYYY-MM-DD HH:MM:SS'（北京时间）。"""
    if not v:
        return None
    s = str(v)
    if s.isdigit():
        return int(s) / (1000 if len(s) > 10 else 1)
    try:
        return datetime.fromisoformat(s.replace(" ", "T")).timestamp()
    except ValueError:
        return None


def _workbuddy_account():
    """重放用户粘贴的 /billing/meter/get-user-resource 请求，得到账号级积分。"""
    def patch(body):
        if "PackageStartTimeRangeEnd" in body:
            body["PackageStartTimeRangeEnd"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return body
    try:
        d = manual_auth.replay("workbuddy", patch)
    except manual_auth.AuthMissing:
        raise NeedAction("在设置页粘贴一次 WorkBuddy 的额度请求，即可显示账号积分")
    except manual_auth.AuthExpired:
        raise NeedAction("WorkBuddy 登录已过期，请在设置页重新粘贴一次")
    # 桌面版接口返回 data.Response.Data.Accounts；网页版 get-user-resource-paid-packages 返回 data.Accounts
    data = d.get("data") or {}
    accts = ((data.get("Response") or {}).get("Data") or {}).get("Accounts")
    if accts is None:
        accts = data.get("Accounts")
    if accts is None:
        raise NeedAction("WorkBuddy 返回格式不对，请在设置页重新粘贴")
    accts = _workbuddy_monthly(accts)
    now = time.time()
    windows, total, left, plan = [], 0.0, 0.0, None
    for a in accts:
        # 字段可能带或不带 Precise 后缀；剩余为 0 是合法值，不能用 or 兜底
        cap = float(a.get("CycleCapacitySizePrecise") or a.get("CycleCapacitySize") or 0)
        rem_v = a.get("CycleCapacityRemainPrecise")
        if rem_v is None:
            rem_v = a.get("CycleCapacityRemain")
        rem = float(rem_v or 0)
        end = _to_epoch(a.get("DeductionEndTime")) or _to_epoch(a.get("CycleEndTime"))
        if not cap or (end and end < now):
            continue
        total += cap
        left += rem
        name = a.get("PackageName") or a.get("PackageCode") or "积分包"
        plan = plan or name
        windows.append({"label": name, "percent": round((cap - rem) / cap * 100),
                        "resets_at": _to_epoch(a.get("CycleEndTime")),
                        "detail": f"剩 {rem:,.0f} / {cap:,.0f}"})
    # 每月额度 = 付费套餐 + 同周期的赠送包，合成一个百分比（例：4000 + 5000 = 9000）
    if windows:
        windows = [{"label": plan, "percent": round((total - left) / total * 100) if total else 0,
                    "resets_at": windows[0]["resets_at"], "detail": f"剩 {left:,.0f} / {total:,.0f}"}]
    return {"plan": plan, "windows": windows, "remaining": round(left, 1), "capacity": round(total, 1)}


def _wb_size(a):
    return float(a.get("CycleCapacitySizePrecise") or a.get("CycleCapacitySize") or 0)


def _workbuddy_monthly(paid):
    """网页版付费套餐接口只返回付费包（如 4000）。再查完整资源列表，把与套餐**同一周期结束**的
    有效包（如「Codebuddy版本赠送包」5000）一并计入每月基数；裂变/补偿等有效期不同的零散包不算。
    完整列表查不到时退回只算付费包。"""
    plan = next((a for a in paid if _wb_size(a) > 0 and a.get("CycleEndTime")), None)
    entry = manual_auth.load_all().get("workbuddy") or {}
    if not plan or "get-user-resource-paid-packages" not in entry.get("url", ""):
        return paid
    body = {"PageNumber": 1, "PageSize": 100, "ProductCode": "p_tcaca", "Status": [0, 3],
            "PackageStartTimeRangeBegin": "2024-12-01 00:00:00",
            "PackageStartTimeRangeEnd": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    try:
        d = _http_json(entry["url"].replace("get-user-resource-paid-packages", "get-user-resource"),
                       entry["headers"], method="POST", body=body)
    except Exception:
        return paid
    data = d.get("data") or {}
    full = ((data.get("Response") or {}).get("Data") or {}).get("Accounts") or data.get("Accounts") or []
    monthly = [a for a in full if a.get("Status") == 0 and _wb_size(a) > 0
               and a.get("CycleEndTime") == plan.get("CycleEndTime")]
    # 套餐排第一，名称取套餐的
    monthly.sort(key=lambda a: a.get("PackageCode") != plan.get("PackageCode"))
    return monthly or paid


DEFAULT_TOKENS_PER_CREDIT = 17000  # 2026-10 本机实测的加权平均，数据不足时用
_ratio_cache = {"at": 0, "value": None}


def _workbuddy_tokens_per_credit():
    """用本机数据实测「1 积分 ≈ 多少 token」：把 session_usage 的积分和
    traces 里同一会话的 token 数对上，取加权平均。每小时重算一次。"""
    if time.time() - _ratio_cache["at"] < 3600 and _ratio_cache["value"]:
        return _ratio_cache["value"]
    import sqlite3
    ratio = DEFAULT_TOKENS_PER_CREDIT
    try:
        con = sqlite3.connect(f"file:{HOME / '.workbuddy' / 'workbuddy.db'}?mode=ro", uri=True, timeout=5)
        try:
            credits = {}
            for sid, cj in con.execute("SELECT session_id, credit_json FROM session_usage "
                                       "WHERE credit_json IS NOT NULL AND credit_json != ''"):
                try:
                    credits[sid] = sum(float(v) for v in json.loads(cj).values())
                except (ValueError, TypeError, AttributeError):
                    pass
        finally:
            con.close()
        tokens = {}
        for f in (HOME / ".workbuddy" / "traces").rglob("trace_*.json"):
            try:
                with open(f, encoding="utf-8", errors="ignore") as fh:
                    t = (json.load(fh) or {}).get("trace") or {}
            except (OSError, ValueError):
                continue
            mi = t.get("modelInfo") or {}
            if t.get("sessionId") in credits:
                # 与其他账号同一口径：不计缓存命中的输入（重复读取的上下文）
                fresh_in = (mi.get("totalInputTokens") or 0) - (mi.get("totalCachedTokens") or 0)
                tokens[t["sessionId"]] = tokens.get(t["sessionId"], 0) + \
                    max(fresh_in, 0) + (mi.get("totalOutputTokens") or 0)
        matched = [s for s in tokens if tokens[s] and credits.get(s)]
        c = sum(credits[s] for s in matched)
        if len(matched) >= 5 and c >= 200:
            ratio = sum(tokens[s] for s in matched) / c
    except Exception:
        pass
    _ratio_cache.update(at=time.time(), value=ratio)
    return ratio


def fetch_workbuddy():
    ratio = _workbuddy_tokens_per_credit()
    stats = [{**s, "sub": f"≈ {_fmt_tokens(s['value'] * ratio)} token"} for s in _workbuddy_local_stats()]
    basis = f"token 按本机实测约 {ratio / 10000:.1f} 万 token / 积分估算"
    try:
        acct = _workbuddy_account()
        used = acct["capacity"] - acct["remaining"]
        return {**acct, "unit": "积分", "stats": stats, "used": round(used, 1),
                "used_tokens": round(used * ratio),
                "note": f"账号本周期已用 {used:,.0f} 积分（≈ {_fmt_tokens(used * ratio)} token），"
                        f"剩余 {acct['remaining']:,.0f} / {acct['capacity']:,.0f}，含所有设备。{basis}"}
    except NeedAction as e:
        return {"plan": None, "windows": [], "unit": "积分", "stats": stats, "hint": str(e),
                "note": f"上面是这台 Mac 的积分消耗。{basis}"}


# ---------- Gemini（用户粘贴的 gemini.google.com/usage 页面请求） ----------
GEMINI_POOLS = {"GXU_FIVE_HOURLY": "5 小时", "GXU_WEEKLY": "本周", "GXU_DAILY": "今天"}
GEMINI_TIERS = {"TIER1": "Pro", "TIER2": "Ultra", "TIER0": "Free"}
_GEMINI_POOL_RE = re.compile(
    r'"poolType":"(\w+)","percentageUsed":([\d.]+)(?:,"gxu":\{"windowRefreshesAt":"([^"]+)")?')


def _gemini_from_ext(p):
    """Chrome 扩展上报的格式：{"tier": "TIER1", "pools": [{"type","percent","refreshesAt"}]}"""
    order = ["GXU_FIVE_HOURLY", "GXU_WEEKLY", "GXU_DAILY"]
    pools = sorted(p.get("pools") or [], key=lambda x: order.index(x["type"]) if x["type"] in order else 99)
    windows = [{"label": GEMINI_POOLS.get(x["type"], x["type"].replace("GXU_", "").title()),
                "percent": round(float(x["percent"]), 1),
                "resets_at": _iso_to_epoch(x.get("refreshesAt"))} for x in pools]
    t = p.get("tier")
    return {"plan": GEMINI_TIERS.get(t, t), "windows": windows}


def fetch_gemini():
    """优先用 Chrome 扩展上报的数据；没有时退回设置页粘贴的请求。
    页面里用量是转义 JSON："getUsageInfo":{"subscriptionTier":"TIER1","pools":[...]}"""
    payload, ext_err = ext_bridge.get("gemini")
    if payload:
        return _gemini_from_ext(payload)
    if not manual_auth.load_all().get("gemini"):
        raise NeedAction(ext_err or "安装并打开 Chrome 扩展「AI 额度看板助手」后自动显示")
    try:
        html = manual_auth.replay("gemini")
    except manual_auth.AuthMissing:
        raise NeedAction("在设置页粘贴一次 Gemini 用量页的请求，即可显示额度")
    except manual_auth.AuthExpired:
        raise NeedAction("Google 登录已失效，请在设置页重新粘贴一次")
    text = html.replace('\\"', '"')
    if "getUsageInfo" not in text:
        raise NeedAction("Google 登录已失效（页面里没有用量数据），请在设置页重新粘贴一次")
    tier = re.search(r'"subscriptionTier":"(\w+)"', text)
    pools = {}
    for ptype, pct, refresh in _GEMINI_POOL_RE.findall(text):
        pools.setdefault(ptype, (float(pct), refresh))
    if not pools:
        raise NeedAction("Gemini 页面格式变了，没找到用量数据（告诉 Claude 来适配）")
    # percentageUsed 看起来已是 0~100；若全部 ≤ 1 且有非零值，视为 0~1 的比例
    scale = 100 if all(p <= 1 for p, _ in pools.values()) and any(p > 0 for p, _ in pools.values()) else 1
    order = ["GXU_FIVE_HOURLY", "GXU_WEEKLY", "GXU_DAILY"]
    windows = [{"label": GEMINI_POOLS.get(t, t.replace("GXU_", "").title()),
                "percent": round(pools[t][0] * scale, 1), "resets_at": _iso_to_epoch(pools[t][1]) if pools[t][1] else None}
               for t in sorted(pools, key=lambda t: order.index(t) if t in order else 99)]
    t = tier.group(1) if tier else None
    return {"plan": GEMINI_TIERS.get(t, t), "windows": windows}


def _fmt_tokens(n):
    if n >= 1e8:
        return f"{n / 1e8:.1f} 亿"
    if n >= 1e4:
        return f"{n / 1e4:.0f} 万"
    return f"{n:.0f}"


# ---------- Kimi Work（用户粘贴的 kimi.com 计费请求） ----------
SCOPE_NAMES = {"FEATURE_CODING": "Code", "FEATURE_AGENT": "Agent", "FEATURE_OK_COMPUTER": "Agent",
               "FEATURE_WORK": "Work", "FEATURE_CHAT": "对话"}


def fetch_kimiwork():
    # 优先用 Chrome 扩展旁听到的「我的订阅」页数据；没有时退回设置页粘贴的请求
    payload, ext_err = ext_bridge.get("kimiwork")
    if payload:
        return {**_kimi_subscription_stats(payload), "plan": payload.get("plan")}
    if ext_err and not manual_auth.load_all().get("kimiwork"):
        raise NeedAction(ext_err)
    try:
        d = manual_auth.replay("kimiwork")
    except manual_auth.AuthMissing:
        raise NeedAction("安装 Chrome 扩展「AI 额度看板助手」并在 Chrome 登录 kimi.com 后自动显示")
    except manual_auth.AuthExpired:
        raise NeedAction("Kimi 登录已过期，请在设置页重新粘贴一次")
    if "subscriptionBalance" in d or "ratelimitCode5h" in d:
        return _kimi_subscription_stats(d)
    windows = [] if "usages" in d else _generic_windows(d)
    for u in d.get("usages") or []:
        scope = SCOPE_NAMES.get(u.get("scope"), (u.get("scope") or "").replace("FEATURE_", "").title())
        r = _kimi_detail(u.get("detail") or {})
        if r:
            windows.append({"label": f"{scope} 本周", "percent": r[0], "resets_at": r[1]})
        for lm in u.get("limits") or []:
            r = _kimi_detail(lm.get("detail") or {})
            win = lm.get("window") or {}
            mins = win.get("duration", 0) * (60 if "HOUR" in str(win.get("timeUnit")) else 1)
            if r:
                windows.append({"label": f"{scope} {round(mins / 60)} 小时" if mins else scope,
                                "percent": r[0], "resets_at": r[1]})
    if not windows:
        _dump_shape("kimiwork", d)
        raise NeedAction("Kimi 返回里没识别出额度数据（已记录返回结构，告诉 Claude 来适配）")
    return {"plan": None, "windows": windows[:4]}


def _any_time(v):
    """Kimi 时间字段可能是 ISO 字符串、秒或毫秒（数字或数字串）。"""
    if isinstance(v, dict):
        v = v.get("timestamp") or v.get("seconds")
    if v in (None, ""):
        return None
    s = str(v)
    if s.replace(".", "", 1).isdigit():
        n = float(s)
        return n / 1000 if n > 1e12 else n
    return _iso_to_epoch(s)


def _kimi_subscription_stats(d):
    """kimi.com「我的订阅」页 GetSubscriptionStats。会员额度按总用量算（Kimi Work + Code 合计）：
    subscriptionBalance.amountUsedRatio 是会员总额度的已用比例（已含 Code），
    kimiCodeUsedRatio 是其中 Code 的部分，只用来显示「其中 Code x%」；
    upcomingExpiration 是下次刷新时间（每 7 天一个周期）；giftBalances 是赠送额度。"""
    windows = []
    sb = d.get("subscriptionBalance") or {}
    if isinstance(sb.get("amountUsedRatio"), (int, float)):
        code = sb.get("kimiCodeUsedRatio") if isinstance(sb.get("kimiCodeUsedRatio"), (int, float)) else 0
        windows.append({"label": "会员额度", "percent": round(sb["amountUsedRatio"] * 100, 1),
                        "code_percent": round(code * 100, 1),
                        "resets_at": _any_time(sb.get("upcomingExpiration")), "window_secs": 7 * 86400})
    for key, label in (("ratelimitCode5h", "Code 5 小时"), ("ratelimitCode7d", "Code 本周")):
        w = d.get(key) or {}
        if w.get("enabled") is not False and isinstance(w.get("ratio"), (int, float)):
            windows.append({"label": label, "percent": round(w["ratio"] * 100, 2),
                            "resets_at": _any_time(w.get("resetTime"))})
    for g in d.get("giftBalances") or []:
        if isinstance(g.get("amountUsedRatio"), (int, float)):
            windows.append({"label": (g.get("displayName") or "赠送额度")[:12],
                            "percent": round(g["amountUsedRatio"] * 100, 1),
                            "resets_at": None, "expires_at": _any_time(g.get("expireTime"))})
    if not windows:
        _dump_shape("kimiwork", d)
        raise NeedAction("Kimi 返回里没识别出额度数据（已记录返回结构，告诉 Claude 来适配）")
    return {"plan": None, "windows": windows}


def _generic_windows(d):
    """GetSubscriptionStats 的返回格式未公开：遍历 JSON，凡是同时有
    「已用/上限」或「已用比例」的对象都当作一个额度窗口。"""
    out = []

    def pct_of(o):
        for k in ("usedRatio", "used_ratio", "usageRatio", "ratio"):
            if isinstance(o.get(k), (int, float, str)) and str(o[k]).replace(".", "", 1).isdigit():
                v = float(o[k])
                return v * 100 if v <= 1 else v
        for k in ("percent", "usedPercent", "used_percent", "usagePercent"):
            if isinstance(o.get(k), (int, float)):
                return float(o[k])
        lim = o.get("limit") or o.get("total") or o.get("quota")
        used = o.get("used") if o.get("used") is not None else (
            float(lim) - float(o["remaining"]) if lim and o.get("remaining") is not None else None)
        try:
            if lim and used is not None and float(lim) > 0:
                return float(used) / float(lim) * 100
        except (TypeError, ValueError):
            pass
        return None

    def walk(o, path):
        if isinstance(o, dict):
            p = pct_of(o)
            if p is not None:
                name = o.get("title") or o.get("name") or o.get("displayName") or o.get("scope") or \
                    (path[-1] if path else "总量")
                reset = o.get("resetTime") or o.get("reset_time") or o.get("resetAt") or o.get("nextResetTime")
                out.append({"label": str(name).replace("FEATURE_", "").title()[:16],
                            "percent": round(p, 1), "resets_at": _iso_to_epoch(reset) if isinstance(reset, str)
                            else (reset / (1000 if reset and reset > 1e12 else 1) if reset else None)})
            for k, v in o.items():
                walk(v, path + [k])
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, path)
    walk(d, [])
    return out


def _dump_shape(name, d):
    """把返回结构（只有字段名和数值类型，不含字符串内容）存下来，方便适配。"""
    def shape(x, depth=0):
        if isinstance(x, dict) and depth < 5:
            return {k: shape(v, depth + 1) for k, v in list(x.items())[:40]}
        if isinstance(x, list):
            return [shape(x[0], depth + 1)] if x else []
        return x if isinstance(x, (int, float, bool)) or x is None else "str"
    try:
        (Path(__file__).resolve().parent / f".shape_{name}.json").write_text(
            json.dumps(shape(d), ensure_ascii=False, indent=1))
    except OSError:
        pass


ACCOUNTS = [
    {"id": "claude", "name": "Claude", "fetch": fetch_claude},
    {"id": "chatgpt", "name": "ChatGPT", "fetch": fetch_chatgpt},
    {"id": "kimiwork", "name": "Kimi", "fetch": fetch_kimiwork},
    {"id": "workbuddy", "name": "WorkBuddy", "fetch": fetch_workbuddy},
    {"id": "gemini", "name": "Gemini", "fetch": fetch_gemini},
]
UNSUPPORTED = []


def _load_disk():
    try:
        return json.loads(CACHE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def get_quotas(force=False):
    """返回各账号额度。成功结果缓存到磁盘，失败时回退到上次成功的数据。"""
    with _lock:
        if not _mem:
            _mem.update(_load_disk())
        now = time.time()
        out = []
        for acc in ACCOUNTS:
            prev = _mem.get(acc["id"]) or {}
            entry = dict(prev)
            if force or now - prev.get("checked_at", 0) > TTL:
                entry["checked_at"] = now
                try:
                    entry = {**acc["fetch"](), "status": "ok", "message": None,
                             "fetched_at": now, "checked_at": now}
                except NeedAction as e:
                    entry.update(status="action", message=str(e))
                except Exception as e:  # 网络等临时错误
                    entry.update(status="error", message=f"暂时获取失败（{type(e).__name__}）")
                _mem[acc["id"]] = entry
            out.append({"id": acc["id"], "name": acc["name"], **entry})
        try:
            CACHE_FILE.write_text(json.dumps(_mem, ensure_ascii=False))
        except OSError:
            pass
    for u in UNSUPPORTED:
        out.append({**u, "status": "unsupported", "windows": []})
    for a in out:
        a["card"] = build_card(a)
    return out


# 由 server.py 注入：TOKEN_FN(账号 id, 起始 epoch 秒) → 这台 Mac 在该时段的 token 数
TOKEN_FN = None


def _win(a, *labels):
    """按标签找额度窗口，找不到返回 None。"""
    for w in a.get("windows") or []:
        if w.get("label") in labels:
            return w
    return None


def _pct(w):
    """已过重置时间的窗口按 0% 算。"""
    if not w:
        return None
    return 0 if w.get("resets_at") and w["resets_at"] < time.time() else w.get("percent")


def _wan(n):
    """token 数写成「2,180 万」「2.35 亿」这种形式。"""
    if n is None:
        return None
    if n >= 1e8:
        return f"{n / 1e8:.2f} 亿"
    return f"{n / 1e4:,.0f} 万" if n >= 1e4 else f"{n:,.0f}"


def _reset_text(ts):
    """48 小时内写「4:26 后重置」，更远写「10/23 重置」。"""
    if not ts:
        return None
    sec = ts - time.time()
    if sec <= 0:
        return "已重置"
    if sec < 48 * 3600:
        return f"{int(sec // 3600)}:{int(sec % 3600 // 60):02d} 后重置"
    return datetime.fromtimestamp(ts).strftime("%-m/%d 重置")


def _month_start():
    return datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


def _month_tokens(acc_id):
    """这台 Mac 本月 1 日以来该账号的 token 数；没有本地日志返回 None。"""
    if not TOKEN_FN:
        return None
    try:
        n = TOKEN_FN(acc_id, _month_start())
    except Exception:
        return None
    return n or None


def build_card(a):
    """把各账号整理成同一种结构，网页 pizza 图和手机小组件都只读它：
    primary（大号 %，决定填充和颜色）、sub（次要指标）、tokens（本月累计 token 行）、
    footer（主指标的重置时间）、warn（需要处理的提示）。"""
    aid = a.get("id")
    # 会员等级：来自各家接口；Kimi 由 Chrome 扩展从「我的订阅」页读取
    plan = a.get("plan")
    if plan:
        plan = plan.replace("Buddy AI", "").strip()
    card = {"primary": None, "sub": None, "tokens": None, "footer": None, "warn": None, "plan": plan}
    msg = a.get("hint") or (a.get("message") if a.get("status") not in ("ok", None) else None)
    p, month = None, None
    if aid in ("claude", "chatgpt", "gemini"):
        # 大号 % 用 5 小时额度（日常最先卡住的瓶颈），本周额度放在次要行
        p, wk = _win(a, "5 小时"), _win(a, "本周")
        if p is None:
            p, wk = wk, None
        if p:
            card["sub"] = f"本周 {_pct(wk):g}%" if wk else p["label"]
        month = _month_tokens(aid)
    elif aid == "kimiwork":
        p = _win(a, "会员额度")
        if p:
            c7 = _win(a, "Code 本周")
            card["sub"] = f"Code 本周 {_pct(c7):g}%" if c7 else ""
        month = _month_tokens(aid)
    elif aid == "workbuddy":
        p = (a.get("windows") or [None])[0]
        if p:
            card["sub"] = f"积分 · 剩 {a.get('remaining', 0):,.0f}"
        month = a.get("used_tokens")  # 账号本周期（约一个月）已用积分 × 每积分 token 数
    if p:
        pct = _pct(p)
        card["primary"] = {"label": p["label"], "percent": pct, "value": f"{pct:g}%"}
        card["footer"] = _reset_text(p.get("resets_at"))
        card["tokens"] = f"本月消耗 {_wan(month)} token" if month else "本月消耗 —"
    if msg:
        card["warn"] = msg
        # 披萨图和小组件里放不下整句，用一个短标签说清楚要做什么
        card["warn_short"] = ("需重新粘贴" if "重新粘贴" in msg else "需粘贴" if "粘贴" in msg else "需打开 Claude Code" if "Claude Code" in msg
                              else "需打开 Codex" if "Codex" in msg else "需打开 Chrome" if "Chrome" in msg
                              else "需重新登录" if "登录" in msg else "需处理")
    return card


def invalidate(acc_id):
    """设置页保存新的登录请求后，让该账号下次立即重新查询。"""
    with _lock:
        if acc_id in _mem:
            _mem[acc_id]["checked_at"] = 0


if __name__ == "__main__":
    print(json.dumps(get_quotas(force=True), ensure_ascii=False, indent=1))
