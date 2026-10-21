import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]


def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod


core=load('switcher',ROOT/'core/claude-switch.py')
stun=load('stun',ROOT/'core/stun-probe.py')
profile=load('profile_tool',ROOT/'installer/profile.py')
installer=load('installer_tool',ROOT/'installer/install.py')


class Sandbox(core.Backend):
    def __init__(self,home):
        super().__init__(home)
        self.live=False;self.healthy=True;self.country='US';self.fail_launch=False;self.fail_copy=False
        self.checks=0;self.fail_check=0
    def running(self):return self.live
    def process_ids(self):return {'12345'} if self.live else set()
    def profile_digest(self):return 'test-profile'
    def quit_claude(self):self.live=False
    def require_safe(self):
        self.checks+=1
        if self.checks==self.fail_check:raise core.Failure('模拟网络失效')
    def trace(self):return {'loc':self.country,'ip':'203.0.113.10'}
    def local_health(self):return self.healthy
    def copy(self,source,target):
        if self.fail_copy and source==self.accounts/'B':raise core.Failure('模拟磁盘错误')
        shutil.copytree(source,target)
    def cmd(self,*args,timeout=10):
        if args[:3]==('/usr/bin/open','-a','Claude'):
            if self.fail_launch:return subprocess.CompletedProcess(args,1,'','')
            self.live=True
        return subprocess.CompletedProcess(args,0,'','')


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.b=Sandbox(Path(self.tmp.name))
        self.b.data.mkdir(parents=True)
        (self.b.data/'session').write_text('original A login fixture')
    def initialize(self):self.b.switch('A')
    def test_round_trip_preserves_both(self):
        self.initialize();self.b.switch('B')
        self.assertEqual(list(self.b.data.iterdir()),[])
        (self.b.data/'session').write_text('B login fixture')
        self.b.switch('A')
        self.assertEqual((self.b.data/'session').read_text(),'original A login fixture')
        self.assertEqual((self.b.accounts/'B/session').read_text(),'B login fixture')
        self.assertEqual(self.b.current(),'A')
        self.assertFalse((self.b.base/'transaction.json').exists())
    def test_first_switch_b_archives_existing_a(self):
        self.b.switch('B')
        self.assertEqual((self.b.accounts/'A/session').read_text(),'original A login fixture')
        self.assertEqual(self.b.current(),'B')
    def test_invalid_targets_cannot_touch_data(self):
        for target in ('','../A','A/../../tmp','C','/tmp'):
            with self.assertRaises(core.Failure):self.b.switch(target)
        self.assertEqual((self.b.data/'session').read_text(),'original A login fixture')
        self.assertEqual(self.b.checks,0)
    def test_network_failure_prevents_quit_or_copy(self):
        self.b.live=True;self.b.fail_check=1
        with self.assertRaises(core.Failure):self.b.switch('B')
        self.assertTrue(self.b.live)
        self.assertFalse((self.b.accounts/'A').exists())
    def test_copy_failure_leaves_data_and_state(self):
        self.initialize();(self.b.accounts/'B').mkdir();(self.b.accounts/'B/session').write_text('B')
        self.b.fail_copy=True
        with self.assertRaises(core.Failure):self.b.switch('B')
        self.assertEqual(self.b.current(),'A')
        self.assertEqual((self.b.data/'session').read_text(),'original A login fixture')
        self.assertEqual((self.b.accounts/'B/session').read_text(),'B')
    def test_final_check_failure_rolls_back(self):
        self.initialize();self.b.fail_check=self.b.checks+2
        with self.assertRaises(core.Failure):self.b.switch('B')
        self.assertEqual(self.b.current(),'A');self.assertTrue((self.b.data/'session').exists())
    def test_launch_failure_restores_original_directory(self):
        self.initialize();self.b.fail_launch=True
        with self.assertRaises(core.Failure):self.b.switch('B')
        self.assertEqual(self.b.current(),'A')
        self.assertEqual((self.b.data/'session').read_text(),'original A login fixture')
        self.assertFalse((self.b.base/'managed-session.json').exists())
    def test_egress_change_before_launch_blocks_and_restores(self):
        self.initialize();self.b.country='DE'
        with self.assertRaises(core.Failure):self.b.switch('B')
        self.assertEqual(self.b.current(),'A');self.assertTrue((self.b.data/'session').exists())
    def test_tunnel_loss_before_launch_blocks(self):
        self.b.healthy=False
        with self.assertRaises(core.Failure):self.b.switch('A')
        self.assertFalse(self.b.running())
    def test_symlink_account_is_rejected(self):
        external=self.b.home/'outside';external.mkdir()
        (self.b.accounts/'B').symlink_to(external,target_is_directory=True)
        with self.assertRaises(core.Failure):self.b.switch('B')
        self.assertEqual(list(external.iterdir()),[])
    def test_concurrent_switch_is_rejected(self):
        other=Sandbox(self.b.home)
        with self.b.lock():
            with self.assertRaises(core.Failure):other.switch('B')
    def test_crash_recovery_restores_backup(self):
        self.initialize();self.b.live=False
        journal={'id':'a'*32,'previous':'A','target':'B','had_data':True,'state':'preparing'}
        stage,backup=self.b.txn_paths(journal)
        os.replace(self.b.data,backup);self.b.data.mkdir();(self.b.data/'session').write_text('uncommitted B')
        core.atomic(self.b.base/'transaction.json',json.dumps(journal))
        core.atomic(self.b.base/'current','B\n')
        self.b.recover()
        self.assertEqual(self.b.current(),'A')
        self.assertEqual((self.b.data/'session').read_text(),'original A login fixture')
    def test_recovery_refuses_live_process(self):
        core.atomic(self.b.base/'transaction.json',json.dumps({'id':'b'*32,'previous':'A','had_data':True}))
        self.b.live=True
        with self.assertRaises(core.Failure):self.b.recover()
        self.assertTrue((self.b.data/'session').exists())
    def test_corrupt_journal_does_not_delete_data(self):
        core.atomic(self.b.base/'transaction.json',json.dumps({'id':'../../x','previous':'A'}))
        with self.assertRaises(core.Failure):self.b.recover()
        self.assertTrue((self.b.data/'session').exists())
    def test_interrupted_snapshot_restores_previous(self):
        old=self.b.accounts/'A.previous';old.mkdir();(old/'session').write_text('previous')
        temp=self.b.accounts/'A.tmp';temp.mkdir();(temp/'incomplete').write_text('partial')
        core.atomic(self.b.base/'snapshot.json',json.dumps({'account':'A'}))
        self.b.recover()
        self.assertEqual((self.b.accounts/'A/session').read_text(),'previous')
        self.assertFalse(temp.exists())
    def test_snapshot_copy_failure_retains_old(self):
        self.initialize();self.b.live=False
        def partial_copy(source,target):
            target.mkdir();(target/'partial').write_text('x');raise core.Failure('copy failed')
        with patch.object(self.b,'copy',side_effect=partial_copy):
            with self.assertRaises(core.Failure):self.b.archive('A')
        self.b.recover()
        self.assertEqual((self.b.accounts/'A/session').read_text(),'original A login fixture')
    def test_switch_calls_audit_even_same_account(self):
        self.initialize();count=self.b.checks;self.b.switch('A')
        self.assertEqual(self.b.checks-count,2)
    def test_private_files_and_no_raw_ip_in_saved_egress(self):
        self.initialize()
        p=self.b.base/'egress.json'
        self.assertEqual(p.stat().st_mode&0o777,0o600)
        self.assertNotIn('203.0.113.10',p.read_text())

    def test_guard_does_not_touch_unmanaged_claude(self):
        self.b.live=True
        with patch.object(self.b,'cmd',wraps=self.b.cmd) as cmd:
            self.b.guard_tick()
            self.assertFalse(any(call.args[:2]==('/bin/kill','-TERM') for call in cmd.call_args_list))
    def test_guard_closes_only_bound_pid_on_tunnel_failure(self):
        self.initialize();self.b.healthy=False
        with patch.object(self.b,'cmd',wraps=self.b.cmd) as cmd:
            self.b.guard_tick()
            self.assertTrue(any(call.args==('/bin/kill','-TERM','12345') for call in cmd.call_args_list))
        self.assertFalse((self.b.base/'managed-session.json').exists())
    def test_guard_disarms_after_process_exit(self):
        self.initialize();self.b.live=False
        with patch.object(self.b,'cmd',wraps=self.b.cmd) as cmd:
            self.b.guard_tick()
            self.assertFalse(any(call.args[:2]==('/bin/kill','-TERM') for call in cmd.call_args_list))
        self.assertFalse((self.b.base/'managed-session.json').exists())
    def test_guard_detects_profile_drift(self):
        self.initialize()
        with patch.object(self.b,'profile_digest',return_value='changed'), patch.object(self.b,'cmd',wraps=self.b.cmd) as cmd:
            self.b.guard_tick()
            self.assertTrue(any(call.args==('/bin/kill','-TERM','12345') for call in cmd.call_args_list))

    @unittest.skipUnless(__import__('sys').platform=='darwin','macOS ditto integration')
    def test_real_ditto_round_trip_with_file_metadata(self):
        original_cmd=self.b.cmd
        def command(*args,timeout=10):
            if args[0]=='/usr/bin/ditto':return core.run(list(args),timeout)
            return original_cmd(*args,timeout=timeout)
        self.b.cmd=command
        self.b.copy=lambda source,target:core.Backend.copy(self.b,source,target)
        subprocess.run(['/usr/bin/xattr','-w','com.claude-switcher.fixture','test-metadata',str(self.b.data/'session')],check=True)
        self.b.switch('A');self.b.switch('B')
        (self.b.data/'session').write_text('B fixture through real ditto')
        self.b.switch('A')
        self.assertEqual((self.b.data/'session').read_text(),'original A login fixture')
        result=subprocess.run(['/usr/bin/xattr','-p','com.claude-switcher.fixture',str(self.b.data/'session')],capture_output=True,text=True,check=True)
        self.assertEqual(result.stdout.strip(),'test-metadata')
        self.assertEqual((self.b.accounts/'B/session').read_text(),'B fixture through real ditto')
    def test_non_directory_account_is_rejected(self):
        (self.b.accounts/'B').write_text('not a snapshot directory')
        with self.assertRaises(core.Failure):self.b.switch('B')
        self.assertTrue((self.b.data/'session').exists())


