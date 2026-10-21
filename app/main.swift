import SwiftUI
import AppKit
import Darwin

// User-selected, file-scoped access to Shadowrocket metadata and the active profile.
final class ProfileAccess {
    static let shared = ProfileAccess()
    private var urls: [URL] = []
    private let home = FileManager.default.homeDirectoryForCurrentUser
    private var store: URL { home.appendingPathComponent(".claude-switcher/file-access.plist") }

    func restore() {
        guard let data = try? Data(contentsOf: store),
              let bookmarks = try? PropertyListDecoder().decode([Data].self, from: data) else { return }
        for bookmark in bookmarks {
            var stale = false
            if let url = try? URL(resolvingBookmarkData: bookmark, options: [.withSecurityScope, .withoutUI],
                                  relativeTo: nil, bookmarkDataIsStale: &stale), !stale {
                _ = url.startAccessingSecurityScopedResource()
                urls.append(url)
            }
        }
    }

    private func select(_ expected: URL, title: String) throws -> Data {
        let panel = NSOpenPanel()
        panel.title = title
        panel.message = "只读检查当前生效规则。请选择下方指定文件，不需要授权整个文件夹。"
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.directoryURL = expected.deletingLastPathComponent()
        panel.nameFieldStringValue = expected.lastPathComponent
        guard panel.runModal() == .OK, let url = panel.url,
              url.standardizedFileURL == expected.standardizedFileURL else {
            throw NSError(domain: "ProfileAccess", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "未选择指定配置文件，授权未完成"])
        }
        _ = url.startAccessingSecurityScopedResource()
        urls.append(url)
        _ = try Data(contentsOf: url)
        return try url.bookmarkData(options: [.withSecurityScope, .securityScopeAllowOnlyReadAccess], includingResourceValuesForKeys: nil, relativeTo: nil)
    }

    func authorize() throws {
        let pref = home.appendingPathComponent("Library/Group Containers/group.com.liguangming.Shadowrocket/Library/Preferences/group.com.liguangming.Shadowrocket.plist")
        let first = try select(pref, title: "1/2：允许读取 Shadowrocket 当前配置名称")
        let data = try Data(contentsOf: pref)
        guard let dict = try PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any],
              let name = dict["group.com.liguangming.CurrentRuleFileName"] as? String,
              (name as NSString).lastPathComponent == name, name.hasSuffix(".db") else {
            throw NSError(domain: "ProfileAccess", code: 2,
                          userInfo: [NSLocalizedDescriptionKey: "未找到当前启用的配置，请先在 Shadowrocket 使用配置"])
        }
        let db = home.appendingPathComponent("Library/Containers/com.liguangming.Shadowrocket/Data/Documents/Databases/" + name)
        let second = try select(db, title: "2/2：允许读取当前 Shadowrocket 配置数据库")
        let encoded = try PropertyListEncoder().encode([first, second])
        try encoded.write(to: store, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: store.path)
    }
}

// MARK: - 模型

enum RowState { case pending, ok, warn, fail }

struct CheckRow: Identifiable {
    let id = UUID()
    let name: String
    let icon: String
    var state: RowState = .pending
    var detail: String = ""
}

enum Phase { case checking, ready, failed, switching, done, fixing }

final class VM: ObservableObject {
    @Published var rows: [CheckRow] = [
        CheckRow(name: "代理内核", icon: "cpu"),
        CheckRow(name: "TUN 网卡", icon: "cable.connector"),
        CheckRow(name: "出口 IP", icon: "globe.americas"),
        CheckRow(name: "IP 与上次一致", icon: "checkmark.seal"),
        CheckRow(name: "系统时区", icon: "clock"),
        CheckRow(name: "系统地区", icon: "flag"),
        CheckRow(name: "IPv6 路径", icon: "6.circle"),
        CheckRow(name: "DNS 走隧道", icon: "magnifyingglass"),
        CheckRow(name: "WebRTC", icon: "eye.slash"),
        CheckRow(name: "保镖在线", icon: "hand.raised"),
        CheckRow(name: "规则置顶", icon: "list.number"),
        CheckRow(name: "客户端语言", icon: "character.bubble"),
        CheckRow(name: "ASN 属性", icon: "network"),
        CheckRow(name: "服务连通性", icon: "antenna.radiowaves.left.and.right"),
    ]
    @Published var phase: Phase = .checking
    @Published var current = "?"
    @Published var hasB = false
    @Published var banner = ""
    @Published var note = ""

