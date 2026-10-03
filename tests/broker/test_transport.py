import json
import socket
import subprocess
import sys
import threading
from pathlib import Path
import pytest
from broker.network import public_ip, resolve_public, service_url, transmit
from broker.protocol import Rejected, decode
from broker.server import Connections, accept_with_capacity, parse_isolated
from broker.inference import InferenceProxy


@pytest.mark.parametrize('address', ['127.0.0.1','10.0.0.1','192.168.1.1','172.16.1.1','169.254.169.254',
 '169.254.1.1','0.0.0.0','224.0.0.1','::1','::ffff:93.184.216.34','fd00:ec2::254','100.64.0.1'])
def test_nonpublic_routing_rejected(address):
    with pytest.raises(Rejected): public_ip(address)


def test_dns_resolution_requests_ipv4_only(monkeypatch):
    seen = {}
    def fake(*a, **kw):
        seen.update(kw)
        return [(2,1,6,'',('93.184.216.34',443))]
    monkeypatch.setattr(socket, 'getaddrinfo', fake)
    assert resolve_public('dual-stack.example.com') == ['93.184.216.34']
    assert seen['family'] == socket.AF_INET


def test_dns_mixed_or_rebound_answer_refuses(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [(2,1,6,'',('93.184.216.34',443)),(2,1,6,'',('127.0.0.1',443))])
    with pytest.raises(Rejected): resolve_public('example.com')
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [(2,1,6,'',('93.184.216.34',443))])
    with pytest.raises(Rejected, match='DNS changed'):
        transmit({'endpoint':'https://example.com/send'}, {'text':'x'}, 'id', 'never-disclosed', Connections(), {'8.8.8.8'})


@pytest.mark.parametrize('endpoint', ['http://example.com/send','https://user:pass@example.com/send',
 'https://example.com/send?url=x','https://example.com:444/send','https://169.254.169.254/send',
 'https://example.com/%2fadmin','https://example.com/../admin','https://example.com//admin'])
def test_ambiguous_or_unapproved_endpoints(endpoint):
    with pytest.raises(Rejected): service_url(endpoint)


def http(body=b'{"x":1}', extra=b'', host=b'broker:443'):
    return b'POST /v1/execute HTTP/1.1\r\nHost: '+host+b'\r\nContent-Type: application/json\r\nContent-Length: '+str(len(body)).encode()+b'\r\n'+extra+b'\r\n'+body


@pytest.mark.skipif(sys.platform != "linux", reason="requires actual Linux seccomp")
def test_isolated_parser_positive():
    assert parse_isolated(http(), 'broker:443')['body'] == {'x':1}


@pytest.mark.parametrize('raw', [http(extra=b'Transfer-Encoding: chunked\r\n'),
 http(extra=b'Content-Length: 7\r\n'),http(host=b'evil:443'),http(b'{"x":1,"x":2}'),
 http(b'{"x":NaN}'),http(b'{"x":1e999}'),http()+http(),
 http().replace(b'/v1/execute',b'/api/pull'),http(extra=b'X-Command: bash\r\n')])
@pytest.mark.skipif(sys.platform != "linux", reason="requires actual Linux seccomp")
def test_smuggling_splitting_and_data_refused(raw):
    with pytest.raises(Rejected): parse_isolated(raw,'broker:443')