class ProfileTests(unittest.TestCase):
    def rows(self):
        return [('general','udp-policy-not-supported-behaviour','REJECT','','','')]+[
            ('rule','DOMAIN-SUFFIX',domain,'PROXY','','') for domain in core.DOMAINS]
    def test_valid_managed_prefix(self):self.assertEqual(core.profile_issues(self.rows()),[])
    def test_earlier_direct_rule_fails(self):
        rows=self.rows();rows.insert(1,('rule','FINAL','','DIRECT','',''))
        self.assertTrue(core.profile_issues(rows))
    def test_udp_direct_is_rejected(self):
        rows=self.rows();rows[0]=('general','udp-policy-not-supported-behaviour','DIRECT','','','')
        self.assertTrue(core.profile_issues(rows))
    def test_comment_is_ignored(self):
        rows=self.rows();rows.insert(1,('rule','COMMENT','# header','0','0',''))
        self.assertFalse(core.profile_issues(rows))
    def test_export_preserves_tailscale_and_adds_udp(self):
        rows=[('general','ipv6','false','','',''),('rule','IP-CIDR','100.64.0.0/10','TAILSCALE','no-resolve',''),('rule','FINAL','','PROXY','','')]
        s=profile.render(rows)
        self.assertIn('IP-CIDR,100.64.0.0/10,TAILSCALE,no-resolve',s)
        self.assertLess(s.index('DOMAIN-SUFFIX,claude.ai'),s.index('100.64.0.0/10'))
        self.assertIn('udp-policy-not-supported-behaviour = REJECT',s)
    def test_export_refuses_unknown_sections(self):
        with self.assertRaises(profile.core.Failure):profile.render([('mitm','private-key','secret','','','')])
    def test_fake_range_accepts_both_halves(self):
        self.assertTrue(core.is_fake('198.19.0.1'));self.assertTrue(core.is_fake('198.18.0.1'))
        self.assertFalse(core.is_fake('198.20.0.1'));self.assertFalse(core.is_fake('malformed'))


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.b=core.Backend(Path(self.tmp.name))
    def test_ipv4_mapped_ipv6_uses_ipv4_route(self):
        self.b.cmd=lambda *a,**k:subprocess.CompletedProcess(a,0,'ip=203.0.113.10\nloc=US\nremote=::ffff:198.18.0.2','')
        self.b.addresses=lambda:['198.18.0.3']
        seen=[]
        def route(a,ipv6=False):seen.append((a,ipv6));return 'utun4'
        self.b.route=route
        state,detail=self.b.ipv6()
        self.assertEqual(state,'warn')
        self.assertIn(('198.18.0.2',False),seen)
    def test_ipv6_in_another_tunnel_fails(self):
        self.b.cmd=lambda *a,**k:subprocess.CompletedProcess(a,0,'ip=2001:db8::a\nloc=US\nremote=2001:db8::b','')
        self.b.addresses=lambda:['198.18.0.3']
        self.b.route=lambda a,ipv6=False:'utun0' if ipv6 else 'utun4'
        self.assertEqual(self.b.ipv6()[0],'fail')
    def test_unavailable_ipv6_is_not_a_safety_pass(self):
        self.b.cmd=lambda *a,**k:subprocess.CompletedProcess(a,7,'','')
        self.assertEqual(self.b.ipv6()[0],'warn')
    def test_stale_profile_cache_cannot_bypass_private_read_failure(self):
        self.b.direct_profile_state=lambda:{'readable':False,'issues':['denied']}
        self.b.guard_ready=lambda:True
        core.atomic(self.b.base/'guard-heartbeat.json',json.dumps({'time':0,'profile':{'readable':True,'issues':[],'digest':'stale'}}))
        with self.assertRaises(core.Failure):self.b.profile_digest()
    def test_profile_cache_requires_live_service(self):
        import time
        self.b.direct_profile_state=lambda:{'readable':False,'issues':['denied']}
        self.b.guard_ready=lambda:False
        core.atomic(self.b.base/'guard-heartbeat.json',json.dumps({'time':time.time(),'profile':{'readable':True,'issues':[],'digest':'fresh'}}))
        with self.assertRaises(core.Failure):self.b.profile_digest()
    def test_stun_protection_preference_is_required_for_launch_baseline(self):
        self.b.profile_state=lambda:{'readable':True,'issues':[],'digest':'rules','stun_disabled':False}
        with self.assertRaises(core.Failure):self.b.profile_digest()
    def test_guard_ready_rejects_orphan_heartbeat(self):
        import time
        core.atomic(self.b.base/'guard-heartbeat.json',json.dumps({'time':time.time(),'pid':12345,'profile':{'readable':True}}))
        self.b.cmd=lambda *a,**k:subprocess.CompletedProcess(a,0,'state = running\npid = 54321\n','')
        self.assertFalse(self.b.guard_ready())
    def test_guard_ready_requires_actual_launchd_pid(self):
        import time
        core.atomic(self.b.base/'guard-heartbeat.json',json.dumps({'time':time.time(),'pid':12345,'profile':{'readable':True}}))
        self.b.cmd=lambda *a,**k:subprocess.CompletedProcess(a,0,'state = running\npid = 12345\n','')
        self.assertTrue(self.b.guard_ready())
    def test_guard_with_revoked_file_access_is_not_ready(self):
        import time
        core.atomic(self.b.base/'guard-heartbeat.json',json.dumps({'time':time.time(),'pid':12345,'profile':{'readable':False}}))
        self.b.cmd=lambda *a,**k:subprocess.CompletedProcess(a,0,'state = running\npid = 12345\n','')
        self.assertFalse(self.b.guard_ready())
    def test_environment_proxy_is_bypassed_for_observations(self):
        commands=[]
        def command(*a,**k):
            commands.append(a)
            return subprocess.CompletedProcess(a,0,'ip=203.0.113.10\nloc=US','')
        self.b.cmd=command;self.b.trace()
        self.assertIn('--noproxy',commands[0])
    def test_check_requires_all_failing_rows(self):
        self.b.audit=lambda:[('出口IP','fail','not US'),('时区','warn','preserved')]
        with self.assertRaises(core.Failure):self.b.require_safe()
    def test_warnings_do_not_become_failures_or_hidden(self):
        self.b.audit=lambda:[('出口IP','ok','US'),('时区','warn','preserved')]
        self.b.require_safe()


class InstallationPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.b=core.Backend(Path(self.tmp.name))
    def executable(self,bundle):
        path=bundle/'Contents/MacOS/ClaudeSwitcher'
        path.parent.mkdir(parents=True);path.write_text('fixture')
        return path
    def test_configured_location_is_used_for_guard(self):
        executable=self.executable(self.b.home/'Applications/Claude切换器.app')
        self.b.config['app_path']=str(executable.parents[2])
        self.assertEqual(self.b.guard_executable(),executable)
        self.b.cmd=lambda *a,**k:subprocess.CompletedProcess(a,0,'','')
        self.b.guard_ready=lambda:True
        self.b.install_guard()
        import plistlib
        state=plistlib.loads((self.b.home/'Library/LaunchAgents'/ (core.LABEL+'.plist')).read_bytes())
        self.assertEqual(state['ProgramArguments'][0],str(executable))
    def test_missing_recorded_app_is_not_silently_replaced(self):
        self.b.config['app_path']=str(self.b.home/'missing.app')
        with self.assertRaises(core.Failure):self.b.guard_executable()
    def test_relative_recorded_location_is_rejected(self):
        self.b.config['app_path']='relative.app'
        with self.assertRaises(core.Failure):self.b.guard_executable()
    def test_legacy_desktop_location_can_still_be_found(self):
        expected=self.executable(self.b.home/'Desktop/Claude切换器.app')
        original=Path.is_file
        def is_file(path):
            if str(path).startswith('/Applications/'):return False
            return original(path)
        with patch.object(Path,'is_file',is_file):self.assertEqual(self.b.guard_executable(),expected)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name)
        self.base=self.home/'.claude-switcher';self.base.mkdir()
        (self.base/'config.json').write_text(json.dumps({'vpn_service':'fixture','system_preferences':'preserve'}))
        (self.base/'Claude-Shadowrocket.conf').write_text('fixture profile')
        self.source=self.home/'build/Claude切换器.app'
        self.legacy=self.home/'Desktop/Claude切换器.app'
        import plistlib
        for p in [self.source,self.legacy]:
            (p/'Contents/MacOS').mkdir(parents=True)
            (p/'Contents/MacOS/ClaudeSwitcher').write_text('fixture executable')
            (p/'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier':'local.claude-switcher'}))
        self.calls=[];self.fail_guard=False
    def command(self,args,**kwargs):
        args=[str(a) for a in args];self.calls.append(args)
        if args[0]=='/usr/bin/ditto':shutil.copytree(args[1],args[2])
        if args[-1]=='install-guard' and self.fail_guard:raise subprocess.CalledProcessError(1,args)
        return subprocess.CompletedProcess(args,0,'','')
    def test_migration_records_target_and_backs_up_desktop(self):
        with patch.object(Path,'home',return_value=self.home),patch.object(installer.subprocess,'run',side_effect=self.command):
            target=installer.install(self.source,guard=True,app_dir=self.home/'Applications')
        self.assertEqual(target,(self.home/'Applications/Claude切换器.app').resolve())
        self.assertTrue((target/'Contents/MacOS/ClaudeSwitcher').exists())
        self.assertTrue((self.base/'stun-probe.py').is_file())
        self.assertTrue((self.base/'claude-switch.py').is_file())
        self.assertEqual(json.loads((self.base/'config.json').read_text())['app_path'],str(target))
        self.assertFalse(self.legacy.exists())
        self.assertEqual(len(list((self.base/'installation-backups').glob('*/desktop-Claude切换器.app'))),1)
        self.assertTrue(any(args[-1]=='install-guard' for args in self.calls))
    def test_misplaced_bundle_at_script_path_is_backed_up_and_repaired(self):
        misplaced=self.base/'stun-probe.py'
        shutil.copytree(self.source,misplaced)
        with patch.object(Path,'home',return_value=self.home),patch.object(installer.subprocess,'run',side_effect=self.command):
            target=installer.install(self.source,guard=True,app_dir=self.home/'Applications')
        self.assertEqual(target,(self.home/'Applications/Claude切换器.app').resolve())
        self.assertTrue(misplaced.is_file())
        self.assertEqual(len(list((self.base/'installation-backups').glob('*/misplaced-stun-probe.py.app'))),1)
    def test_failed_guard_keeps_original_desktop_and_configuration(self):
        self.fail_guard=True
        before=(self.base/'config.json').read_bytes()
        with patch.object(Path,'home',return_value=self.home),patch.object(installer.subprocess,'run',side_effect=self.command):
            with self.assertRaises(subprocess.CalledProcessError):
                installer.install(self.source,guard=True,app_dir=self.home/'Applications')
        self.assertTrue(self.legacy.exists())
        self.assertEqual((self.base/'config.json').read_bytes(),before)


class STUNTests(unittest.TestCase):
    txn=b'abcdefghijkl'
    def message(self,txn=None,kind=0x0020):
        ip=bytes(a^b for a,b in zip(bytes([203,0,113,2]),struct.pack('>I',stun.MAGIC)))
        attr=struct.pack('!HHBBH',kind,8,0,1,1234)+ip
        return struct.pack('!HHI12s',0x0101,len(attr),stun.MAGIC,txn or self.txn)+attr
    def test_valid_xor(self):self.assertEqual(stun.parse_response(self.message(),self.txn),'203.0.113.2')
    def test_wrong_transaction_rejected(self):self.assertIsNone(stun.parse_response(self.message(b'xxxxxxxxxxxx'),self.txn))
    def test_truncated_rejected(self):self.assertIsNone(stun.parse_response(self.message()[:-1],self.txn))
    def test_bad_cookie_rejected(self):
        data=bytearray(self.message());data[4]=0
        self.assertIsNone(stun.parse_response(data,self.txn))


if __name__=='__main__':unittest.main()
