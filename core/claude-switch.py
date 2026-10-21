#!/usr/bin/env python3
"""Shadowrocket backend. No system settings or private Shadowrocket DB writes."""
import concurrent.futures
import contextlib
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import shlex
import signal
import sqlite3
import subprocess
import sys
import time
import uuid

DOMAINS = ('claude.ai', 'claude.com', 'anthropic.com', 'claudeusercontent.com',
           'challenges.cloudflare.com', 'datadoghq.com', 'segment.io', 'stripe.com',
           'stripe.network', 'stripecdn.com', 'intercom.io', 'intercomcdn.com',
           'intercomassets.com')
FAKE = ipaddress.ip_network('198.18.0.0/15')
LABEL = 'local.claude-switcher.bodyguard'
GROUP = 'Library/Group Containers/group.com.liguangming.Shadowrocket'
CLAUDE_PROCESS = r'^/Applications/Claude\.app/Contents/MacOS/Claude($| )'
TUNNEL_PROCESS = r'^/Applications/Shadowrocket\.app/Contents/PlugIns/MacPacketTunnel\.appex/Contents/MacOS/MacPacketTunnel($| )'


class Failure(Exception):
    pass


def run(args, timeout=10):
    try:
        return subprocess.run(args, capture_output=True, text=True, errors='replace', timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(args, 127, '', '')


def atomic(path, value):
    path = Path(path)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(value)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path, fallback=None):
    try:
        result=json.loads(Path(path).read_text())
        return result if isinstance(result,dict) else fallback
    except (OSError, ValueError):
        return fallback


def is_fake(value):
    try:
        return ipaddress.ip_address(value) in FAKE
    except ValueError:
        return False


def plist(path):
    try:
        return plistlib.loads(Path(path).read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        return {}


def active_profile(home):
    """Read-only, version-scoped inspection; never touches node/credential stores."""
    prefs = plist(home / GROUP / 'Library/Preferences/group.com.liguangming.Shadowrocket.plist')
    name = prefs.get('group.com.liguangming.CurrentRuleFileName', '')
    if not name or Path(name).name != name or not name.endswith('.db'):
        raise Failure('无法识别 Shadowrocket 当前配置')
    db = home / 'Library/Containers/com.liguangming.Shadowrocket/Data/Documents/Databases' / name
    try:
        with contextlib.closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=2)) as c:
            rows = c.execute('select section,name,value,option,ext,remarks from config order by rowid').fetchall()
            rows = [tuple(str(v) if v is not None else '' for v in row) for row in rows]
    except sqlite3.Error:
        raise Failure('当前配置格式无法读取，请在 Shadowrocket 中重新编译配置')
    return name, rows


def profile_issues(rows):
    general = {str(n).lower(): str(v).strip().lower() for s,n,v,*_ in rows if s.lower() == 'general' and n != 'COMMENT'}
    rules = [(n.upper(), str(v).lower(), str(o)) for s,n,v,o,*_ in rows if s.lower() == 'rule' and n != 'COMMENT']
    # A literal prefix is intentional: no earlier remote rule, logical rule or FINAL may override it.
    required = [('DOMAIN-SUFFIX', d, 'PROXY') for d in DOMAINS]
    issues = []
    if rules[:len(required)] != required:
        issues.append('Claude 域名置顶规则未生效')
    if general.get('udp-policy-not-supported-behaviour') != 'reject':
        issues.append('UDP 回退未明确设为 REJECT')
    return issues


