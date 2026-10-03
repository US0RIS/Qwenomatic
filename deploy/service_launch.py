#!/usr/bin/env python3
"""Privileged fixed broker/inference namespace launcher. No agent access."""
import argparse
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import pwd
import signal
import socket
import subprocess
import sys
import threading
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deploy.launch import protected_path, verify_runtime, run
from supervisor.safety.boundary import protected_json, SafetyError
from storage.events.canonical import digest


def firewall(destinations, child, listen, peers):
    allows = '\n'.join(f'ip daddr {ip} tcp dport {port} accept' for ip, port in sorted(destinations))
    inbound = '\n'.join(f'ip saddr {ip} tcp dport {listen[1]} accept' for ip in peers)
    return f'''table inet qwenomatic {{
      chain output {{ type filter hook output priority -150; policy drop;
        {allows}
        ct state established,related accept
        ip daddr {child} icmp type destination-unreachable icmp code admin-prohibited accept
        reject with icmpx type admin-prohibited
      }}
      chain input {{ type filter hook input priority -150; policy drop;
        {inbound}
        ct state established,related accept
        ip daddr {child} icmp type destination-unreachable icmp code admin-prohibited accept
      }}
      chain forward {{ type filter hook forward priority -150; policy drop; }}
    }}'''


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--role', choices=['broker', 'inference'], required=True)
    p.add_argument('--config')
    p.add_argument('--user')
    p.add_argument('--farm-user')
    p.add_argument('--python')
    p.add_argument('--cleanup', action='store_true')
    p.add_argument('--check-only', action='store_true')
    a = p.parse_args()
    if os.geteuid() != 0 or not sys.flags.isolated or not sys.flags.no_site:
        raise SafetyError('trusted system Python -I -S as root required')
    lock = open('/run/qwenomatic-service-' + a.role + '.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    record = Path('/run/qwenomatic-service-' + a.role + '.json')
    def cleanup(state):
        expected = 'qs-' + state['suffix']
        if not __import__('re').fullmatch('[0-9a-f]{8}', state['suffix']) or state['namespace'] != expected:
            raise SafetyError('invalid service recovery')
        result = subprocess.run(['ip', 'netns', 'pids', expected], capture_output=True, text=True)
        for pid in result.stdout.split():
            try:
                os.kill(int(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        for command in (['ip','netns','del',expected], ['ip','link','del','qsh'+state['suffix']], ['nft','delete','table','ip','qsn_'+state['suffix']]):
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode and not any(x in result.stderr.lower() for x in ('no such', 'cannot find', 'does not exist')):
                raise SafetyError('service cleanup failed')
        if state.get('evidence'):
            evidence = Path(state['evidence'])
            if evidence.parent != Path('/run/qwenomatic-services') or not evidence.stem.isdecimal():
                raise SafetyError('invalid recovery evidence')
            evidence.unlink(missing_ok=True)
        record.unlink(missing_ok=True)
    if a.cleanup:
        if record.exists():
            cleanup(protected_json(record))
        return
    if record.exists():
        raise SafetyError('interrupted service; run --cleanup')
    if not all((a.config, a.user, a.farm_user, a.python)):
        p.error('explicit config, service/farm user and Python required')
    verify_runtime(a.python)
    checkout = Path(__file__).resolve().parents[1]
    for source in checkout.rglob('*.py'):
        protected_path(source)
    cfg = protected_json(Path(a.config))
    if set(cfg) != {'service', 'subnet', 'peers'}:
        raise SafetyError('invalid service deployment config')
    service_path = Path(cfg['service'])
    service = protected_json(service_path)
    network = ipaddress.IPv4Network(cfg['subnet'], strict=True)
    if network.prefixlen != 30 or not network.is_private:
        raise SafetyError('explicit private /30 required')
    host, child = str(network.network_address + 1), str(network.network_address + 2)
    if service['listen'][0] != child or type(service['listen'][1]) is not int or not 1 <= service['listen'][1] <= 65535:
        raise SafetyError('fixed bind endpoint mismatch')
    if not cfg['peers'] or any(str(ipaddress.IPv4Address(ip)) != ip for ip in cfg['peers']):
        raise SafetyError('explicit IPv4 inbound peers required')
    user, farm = pwd.getpwnam(a.user), pwd.getpwnam(a.farm_user)
    if not user.pw_uid or user.pw_uid == farm.pw_uid or user.pw_gid == farm.pw_gid:
        raise SafetyError('distinct nonroot service UID/group required')
    if Path('/proc/sys/net/ipv4/ip_forward').read_text().strip() != '1':
        raise SafetyError('dedicated VM forwarding must be operator-provisioned')
    destinations, public = set(), set()
    if a.role == 'broker':
        from broker.authority import validate_policy
        from broker.network import resolve_public, service_url
        validate_policy(service['policy'])
        for spec in service['policy']['services'].values():
            public.update(resolve_public(service_url(spec['endpoint'])[0]))
        destinations.update((ip, 443) for ip in public)
        # Runtime DNS can resolve approved names, but cannot route to a new IP.
        # Use /etc/hosts or operator-provisioned resolver reachable within broker
        # VM; no general UDP/DNS egress is granted by this launcher.
        if service['inference'] is not None:
            from broker.inference import InferenceProxy
            inf = InferenceProxy(service['inference'])
            destinations.add((inf.host, inf.port))
        secret_paths = [service['signing_key'], service['tls_key']] + [s['credential_file'] for s in service['policy']['services'].values()]
        for path in secret_paths:
            protected_path(path)
            probe = subprocess.run(['setpriv','--reuid',str(farm.pw_uid),'--regid',str(farm.pw_gid),'--clear-groups','--bounding-set=-all','--no-new-privs','/usr/bin/test','-r',path])
            if probe.returncode == 0:
                raise SafetyError('farm can read broker secret')
    else:
        if set(service) != {'listen', 'executable'}:
            raise SafetyError('inference service supports only fixed Ollama serve')
        protected_path(service['executable'])
    suffix = uuid.uuid4().hex[:8]
    ns, hi, ci, nat = 'qs-'+suffix, 'qsh'+suffix, 'qsc'+suffix, 'qsn_'+suffix
    state = {'suffix': suffix, 'namespace': ns, 'evidence': None}
    def save():
        temporary = record.with_suffix('.tmp')
        with temporary.open('w') as stream:
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        temporary.replace(record)
    save()
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    canary = socket.socket()
    try:
        run('ip','netns','add',ns)
        run('ip','link','add',hi,'type','veth','peer','name',ci)
        run('ip','link','set',ci,'netns',ns)
        run('ip','addr','add',host+'/30','dev',hi)
        run('ip','link','set',hi,'up')
        run('ip','netns','exec',ns,'ip','addr','add',child+'/30','dev',ci)
        run('ip','netns','exec',ns,'ip','link','set',ci,'up')
        run('ip','netns','exec',ns,'ip','link','set','lo','up')
        run('ip','netns','exec',ns,'ip','route','add','default','via',host)
        run('ip','netns','exec',ns,'nft','-f','-',input=firewall(destinations,child,service['listen'],cfg['peers']))
        run('nft','-f','-',input=f'table ip {nat} {{ chain nat {{ type nat hook postrouting priority srcnat; policy accept; ip saddr {child} masquerade; }}; }}')
        canary.bind((host,0))
        canary.listen(8)
        def accept():
            while True:
                try:
                    conn,_ = canary.accept()
                    conn.close()
                except OSError:
                    return
        threading.Thread(target=accept,daemon=True).start()
        port = canary.getsockname()[1]
        run('ip','netns','exec',ns,'nft','insert','rule','inet','qwenomatic','output','ip','daddr',host,'tcp','dport',str(port),'accept')
        probe = f'import socket;s=socket.create_connection(({host!r},{port}),2);s.close()'
        run('ip','netns','exec',ns,a.python,'-I','-S','-c',probe)
        run('ip','netns','exec',ns,'nft','-f','-',input='delete table inet qwenomatic\n'+firewall(destinations,child,service['listen'],cfg['peers']))
        inode=run('ip','netns','exec',ns,a.python,'-I','-S','-c',"import os;print(os.stat('/proc/self/ns/net').st_ino)").strip()
        directory=Path('/run/qwenomatic-services')
        directory.mkdir(mode=0o755,exist_ok=True)
        protected_path(directory)
        evidence=directory/(inode+'.json')
        state['evidence']=str(evidence)
        save()
        evidence.write_text(json.dumps({'role':a.role,'uid':user.pw_uid,'namespace':'net:['+inode+']','canary':[host,port], 'public_ips':sorted(public),'config_hash':digest(service)}))
        evidence.chmod(0o444)
        prefix=['ip','netns','exec',ns,'setpriv','--reuid',str(user.pw_uid),'--regid',str(user.pw_gid),'--clear-groups','--bounding-set=-all','--inh-caps=-all','--ambient-caps=-all','--no-new-privs']
        bootstrap='import sys;sys.path.insert(0,'+repr(str(checkout))+'); '
        check=bootstrap+'from broker.deployment import ServiceBoundary;ServiceBoundary('+repr(a.role)+');print("service kernel barrier verified")'
        env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C.UTF-8'}
        subprocess.run(prefix+[a.python,'-I','-c',check],check=True,env=env,cwd='/')
        if a.check_only:
            return
        if a.role=='broker':
            command=bootstrap+'from broker.server import main;sys.argv=["broker","--config",'+repr(str(service_path))+'];main()'
            subprocess.run(prefix+[a.python,'-I','-c',command],check=True,env=env,cwd='/')
        else:
            # Independent inference namespace has NO outbound allow rule.
            # No model-supplied launch arguments or management interfaces.
            env['OLLAMA_HOST']=f'{child}:{service["listen"][1]}'
            subprocess.run(prefix+[service['executable'],'serve'],check=True,env=env,cwd='/')
    finally:
        canary.close()
        cleanup(state)


if __name__ == '__main__':
    main()
