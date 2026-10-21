#!/usr/bin/env python3
"""Token 看板：读取本机 Claude Code 与 Codex 的会话日志，汇总 token 用量，
在局域网内提供一个适合手机查看的网页。只用 Python 标准库。

用法: python3 server.py [--port 8787] [--host 0.0.0.0]
"""
import argparse
import json
import os
import secrets
import socket
import struct
import threading
import zlib
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import ext_bridge
import gist_push
import manual_auth
import quota

HERE = Path(__file__).resolve().parent
CLAUDE_DIR = Path.home() / ".claude" / "projects"
CODEX_DIR = Path.home() / ".codex" / "sessions"
WORKBUDDY_DIR = Path.home() / ".workbuddy" / "traces"
GEMINI_DIR = Path.home() / ".gemini" / "tmp"
# Kimi Work（Kimi 桌面版内置的 kimi-code）和独立安装的 Kimi Code CLI 用同一种 wire.jsonl 日志
KIMI_DIRS = [
    Path.home() / "Library" / "Application Support" / "kimi-desktop" / "daimon-share" / "daimon"
    / "runtime" / "kimi-code" / "home" / "sessions",
    Path.home() / ".kimi-code",
]
KEY_FILE = HERE / ".access_key"
LOCAL_TZ = datetime.now().astimezone().tzinfo

# 每个文件的解析结果按 (mtime, size) 缓存，只重新解析有变化的文件
_cache = {}
_lock = threading.Lock()


