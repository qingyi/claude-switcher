#!/bin/bash
set -eu
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
launchctl bootout "gui/$(id -u)/local.claude-switcher.bodyguard" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/local.claude-switcher.bodyguard.plist"
pkill -x ClaudeSwitcher 2>/dev/null || true
# Move only bundles owned by this project to Trash; retain all private data.
python3 - <<'PY_UNINSTALL'
from pathlib import Path
import json,plistlib,shutil,uuid
home=Path.home()
paths=[Path('/Applications/Claude切换器.app'),home/'Applications/Claude切换器.app',home/'Desktop/Claude切换器.app']
config=home/'.claude-switcher/config.json'
if config.exists():
    value=json.loads(config.read_text()).get('app_path')
    if value:paths.insert(0,Path(value))
trash=home/'.Trash';trash.mkdir(exist_ok=True)
for app in dict.fromkeys(paths):
    if app.is_symlink() or not app.is_dir():continue
    info=app/'Contents/Info.plist'
    if not info.exists() or plistlib.loads(info.read_bytes()).get('CFBundleIdentifier')!='local.claude-switcher':continue
    shutil.move(app,trash/('Claude切换器-'+uuid.uuid4().hex+'.app'))
print('已停用并将应用移至废纸篓。账号存档、脚本和安装备份已保留，系统设置未修改。')
PY_UNINSTALL