class Backend:
    def __init__(self, home=None, runner=run):
        self.home = Path(home or Path.home())
        self.base = self.home / '.claude-switcher'
        self.accounts = self.base / 'accounts'
        self.data = self.home / 'Library/Application Support/Claude'
        self.runner = runner
        self.config = read_json(self.base / 'config.json', {})
        self.base.mkdir(mode=0o700, exist_ok=True)
        self.accounts.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.base, 0o700)
        os.chmod(self.accounts, 0o700)

    def cmd(self, *args, timeout=10):
        return self.runner(list(args), timeout=timeout)

    def log(self, text):
        # Only fixed event descriptions; never log IPs or authentication contents.
        with open(self.base / 'events.log', 'a') as f:
            f.write(time.strftime('%Y-%m-%d %H:%M:%S ') + text + '\n')
        os.chmod(self.base / 'events.log', 0o600)

    def current(self):
        p = self.base / 'current'
        if not p.exists():
            return 'none'
        value = p.read_text().strip()
        if value not in ('A', 'B', 'none'):
            raise Failure('账号状态损坏，请从备份恢复 current')
        return value

    def process_ids(self):
        r=self.cmd('/usr/bin/pgrep','-f',CLAUDE_PROCESS)
        return {p for p in r.stdout.splitlines() if p.isdigit()} if r.returncode==0 else set()

    def running(self):
        return bool(self.process_ids())

    def direct_profile_state(self):
        try:
            _,rows=active_profile(self.home)
            prefs=plist(self.home / GROUP / 'Library/Preferences/group.com.liguangming.Shadowrocket.plist')
            return {'readable':True,'issues':profile_issues(rows),
                    'digest':hashlib.sha256(json.dumps(rows,ensure_ascii=False).encode()).hexdigest(),
                    'stun_disabled':prefs.get('group.com.liguangming.UDPSocketDisableSTUN') is True}
        except Failure:
            return {'readable':False,'issues':['无法读取 Shadowrocket 当前配置']}

    def profile_state(self):
        state=self.direct_profile_state()
        if state['readable']:return state
        # The user's launchd helper publishes fresh, sanitized observations for the GUI.
        # No root service, credential read, private DB write or stale fallback is involved.
        heartbeat=read_json(self.base/'guard-heartbeat.json',{})
        age=time.time()-heartbeat.get('time',0)
        if 0<=age<20 and self.guard_ready():
            return heartbeat.get('profile',state)
        return state

    def profile_digest(self):
        state=self.profile_state()
        if not state.get('readable') or state.get('issues') or not state.get('stun_disabled'):
            raise Failure('无法确认当前规则或 STUN 防护设置，请在 Shadowrocket 开启禁用 STUN')
        return hashlib.sha256((state['digest']+'|stun-disabled').encode()).hexdigest()

    def vpn(self):
        service = self.config.get('vpn_service', 'Shadowrocket')
        result = self.cmd('/usr/sbin/scutil', '--nc', 'status', service)
        return result.returncode == 0 and result.stdout.splitlines()[:1] == ['Connected']

    def process(self):
        return self.cmd('/usr/bin/pgrep', '-f', TUNNEL_PROCESS).returncode == 0

    def addresses(self):
        result = self.cmd('/usr/bin/dig', '+short', '+time=2', '+tries=1', 'claude.ai', timeout=4)
        return [line for line in result.stdout.splitlines() if is_fake(line)]

    def route(self, address, ipv6=False):
        args = ['/sbin/route', '-n', 'get'] + (['-inet6'] if ipv6 else []) + [address]
        result = self.runner(args, timeout=3)
        match = re.search(r'^\s*interface:\s*(utun\d+)\s*$', result.stdout, re.M)
        return match.group(1) if match and result.returncode == 0 else ''

    def local_health(self):
        if not self.vpn() or not self.process():
            return False
        return any(self.route(a) for a in self.addresses())

    def trace(self):
        r = self.cmd('/usr/bin/curl', '--noproxy', '*', '-4', '-fsS', '--connect-timeout', '3', '--max-time', '7',
                     'https://www.cloudflare.com/cdn-cgi/trace', timeout=9)
        d = dict(line.split('=',1) for line in r.stdout.splitlines() if '=' in line)
        try:
            ipaddress.ip_address(d.get('ip', ''))
        except ValueError:
            return {}
        return d if r.returncode == 0 else {}

    def guard_ready(self):
        state = read_json(self.base / 'guard-heartbeat.json', {})
        age = time.time() - state.get('time', 0)
        r = self.cmd('/bin/launchctl', 'print', f'gui/{os.getuid()}/{LABEL}')
        match=re.search(r'^\s*pid = (\d+)\s*$',r.stdout,re.M)
        profile=state.get('profile',{})
        return (r.returncode == 0 and 0 <= age < 20 and match is not None
                and str(state.get('pid'))==match.group(1) and isinstance(profile,dict) and profile.get('readable') is True)

    def audit(self, persist=True):
        # Network requests run concurrently, each with a hard timeout.
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            tf = pool.submit(self.trace)
            sf = pool.submit(self.stun)
            vf = pool.submit(self.ipv6)
            ef = pool.submit(self.endpoints)
            vpn, process, addrs = self.vpn(), self.process(), self.addresses()
            iface = next((self.route(a) for a in addrs if self.route(a)), '')
            trace, stun, ipv6, endpoints = tf.result(), sf.result(), vf.result(), ef.result()
        rows = []
        def row(name, state, detail):
            rows.append((name,state,detail))
        row('代理内核', 'ok' if vpn and process else 'fail', 'Shadowrocket VPN 已连接' if vpn and process else '请连接 Shadowrocket VPN')
        row('TUN网卡', 'ok' if iface else 'fail', iface or 'Claude 目标路由未进入隧道')
        country = trace.get('loc')
        row('出口IP','ok' if country == 'US' else 'fail', '美国（IP 已隐藏）' if country == 'US' else '未取得美国出口')
        ip = trace.get('ip','')
        fingerprint = hashlib.sha256(ip.encode()).hexdigest() if ip else ''
        old = read_json(self.base / 'egress.json', {})
        row('IP一致', 'warn' if old.get('fingerprint') and old['fingerprint'] != fingerprint else ('ok' if ip else 'fail'),
            '出口已变化，请确认节点' if old.get('fingerprint') and old['fingerprint'] != fingerprint else ('一致或首次记录' if ip else '出口未知'))
        tz = str(Path('/etc/localtime').readlink()) if Path('/etc/localtime').is_symlink() else '未知'
        row('时区','warn','保留系统时区：'+tz.rsplit('zoneinfo/',1)[-1])
        locale = self.cmd('/usr/bin/defaults', 'read', '-g', 'AppleLocale').stdout.strip()
        row('地区','ok' if locale == 'en_US' else 'warn','保留系统地区：'+(locale or '未知'))
        row('IPv6', *ipv6)
        row('DNS','ok' if addrs and iface else 'fail','Fake-IP 路由已确认；不代表全部 DNS 无泄漏' if iface else '未确认 Fake-IP 隧道路由')
        protection=self.profile_state()
        if protection.get('readable') and not protection.get('stun_disabled'):
            row('WebRTC','fail','请在 Shadowrocket 设置 → UDP 开启禁用 STUN')
        elif stun.get('status') == 'responses':
            addresses = stun.get('addresses',[])
            safe = bool(ip) and bool(addresses) and all(a == ip for a in addresses)
            row('WebRTC','ok' if safe else 'fail','仅观察到当前代理出口' if safe else 'STUN 观察到不同出口，已隐藏地址')
        elif stun.get('status') == 'no_response':
            row('WebRTC','warn','探针无响应，未证实是否阻断')
        else:
            row('WebRTC','fail','探针执行失败')
        row('保镖','ok' if self.guard_ready() else 'fail','用户级断线保护待命' if self.guard_ready() else '请点一键修复安装用户级保护')
        state=self.profile_state()
        issues=state.get('issues',['无法确认 Shadowrocket 当前配置'])
        row('规则置顶','fail' if issues or not state.get('readable') else 'ok',
            '；'.join(issues) if issues else '当前配置的 13 条规则及 UDP 回退已确认')
        languages = self.cmd('/usr/bin/defaults','read','com.anthropic.claudefordesktop','AppleLanguages').stdout
        if not languages:
            languages = self.cmd('/usr/bin/defaults','read','-g','AppleLanguages').stdout
        row('客户端语言','ok' if re.search(r'\ben[-_]US\b', languages) else 'warn','保留原语言设置')
        row('ASN属性','warn','未鉴定住宅/机房；不据运营商名称推断')
        row('生态可达性', *endpoints)
        if persist:
            atomic(self.base / 'last-check.txt', '\n'.join(f'{k}={s}:{d}' for k,s,d in rows)+'\n')
            # Successful checks do not erase the last launched session's baseline.
        return rows

    def stun(self):
        p = self.base / 'stun-probe.py'
        r = self.cmd(sys.executable, str(p), '--json', timeout=12)
        try:
            return json.loads(r.stdout) if r.returncode == 0 else {}
        except ValueError:
            return {}

    def ipv6(self):
        r = self.cmd('/usr/bin/curl','--noproxy','*','-6','-fsS','--connect-timeout','2','--max-time','5',
                     '-w','\nremote=%{remote_ip}','https://www.cloudflare.com/cdn-cgi/trace', timeout=7)
        if r.returncode != 0:
            return ('warn','当前 IPv6 探测不可达；保留系统设置')
        d = dict(line.split('=',1) for line in r.stdout.splitlines() if '=' in line)
        try:
            ipaddress.ip_address(d.get('ip',''))
            remote=ipaddress.IPv6Address(d.get('remote',''))
        except ValueError:
            return ('fail','IPv6 探测返回异常')
        mapped=remote.ipv4_mapped
        interface=self.route(str(mapped)) if mapped else self.route(str(remote),ipv6=True)
        expected={self.route(a) for a in self.addresses()} - {''}
        if not interface or interface not in expected or d.get('loc') != 'US':
            return ('fail','IPv6 请求未确认由同一美国隧道接管')
        if mapped:return ('warn','本次使用 IPv4 映射经同一隧道；原生 IPv6 未验证')
        return ('ok','本次 IPv6 请求经同一隧道；出口为美国')

    def endpoints(self):
        codes=[]
        for url in ('https://claude.ai/', 'https://api.anthropic.com/v1/models'):
            r=self.cmd('/usr/bin/curl','--noproxy','*','-4','-sS','--connect-timeout','2','--max-time','5','-o','/dev/null','-w','%{http_code}',url,timeout=7)
            if r.returncode != 0 or not re.fullmatch(r'[1-5]\d\d',r.stdout):
                return ('fail','目标服务网络不可达')
            codes.append(r.stdout)
        return ('warn' if '403' in codes else 'ok', '/'.join(codes)+'；仅表示 HTTP 可达，不保证登录')

    def require_safe(self):
        rows=self.audit()
        fails=[k+'：'+d for k,s,d in rows if s=='fail']
        if fails:
            raise Failure('安检未通过；'+'；'.join(fails))

    @contextlib.contextmanager
    def lock(self):
        p=self.base/'switch.lock'
        with open(p,'a') as f:
            os.chmod(p,0o600)
            try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise Failure('另一个切换或恢复正在进行')
            try:yield
            finally:fcntl.flock(f,fcntl.LOCK_UN)

    def validate_paths(self):
        for p in (self.base,self.accounts,self.data,self.accounts/'A',self.accounts/'B'):
            if p.is_symlink():raise Failure('拒绝使用符号链接账号或数据目录')
            if p.exists() and not p.is_dir():raise Failure('账号或数据路径不是目录，未修改数据')

    def quit_claude(self):
        if not self.running():return
        self.cmd('/usr/bin/osascript','-e','tell application "Claude" to quit',timeout=10)
        for _ in range(30):
            if not self.running():return
            time.sleep(.5)
        raise Failure('Claude 未完全退出，未交换账号数据')

    def copy(self, source, target):
        if sys.platform=='darwin':
            r=self.cmd('/usr/bin/ditto',str(source),str(target),timeout=600)
            if r.returncode:raise Failure('快照复制失败，原数据已保留')
        else:shutil.copytree(source,target)

    def archive(self, account):
        target=self.accounts/account
        temp=self.accounts/(account+'.tmp')
        old=self.accounts/(account+'.previous')
        if temp.exists() or temp.is_symlink() or old.exists() or old.is_symlink():
            raise Failure('存在未完成快照，请先恢复或保留备份后处理')
        atomic(self.base/'snapshot.json',json.dumps({'account':account}))
        try:
            self.copy(self.data,temp)
            if target.exists():os.replace(target,old)
            try:os.replace(temp,target)
            except BaseException:
                if old.exists():os.replace(old,target)
                raise
            if old.exists():shutil.rmtree(old)
            (self.base/'snapshot.json').unlink(missing_ok=True)
        finally:
            if temp.exists() and not temp.is_symlink():shutil.rmtree(temp)

    def recover_snapshot(self):
        p=self.base/'snapshot.json'
        if not p.exists():return
        record=read_json(p,{})
        account=record.get('account')
        if account not in ('A','B'):raise Failure('快照恢复记录损坏')
        target=self.accounts/account
        temp=self.accounts/(account+'.tmp')
        old=self.accounts/(account+'.previous')
        for candidate in (target,temp,old):
            if candidate.is_symlink():raise Failure('快照恢复路径含符号链接')
        if old.exists():
            if target.exists():shutil.rmtree(old)
            else:os.replace(old,target)
        if temp.exists():shutil.rmtree(temp)
        p.unlink()

    def txn_paths(self, journal):
        ident=journal.get('id','')
        if not re.fullmatch('[a-f0-9]{32}',ident):raise Failure('恢复记录无效，未修改数据')
        return (self.data.with_name('Claude.switcher-next-'+ident),self.data.with_name('Claude.switcher-rollback-'+ident))

    def recover_locked(self):
        self.recover_snapshot()
        p=self.base/'transaction.json'
        if not p.exists():return False
        journal=read_json(p)
        if not isinstance(journal,dict) or journal.get('previous') not in ('none','A','B'):
            raise Failure('恢复记录损坏，原始备份未删除')
        stage,backup=self.txn_paths(journal)
        for candidate in (stage,backup):
            if candidate.is_symlink():raise Failure('恢复路径含符号链接，已中止')
        committed=journal.get('state')=='committed'
        if not committed:
            if self.running():raise Failure('存在未完成切换，请先退出 Claude 再点恢复')
            if backup.exists():
                if self.data.exists():shutil.rmtree(self.data)
                os.replace(backup,self.data)
            elif not journal.get('had_data') and not stage.exists() and self.data.exists():
                shutil.rmtree(self.data)
            atomic(self.base/'current',journal['previous']+'\n')
            (self.base/'managed-session.json').unlink(missing_ok=True)
            self.log('已恢复未完成的账号切换')
        for candidate in (stage,backup):
            if candidate.exists():shutil.rmtree(candidate)
        p.unlink()
        return True

    def recover(self):
        with self.lock():
            self.validate_paths()
            return self.recover_locked()

    def launch(self,target,trace):
        if trace.get('loc') != 'US' or not trace.get('ip'):
            raise Failure('启动前未取得美国出口，已中止')
        if not self.local_health():raise Failure('启动前隧道状态已变化，已中止')
        session={'account':target,'started':time.time(),'profile':self.profile_digest(),
                 'egress':hashlib.sha256(trace['ip'].encode()).hexdigest()}
        atomic(self.base/'managed-session.json',json.dumps(session))
        r=self.cmd('/usr/bin/open','-a','Claude')
        if r.returncode:raise Failure('Claude 启动失败')
        for _ in range(20):
            if self.running():break
            time.sleep(.25)
        else:raise Failure('未确认 Claude 进程启动')
        ids=self.process_ids()
        if len(ids)!=1:raise Failure('Claude 主进程不唯一，已中止会话保护绑定')
        session['pid']=next(iter(ids))
        atomic(self.base/'managed-session.json',json.dumps(session))
        if trace.get('ip'):
            atomic(self.base/'egress.json',json.dumps({'fingerprint':hashlib.sha256(trace['ip'].encode()).hexdigest()}))

    def switch(self,target):
        if target not in ('A','B'):raise Failure('账号只能为 A 或 B')
        with self.lock():
            self.validate_paths()
            self.recover_locked()
            self.require_safe()
            previous=self.current()
            self.quit_claude()
            (self.base/'managed-session.json').unlink(missing_ok=True)
            if previous=='none' and self.data.exists() and any(self.data.iterdir()):
                if (self.accounts/'A').exists():raise Failure('已有 A 存档但缺少 current，请先恢复账号状态')
                self.archive('A')
                previous='A'
                atomic(self.base/'current','A\n')
            if target==previous:
                try:
                    self.require_safe()
                    self.launch(target,self.trace())
                except BaseException:
                    (self.base/'managed-session.json').unlink(missing_ok=True)
                    raise
                return 'INFO:当前已是账号'+target
            if previous!='none' and self.data.exists():self.archive(previous)
            journal={'id':uuid.uuid4().hex,'previous':previous,'target':target,'had_data':self.data.exists(),'state':'preparing'}
            stage,backup=self.txn_paths(journal)
            atomic(self.base/'transaction.json',json.dumps(journal))
            try:
                source=self.accounts/target
                if source.exists():self.copy(source,stage)
                else:stage.mkdir(mode=0o700)
                self.require_safe()
                if self.running():raise Failure('Claude 被其他程序重新打开，已中止切换')
                if self.data.exists():os.replace(self.data,backup)
                os.replace(stage,self.data)
                self.launch(target,self.trace())
                atomic(self.base/'current',target+'\n')
                journal['state']='committed'
                atomic(self.base/'transaction.json',json.dumps(journal))
            except BaseException:
                # If launch succeeded before an error, ensure no process writes into data being restored.
                (self.base/'managed-session.json').unlink(missing_ok=True)
                self.quit_claude()
                self.recover_locked()
                raise
            self.recover_locked()
            self.log('已切换到账号'+target)
            return 'OK:已切换到账号'+target

    def install_guard(self):
        folder=self.home/'Library/LaunchAgents'
        folder.mkdir(parents=True,exist_ok=True)
        path=folder/(LABEL+'.plist')
        app=self.guard_executable()
        arguments=[str(app),'--guard-helper',sys.executable] if app.exists() else [sys.executable,str(self.base/'claude-switch.py'),'guard']
        content={'Label':LABEL,'ProgramArguments':arguments,
                 'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':5,
                 'EnvironmentVariables':{'PATH':'/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'}}
        atomic(path,plistlib.dumps(content).decode())
        os.chmod(path,0o600)
        domain=f'gui/{os.getuid()}'
        self.cmd('/bin/launchctl','bootout',domain+'/'+LABEL)
        # Upgrade cleanup for older wrappers that detached the Python guardian.
        # Validate UID and exact script+action before sending any signal.
        old=read_json(self.base/'guard-heartbeat.json',{}).get('pid')
        if isinstance(old,int) and old!=os.getpid():
            result=self.cmd('/bin/ps','-o','uid=,command=','-p',str(old))
            fields=result.stdout.strip().split(None,1)
            if len(fields)==2 and fields[0]==str(os.getuid()):
                try:parts=shlex.split(fields[1])
                except ValueError:parts=[]
                if parts[-2:]==[str(self.base/'claude-switch.py'),'guard']:
                    try:os.kill(old,signal.SIGTERM)
                    except ProcessLookupError:pass
                    for _ in range(20):
                        if self.cmd('/bin/ps','-o','stat=','-p',str(old)).returncode!=0:break
                        time.sleep(.1)
        for attempt in range(5):
            if self.cmd('/bin/launchctl','bootstrap',domain,str(path)).returncode==0:break
            time.sleep(.25*(attempt+1))
        else:raise Failure('用户级保镖注册失败')
        for _ in range(20):
            if self.guard_ready():return
            time.sleep(.25)
        raise Failure('保镖尚未就绪，请重新检查')

    def guard_executable(self):
        configured=self.config.get('app_path')
        if configured:
            bundle=Path(configured)
            if not bundle.is_absolute():raise Failure('应用安装路径必须为绝对路径')
            executable=bundle/'Contents/MacOS/ClaudeSwitcher'
            if not executable.is_file():raise Failure('已记录的应用路径不可用，请重新安装')
            return executable
        # Older installations did not record a location. Prefer standard locations.
        for directory in (Path('/Applications'),self.home/'Applications',self.home/'Desktop'):
            executable=directory/'Claude切换器.app/Contents/MacOS/ClaudeSwitcher'
            if executable.is_file():return executable
        return self.home/'Applications/Claude切换器.app/Contents/MacOS/ClaudeSwitcher'

    def guard_tick(self,next_remote=0):
        atomic(self.base/'guard-heartbeat.json',json.dumps({'pid':os.getpid(),'time':time.time(),'profile':self.direct_profile_state()}))
        path=self.base/'managed-session.json'
        if not path.exists():return next_remote
        marker=read_json(path,{})
        pid=marker.get('pid')
        # A short launch grace period is unarmed. Never attach to an unrelated future process.
        if not pid:
            if time.time()-marker.get('started',0)>10:path.unlink(missing_ok=True)
            return next_remote
        if pid not in self.process_ids():
            path.unlink(missing_ok=True)
            return next_remote
        healthy=self.local_health()
        if healthy and time.monotonic()>=next_remote:
            trace=self.trace()
            fingerprint=hashlib.sha256(trace.get('ip','').encode()).hexdigest()
            healthy=trace.get('loc')=='US' and fingerprint==marker.get('egress')
            try:healthy=healthy and self.profile_digest()==marker.get('profile')
            except Failure:healthy=False
            next_remote=time.monotonic()+15
        if not healthy:
            # Only the PID bound by a successful managed launch is a termination target.
            result=self.cmd('/bin/kill','-TERM',pid)
            if result.returncode==0:
                path.unlink(missing_ok=True)
                self.log('Shadowrocket 网络或配置检查失败，已终止受保护 Claude 会话')
        return next_remote

    def guard(self):
        lock=open(self.base/'guard.lock','a')
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return
        next_remote=0
        while True:
            next_remote=self.guard_tick(next_remote)
            time.sleep(2)

    def fix(self):
        self.install_guard()
        print('FIX:用户级断线保护已安装')
        if not self.vpn():
            self.cmd('/usr/bin/open','shadowrocket://connect')
            print('FIX:已请求连接 Shadowrocket，请确认所选节点')
        state=self.profile_state()
        issues=state.get('issues',['无法确认当前配置'])
        if issues:
            profile=self.base/'Claude-Shadowrocket.conf'
            if profile.exists():self.cmd('/usr/bin/open','-a','Shadowrocket',str(profile))
            print('FIX:请在 Shadowrocket 导入并启用 Claude 专用配置，原配置可随时切回')
        if state.get('readable') and not state.get('stun_disabled'):
            print('FIX:请在 Shadowrocket 设置 → UDP 开启禁用 STUN')
        print('FIX:系统时区、地区、语言和 IPv6 设置保持不变')