    let core = NSHomeDirectory() + "/.local/bin/claude-switch.sh"

    func run(_ args: [String], timeout: TimeInterval = 90, done: @escaping (String) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async {
            let started = Date()
            let tmp = "/tmp/switcher-out-\(UUID().uuidString).txt"
            FileManager.default.createFile(atPath: tmp, contents: nil)
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/bin/bash")
            p.arguments = [self.core] + args
            if let fh = try? FileHandle(forWritingTo: URL(fileURLWithPath: tmp)) {
                p.standardOutput = fh
                p.standardError = fh
            }
            var timedOut = false
            let watchdog = DispatchWorkItem {
                if p.isRunning { timedOut = true; p.terminate() }
            }
            DispatchQueue.global().asyncAfter(deadline: .now() + timeout, execute: watchdog)
            do { try p.run() } catch {
                DispatchQueue.main.async { done("FAIL:无法启动脚本 \(error.localizedDescription)") }
                return
            }
            p.waitUntilExit()
            watchdog.cancel()
            if let fh = p.standardOutput as? FileHandle { try? fh.close() }
            let data = (try? Data(contentsOf: URL(fileURLWithPath: tmp))) ?? Data()
            let out = String(decoding: data, as: UTF8.self)  // 容错解码:坏字节替换而非整体失败
            try? FileManager.default.removeItem(atPath: tmp)
            let dbg = "[\(Date())] args=\(args) timedOut=\(timedOut) exit=\(p.terminationStatus) 耗时\(String(format: "%.1f", Date().timeIntervalSince(started)))s stdout=\(out.count)字符\n"
            if let h = try? FileHandle(forWritingTo: URL(fileURLWithPath: "/tmp/switcher-debug.log")) {
                h.seekToEndOfFile(); h.write(dbg.data(using: .utf8)!); h.closeFile()
            } else { try? dbg.write(toFile: "/tmp/switcher-debug.log", atomically: true, encoding: .utf8) }
            if timedOut {
                DispatchQueue.main.async { done("FAIL:操作超时，请重试") }
                return
            }
            DispatchQueue.main.async { done(out.trimmingCharacters(in: .whitespacesAndNewlines)) }
        }
    }

    func refresh() {
        phase = .checking
        banner = ""
        note = ""
        for i in rows.indices { rows[i].state = .pending; rows[i].detail = "" }
        run(["checkdetail"]) { out in
            var allOk = true
            var matched = Set<String>()
            let keyMap: [String: String] = [
                "代理内核": "代理内核", "TUN网卡": "TUN 网卡", "出口IP": "出口 IP",
                "IP一致": "IP 与上次一致", "时区": "系统时区", "地区": "系统地区",
                "IPv6": "IPv6 路径", "DNS": "DNS 走隧道", "WebRTC": "WebRTC",
                "保镖": "保镖在线", "规则置顶": "规则置顶",
                "客户端语言": "客户端语言", "ASN属性": "ASN 属性", "生态可达性": "服务连通性",
            ]
            for line in out.split(separator: "\n") {
                let parts = line.split(separator: "=", maxSplits: 1).map(String.init)
                guard parts.count == 2 else { continue }
                let rhs = parts[1]
                let status = rhs.split(separator: ":", maxSplits: 1).map(String.init)
                guard let code = status.first else { continue }
                let detail = status.count > 1 ? status[1] : ""
                guard let rowName = keyMap[parts[0]], let idx = self.rows.firstIndex(where: { $0.name == rowName }) else { continue }
                matched.insert(parts[0])
                switch code {
                case "ok": self.rows[idx].state = .ok
                case "warn": self.rows[idx].state = .warn
                default: self.rows[idx].state = .fail; allOk = false
                }
                self.rows[idx].detail = detail
            }
            self.run(["status"]) { st in
                self.current = st.contains("current=B") ? "B" : (st.contains("current=A") ? "A" : "未初始化")
                self.hasB = st.contains("hasB=1")
                if matched.count < self.rows.count {
                    self.banner = "检测输出不完整(\(matched.count)/\(self.rows.count))，请点重新检查"
                    self.phase = .failed
                } else {
                    self.phase = allOk ? .ready : .failed
                    let warnings = self.rows.filter { $0.state == .warn }.count
                    self.note = warnings > 0 ? "有 \(warnings) 项提示，请阅读检查详情。" : ""
                }
            }
        }
    }

