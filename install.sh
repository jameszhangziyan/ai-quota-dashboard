#!/bin/bash
# AI 额度 Pizza 看板 · 一键安装（macOS）
# 做的事：检查环境 → 生成访问密钥 → 生成 Chrome 扩展配置 → 设置开机自启 → 打印后续步骤。
# 可重复运行；不会覆盖已有的密钥和登录信息。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
APP="$ROOT/token_dashboard"
PORT="${PORT:-8787}"
LABEL="com.$(whoami).ai-quota-dashboard"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/ai-quota-dashboard.log"

say() { printf "\n\033[1m%s\033[0m\n" "$1"; }

say "1/5 检查环境"
[[ "$(uname)" == "Darwin" ]] || { echo "目前只支持 macOS。"; exit 1; }
PY="$(command -v python3 || true)"
[[ -n "$PY" ]] || { echo "没有 python3：运行 xcode-select --install 安装命令行工具。"; exit 1; }
echo "python3: $PY ($("$PY" --version 2>&1))"
case "$ROOT" in
  "$HOME/Documents"*|"$HOME/Desktop"*|"$HOME/Downloads"*)
    echo "⚠️  工程在「文稿/桌面/下载」里：macOS 隐私保护会阻止开机自启的服务读取这里。"
    echo "   建议移到主目录下，例如：mv \"$ROOT\" ~/ai-quota-dashboard，再重新运行本脚本。"
    read -r -p "仍然继续？[y/N] " a; [[ "$a" == "y" ]] || exit 1 ;;
esac

say "2/5 生成访问密钥"
cd "$APP"
"$PY" -c "import server; server.access_key()"
KEY="$(cat "$APP/.access_key")"
echo "密钥已保存在 token_dashboard/.access_key（权限 600）"

say "3/5 生成 Chrome 扩展配置"
printf 'self.AIQ_CONFIG = { dashboard: "http://127.0.0.1:%s", key: "%s" };\n' "$PORT" "$KEY" > "$APP/chrome_extension/config.js"
echo "已写入 token_dashboard/chrome_extension/config.js"

say "4/5 检查 GitHub CLI（用于把数据推到私密 Gist，供手机小组件读取）"
if command -v gh >/dev/null 2>&1; then
  if gh auth status >/dev/null 2>&1; then echo "gh 已登录"; else
    echo "gh 未登录：稍后运行  gh auth login --web --scopes gist"; fi
else
  echo "未安装 gh：运行  brew install gh && gh auth login --web --scopes gist"
  echo "（不装也能用网页看板，只是手机小组件在外网读不到数据）"
fi

say "5/5 设置开机自启（LaunchAgent）"
read -r -p "现在设置开机自启并启动服务？[Y/n] " a
if [[ "${a:-Y}" != "n" ]]; then
  mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$PY</string><string>-u</string><string>server.py</string><string>--port</string><string>$PORT</string></array>
  <key>WorkingDirectory</key><string>$APP</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  sleep 5
  if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then echo "服务已启动（日志：$LOG）"
  else echo "服务没起来，看日志：tail -20 \"$LOG\""; fi
else
  echo "跳过。手动启动：cd \"$APP\" && python3 server.py"
fi

cat <<EOF

✅ 安装完成。接下来：

1. 打开看板：     http://127.0.0.1:$PORT/?k=$KEY
2. Chrome 扩展（Gemini、Kimi 额度）：
   chrome://extensions → 打开「开发者模式」→「加载已解压的扩展程序」→ 选
   $APP/chrome_extension
   并确保 Chrome 里已登录 gemini.google.com 和 kimi.com。
3. WorkBuddy 额度：打开 http://127.0.0.1:$PORT/settings?k=$KEY ，按页面步骤粘贴一次请求。
4. iPhone 小组件（需 gh 已登录，且服务已运行几分钟完成首次推送）：
   cd "$APP" && python3 make_loader.py
   把生成的 AI额度.js 隔空投送到 iPhone，用 Scriptable 打开，再把它加到桌面小组件。
EOF
