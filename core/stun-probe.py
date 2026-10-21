#!/usr/bin/env python3
"""Bounded STUN observations, not a proof of all WebRTC traffic being blocked."""
import concurrent.futures
import ipaddress
import json
import os
import signal
import socket
import struct
import sys

SERVERS=(('stun.miwifi.com',3478),('stun.chat.bilibili.com',3478),('stun.l.google.com',19302),('stun.cloudflare.com',3478))
MAGIC=0x2112A442


def parse_response(data, txn):
    if len(data)<20:return None
    typ,length,cookie,received=struct.unpack('!HHI12s',data[:20])
    if typ!=0x0101 or cookie!=MAGIC or received!=txn or length%4 or len(data)<20+length:return None
    pos=20
    while pos+4<=20+length:
        kind,size=struct.unpack('!HH',data[pos:pos+4])
        if pos+4+size>20+length:return None
        value=data[pos+4:pos+4+size]
        if kind in (0x0020,0x0001) and size>=8 and value[1]==1:
            address=value[4:8]
            if kind==0x0020:address=bytes(a^b for a,b in zip(address,struct.pack('!I',MAGIC)))
            return str(ipaddress.IPv4Address(address))
        pos+=4+size+(-size%4)
    return None


def probe(host,port):
    txn=os.urandom(12)
    try:
        addr=socket.gethostbyname(host)
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as s:
            s.settimeout(2)
            s.connect((addr,port))
            s.send(struct.pack('!HHI12s',0x0001,0,MAGIC,txn))
            data=s.recv(2048)
        return parse_response(data,txn)
    except OSError:return None


def output(addresses, timed_out=False):
    result={'status':'responses' if addresses else 'no_response','addresses':sorted(addresses),'timed_out':timed_out}
    if '--json' in sys.argv:print(json.dumps(result))
    else:print('RESULT:'+(','.join(sorted(addresses)) if addresses else 'NONE'))


def main():
    addresses=set()
    def alarm(*_):
        # DNS may block inside a worker. Exit the whole process, not just its main thread.
        output(addresses,True)
        sys.stdout.flush()
        os._exit(0)
    signal.signal(signal.SIGALRM,alarm)
    signal.alarm(8)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures=[pool.submit(probe,*hp) for hp in SERVERS]
        for future in concurrent.futures.as_completed(futures):
            address=future.result()
            if address:addresses.add(address)
    signal.alarm(0)
    output(addresses)


if __name__=='__main__':main()