    func fixAll() {
        phase = .fixing
        banner = "正在准备 Shadowrocket 与用户级保护…"
        run(["fix"], timeout: 180) { out in
            let steps = out.split(separator: "\n").map(String.init).filter { $0.hasPrefix("FIX:") }.map { String($0.dropFirst(4)) }
            self.banner = steps.last ?? ""
            self.refresh()
        }
    }

    func switchTo(_ target: String) {
        phase = .switching
        banner = "正在切换到账号 \(target)…"
        run(["switch", target], timeout: 900) { out in
            if out.hasPrefix("FAIL:") || !(out.hasPrefix("OK:") || out.hasPrefix("INFO:")) {
                self.phase = .failed
                self.banner = String(out.dropFirst(5))
                return
            }
            var extra = ""
            if target == "B" && !self.hasB { extra = "B 号首次使用：请在打开的 Claude 里登录 B 账号，登录一次即可。" }
            if out.hasPrefix("INFO:") { extra = "" }
            self.current = target
            self.phase = .done
            self.banner = out.hasPrefix("INFO:") ? "当前已是账号 \(target)" : "已切换到账号 \(target)"
            self.note = extra
            DispatchQueue.main.asyncAfter(deadline: .now() + 2.2) {
                NSApplication.shared.terminate(nil)
            }
        }
    }
}

// MARK: - 视图

struct ContentView: View {
    @StateObject private var vm = VM()
    @State private var showNotice = false

    var body: some View {
        ZStack {
            LinearGradient(colors: [Color(red: 0.07, green: 0.08, blue: 0.13), Color(red: 0.10, green: 0.07, blue: 0.16)],
                           startPoint: .top, endPoint: .bottom).ignoresSafeArea()
            VStack(spacing: 16) {
                header
                ScrollView { checksCard }.frame(maxHeight: 490)
                if vm.phase == .ready || vm.phase == .switching || vm.phase == .done {
                    accountCard
                } else if vm.phase == .failed || vm.phase == .fixing {
                    retryCard
                }
                Spacer(minLength: 0)
                footer
            }
            .padding(20)
        }
        .frame(width: 440, height: 850)
        .onAppear { vm.refresh() }
    }

    var header: some View {
        HStack(spacing: 12) {
            ZStack {
                RoundedRectangle(cornerRadius: 12)
                    .fill(LinearGradient(colors: [Color.indigo, Color.purple], startPoint: .topLeading, endPoint: .bottomTrailing))
                    .frame(width: 44, height: 44)
                Image(systemName: "shield.lefthalf.filled").font(.title2).foregroundColor(.white)
            }
            VStack(alignment: .leading, spacing: 2) {
                Text("Claude 账号切换器").font(.title3.bold()).foregroundColor(.white)
                Text("Shadowrocket · 安检 · 账号快照").font(.caption).foregroundColor(.white.opacity(0.55))
            }
            Spacer()
            Button { showNotice = true } label: {
                Image(systemName: "info.circle").font(.title3).foregroundColor(.white.opacity(0.6))
            }
            .buttonStyle(.plain)
            .help("安全防护体系公告")
            statusPill
        }
        .sheet(isPresented: $showNotice) { NoticeView() }
    }

