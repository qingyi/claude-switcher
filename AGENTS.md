# macOS Shadowrocket 版交接

用户要求以 Shadowrocket 替代 Clash Verge，并保留已有代理节点和系统时区、地区、语言、Wi-Fi IPv6 偏好。不得恢复上游写死的 Clash 检查或自动修改这些系统偏好。Windows/Linux 仍保留上游实现。

- 核心逻辑：`core/claude-switch.py`，`core/claude-switch.sh` 是 GUI/CLI 公共入口。
- UI 必须展示与后端相同的 14 项结果；红色故障阻止启动，黄色提示不能标成验证通过。
- 任何实际 Claude 启动都必须经过安检，出口必须确认为美国。
- 账号参数只能 A 或 B，完全退出 Claude 后交换数据。保留 `accounts/A`、`accounts/B` 布局，不修改钥匙串。
- 复制失败不可删除原数据；切换加锁，保留事务记录与回滚数据；损坏记录不可猜测路径继续删除。
- 保镖安装为用户 LaunchAgent，只作用于本程序绑定 PID 的托管会话；不终止用户已有的非托管 Claude。
- 日志、文档、工具输出不得泄露节点、住宅/代理 IP、订阅链接、凭据或快照原文。仅保存出口散列与脱敏状态。
- Shadowrocket 当前配置数据库仅只读审查。自动导出仅支持 General/Rule，独立副本由 Shadowrocket 原生导入，原配置保留。
- 安装：`python3 installer/install.py --install --guard`；编译需 Swift、Python3，默认安装到 /Applications，可用 --app-dir 指定目录；写入 config.json 的 app_path 并更新 LaunchAgent，旧版程序必须备份。
- 测试：`python3 -m unittest discover -s tests -v`。测试隔离 HOME/数据和进程，不断开真实 VPN，不切换真实用户账号。新增保护逻辑应补充对应的失败路径测试。
- 活体验收：指定 VPN、隧道进程、Fake-IP 路由、出口与当前生效规则通过，GUI/CLI 结果一致。不能宣称模拟测试已经证明真实 A/B 登录或绝对防泄漏。

具体安装、恢复与已知边界见 README.md。
