#!/bin/bash
set -eu
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd "$(dirname "$0")"
python3 install.py --install --guard
printf '\n安装完成。Shadowrocket 当前配置需具有 Claude 置顶规则，详见 README。\n'