    var statusPill: some View {
        let (txt, color): (String, Color) = {
            switch vm.phase {
            case .checking: return ("检查中", .orange)
            case .ready: return ("可启动", .green)
            case .failed: return ("检查未过", .red)
            case .switching: return ("切换中", .blue)
            case .fixing: return ("修复中", .orange)
            case .done: return ("完成", .green)
            }
        }()
        return Text(txt)
            .font(.caption.bold())
            .padding(.horizontal, 10).padding(.vertical, 5)
            .background(color.opacity(0.18))
            .foregroundColor(color)
            .clipShape(Capsule())
    }

    var checksCard: some View {
        VStack(spacing: 0) {
            ForEach(Array(vm.rows.enumerated()), id: \.element.id) { idx, row in
                HStack(spacing: 10) {
                    Image(systemName: row.icon)
                        .frame(width: 20)
                        .foregroundColor(.white.opacity(0.65))
                    Text(row.name).font(.callout).foregroundColor(.white.opacity(0.9))
                    Spacer()
                    if !row.detail.isEmpty {
                        Text(row.detail).font(.caption).foregroundColor(.white.opacity(0.45)).lineLimit(1)
                    }
                    stateIcon(row.state)
                }
                .padding(.vertical, 7).padding(.horizontal, 12)
                if idx < vm.rows.count - 1 {
                    Divider().background(Color.white.opacity(0.08))
                }
            }
        }
        .background(Color.white.opacity(0.05))
        .clipShape(RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(Color.white.opacity(0.08)))
    }

    func stateIcon(_ s: RowState) -> some View {
        Group {
            switch s {
            case .pending: ProgressView().scaleEffect(0.55).frame(width: 16, height: 16)
            case .ok: Image(systemName: "checkmark.circle.fill").foregroundColor(.green)
            case .warn: Image(systemName: "exclamationmark.triangle.fill").foregroundColor(.yellow)
            case .fail: Image(systemName: "xmark.octagon.fill").foregroundColor(.red)
            }
        }
        .font(.callout)
    }

    var accountCard: some View {
        VStack(spacing: 12) {
            if vm.phase == .switching {
                HStack(spacing: 8) {
                    ProgressView().scaleEffect(0.7)
                    Text(vm.banner).font(.callout).foregroundColor(.white.opacity(0.85))
                }
                .frame(maxWidth: .infinity).padding(.vertical, 10)
            } else if vm.phase == .done {
                VStack(spacing: 4) {
                    Label(vm.banner, systemImage: "checkmark.circle.fill").foregroundColor(.green).font(.callout.bold())
                    if !vm.note.isEmpty { Text(vm.note).font(.caption).foregroundColor(.yellow.opacity(0.9)).multilineTextAlignment(.center) }
                }
                .frame(maxWidth: .infinity).padding(.vertical, 10)
            } else {
                if !vm.note.isEmpty { Text(vm.note).font(.caption).foregroundColor(.yellow) }
                HStack(spacing: 6) {
                    Text("当前账号").font(.caption).foregroundColor(.white.opacity(0.5))
                    Text(vm.current).font(.caption.bold())
                        .padding(.horizontal, 8).padding(.vertical, 3)
                        .background(Color.indigo.opacity(0.3)).foregroundColor(.indigo.opacity(1.4))
                        .clipShape(Capsule())
                    Spacer()
                }
                HStack(spacing: 10) {
                    accountButton("A", subtitle: vm.current == "A" ? "当前" : "启动 / 切换")
                    accountButton("B", subtitle: vm.current == "B" ? "当前" : (vm.hasB ? "已建档" : "未初始化"))
                }
            }
        }
        .padding(12)
        .background(Color.white.opacity(0.05))
        .clipShape(RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(Color.white.opacity(0.08)))
    }

