"""Fresh disposable TLS/HTTP frontend, without provider credentials in memory.

Only its already accepted socket and stdin/stdout are usable after seccomp. It
cannot open provider files, create sockets, connect, fork or execute a program.
"""
import hashlib
import json
import resource
import socket
import ssl
import sys
from .parser import parse, sandbox
from .protocol import canonical, decode, fields
from .server import read_http, response


def main():
    cfg = json.loads(sys.argv[1])
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cfg['client_ca'])
    context.load_cert_chain(cfg['tls_cert'], cfg['tls_key'])
    raw = socket.socket(fileno=int(sys.argv[2]))
    raw.settimeout(5)
    resource.setrlimit(resource.RLIMIT_CPU, (2, 2))
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (16, 16))
    sandbox(tls=True)
    with context.wrap_socket(raw, server_side=True) as connection:
        try:
            parsed = parse(read_http(connection), cfg['expected_host'])
            value = {'fingerprint': hashlib.sha256(connection.getpeercert(binary_form=True)).hexdigest(),
                     'path': parsed['path'], 'body': parsed['body']}
            sys.stdout.buffer.write(canonical(value) + b'\n');sys.stdout.buffer.flush()
            reply = decode(sys.stdin.buffer.readline(65537))
            fields(reply, ('status','body'))
            response(connection, reply['body'], reply['status'])
        except Exception:
            response(connection, {'status':'refused'}, 403)


if __name__ == '__main__': main()
