#!/usr/bin/env python3
"""Export a separate local profile using a read-only snapshot of the active DB."""
import importlib.util
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('switcher',ROOT/'core/claude-switch.py')
core=importlib.util.module_from_spec(spec);spec.loader.exec_module(core)


def render(rows):
    # General + Rule are the only sections in the supported local format.
    # Other sections can contain certificates/scripts and need a faithful native export.
    if any(s.lower() not in ('general','rule') for s,*_ in rows):
        raise core.Failure('配置含脚本、证书或策略组，请先使用 Shadowrocket 原生导出再适配')
    general=[];rules=[]
    for section,name,value,option,ext,remarks in rows:
        if name=='COMMENT':
            line=value if value.startswith(('#',';')) else '# '+value
        elif section.lower()=='general':
            if name.lower()=='udp-policy-not-supported-behaviour':continue
            line=name+' = '+value
        else:
            # Preserve optional rule modifiers verbatim, without serializing internal UI flags.
            values=[name]
            if value:values.append(value)
            if option:values.append(option)
            if ext and ext not in ('0','1'):values.append(ext)
            line=','.join(values)
        (general if section.lower()=='general' else rules).append(line)
    prefix=['# Claude Switcher: Shadowrocket managed prefix']
    prefix += ['DOMAIN-SUFFIX,'+d+',PROXY' for d in core.DOMAINS]
    while rules[:len(prefix)] == prefix:
        rules=rules[len(prefix):]
    return '[General]\n'+'\n'.join(general+['udp-policy-not-supported-behaviour = REJECT'])+'\n\n[Rule]\n'+'\n'.join(prefix+rules)+'\n'


if __name__=='__main__':
    try:
        name,rows=core.active_profile(Path.home())
        destination=Path(sys.argv[1])
        core.atomic(destination,render(rows))
        print('已生成保留现有分流规则的独立 Claude 配置')
    except core.Failure as e:sys.exit(str(e))