    func accountButton(_ t: String, subtitle: String) -> some View {
        Button { vm.switchTo(t) } label: {
            VStack(spacing: 4) {
                Text("账号 \(t)").font(.headline).foregroundColor(.white)
                Text(subtitle).font(.caption2).foregroundColor(.white.opacity(0.55))
            }
            .frame(maxWidth: .infinity).padding(.vertical, 14)
            .background(
                vm.current == t
                ? LinearGradient(colors: [Color.indigo.opacity(0.6), Color.purple.opacity(0.5)], startPoint: .top, endPoint: .bottom)
                : LinearGradient(colors: [Color.white.opacity(0.08), Color.white.opacity(0.05)], startPoint: .top, endPoint: .bottom)
            )
            .clipShape(RoundedRectangle(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(vm.current == t ? Color.indigo : Color.white.opacity(0.1), lineWidth: 1.2))
        }
        .buttonStyle(.plain)
    }

    var retryCard: some View {
        VStack(spacing: 10) {
            if vm.phase == .fixing {
                HStack(spacing: 8) {
                    ProgressView().scaleEffect(0.7)
                    Text(vm.banner).font(.callout).foregroundColor(.white.opacity(0.85))
                }
            } else {
                if !vm.banner.isEmpty {
                    Text(vm.banner).font(.callout).foregroundColor(.red.opacity(0.95)).multilineTextAlignment(.center)
                }
                Text("故障会阻止启动；环境提示保留系统偏好。修复可安装用户级保护并打开配置导入。").font(.caption).foregroundColor(.white.opacity(0.6))
                HStack(spacing: 10) {
                    Button { vm.fixAll() } label: {
                        Label("一键修复", systemImage: "wand.and.stars")
                            .font(.callout.bold())
                            .padding(.horizontal, 18).padding(.vertical, 8)
                            .background(LinearGradient(colors: [Color.indigo, Color.purple], startPoint: .leading, endPoint: .trailing))
                            .foregroundColor(.white)
                            .clipShape(Capsule())
                    }
                    .buttonStyle(.plain)
                    Button { vm.refresh() } label: {
                        Label("重新检查", systemImage: "arrow.clockwise")
                            .font(.callout.bold())
                            .padding(.horizontal, 18).padding(.vertical, 8)
                            .background(Color.white.opacity(0.1))
                            .foregroundColor(.white)
                            .clipShape(Capsule())
                    }
                    .buttonStyle(.plain)
                }
            }
        }
        .frame(maxWidth: .infinity)
        .padding(12)
        .background(Color.white.opacity(0.05))
        .clipShape(RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(Color.red.opacity(0.25)))
    }

    var footer: some View {
        HStack {
            Image(systemName: "lock.shield").font(.caption2)
            Text("故障阻止启动 · 提示保留原设置")
            Spacer()
            Button("授权读取配置") {
                do {
                    try ProfileAccess.shared.authorize()
                    vm.run(["install-guard"]) { _ in vm.refresh() }
                } catch { vm.banner = error.localizedDescription }
            }.buttonStyle(.plain)
            Button("刷新") { vm.refresh() }.buttonStyle(.plain)
            Button("恢复") {
                vm.run(["recover"]) { result in
                    vm.banner = result
                    vm.refresh()
                    vm.banner = result
                }
            }.buttonStyle(.plain).font(.caption2)
        }
        .foregroundColor(.white.opacity(0.35))
    }
}

// MARK: - 公告页

struct NoticeView: View {
    @Environment(\.dismiss) private var dismiss

    let sections: [(String, String)] = [
        ("Shadowrocket 网络检查", "检查系统 VPN 状态、Packet Tunnel 进程、Claude Fake-IP 路由和美国出口。Fake-IP 只能作为网络健康信号，不证明所有 DNS 或应用流量都没有绕行。"),
        ("14 项状态", "必要网络故障阻止启动；系统时区、地区、客户端语言等保留原设置并显示提示。STUN 无响应表示无法确认，不伪装为已经证实安全。"),
        ("用户级断线保护", "保护仅作用于通过本切换器启动的 Claude 会话。每两秒检查隧道，定期检查出口和当前配置。检测到故障会终止该会话；检测存在时间窗口，不保证零泄漏。"),
        ("独立路由配置", "在当前 Shadowrocket 配置副本顶部加入 Claude 及相关服务的 13 条代理规则，保留现有 Tailscale 和其他分流。配置生效后才允许启动。"),
        ("账号数据快照", "首次将当前 Claude 数据保存为 A；B 首次启动需要登录。切换前完全退出 Claude，校验参数并加互斥锁，复制失败不覆盖原数据，未完成事务可恢复。系统钥匙串不参与复制，登录兼容性需实际账号验证。"),
        ("隐私与恢复", "日志不包含 IP 或凭据。安装会备份旧程序；快照文件仅当前用户可访问。恢复不会绕过启动安检。")
    ]

