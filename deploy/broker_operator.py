#!/usr/bin/env python3
"""Root-only broker grant/revoke/halt utility. This is never an agent tool."""
import argparse
import json
import os
from pathlib import Path
import socket
import stat
import struct
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from broker.protocol import Rejected, canonical, fields, identifier, receive, send
from supervisor.safety.boundary import protected_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, help='protected broker service JSON')
    parser.add_argument('--operator', required=True)
    sub = parser.add_subparsers(dest='action', required=True)
    grant = sub.add_parser('grant')
    grant.add_argument('--farm', required=True)
    grant.add_argument('--request-file', required=True, help='protected exact service/args/invocation_id JSON')
    grant.add_argument('--ttl', type=int, default=60)
    revoke = sub.add_parser('revoke')
    revoke.add_argument('--farm', required=True)
    revoke.add_argument('--invocation-id', required=True)
    sub.add_parser('halt')
    args = parser.parse_args()
    if os.geteuid() != 0 or not sys.flags.isolated or not sys.flags.no_site:
        raise Rejected('trusted system Python -I -S as root required')
    identifier(args.operator)
    config = protected_json(Path(args.config))
    state = Path(config['state_dir'])
    if not state.is_absolute():
        raise Rejected('absolute protected broker state required')
    st = state.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid == 0 or st.st_mode & 0o077:
        raise Rejected('private unprivileged broker state required')
    for parent in state.parents:
        p = parent.lstat()
        if not stat.S_ISDIR(p.st_mode) or p.st_uid != 0 or p.st_mode & 0o022:
            raise Rejected('unsafe state ancestry')
    admin = state/'operator.sock'
    ast = admin.lstat()
    if not stat.S_ISSOCK(ast.st_mode) or ast.st_uid != st.st_uid or stat.S_IMODE(ast.st_mode) != 0o600:
        raise Rejected('unsafe operator socket')
    request = {'action':args.action, 'operator':args.operator}
    if args.action == 'grant':
        if not 1 <= args.ttl <= 300:
            raise Rejected('TTL must be 1..300 seconds')
        identifier(args.farm)
        content = protected_json(Path(args.request_file))
        fields(content, ('service','args','invocation_id'))
        request.update(farm=args.farm,request=content,expires=time.time()+args.ttl)
    elif args.action == 'revoke':
        request.update(farm=identifier(args.farm),invocation_id=identifier(args.invocation_id))
    with socket.socket(socket.AF_UNIX) as conn:
        conn.settimeout(5)
        conn.connect(str(admin))
        _, uid, _ = struct.unpack('3i',conn.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
        if uid != st.st_uid:
            raise Rejected('broker peer identity mismatch')
        send(conn,request)
        receipt=receive(conn)
    if receipt.get('status') == 'refused':
        raise Rejected('broker refused operator request')
    fields(receipt, ('record','signature'))
    expected={'grant':'operator_grant','revoke':'revoked','halt':'halted'}[args.action]
    if receipt['record'].get('author') != 'broker' or receipt['record'].get('kind') != expected:
        raise Rejected('unexpected broker receipt')
    print(canonical(receipt).decode())


if __name__ == '__main__':
    main()
