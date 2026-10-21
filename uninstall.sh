#!/bin/bash
# 停止并移除开机自启（不删除工程文件和本地数据）
LABEL="com.$(whoami).ai-quota-dashboard"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null && echo "已停止服务" || echo "服务未在运行"
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist" && echo "已移除开机自启"
echo "如需彻底清理：删除工程文件夹、chrome://extensions 里的扩展、GitHub 上的 Gist、iPhone 上的 Scriptable 脚本。"
