#!/usr/bin/env python3
"""Root-only, controlled-network Phase 1 acceptance; never contacts the Internet.

Run in a throwaway Linux VM, with cryptography installed in --python. The test
creates its own TLS provider, inference server and principals, and modifies the
VM's /etc/hosts and default CA bundle only for the duration of this test.
"""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--python',required=True);a=parser.parse_args()
    if os.geteuid()!=0: raise RuntimeError('requires root in a disposable Linux VM')
    root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root))
    from broker.protocol import canonical, receive, send, Rejected
    from broker.network import public_ip
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa, ed25519
    import ipaddress
    broker_user=pwd.getpwnam('qbroker');infer_user=pwd.getpwnam('qinfer');farm=pwd.getpwnam('nobody')
    assert len({broker_user.pw_uid,infer_user.pw_uid,farm.pw_uid})==3
    forwarding=Path('/proc/sys/net/ipv4/ip_forward');old_forward=forwarding.read_text()
    hosts=Path('/etc/hosts');old_hosts=hosts.read_bytes()
    ca_path=Path(ssl.get_default_verify_paths().openssl_cafile)
    ca_path.parent.mkdir(parents=True,exist_ok=True)
    old_ca=ca_path.read_bytes() if ca_path.exists() else None
    processes=[];sockets=[]
    def run(*cmd, **kw):
        try:return subprocess.run(cmd,check=True,capture_output=True,text=True,**kw).stdout
        except subprocess.CalledProcessError as exc:
            print('COMMAND FAILED:',exc.cmd[-1][:300] if exc.cmd else exc.cmd,'\nSTDOUT:',exc.stdout,'\nSTDERR:',exc.stderr,file=sys.stderr);raise
    def wait(test, seconds=20):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            if test():return
            for process in processes:
                if process.poll() is not None:raise AssertionError('service died; inspect service log')
            time.sleep(.1)
        raise AssertionError('startup deadline')
    with tempfile.TemporaryDirectory(prefix='broker-smoke-',dir='/run') as tmp:
        base=Path(tmp);base.chmod(0o755)
        private=base/'private';private.mkdir();private.chmod(0o750);os.chown(private,0,broker_user.pw_gid)
        state=base/'state';state.mkdir();state.chmod(0o700);os.chown(state,broker_user.pw_uid,broker_user.pw_gid)
        def file(name,data,secret=False):
            path=(private if secret else base)/name
            path.write_bytes(data if isinstance(data,bytes) else canonical(data))
            path.chmod(0o440 if secret else 0o444)
            if secret:os.chown(path,0,broker_user.pw_gid)
            return str(path)
        ca_key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        now=datetime.now(timezone.utc)
        ca_name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Qwenomatic test CA')])
        ca_cert=(x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True,path_length=0),critical=True).sign(ca_key,hashes.SHA256()))
        ca=file('ca.pem',ca_cert.public_bytes(serialization.Encoding.PEM))
        def cert(name,ip=None,dns=None,secret=False):
            key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
            builder=(x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,name)]))
                .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=1)))
            if ip:builder=builder.add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(ip))]),critical=False)
            if dns:builder=builder.add_extension(x509.SubjectAlternativeName([x509.DNSName(dns)]),critical=False)
            value=builder.sign(ca_key,hashes.SHA256())
            return (file(name+'.pem',value.public_bytes(serialization.Encoding.PEM)),
                    file(name+'.key',key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()),secret),
                    hashlib.sha256(value.public_bytes(serialization.Encoding.DER)).hexdigest())
        broker_cert,broker_key,_=cert('broker',ip='10.204.1.2',secret=True)
        client_cert,client_key,client_fp=cert('farm-session')
        provider_cert,provider_key,_=cert('provider',dns='provider.test')
        signing=ed25519.Ed25519PrivateKey.generate()
        signing_path=file('audit.key',signing.private_bytes_raw(),True)
        token_path=file('token.json',{'token':'CONTROLLED_TEST_SECRET'},True)
        bounds=dict(requests=4,messages=4,purchases=0,bytes=20000,spend_cents=0,concurrency=2,period_seconds=3600)
        policy=dict(version='smoke-v1',operator='test-owner',approval_reference='controlled-kernel-test',
            farms={'farm':dict(limits=bounds,certificates=[client_fp])},
            services={'text':dict(kind='fixed_json',endpoint='https://provider.test/receive',account='test',credential_file=token_path,
                                 payee=None,hard_cap_cents=0,limits=bounds,farms=['farm'])})
        broker_cfg=file('broker.json',dict(policy=policy,signing_key=signing_path,tls_cert=broker_cert,tls_key=broker_key,client_ca=ca,
            listen=['10.204.1.2',9443],state_dir=str(state),slots=4,
            inference=dict(endpoint='http://10.204.2.2:11434/v1/chat/completions',model='fixed',max_tokens=64,max_messages=4,
                max_content_bytes=1024,requests_per_period=20,period_seconds=3600,concurrency=2)),True)
        # Fixed root-owned executable is a controlled stand-in for Ollama serve.
        inference=base/'inference'
        inference.write_text('#!'+a.python+'\n'+'''import http.server,json,os
class Handler(http.server.BaseHTTPRequestHandler):
 def do_POST(self):
  if self.path!='/v1/chat/completions': self.send_error(403);return
  body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  assert body['model']=='fixed'
  result=json.dumps({'choices':[{'message':{'content':'{"action":"wait"}'}}],'usage':{'prompt_tokens':1,'completion_tokens':1}}).encode()
  self.send_response(200);self.send_header('Content-Length',str(len(result)));self.end_headers();self.wfile.write(result)
 def log_message(self,*a):pass
host,port=os.environ['OLLAMA_HOST'].split(':')
http.server.ThreadingHTTPServer((host,int(port)),Handler).serve_forever()
''');inference.chmod(0o555)
        infer_cfg=file('inference.json',dict(listen=['10.204.2.2',11434],executable=str(inference)))
        deploy_broker=file('deploy-broker.json',dict(service=broker_cfg,subnet='10.204.1.0/30',peers=['10.204.1.1']))
        deploy_infer=file('deploy-inference.json',dict(service=infer_cfg,subnet='10.204.2.0/30',peers=['10.204.2.1']))
        logs=[]
        def launch(role,cfg,user):
            log=open(base/(role+'.log'),'w+');logs.append(log)
            command=['/usr/bin/python3','-I','-S',str(root/'deploy/service_launch.py'),'--role',role,'--config',cfg,
                     '--user',user,'--farm-user','nobody','--python',a.python]
            process=subprocess.Popen(command,stdout=log,stderr=log);processes.append(process)
            return process
        calls=[]
        try:
            run('ip','link','set','lo','up')
            forwarding.write_text('1\n');hosts.write_bytes(old_hosts+b'\n93.184.216.34 provider.test\n')
            ca_path.write_bytes((old_ca or b'')+ca_cert.public_bytes(serialization.Encoding.PEM))
            run('ip','addr','add','93.184.216.34/32','dev','lo')
            run('ip','addr','add','169.254.169.254/32','dev','lo')
            provider=socket.socket();provider.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);provider.bind(('93.184.216.34',443));provider.listen(4);sockets.append(provider)
            context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(provider_cert,provider_key)
            def serve():
                while True:
                    try:raw,_=provider.accept()
                    except OSError:return
                    try:
                        with context.wrap_socket(raw,server_side=True) as conn:
                            from broker.server import read_http
                            request=read_http(conn);calls.append(request)
                            assert b'POST /receive ' in request and b'Host: provider.test' in request and b'CONTROLLED_TEST_SECRET' in request
                            conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n')
                    except Exception as exc: calls.append(repr(exc))
            threading.Thread(target=serve,daemon=True).start()
            # Independent live positive control for the fixed TLS upstream.
            ctx=ssl.create_default_context(cafile=ca)
            with ctx.wrap_socket(socket.create_connection(('93.184.216.34',443)),server_hostname='provider.test') as conn:
                conn.sendall(b'POST /receive HTTP/1.1\r\nHost: provider.test\r\nContent-Length: 2\r\nAuthorization: Bearer CONTROLLED_TEST_SECRET\r\n\r\n{}');assert b'200 OK' in conn.recv(512)
            calls.clear()
            launch('inference',deploy_infer,'qinfer')
            wait(lambda:Path('/run/qwenomatic-service-inference.json').exists() and json.loads(Path('/run/qwenomatic-service-inference.json').read_text()).get('evidence'))
            launch('broker',deploy_broker,'qbroker')
            wait(lambda:(state/'operator.sock').exists())
            # Every namespace has a positive control before the launcher checks
            # the very same live listener is kernel-rejected as the service UID.
            def evidence(role):return json.loads(Path(json.loads(Path('/run/qwenomatic-service-'+role+'.json').read_text())['evidence']).read_text())
            def prefix(role,user):
                ns=json.loads(Path('/run/qwenomatic-service-'+role+'.json').read_text())['namespace'];u=pwd.getpwnam(user)
                return ['ip','netns','exec',ns,'setpriv','--reuid',str(u.pw_uid),'--regid',str(u.pw_gid),'--clear-groups','--bounding-set=-all','--inh-caps=-all','--ambient-caps=-all','--no-new-privs']
            def kernel_rejection(role,user,destination):
                code='import sys;sys.path.insert(0,'+repr(str(root))+');from supervisor.safety.boundary import NetworkBoundary;p=object.__new__(NetworkBoundary);p.evidence={"namespace":__import__("os").readlink("/proc/self/ns/net"),"canary":'+repr(list(destination))+'};p.check();print("kernel rejected")'
                result=run(*(prefix(role,user)+[a.python,'-I','-c',code]));assert 'kernel rejected' in result
            # Metadata control runs on a real listener; allow it as ROOT only for
            # this fixture's positive probe, then remove it before the UID probe.
            metadata=socket.socket();metadata.bind(('169.254.169.254',0));metadata.listen(8);sockets.append(metadata)
            for role,user in [('broker','qbroker'),('inference','qinfer')]:
                e=evidence(role);kernel_rejection(role,user,e['canary'])
                ns=json.loads(Path('/run/qwenomatic-service-'+role+'.json').read_text())['namespace']
                port=metadata.getsockname()[1]
                run('ip','netns','exec',ns,'nft','insert','rule','inet','qwenomatic','output','ip','daddr','169.254.169.254','tcp','dport',str(port),'accept','comment','"test-positive"')
                run('ip','netns','exec',ns,a.python,'-I','-S','-c',f'import socket;s=socket.create_connection(("169.254.169.254",{port}),2);s.close()')
                rules=run('ip','netns','exec',ns,'nft','-a','list','chain','inet','qwenomatic','output')
                handle=next(line.rsplit('handle ',1)[1].strip() for line in rules.splitlines() if 'test-positive' in line)
                run('ip','netns','exec',ns,'nft','delete','rule','inet','qwenomatic','output','handle',handle)
                kernel_rejection(role,user,metadata.getsockname())
            # The inference namespace cannot reach even the live approved TLS
            # provider: serving inference never gives it outbound access.
            kernel_rejection('inference','qinfer',('93.184.216.34',443))
            for path in [token_path,signing_path,broker_cfg]:
                assert subprocess.run(['setpriv','--reuid',str(farm.pw_uid),'--regid',str(farm.pw_gid),'--clear-groups','/usr/bin/test','-r',path]).returncode!=0
                assert subprocess.run(prefix('broker','qbroker')+['/usr/bin/test','-r',path]).returncode==0
            transport=dict(host='10.204.1.2',port=9443,ca_file=ca,client_cert=client_cert,client_key=client_key,
                           audit_public_key=signing.public_key().public_bytes_raw().hex())
            # A farm fixture namespace uses the production firewall, mandatory
            # evidence and mTLS client, with only one broker address permitted.
            farm_ns='qwenomatic-broker-smoke';run('ip','netns','add',farm_ns)
            run('ip','link','add','qbf-host','type','veth','peer','name','qbf-child');run('ip','link','set','qbf-child','netns',farm_ns)
            run('ip','addr','add','10.204.3.1/30','dev','qbf-host');run('ip','link','set','qbf-host','up')
            run('ip','netns','exec',farm_ns,'ip','addr','add','10.204.3.2/30','dev','qbf-child');run('ip','netns','exec',farm_ns,'ip','link','set','qbf-child','up')
            run('ip','netns','exec',farm_ns,'ip','link','set','lo','up');run('ip','netns','exec',farm_ns,'ip','route','add','default','via','10.204.3.1')
            from deploy.launch import rules
            run('ip','netns','exec',farm_ns,'nft','-f','-',input=rules({('10.204.1.2',9443)},'10.204.3.2'))
            run('nft','-f','-',input='table ip qbf_nat { chain nat { type nat hook postrouting priority srcnat; policy accept; ip saddr 10.204.3.2 masquerade; }; }')
            farm_prefix=['ip','netns','exec',farm_ns,'setpriv','--reuid',str(farm.pw_uid),'--regid',str(farm.pw_gid),'--clear-groups','--bounding-set=-all','--inh-caps=-all','--ambient-caps=-all','--no-new-privs',a.python,'-I','-c']
            def farm_code(code):return run(*(farm_prefix+['import sys;sys.path.insert(0,'+repr(str(root))+');'+code]))
            # Positive broker mTLS/chat path, with management requests rejected.
            code=f'''import http.client,ssl,json
c=ssl.create_default_context(cafile={ca!r});c.load_cert_chain({client_cert!r},{client_key!r})
body={{'model':'fixed','messages':[{{'role':'user','content':'hello'}}],'max_tokens':32,'temperature':0}}
for path,status in [('/v1/chat/completions',200),('/api/pull',403)]:
 h=http.client.HTTPSConnection('10.204.1.2',9443,context=c,timeout=5);h.request('POST',path,json.dumps(body),{{'Content-Type':'application/json'}});r=h.getresponse();assert r.status==status,(r.status,r.read());r.read();h.close()
'''
            farm_code(code)
            # Production farm launcher and manifest-v2 startup, not just the
            # standalone client fixture. Inference health uses the same proxy.
            import yaml
            config = base/'farm-config';shutil.copytree(root/'config', config)
            fc = yaml.safe_load((config/'farm.yaml').read_text())
            fc['inference']['backend'] = 'openai_compatible'
            fc['inference']['openai_compatible'].update(base_url='https://10.204.1.2:9443/v1',model='fixed')
            (config/'farm.yaml').write_text(yaml.safe_dump(fc))
            data = base/'farm-data';data.mkdir();os.chown(data,farm.pw_uid,farm.pw_gid)
            manifest = file('farm-manifest.json',dict(version=2,operator='owner',approval_reference='controlled-v2-startup',
                model_url='https://10.204.1.2:9443/v1',adapters=[],broker=transport))
            run('/usr/bin/python3','-I','-S',str(root/'deploy/launch.py'),'--manifest',manifest,'--user','nobody',
                '--python',a.python,'--config-dir',str(config),'--data-dir',str(data),'--operation','init')
            from storage.events import EventStore,EventType
            ledger=EventStore(data/'ledger.sqlite3',read_only=True)
            try:
                assert ledger.verify_chain()==(True,None)
                assert any(e.type==EventType.NETWORK_BARRIER_VERIFIED for e in ledger.iter_events())
            finally:ledger.close()
            farm_code('from runtime.inference.openai_compat import OpenAICompatibleBackend;assert OpenAICompatibleBackend('+repr(dict(fc['inference']['openai_compatible'],broker_transport=transport))+').health().ok')
            # Actual live provider and inference endpoints fail from farm UID.
            for target in [('93.184.216.34',443),('10.204.2.2',11434),metadata.getsockname()]:
                farm_code('from supervisor.safety.boundary import NetworkBoundary;p=object.__new__(NetworkBoundary);p.evidence={"namespace":__import__("os").readlink("/proc/self/ns/net"),"canary":'+repr(list(target))+'};p.check()')
            r=dict(service='text',args={'text':'approved fixture'},invocation_id='single-action')
            clientcode='from broker.client import Client;from broker.protocol import Rejected;c=Client('+repr(transport)+');'
            farm_code(clientcode+'\ntry:c.submit("text",{"text":"approved fixture"},"single-action")\nexcept Rejected:pass\nelse:raise AssertionError("missing approval allowed")')
            assert not calls
            operator_command=['/usr/bin/python3','-I','-S',str(root/'deploy/operator.py'),'--config',broker_cfg,'--operator','owner']
            request_file=file('operator-request.json',r)
            receipt=json.loads(run(*(operator_command+['grant','--farm','farm','--request-file',request_file,'--ttl','60'])))
            assert receipt['record']['kind']=='operator_grant'
            farm_code(clientcode+'assert c.submit("text",{"text":"approved fixture"},"single-action")[0]')
            assert len(calls)==1
            farm_code(clientcode+'\ntry:c.submit("text",{"text":"approved fixture"},"single-action")\nexcept Rejected:pass\nelse:raise AssertionError("replay allowed")')
            assert len(calls)==1
            assert json.loads(run(*(operator_command+['halt'])))['record']['kind']=='halted'
            print('PHASE1_KERNEL_SMOKE_PASS: mTLS positive, independent grant, replay blocked; farm direct egress blocked; broker LAN/metadata blocked; inference egress blocked; credentials unreadable',flush=True)
        finally:
            for process in reversed(processes):
                if process.poll() is None:process.terminate()
                try:process.wait(timeout=10)
                except subprocess.TimeoutExpired:process.kill();process.wait()
            for role in ['broker','inference']:
                subprocess.run(['/usr/bin/python3','-I','-S',str(root/'deploy/service_launch.py'),'--role',role,'--cleanup'],capture_output=True)
            for sock in sockets:sock.close()
            for cmd in [('ip','netns','del','qwenomatic-broker-smoke'),('ip','link','del','qbf-host'),('nft','delete','table','ip','qbf_nat'),
                        ('ip','addr','del','93.184.216.34/32','dev','lo'),('ip','addr','del','169.254.169.254/32','dev','lo')]:subprocess.run(cmd,capture_output=True)
            forwarding.write_text(old_forward);hosts.write_bytes(old_hosts)
            if old_ca is None:ca_path.unlink(missing_ok=True)
            else:ca_path.write_bytes(old_ca)
            for log in logs:
                log.flush();log.seek(0);print(log.read(),flush=True);log.close()


if __name__=='__main__':main()