@pytest.mark.skipif(sys.platform != "linux", reason="requires actual Linux seccomp")
def test_parser_kernel_sandbox_denies_files_sockets_exec():
    root = str(Path(__file__).resolve().parents[2])
    code = f'''import sys;sys.path.insert(0,{root!r})
import socket,os
from broker.parser import sandbox
sandbox()
for action in (lambda:open('/etc/passwd').read(), lambda:socket.socket(), lambda:os.fork(), lambda:os.execv('/bin/true',['true'])):
    try: action()
    except PermissionError: pass
    else: raise AssertionError('sandbox bypass')
print('blocked')
'''
    result = subprocess.run([sys.executable,'-I','-S','-c',code],capture_output=True,text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'blocked'


def test_kill_switch_closes_real_inflight_socket():
    first, second = socket.socketpair()
    tracking = Connections(); tracking.add(first); tracking.stop()
    assert second.recv(1) == b''
    with pytest.raises(Rejected): tracking.add(second)
    assert first.fileno() == second.fileno() == -1


def test_connection_admission_waits_for_capacity_before_accepting():
    """A just-finished health call must not make the following burst get reset."""
    waiting = threading.Event()
    gate = threading.Event()
    accepted = threading.Event()

    class Admission:
        def acquire(self):
            waiting.set()
            assert gate.wait(1)
            return True

        def release(self):
            pass

    class Listener:
        def accept(self):
            accepted.set()
            return object(), ('peer', 1)

    result = []
    worker = threading.Thread(
        target=lambda: result.append(accept_with_capacity(Listener(), Admission())),
        daemon=True,
    )
    worker.start()
    assert waiting.wait(1)
    assert not accepted.is_set()  # old accept-then-try-acquire ordering fails here
    gate.set()
    assert accepted.wait(1)
    worker.join(1)
    assert not worker.is_alive()
    assert result and result[0][1] == ('peer', 1)


def test_connection_admission_releases_capacity_if_accept_fails():
    admission = threading.BoundedSemaphore(1)

    class BrokenListener:
        def accept(self):
            raise OSError('listener failed')

    with pytest.raises(OSError, match='listener failed'):
        accept_with_capacity(BrokenListener(), admission)
    assert admission.acquire(blocking=False)  # helper restored the slot
    admission.release()


def inference():
    return InferenceProxy(dict(endpoint='http://10.204.2.2:11434/v1/chat/completions', model='fixed', max_tokens=64,
                              max_messages=4,max_content_bytes=128, requests_per_period=4,period_seconds=3600,concurrency=1))


def test_inference_timeout_is_bounded_and_explicit():
    cfg = dict(inference().config, timeout_seconds=120)
    assert InferenceProxy(cfg).timeout_seconds == 120
    for value in (9, 181, True, '120'):
        with pytest.raises(Rejected, match='bounded inference timeout'):
            InferenceProxy(dict(cfg, timeout_seconds=value))


def chat():
    return dict(model='fixed',messages=[dict(role='user',content='hello')],max_tokens=32,temperature=0)


def test_fixed_chat_only():
    inference().validate(chat())
    for field,value in [('stream',True),('tools',[]),('url','http://evil.test'),('model','unapproved'),('max_tokens',65)]:
        with pytest.raises(Rejected): inference().validate({**chat(),field:value})
    for content in ([{'type':'image_url','image_url':{'url':'http://evil'}}],):
        value=chat();value['messages'][0]['content']=content
        with pytest.raises(Rejected): inference().validate(value)


def test_only_the_thinking_switch_template_option_is_allowed():
    for enabled in (True, False):
        inference().validate({**chat(), 'chat_template_kwargs': {'enable_thinking': enabled}})
    for bad in ({'enable_thinking': 1}, {'enable_thinking': 'yes'}, {'enable_thinking': True, 'other': 1},
                {'system': 'x'}, {}, 'enable_thinking', None, ['enable_thinking']):
        with pytest.raises(Rejected): inference().validate({**chat(), 'chat_template_kwargs': bad})


def test_provider_redirect_is_never_followed(monkeypatch):
    calls = []
    class StatusSocket:
        def __init__(self): self.data = b'HTTP/1.1 302 Found\r\n'
        def recv(self, count):
            out, self.data = self.data[:count], self.data[count:]
            return out
    class Connection:
        def __init__(self, host, address, tracking): self.sock = StatusSocket()
        def request(self, method, path, **kwargs): calls.append((method, path, kwargs))
        def close(self): self.sock = None
    monkeypatch.setattr('broker.network.resolve_public', lambda host: ['93.184.216.34'])
    monkeypatch.setattr('broker.network.PinnedHTTPS', Connection)
    with pytest.raises(Rejected, match='redirect prohibited'):
        transmit({'endpoint':'https://example.com/send'}, {'text':'approved'}, 'invocation', 'fixture-token',
                 Connections(), {'93.184.216.34'})
    assert len(calls) == 1
    assert calls[0][0:2] == ('POST', '/send')
    assert calls[0][2]['headers']['Host'] == 'example.com'
    assert calls[0][2]['body'] == b'{"text":"approved"}'