    var body: some View {
        ZStack {
            LinearGradient(colors: [Color(red: 0.07, green: 0.08, blue: 0.13), Color(red: 0.10, green: 0.07, blue: 0.16)],
                           startPoint: .top, endPoint: .bottom).ignoresSafeArea()
            VStack(spacing: 0) {
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("安全防护体系公告").font(.title3.bold()).foregroundColor(.white)
                        Text("Security Architecture Bulletin").font(.caption).foregroundColor(.white.opacity(0.5))
                    }
                    Spacer()
                    Button { dismiss() } label: {
                        Image(systemName: "xmark.circle.fill").font(.title2).foregroundColor(.white.opacity(0.4))
                    }
                    .buttonStyle(.plain)
                }
                .padding(20)

                ScrollView {
                    VStack(spacing: 10) {
                        Text("此版本使用 Shadowrocket，提供启动前检查、账号数据快照和可选的用户级断线保护。黄色提示表示保留的环境差异或尚未证实的条件；绿色也不代表绝对无泄漏。")
                            .font(.callout)
                            .foregroundColor(.white.opacity(0.75))
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.bottom, 6)

                        ForEach(Array(sections.enumerated()), id: \.offset) { idx, s in
                            VStack(alignment: .leading, spacing: 5) {
                                Text("\(idx + 1). \(s.0)").font(.callout.bold()).foregroundColor(.indigo.opacity(1.3))
                                Text(s.1).font(.caption).foregroundColor(.white.opacity(0.7)).lineSpacing(3)
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(12)
                            .background(Color.white.opacity(0.05))
                            .clipShape(RoundedRectangle(cornerRadius: 10))
                        }

                        Text("断线、IPv6、DNS 与登录态的实际表现取决于系统、节点及 Shadowrocket 配置。")
                            .font(.caption2)
                            .foregroundColor(.white.opacity(0.35))
                            .padding(.top, 8)
                    }
                    .padding(.horizontal, 20).padding(.bottom, 20)
                }
            }
        }
        .frame(width: 460, height: 620)
    }
}

// MARK: - App

class AppDelegate: NSObject, NSApplicationDelegate {
    // 关闭窗口即退出进程,杜绝"旧实例残留"
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

@main
struct SwitcherApp: App {
    init() {
        ProfileAccess.shared.restore()
        if CommandLine.arguments.count >= 3 && CommandLine.arguments[1] == "--guard-helper" {
            // Replace this process so launchd owns the actual guardian PID.
            // This also avoids orphaning a Python process on kickstart/bootout.
            let python = CommandLine.arguments[2]
            let arguments = [python, NSHomeDirectory() + "/.claude-switcher/claude-switch.py", "guard"]
            var argv = arguments.map { strdup($0) } + [nil]
            argv.withUnsafeMutableBufferPointer { buffer in
                _ = execv(python, buffer.baseAddress!)
            }
            exit(1)
        }
    }
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate

    var body: some Scene {
        WindowGroup {
            ContentView()
                .background(WindowAccessor { win in
                    guard let w = win else { return }
                    w.titlebarAppearsTransparent = true
                    w.titleVisibility = .hidden
                    w.styleMask.insert(.fullSizeContentView)
                    w.isMovableByWindowBackground = true
                    w.standardWindowButton(.zoomButton)?.isEnabled = false
                })
        }
        .windowStyle(.hiddenTitleBar)
        .windowResizability(.contentSize)
    }
}

struct WindowAccessor: NSViewRepresentable {
    let cb: (NSWindow?) -> Void
    func makeNSView(context: Context) -> NSView {
        let v = NSView()
        DispatchQueue.main.async { self.cb(v.window) }
        return v
    }
    func updateNSView(_ nsView: NSView, context: Context) {}
}
