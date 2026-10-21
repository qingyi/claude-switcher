# Claude 切换器 · Shadowrocket 版（macOS）

启动前检查 Shadowrocket VPN 与 Claude 路由，管理 A/B 两套 Claude 本地数据快照，并为经切换器启动的会话提供用户级断线保护。本分支替换 macOS 的 Clash Verge 依赖，Windows/Linux 目录保留上游实现，仍使用原来的依赖。本分支保留上游历史，macOS 使用此处的 Python 后端和安装器；不安装上游的自动更新服务，以免覆盖本分支适配。

## 本机安装

需要 macOS 13+、Claude 桌面版、Shadowrocket、Python 3.9+、Swift 命令行工具。当前已在 Apple Silicon、macOS 27.0.1、Shadowrocket 2.2.92 上验证编译、网络检查和配置导入。

```sh
python3 installer/install.py --install --guard
```

也可双击 `installer/安装.command`。默认安装到 `/Applications/Claude切换器.app`。安装会备份旧应用和脚本，生成独立配置副本，并安装用户级 LaunchAgent；不需要 sudo，不修改时区、地区、语言、Wi-Fi IPv6、节点或现有配置文件。

macOS 可能限制桌面程序读取其他 App 的数据。如果规则检查提示无法读取，请点界面「授权读取配置」，通过系统文件选择器仅选择当前配置名称所在的偏好文件与当前配置数据库。程序保存只读安全书签供用户级保护服务使用，不要求全盘访问权限；切换到其他配置数据库后可能需要重新授权。

如无权写入系统应用程序目录，可用 `python3 installer/install.py --install --guard --app-dir "$HOME/Applications"` 安装到本用户的应用程序目录。安装器会记录路径并同步用户级服务，迁移成功后将旧桌面副本移入私有安装备份。

首次在 Shadowrocket 导入 `~/.claude-switcher/Claude-Shadowrocket.conf`，点击该文件选择「使用配置」。原配置仍在原列表中，可以随时切回。保持全局路由为「配置」，选择合适的固定美国节点。切换器检测必要网络条件失败时不会启动 Claude。

配置副本来自当前启用配置的只读数据库快照，顶部增加 13 条 Claude 相关域名的 PROXY 规则，并明确设置 UDP 不支持时 REJECT。现有 Tailscale 与其他分流规则保持原顺序。自动导出目前仅支持 `[General]` 与 `[Rule]`；含脚本、证书或复杂策略组时会拒绝自动导出，需通过 Shadowrocket 原生导出审查后合并，不能直接改其内部数据库。

单独生成配置副本（输出只含配置，不含节点数据库）：

```sh
python3 installer/profile.py /path/to/Claude-Shadowrocket.conf
```

## 使用

1. 在「应用程序」中打开「Claude切换器」，等待 14 项检查。
2. 红色故障阻止启动；黄色提示表示保留的环境差异或尚未证实的条件。黄色不等于已经通过防泄漏验证。
3. 首次选择 A，将当前 Claude 数据存档为 A；B 首次使用会以空数据启动，需要自行登录。
4. 切换前完全退出 Claude，再复制快照、复查网络并启动；复制或启动失败会回滚原数据。
5. 「刷新」重新检查；「恢复」恢复未完成事务。恢复前应退出 Claude，恢复操作不启动 Claude。

Shadowrocket 设置 → UDP 中开启「禁用 STUN」，防止 STUN 请求经原分流中的直连路径暴露出口。该设置可能影响依赖 STUN 的点对点音视频。程序会继续实测，不会仅凭开关声称零泄漏。

「一键修复」安装/重启本用户的保护服务，必要时请求连接 Shadowrocket；若规则缺失，打开已保存配置的导入。它不强行切节点或修改系统偏好。导入后须在 Shadowrocket 选「使用配置」，然后回到切换器刷新。

CLI 与界面使用相同的安检：

```sh
~/.local/bin/claude-switch.sh checkdetail
~/.local/bin/claude-switch.sh check
~/.local/bin/claude-switch.sh status
~/.local/bin/claude-switch.sh switch A
~/.local/bin/claude-switch.sh switch B
~/.local/bin/claude-switch.sh recover
```

## 防护范围

本版本不额外调用 ipinfo.io 等 ASN 归属服务。ASN/住宅属性保持未鉴定提示，不将未执行的查询标成通过。

- 必要检查包含指定 Shadowrocket VPN 连接状态、MacPacketTunnel 进程、Claude Fake-IP 路由、美国出口、STUN 中观察到的不同出口、当前配置置顶前缀及 UDP 回退设置、服务网络可达性、保护服务状态。
- 系统时区/地区/语言保持原状并提示。IPv6 可达但未走所检查的美国隧道时会阻止启动；不可达仅作提示，不证明已阻断所有 IPv6。Fake-IP 仅证明该次解析与路由，不能证明全部 DNS 无泄漏。IPv6 若使用 IPv4 映射地址，按实际 IPv4 路由验证并显示原生 IPv6 未验证。
- STUN 验证事务 ID、报文长度和 magic cookie；无响应显示未验证，解析或执行故障不伪装为成功。四个探针均有超时和整体硬超时。
- 用户保镖每两秒检查本地连接，约每 15 秒加请求耗时检查出口和配置基线。检测异常后向受保护的主进程发送 SIGTERM。它按启动时绑定的 PID 工作，不接管此前手动打开的 Claude。
- 轮询、网络超时和进程终止都有反应窗口；本项目不提供内核防火墙或“零泄漏”保证。全局路由模式和节点实际策略仍需在 Shadowrocket 核实，当前配置数据库检查不等于观测了每条连接的策略。
- 保留上游 `accounts/A`、`accounts/B` 存档结构。仅切换 Claude 用户数据目录，不复制或修改系统钥匙串；不同 Claude 版本的真实登录态隔离须实际账号验证。
- 配置变化或出口变化时，保镖停止托管会话，不擅自覆盖用户配置。重新启用保存的配置、确认节点后通过切换器重新启动，会建立新的检查基线。

## 恢复与卸载

状态、配置和事件日志位于 `~/.claude-switcher`，目录权限 700，日志和状态文件权限 600。日志不输出 IP、订阅链接或凭据；出口一致性只保存散列。登录快照本身仍属于敏感文件，请不要分享。

未完成切换记录保存在 `transaction.json`，原数据在与 Claude 数据目录同级的事务备份中。`recover` 根据记录恢复原目录和 current；记录损坏、存在符号链接或 Claude 仍在运行时拒绝恢复，保留数据供手动处理。

双击 `installer/卸载.command` 会停止用户级保镖并将应用移到废纸篓，保留快照、脚本、安装备份和 Shadowrocket 原配置。未改动系统级上游时区守护服务。

## 开发与验证

```sh
python3 -m unittest discover -s tests -v
python3 installer/install.py --build-dir build
```

测试使用临时目录与模拟进程，不会切换真实账号或断开 VPN，覆盖守护服务 PID 归属与文件权限失效、A/B 往返、网络失效阻止、复制失败、启动失败、崩溃恢复、并发锁、参数路径、符号链接、配置前缀、UDP 回退和 STUN 协议校验。实际 B 账号登录、网络断开、睡眠唤醒、钥匙串兼容性须另做人工验收。

MIT。原项目：[YOURSJEYO/claude-switcher](https://github.com/YOURSJEYO/claude-switcher)。
