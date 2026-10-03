"""mTLS broker service; operator authority arrives only over protected local IPC."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import ssl
import struct
import subprocess
import sys
import threading
from .authority import Authority
from .network import transmit
from .protocol import Rejected, canonical, decode, digest, fields, receive, send


class Connections:
    def __init__(self):
        self.lock = threading.RLock()
        self.sockets = set()
        self.halted = False

    def add(self, sock):
        with self.lock:
            if self.halted:
                sock.close()
                raise Rejected("halted")
            self.sockets.add(sock)

    def replace(self, old, new):
        with self.lock:
            self.remove(old)
            self.add(new)

    def remove(self, sock):
        with self.lock:
            self.sockets.discard(sock)

    def stop(self):
        with self.lock:
            self.halted = True
            for sock in list(self.sockets):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()
            self.sockets.clear()


def secret(path):
    # Broker secrets require a separate broker-owned private directory; farm
    # access is checked separately at installation using an actual farm UID.
    p = Path(path)
    for item in (p, *p.parents):
        st = item.lstat()
        if item.is_symlink() or st.st_uid not in (0, os.geteuid()) or st.st_mode & 0o022:
            raise Rejected("unsafe broker file")
    st = p.stat()
    if st.st_uid != 0 or st.st_mode & 0o007 or (st.st_mode & 0o070 and st.st_gid != os.getegid()):
        raise Rejected("broker secret must be root-owned and private to its service group")
    return p.read_bytes()


def parse_isolated(raw, host):
    root = str(Path(__file__).resolve().parents[1])
    bootstrap = "import sys;sys.path.insert(0," + repr(root) + ");from broker.parser import main;main()"
    result = subprocess.run([sys.executable, "-I", "-S", "-c", bootstrap, host], input=raw,
                            capture_output=True, timeout=3, env={"PATH": "/usr/bin:/bin"}, cwd="/")
    if result.returncode:
        raise Rejected("request parser refusal")
    return decode(result.stdout)


def read_http(sock):
    # Bounded framing only; interpretation is in a disposable parser process.
    head = bytearray()
    while not head.endswith(b"\r\n\r\n"):
        if len(head) >= 8192:
            raise Rejected("header limit")
        chunk = sock.recv(1)
        if not chunk:
            raise Rejected("truncated headers")
        head.extend(chunk)
    matches = [line for line in bytes(head).split(b"\r\n") if line.lower().startswith(b"content-length:")]
    if len(matches) != 1:
        raise Rejected("framing unavailable")
    size = matches[0].split(b":", 1)[1].strip()
    if not size.isdigit() or len(size) > 6 or not 1 <= int(size) <= 65536:
        raise Rejected("body limit")
    body = bytearray()
    while len(body) < int(size):
        chunk = sock.recv(int(size) - len(body))
        if not chunk:
            raise Rejected("truncated body")
        body.extend(chunk)
    # Exactly one request per connection; trailing bytes are never dispatched.
    return bytes(head) + bytes(body)


def terminate(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    sock.close()


def response(sock, value, status=200):
    body = canonical(value)
    sock.sendall((f"HTTP/1.1 {status} {'OK' if status == 200 else 'Forbidden'}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body)


class Broker:
    def __init__(self, authority, credentials, ips, inference=None):
        self.authority = authority
        self.credentials = credentials
        self.ips = ips
        self.inference = inference
        self.connections = Connections()
        if authority.db.execute("SELECT 1 FROM settings WHERE name='halted'").fetchone():
            self.connections.stop()

    def execute(self, farm, request):
        spec, body, scopes = self.authority.reserve(farm, request)
        accepted = False
        try:
            accepted = transmit(spec, body, request["invocation_id"], self.credentials[request["service"]], self.connections, self.ips)
        except Exception:
            pass  # no retry and no provider details in response
        return self.authority.finish(farm, request["invocation_id"], scopes, accepted)

    def operator(self, request):
        action = request.get("action")
        if action == "grant":
            fields(request, ("action", "farm", "request", "expires", "operator"))
            return self.authority.grant(request["farm"], request["request"], request["expires"], request["operator"])
        if action == "revoke":
            fields(request, ("action", "farm", "invocation_id", "operator"))
            return self.authority.revoke(request["farm"], request["invocation_id"], request["operator"])
        if action == "halt":
            fields(request, ("action", "operator"))
            receipt = self.authority.halt(request["operator"])
            self.connections.stop()
            return receipt
        raise Rejected("unknown operator operation")


def run_server(broker, tls, address, admin_path, slots, expected_host):
    if os.geteuid() == 0:
        raise Rejected("broker must run as a distinct unprivileged service")
    admin = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    if os.path.lexists(admin_path):
        import stat
        previous = os.lstat(admin_path)
        if previous.st_uid != os.geteuid() or not stat.S_ISSOCK(previous.st_mode):
            raise Rejected("unsafe operator socket")
        os.unlink(admin_path)
    admin.bind(admin_path)
    os.chmod(admin_path, 0o600)
    admin.listen(4)
    def operator_loop():
        while True:
            conn, _ = admin.accept()
            with conn:
                conn.settimeout(5)
                try:
                    _, uid, _ = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    if uid != 0:
                        raise Rejected("operator IPC requires root")
                    send(conn, broker.operator(receive(conn)))
                except Exception:
                    send(conn, {"status": "refused"})
    threading.Thread(target=operator_loop, daemon=True).start()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(address)
    listener.listen(slots)
    admission = threading.BoundedSemaphore(slots)
    def handle(raw):
        process = None
        operation = "unknown"
        def expire():
            terminate(raw)
            if process is not None:
                process.kill()
        timer = threading.Timer(12, expire)
        timer.start()
        try:
            broker.connections.add(raw)
            root = str(Path(__file__).resolve().parents[1])
            bootstrap = "import sys;sys.path.insert(0," + repr(root) + ");from broker.frontend import main;main()"
            config = dict(tls, expected_host=expected_host,
                          inference_timeout=broker.inference.timeout_seconds if broker.inference else 10)
            process = subprocess.Popen([sys.executable, "-I", "-c", bootstrap, json.dumps(config), str(raw.fileno())],
                                       pass_fds=(raw.fileno(),), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, env={"PATH":"/usr/bin:/bin"}, cwd="/")
            parsed = decode(process.stdout.readline(65537))
            fields(parsed, ("fingerprint", "path", "body"))
            operation = parsed["path"]
            farm = broker.authority.authenticate(parsed["fingerprint"])
            if parsed["path"] == "/v1/execute":
                value = broker.execute(farm, parsed["body"])
            elif parsed["path"] == "/v1/chat/completions" and broker.inference is not None:
                timer.cancel()
                timer = threading.Timer(broker.inference.timeout_seconds + 10, expire)
                timer.start()
                value = broker.inference.generate(farm, parsed["body"], broker)
            else:
                raise Rejected("inference unavailable")
            process.stdin.write(canonical({"status":200,"body":value}) + b"\n")
            process.stdin.flush()
            process.wait(timeout=5)
        except Exception as exc:
            if operation == "/v1/chat/completions":
                # Operator-only stderr: no prompts, response bodies or keys.
                reason = str(exc) if isinstance(exc, Rejected) else type(exc).__name__
                print(f"broker chat refused: {reason}", file=sys.stderr, flush=True)
            if process is not None and process.poll() is None:
                try:
                    process.stdin.write(canonical({"status":403,"body":{"status":"refused"}}) + b"\n")
                    process.stdin.flush()
                    process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        finally:
            timer.cancel()
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait()
                process.stdin.close();process.stdout.close()
            broker.connections.remove(raw)
            terminate(raw)
            admission.release()
    while True:
        raw, _ = listener.accept()
        if not admission.acquire(blocking=False):
            raw.close()
            continue
        threading.Thread(target=handle, args=(raw,), daemon=True).start()


def main():
    from supervisor.safety.boundary import protected_json
    from .deployment import ServiceBoundary
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    cfg = protected_json(Path(args.config))
    fields(cfg, ("policy", "signing_key", "tls_cert", "tls_key", "client_ca", "listen", "state_dir", "slots", "inference"))
    boundary = ServiceBoundary("broker")
    if boundary.evidence["config_hash"] != digest(cfg):
        raise Rejected("deployment/config mismatch")
    state = Path(cfg["state_dir"])
    for directory in (state, *state.parents):
        st = directory.lstat()
        if directory.is_symlink() or st.st_uid not in (0, os.geteuid()) or st.st_mode & 0o022:
            raise Rejected("unsafe state ancestry")
    if state.stat().st_uid != os.geteuid() or state.stat().st_mode & 0o077:
        raise Rejected("broker state requires a private service directory")
    authority = Authority(state / "authority.sqlite3", cfg["policy"], secret(cfg["signing_key"]))
    credentials = {}
    for name, spec in cfg["policy"]["services"].items():
        token = decode(secret(spec["credential_file"]))
        fields(token, ("token",))
        if not isinstance(token["token"], str) or not token["token"] or any(ord(c) < 33 or ord(c) > 126 for c in token["token"]):
            raise Rejected("invalid bearer credential")
        credentials[name] = token["token"]
    secret(cfg["tls_key"])
    from deploy.launch import protected_path
    protected_path(Path(cfg["tls_cert"]))
    protected_path(Path(cfg["client_ca"]))
    tls = {k: cfg[k] for k in ("client_ca", "tls_cert", "tls_key")}
    from .inference import InferenceProxy
    inference = InferenceProxy(cfg["inference"]) if cfg["inference"] is not None else None
    broker = Broker(authority, credentials, boundary.evidence["public_ips"], inference)
    run_server(broker, tls, tuple(cfg["listen"]), str(state / "operator.sock"), cfg["slots"], f"{cfg['listen'][0]}:{cfg['listen'][1]}")


if __name__ == "__main__":
    main()
