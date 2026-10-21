"""每 5 分钟把额度结果推到 GitHub 私密 Gist，让 iPhone 小组件在任何网络都能读到。

- 只上传展示用的数字：账号名、会员等级、百分比、重置时间、本月 token、需处理提示。
  不含任何令牌、Cookie、请求地址。
- 用 GitHub 官方命令行 gh 上传，令牌由 gh 保存在系统钥匙串里，本程序不接触。
- Gist ID 记在 .gist_id；第一次运行时自动创建（私密，不公开列出）。
"""
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
GIST_ID_FILE = HERE / ".gist_id"
FILENAME = "ai-quota.json"
INTERVAL = 300
GH = shutil.which("gh") or "/opt/homebrew/bin/gh"
CARD_KEYS = ("primary", "sub", "tokens", "footer", "warn", "warn_short", "plan")

status = {"last_ok": None, "last_error": None, "raw_url": None}
SERVER_URL = ""  # 由 server.py 启动时填入，例如 http://192.168.1.10:8787
_user = {}


def public_payload(widget):
    """从小组件数据里挑出可以公开的部分。"""
    return {
        "generated": widget.get("generated"),
        "quotas": [{"id": a.get("id"), "name": a.get("name"),
                    "card": {k: (a.get("card") or {}).get(k) for k in CARD_KEYS}}
                   for a in widget.get("quotas") or []],
    }


def _gh(*args, input_text=None):
    r = subprocess.run([GH, *args], capture_output=True, text=True, timeout=60, input=input_text)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:300])
    return r.stdout.strip()


def _ensure_gist(path):
    if GIST_ID_FILE.exists():
        return GIST_ID_FILE.read_text().strip()
    url = _gh("gist", "create", "--filename", FILENAME, "--desc", "AI 额度看板数据（只含百分比）", str(path))
    gid = url.rstrip("/").split("/")[-1]
    GIST_ID_FILE.write_text(gid)
    return gid


WIDGET_SRC = HERE / "TokenWidget.js"
WIDGET_NAME = "widget.js"
WIDGET_HASH_FILE = HERE / ".widget_hash"


def _sync_widget_code(gid, tmpdir, data_url):
    """小组件代码有改动时，同步一份到 Gist（手机上的加载器每次从这里取最新代码）。
    仓库里的代码是通用的：上传时填入本机的数据地址和局域网地址；访问密钥不上传，由加载器自带。"""
    code = WIDGET_SRC.read_text()
    code = re.sub(r'globalThis\.TW_KEY \|\| "[^"]*"', 'globalThis.TW_KEY || ""', code)
    code = code.replace('"__DATA_URL__"', json.dumps(data_url)).replace('"__SERVER__"', json.dumps(SERVER_URL))
    digest = hashlib.sha256(code.encode()).hexdigest()
    if WIDGET_HASH_FILE.exists() and WIDGET_HASH_FILE.read_text().strip() == digest:
        return
    p = tmpdir / WIDGET_NAME
    p.write_text(code)
    files = json.loads(_gh("api", f"gists/{gid}", "--jq", "[.files | keys[]]"))
    if WIDGET_NAME in files:
        _gh("gist", "edit", gid, "--filename", WIDGET_NAME, str(p))
    else:
        _gh("gist", "edit", gid, "--add", str(p))
    WIDGET_HASH_FILE.write_text(digest)


def raw_url(gid, name=FILENAME):
    if "login" not in _user:
        _user["login"] = _gh("api", "user", "--jq", ".login")
    return f"https://gist.githubusercontent.com/{_user['login']}/{gid}/raw/{name}"


def push_once(widget):
    data = json.dumps(public_payload(widget), ensure_ascii=False)
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / FILENAME
        p.write_text(data)
        new = not GIST_ID_FILE.exists()
        gid = _ensure_gist(p)
        if not new:
            _gh("gist", "edit", gid, "--filename", FILENAME, str(p))
        status["raw_url"] = raw_url(gid)
        _sync_widget_code(gid, Path(td), status["raw_url"])
    status.update(last_ok=time.time(), last_error=None)
    return status["raw_url"]


def start(get_widget, server_url=""):
    """后台线程：每 INTERVAL 秒推一次；失败（未登录、断网）只记录，不影响看板。"""
    global SERVER_URL
    SERVER_URL = server_url
    def loop():
        while True:
            try:
                push_once(get_widget())
            except Exception as e:
                status["last_error"] = f"{type(e).__name__}: {e}"[:300]
            time.sleep(INTERVAL)
    threading.Thread(target=loop, daemon=True, name="gist-push").start()