def main():
    b=Backend()
    args=sys.argv[1:]
    action=args[0] if args else 'help'
    try:
        if action=='checkdetail':
            for k,s,d in b.audit():print(k+'='+s+':'+d)
        elif action=='check':
            b.require_safe();print('OK:必要网络检查通过；环境提示请查看界面')
        elif action=='switch' and len(args)==2:print(b.switch(args[1]))
        elif action=='status':print(f'current={b.current()} hasA={int((b.accounts/"A").exists())} hasB={int((b.accounts/"B").exists())}')
        elif action=='recover':print('OK:已恢复' if b.recover() else 'OK:没有未完成的切换')
        elif action=='guard':b.guard()
        elif action=='install-guard':b.install_guard();print('OK:用户级断线保护已安装')
        elif action=='fix':b.fix()
        else:raise Failure('用法：check | checkdetail | status | switch A|B | recover | fix | install-guard')
    except (Failure,OSError,ValueError) as e:
        # No raw subprocess stderr, JSON, paths containing credentials or network addresses.
        print('FAIL:'+ (str(e) if isinstance(e,Failure) else '本地文件操作失败，已保留恢复记录'))
        return 1
    return 0


if __name__=='__main__':
    signal.signal(signal.SIGTERM,lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    sys.exit(main())