def epoch(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def local_day(ts):
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d")


def project_name(cwd):
    if not cwd:
        return "(未知)"
    if "scratch-workspaces" in cwd:
        return "(临时会话)"
    return Path(cwd).name or cwd


def parse_claude(path):
    """返回记录列表: (day, model, project, input, cache_write, cache_read, output, epoch秒)。
    同一条消息会因流式输出写多行，按 message.id + requestId 去重。"""
    seen, rows = set(), []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if '"usage"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            m = d.get("message") or {}
            u = m.get("usage")
            if not isinstance(u, dict) or not d.get("timestamp"):
                continue
            key = (m.get("id"), d.get("requestId"))
            if key[0] and key in seen:
                continue
            seen.add(key)
            model = m.get("model") or "unknown"
            if model == "<synthetic>":
                continue
            rows.append((
                local_day(d["timestamp"]), model, project_name(d.get("cwd")),
                u.get("input_tokens", 0) or 0,
                u.get("cache_creation_input_tokens", 0) or 0,
                u.get("cache_read_input_tokens", 0) or 0,
                u.get("output_tokens", 0) or 0,
                epoch(d["timestamp"]),
            ))
    return {"rows": rows, "limits": None}


def parse_codex(path):
    """Codex 每次请求后写一条 token_count 事件，last_token_usage 是本次增量。
    input_tokens 已包含缓存命中部分，这里拆成 未缓存输入 + 缓存读取。"""
    rows, model, cwd, last_total, limits = [], "unknown", None, None, None
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if "turn_context" in line[:120] or '"session_meta"' in line[:120]:
                try:
                    p = json.loads(line).get("payload") or {}
                except ValueError:
                    continue
                model = p.get("model") or model
                cwd = p.get("cwd") or cwd
                continue
            if '"token_count"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            p = d.get("payload") or {}
            info = p.get("info")
            if p.get("rate_limits"):
                limits = {"ts": d.get("timestamp"), **p["rate_limits"]}
            if not info:
                continue
            total = (info.get("total_token_usage") or {}).get("total_tokens")
            if total is not None and total == last_total:
                continue  # 重复事件
            last_total = total
            u = info.get("last_token_usage") or {}
            inp = u.get("input_tokens", 0) or 0
            cached = u.get("cached_input_tokens", 0) or 0
            rows.append((
                local_day(d["timestamp"]), model, project_name(cwd),
                max(inp - cached, 0),
                u.get("cache_write_input_tokens", 0) or 0,
                cached,
                u.get("output_tokens", 0) or 0,
                epoch(d["timestamp"]),
            ))
    return {"rows": rows, "limits": limits}


def parse_workbuddy(path):
    """WorkBuddy 每个 trace 文件头部有本次任务的汇总 (modelInfo)。
    totalInputTokens 含缓存命中部分。"""
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        t = (json.load(f) or {}).get("trace") or {}
    mi = t.get("modelInfo") or {}
    inp = mi.get("totalInputTokens") or 0
    out = mi.get("totalOutputTokens") or 0
    cached = mi.get("totalCachedTokens") or 0
    if not (inp or out) and t.get("totalTokens"):
        inp = t["totalTokens"]
    if not (inp or out) or not t.get("startedAt"):
        return {"rows": [], "limits": None}
    models = [m for m in mi.get("models") or [] if m]
    model = models[0] if models else "unknown"
    return {"rows": [(local_day(t["startedAt"]), model, t.get("agentName") or "WorkBuddy",
                      max(inp - cached, 0), 0, cached, out, epoch(t["startedAt"]))], "limits": None}


def parse_kimi(path):
    """每次模型请求后写一条 usage.record：inputOther 为未缓存输入。"""
    rows = []
    proj = "(Kimi Work)" if "kimi-desktop" in str(path) else "(Kimi Code)"
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if '"usage.record"' not in line[:40]:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            u = d.get("usage") or {}
            if not d.get("time"):
                continue
            day = datetime.fromtimestamp(d["time"] / 1000, LOCAL_TZ).strftime("%Y-%m-%d")
            rows.append((day, d.get("model") or "kimi", proj,
                         u.get("inputOther", 0) or 0, u.get("inputCacheCreation", 0) or 0,
                         u.get("inputCacheRead", 0) or 0, u.get("output", 0) or 0, d["time"] / 1000))
    return {"rows": rows, "limits": None}


def parse_gemini(path):
    """Gemini CLI 的会话文件 (~/.gemini/tmp/*/chats/*.json)，每条 gemini 消息带 tokens。"""
    rows = []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        d = json.load(f)
    for m in (d.get("messages") or []) if isinstance(d, dict) else []:
        tk = m.get("tokens") if isinstance(m, dict) else None
        if not tk or not m.get("timestamp"):
            continue
        cached = tk.get("cached", 0) or 0
        rows.append((local_day(m["timestamp"]), m.get("model") or "gemini", "(Gemini CLI)",
                     max((tk.get("input", 0) or 0) - cached, 0), 0, cached,
                     (tk.get("output", 0) or 0) + (tk.get("thoughts", 0) or 0), epoch(m["timestamp"])))
    return {"rows": rows, "limits": None}


FAMILIES = [
    ("Claude", ("claude",)),
    ("ChatGPT / OpenAI", ("gpt", "codex", "o1", "o3", "o4", "openai")),
    ("Kimi", ("kimi", "moonshot", "k2", "k3")),
    ("Gemini", ("gemini",)),
    ("混元", ("hy", "hunyuan")),
    ("GLM", ("glm",)),
    ("MiniMax", ("minimax",)),
    ("阶跃 Step", ("step",)),
    ("DeepSeek", ("deepseek",)),
    ("通义 Qwen", ("qwen",)),
]


def family(model, agent):
    m = model.lower()
    for name, prefixes in FAMILIES:
        if any(m.startswith(p) for p in prefixes):
            return name
    if agent == "Codex":
        return "ChatGPT / OpenAI"
    if agent == "Claude Code":
        return "Claude"
    if agent == "Kimi":
        return "Kimi"
    return "其他 / 自动"


def scan(agent, roots, parser, pattern="*.jsonl"):
    out = []
    paths = [p for r in (roots if isinstance(roots, list) else [roots]) if r.exists()
             for p in r.rglob(pattern)]
    for path in paths:
        try:
            st = path.stat()
        except OSError:
            continue
        sig = (st.st_mtime, st.st_size)
        hit = _cache.get(path)
        if not hit or hit[0] != sig:
            try:
                hit = (sig, parser(path))
            except (OSError, ValueError, AttributeError):
                continue
            _cache[path] = hit
        out.append((st.st_mtime, hit[1]))
    return out


ACCOUNT_SOURCES = {
    # 账号 → (日志来源, 只算哪些项目)；Kimi Work 只算桌面版内置日志，不含 Kimi Code
    "claude": ("Claude Code", None),
    "chatgpt": ("Codex", None),
    "kimiwork": ("Kimi", None),  # Kimi 会员额度含 Kimi Work 和 Kimi Code，两边日志都算
    "gemini": ("Gemini CLI", None),
}
SOURCE_SCANS = {
    "Claude Code": lambda: scan("claude", CLAUDE_DIR, parse_claude),
    "Codex": lambda: scan("codex", CODEX_DIR, parse_codex),
    "Kimi": lambda: scan("kimi", KIMI_DIRS, parse_kimi, "wire.jsonl"),
    "Gemini CLI": lambda: scan("gemini", GEMINI_DIR, parse_gemini, "session-*.json"),
}


def local_tokens_since(acc_id, since):
    """这台 Mac 上某账号自 since（epoch 秒）以来实际消耗的 token 数：输入 + 缓存写入 + 输出。
    不计缓存读取：每轮对话都会把整段上下文从缓存重读一遍，计入会把数字放大几十倍。"""
    src = ACCOUNT_SOURCES.get(acc_id)
    if not src or not since:
        return None
    agent, projects = src
    with _lock:
        files = SOURCE_SCANS[agent]()
    total = 0
    for _mtime, parsed in files:
        for _day, _model, proj, i, cw, cr, o, ts in parsed["rows"]:
            if ts >= since and (projects is None or proj in projects):
                total += i + cw + o
    return total


quota.TOKEN_FN = local_tokens_since


def build_stats(days):
    with _lock:
        sources = {
            "Claude Code": scan("claude", CLAUDE_DIR, parse_claude),
            "Codex": scan("codex", CODEX_DIR, parse_codex),
            "Kimi": scan("kimi", KIMI_DIRS, parse_kimi, "wire.jsonl"),
            "WorkBuddy": scan("workbuddy", WORKBUDDY_DIR, parse_workbuddy, "trace_*.json"),
            "Gemini CLI": scan("gemini", GEMINI_DIR, parse_gemini, "session-*.json"),
        }

    today = datetime.now(LOCAL_TZ).date()
    start = today - timedelta(days=days - 1)
    day_keys = [(start + timedelta(days=i)).isoformat() for i in range(days)]
    blank = lambda: {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}

    daily = {a: {k: blank() for k in day_keys} for a in sources}
    by_model = defaultdict(blank)
    by_family = defaultdict(blank)
    family_daily = defaultdict(lambda: {k: 0 for k in day_keys})
    by_project = defaultdict(blank)
    totals = {a: blank() for a in sources}
    all_time = {a: 0 for a in sources}
    codex_limits = None

    for agent, files in sources.items():
        for mtime, parsed in sorted(files, key=lambda x: x[0]):
            lim = parsed["limits"]
            if lim and (not codex_limits or (lim.get("ts") or "") > (codex_limits.get("ts") or "")):
                codex_limits = lim
            for day, model, proj, i, cw, cr, o, _ts in parsed["rows"]:
                all_time[agent] += i + cw + cr + o
                if day < day_keys[0] or day > day_keys[-1]:
                    continue
                fam = family(model, agent)
                family_daily[fam][day] += i + cw + cr + o
                for bucket in (daily[agent][day], totals[agent], by_family[fam],
                               by_model[(agent, model)], by_project[proj]):
                    bucket["input"] += i
                    bucket["cache_write"] += cw
                    bucket["cache_read"] += cr
                    bucket["output"] += o

    tot = lambda b: sum(b.values())
    return {
        "generated": datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "days": day_keys,
        "agents": list(sources),
        "daily": {a: [daily[a][k] for k in day_keys] for a in sources},
        "totals": totals,
        "all_time": all_time,
        "models": sorted(
            ({"agent": a, "model": m, **b, "total": tot(b)} for (a, m), b in by_model.items()),
            key=lambda r: -r["total"]),
        "families": sorted(
            ({"family": f, **b, "total": tot(b),
              "daily": [family_daily[f][k] for k in day_keys]} for f, b in by_family.items()),
            key=lambda r: -r["total"]),
        "has_data": {a: bool(files) for a, files in sources.items()},
        "projects": sorted(
            ({"project": p, **b, "total": tot(b)} for p, b in by_project.items()),
            key=lambda r: -r["total"])[:12],
        "codex_limits": codex_limits,
    }


def widget_stats():
    """给 iPhone 小组件用的精简数据：今天各智能体、近 7 天每日、厂商排行、Codex 额度。"""
    today, week = build_stats(1), build_stats(7)
    sm = lambda b: sum(b.values())
    agents = [a for a in week["agents"] if week["has_data"][a]]
    return {
        "generated": week["generated"],
        "days": week["days"],
        "agents": [{
            "name": a,
            "today": sm(today["totals"][a]),
            "week": sm(week["totals"][a]),
            "daily": [sm(b) for b in week["daily"][a]],
        } for a in agents],
        "today_total": sum(sm(today["totals"][a]) for a in agents),
        "week_total": sum(sm(week["totals"][a]) for a in agents),
        "families_today": [{"name": f["family"], "total": f["total"]} for f in today["families"]][:5],
        "families_week": [{"name": f["family"], "total": f["total"]} for f in week["families"]][:5],
        "codex_limits": week["codex_limits"],
        "quotas": quota.get_quotas(),
    }


def make_icon(size=180):
    """主屏幕图标：深色圆角底 + 三根彩色柱子（纯标准库写 PNG）。"""
    bg, bars = (24, 24, 30), [((217, 119, 87), 0.55), ((16, 163, 127), 0.8), ((59, 111, 224), 0.38)]
    bw, gap = size * 0.16, size * 0.08
    x0 = (size - (3 * bw + 2 * gap)) / 2
    base, top = size * 0.78, size * 0.22
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            c = bg
            for i, (col, h) in enumerate(bars):
                bx = x0 + i * (bw + gap)
                if bx <= x < bx + bw and base - (base - top) * h <= y < base:
                    c = col
            row += bytes(c)
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


ICON = make_icon()


def access_key():
    if KEY_FILE.exists():
        return KEY_FILE.read_text().strip()
    key = secrets.token_urlsafe(9)
    KEY_FILE.write_text(key)
    os.chmod(KEY_FILE, 0o600)
    return key


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class Handler(BaseHTTPRequestHandler):
    key = ""

    def log_message(self, *args):
        pass

    def send(self, code, body, ctype):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path == "/icon.png":  # 图标不含数据，不需要密钥
            return self.send(200, ICON, "image/png")
        if q.get("k", [""])[0] != self.key:
            return self.send(403, "需要访问密钥：请使用启动时打印的完整链接。", "text/plain; charset=utf-8")
        if url.path == "/":
            return self.send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        if url.path == "/api/stats":
            try:
                days = max(1, min(int(q.get("days", ["30"])[0]), 365))
            except ValueError:
                days = 30
            return self.send(200, json.dumps(build_stats(days), ensure_ascii=False),
                             "application/json; charset=utf-8")
        if url.path in ("/settings", "/api/auth"):
            if not self.is_local():
                return self.send(403, "设置页只能在 Mac 本机打开。", "text/plain; charset=utf-8")
            if url.path == "/settings":
                return self.send(200, (HERE / "settings.html").read_bytes(), "text/html; charset=utf-8")
            return self.send_json(200, manual_auth.status())
        if url.path == "/api/quota":
            return self.send(200, json.dumps(quota.get_quotas(force=q.get("force") == ["1"]),
                                             ensure_ascii=False), "application/json; charset=utf-8")
        if url.path == "/api/widget":
            return self.send(200, json.dumps(widget_stats(), ensure_ascii=False),
                             "application/json; charset=utf-8")
        if url.path == "/manifest.json":
            return self.send(200, json.dumps({
                "name": "Token 看板", "short_name": "Token", "display": "standalone",
                "start_url": f"/?k={self.key}", "scope": "/",
                "background_color": "#16161a", "theme_color": "#16161a",
                "icons": [{"src": "/icon.png", "sizes": "180x180", "type": "image/png"}],
            }, ensure_ascii=False), "application/manifest+json; charset=utf-8")
        self.send(404, "not found", "text/plain; charset=utf-8")

    # ---------- 设置页：保存 / 清除手动粘贴的登录请求（仅本机） ----------
    def is_local(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def send_json(self, code, obj):
        self.send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def read_auth_request(self):
        url = urlparse(self.path)
        if parse_qs(url.query).get("k", [""])[0] != self.key or url.path != "/api/auth":
            self.send_json(403, {"error": "无权访问"})
            return None
        if not self.is_local():
            self.send_json(403, {"error": "设置只能在 Mac 本机进行"})
            return None
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(min(n, 200_000)) or b"{}")
        except ValueError:
            self.send_json(400, {"error": "请求格式不对"})
            return None

    def handle_ext_report(self):
        """Chrome 扩展上报用量（仅本机、需密钥）。只接收用量数字，存进 .ext_data.json。"""
        url = urlparse(self.path)
        if parse_qs(url.query).get("k", [""])[0] != self.key or not self.is_local():
            return self.send_json(403, {"error": "无权访问"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(min(n, 200_000)) or b"{}")
            ext_bridge.save(req.get("provider"), req.get("payload"), req.get("error"))
        except ValueError as e:
            return self.send_json(400, {"error": str(e)})
        quota.invalidate(req.get("provider"))
        self.send_json(200, {"ok": True})

    def do_POST(self):
        if urlparse(self.path).path == "/api/ext":
            return self.handle_ext_report()
        req = self.read_auth_request()
        if req is None:
            return
        asked = req.get("provider")
        try:
            provider = manual_auth.save(asked, req.get("curl") or "")
        except ValueError as e:
            return self.send_json(400, {"error": str(e)})
        moved = "" if provider == asked else \
            f"（这是 {manual_auth.PROVIDERS[provider]['name']} 的请求，已自动存到 {manual_auth.PROVIDERS[provider]['name']}）"
        quota.invalidate(provider)
        acc = next((a for a in quota.get_quotas() if a["id"] == provider), {})
        if acc.get("status") == "ok" and not acc.get("hint") and acc.get("windows"):
            parts = [f"{w['label']} 已用 {w['percent']}%" for w in acc["windows"][:3]]
            return self.send_json(200, {"ok": True, "message": "已保存，测试成功" + moved + "：" + "；".join(parts)})
        msg = acc.get("hint") or acc.get("message") or "已保存，但没查到额度数据"
        return self.send_json(200, {"ok": False, "message": "已保存" + moved + "，但测试失败：" + msg})

    def do_DELETE(self):
        req = self.read_auth_request()
        if req is None:
            return
        manual_auth.remove(req.get("provider"))
        quota.invalidate(req.get("provider"))
        self.send_json(200, {"ok": True})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()
    Handler.key = access_key()
    build_stats(30)  # 预热缓存
    host = lan_ip() if args.host == "0.0.0.0" else args.host
    print(f"本机打开:   http://127.0.0.1:{args.port}/?k={Handler.key}")
    print(f"手机打开:   http://{host}:{args.port}/?k={Handler.key}  (需与 Mac 在同一 Wi-Fi)")
    print(f"额度登录设置（仅本机）: http://127.0.0.1:{args.port}/settings?k={Handler.key}")
    if os.environ.get("AIQ_GIST", "1") != "0":  # AIQ_GIST=0 可关闭推送（只用网页看板时）
        gist_push.start(widget_stats, f"http://{host}:{args.port}")  # 每 5 分钟推送到 GitHub 私密 Gist，供外网的小组件读取
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
