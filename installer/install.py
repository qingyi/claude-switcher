#!/usr/bin/env python3
"""Build and install from tracked source; no sudo and no network downloads."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
LABEL='local.claude-switcher.bodyguard'


def checked(args):
    subprocess.run([str(x) for x in args],check=True)


def build(destination):
    app=destination/'Claude切换器.app'
    if app.exists():shutil.rmtree(app)
    (app/'Contents/MacOS').mkdir(parents=True)
    (app/'Contents/Resources').mkdir()
    info={'CFBundleName':'Claude切换器','CFBundleDisplayName':'Claude切换器','CFBundleIdentifier':'local.claude-switcher',
          'CFBundleVersion':'2','CFBundleShortVersionString':'2.0','CFBundleExecutable':'ClaudeSwitcher',
          'CFBundlePackageType':'APPL','CFBundleIconFile':'AppIcon','NSHighResolutionCapable':True,'LSMinimumSystemVersion':'13.0'}
    (app/'Contents/Info.plist').write_bytes(plistlib.dumps(info))
    sdk=Path('/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk')
    args=['/usr/bin/swiftc']
    if sdk.exists():args+=['-sdk',str(sdk)]
    # Use a typealias to keep @State unambiguously a wrapper with newer SDK macro overloads.
    source=(ROOT/'app/main.swift').read_text().replace('@State private','@ViewState private')
    source=source.replace('import AppKit','import AppKit\ntypealias ViewState<Value> = SwiftUI.State<Value>')
    destination.mkdir(parents=True,exist_ok=True)
    swift=destination/'main.swift';swift.write_text(source)
    args+=['-O','-parse-as-library','-target',platform.machine()+'-apple-macos13.0',str(swift),'-o',str(app/'Contents/MacOS/ClaudeSwitcher'),'-framework','SwiftUI','-framework','AppKit',
           '-module-cache-path',str(destination/'module-cache')]
    checked(args)
    chunks=[]
    with tempfile.TemporaryDirectory(dir=destination) as td:
        for kind,size in [('icp4',16),('icp5',32),('icp6',64),('ic07',128),('ic08',256),('ic09',512),('ic10',1024)]:
            png=Path(td)/(str(size)+'.png')
            subprocess.run(['sips','-z',str(size),str(size),str(ROOT/'assets/icon-2048.png'),'--out',str(png)],check=True,stdout=subprocess.DEVNULL)
            data=png.read_bytes();chunks.append(kind.encode()+struct.pack('>I',len(data)+8)+data)
    body=b''.join(chunks)
    (app/'Contents/Resources/AppIcon.icns').write_bytes(b'icns'+struct.pack('>I',len(body)+8)+body)
    checked(['codesign','--force','--deep','--sign','-',app])
    checked(['codesign','--verify','--deep','--strict',app])
    return app


def install(app, guard=False, app_dir=Path('/Applications')):
    home=Path.home();base=home/'.claude-switcher';binpath=home/'.local/bin'
    app_dir=Path(app_dir).expanduser().resolve()
    target=app_dir/app.name
    if target.is_symlink():raise RuntimeError('拒绝覆盖应用符号链接')
    app_dir.mkdir(parents=True,exist_ok=True)
    base.mkdir(mode=0o700,exist_ok=True);os.chmod(base,0o700)
    binpath.mkdir(parents=True,exist_ok=True)
    backup_root=base/'installation-backups'
    backup_root.mkdir(mode=0o700,exist_ok=True)
    backup=Path(tempfile.mkdtemp(prefix=time.strftime('%Y%m%d-%H%M%S-'),dir=backup_root))
    mappings=[(ROOT/'core/claude-switch.sh',binpath/'claude-switch.sh'),(ROOT/'core/claude-switch.py',base/'claude-switch.py'),
              (ROOT/'core/stun-probe.py',base/'stun-probe.py')]
    for source,script_target in mappings:
        if script_target.is_symlink():raise RuntimeError('拒绝覆盖符号链接 '+str(script_target))
        if script_target.is_dir():
            # Recover only a bundle owned by this application from an older installer.
            info=script_target/'Contents/Info.plist'
            if not info.exists() or plistlib.loads(info.read_bytes()).get('CFBundleIdentifier')!='local.claude-switcher':
                raise RuntimeError('脚本安装路径不是文件，原数据已保留：'+str(script_target))
            shutil.move(script_target,backup/('misplaced-'+script_target.name+'.app'))
        elif script_target.exists():shutil.copy2(script_target,backup/script_target.name)
        shutil.copy2(source,script_target);os.chmod(script_target,0o700)
    (base/'accounts').mkdir(mode=0o700,exist_ok=True)
    config=base/'config.json'
    if not config.exists():
        r=subprocess.run(['/usr/sbin/scutil','--nc','list'],capture_output=True,text=True)
        service='Shadowrocket'
        for line in r.stdout.splitlines():
            if '(com.liguangming.Shadowrocket)' in line:
                match=re.search(r'\b([A-Fa-f0-9-]{36})\b',line)
                if match:service=match.group(1)
        config.write_text(json.dumps({'vpn_service':service,'system_preferences':'preserve'},indent=2)+'\n');os.chmod(config,0o600)
    profile_path=base/'Claude-Shadowrocket.conf'
    if not profile_path.exists():
        spec=importlib.util.spec_from_file_location('profile_tool',ROOT/'installer/profile.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        try:
            _,rows=module.core.active_profile(home)
            module.core.atomic(profile_path,module.render(rows))
        except module.core.Failure:
            print('当前配置需原生导出，请参阅 README 的配置适配说明。')
    previous_config=config.read_bytes()
    previous_agent=home/'Library/LaunchAgents'/(LABEL+'.plist')
    previous_agent_bytes=previous_agent.read_bytes() if previous_agent.exists() else None
    # Quit only the switcher UI. The user's current Claude and VPN remain running.
    subprocess.run(['/usr/bin/pkill','-x','ClaudeSwitcher'],capture_output=True)
    staged=app_dir/(app.name+'.installing')
    if staged.exists():raise RuntimeError('存在未完成应用安装')
    checked(['/usr/bin/ditto',app,staged])
    if target.exists():shutil.move(target,backup/app.name)
    os.replace(staged,target)
    settings=json.loads(previous_config)
    settings['app_path']=str(target)
    with tempfile.NamedTemporaryFile(mode='w',dir=base,delete=False) as f:
        json.dump(settings,f,indent=2,ensure_ascii=False);f.write('\n')
        temp=Path(f.name)
    os.chmod(temp,0o600);os.replace(temp,config)
    try:
        if guard:checked([sys.executable,base/'claude-switch.py','install-guard'])
    except Exception:
        # Keep the old desktop app until the new path and guard are verified.
        config.write_bytes(previous_config);os.chmod(config,0o600)
        if previous_agent_bytes is not None:
            subprocess.run(['/bin/launchctl','bootout',f'gui/{os.getuid()}/{LABEL}'],capture_output=True)
            previous_agent.write_bytes(previous_agent_bytes)
            subprocess.run(['/bin/launchctl','bootstrap',f'gui/{os.getuid()}',str(previous_agent)],capture_output=True)
        raise
    legacy=home/'Desktop'/app.name
    if legacy!=target and legacy.is_dir() and not legacy.is_symlink():
        info=legacy/'Contents/Info.plist'
        if info.exists() and plistlib.loads(info.read_bytes()).get('CFBundleIdentifier')=='local.claude-switcher':
            shutil.move(legacy,backup/('desktop-'+app.name))
    print('已安装至 '+str(target)+'；旧版已备份，系统偏好未修改。')
    return target


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--build-dir',type=Path,default=ROOT/'build');p.add_argument('--install',action='store_true');p.add_argument('--guard',action='store_true');p.add_argument('--existing-app',type=Path);p.add_argument('--app-dir',type=Path,default=Path('/Applications'))
    a=p.parse_args()
    app=a.existing_app or build(a.build_dir.resolve())
    if a.install:install(app,a.guard,a.app_dir)
