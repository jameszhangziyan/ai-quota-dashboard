"""接收 Chrome 扩展「AI 额度看板助手」上报的用量（只有数字，不含登录信息）。

扩展每 15 分钟借用 Chrome 里已登录的 Gemini / Kimi 页面查一次，POST 到 /api/ext。
这里按账号保存最近一次结果，quota.py 优先用它；超过 MAX_AGE 视为过期。
"""
import json
import threading
import time
from pathlib import Path

DATA_FILE = Path(__file__).resolve().parent / ".ext_data.json"
MAX_AGE = 3 * 3600  # 扩展每 15 分钟一次；3 小时没更新说明 Chrome 没开或扩展出错
PROVIDERS = {"gemini", "kimiwork"}
_lock = threading.Lock()


def _load():
    try:
        return json.loads(DATA_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save(provider, payload, error):
    if provider not in PROVIDERS:
        raise ValueError("未知账号")
    with _lock:
        data = _load()
        entry = data.get(provider) or {}
        if payload is not None:
            entry.update(payload=payload, at=time.time(), error=None)
        else:
            entry.update(error=(error or "未知错误")[:200], error_at=time.time())
        data[provider] = entry
        DATA_FILE.write_text(json.dumps(data, ensure_ascii=False))


def get(provider):
    """返回 (payload, error)：有新鲜数据时 error 为 None；否则 payload 为 None。"""
    e = _load().get(provider) or {}
    if e.get("payload") is not None and time.time() - e.get("at", 0) < MAX_AGE:
        return e["payload"], None
    if e.get("error"):
        return None, e["error"]
    if e.get("payload") is not None:
        return None, "Chrome 扩展超过 3 小时没有更新（Chrome 没开？）"
    return None, None
