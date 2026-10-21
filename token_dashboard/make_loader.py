#!/usr/bin/env python3
"""生成手机上的 Scriptable 加载器「AI额度.js」。

需要先运行过一次看板服务（会生成 .access_key 和 .gist_id，并已用 gh 登录 GitHub）。
生成的文件含访问密钥，只用于隔空投送到自己的 iPhone，不要提交或分享。

用法: python3 make_loader.py [--port 8787]
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args()

    key_file, gid_file = HERE / ".access_key", HERE / ".gist_id"
    if not key_file.exists():
        sys.exit("还没有 .access_key：先启动一次看板服务（python3 server.py）。")
    if not gid_file.exists():
        sys.exit("还没有 .gist_id：确认已运行 gh auth login，并让看板服务运行几分钟完成第一次推送。")
    gh = shutil.which("gh") or "/opt/homebrew/bin/gh"
    login = subprocess.run([gh, "api", "user", "--jq", ".login"], capture_output=True, text=True).stdout.strip()
    if not login:
        sys.exit("gh 未登录：先运行 gh auth login。")

    import server  # 复用局域网 IP 检测
    gid = gid_file.read_text().strip()
    base = f"https://gist.githubusercontent.com/{login}/{gid}/raw"
    code = (HERE / "loader.template.js").read_text()
    for k, v in {"KEY": key_file.read_text().strip(), "SERVER": f"http://{server.lan_ip()}:{args.port}",
                 "DATA_URL": f"{base}/ai-quota.json", "CODE_URL": f"{base}/widget.js"}.items():
        code = code.replace("{{%s}}" % k, v)
    out = HERE / "AI额度.js"
    out.write_text(code)
    print(f"已生成 {out}\n用隔空投送发到 iPhone，用 Scriptable 打开并添加即可。")


if __name__ == "__main__":
    main()
