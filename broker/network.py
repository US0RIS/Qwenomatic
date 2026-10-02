"""Public IPv4-only, pinned TLS transport. Never accept request routing from farms."""
import http.client
import ipaddress
import re
import socket
import ssl
import threading
from urllib.parse import urlsplit
from .protocol import Rejected, canonical


def public_ip(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise Rejected("invalid address") from exc
    # Reject IPv6, mapped addresses and non-global special-use networks too.
    if address.version != 4 or not address.is_global or address.is_multicast:
        raise Rejected("nonpublic address")
    return str(address)


def service_url(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value):
        raise Rejected("invalid endpoint")
    u = urlsplit(value)
    if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment or u.port not in (None, 443):
        raise Rejected("fixed HTTPS endpoint required")
    host = u.hostname.encode("idna").decode("ascii")
    if not host or host.endswith(".") or "\\" in value or "%" in host:
        raise Rejected("invalid hostname")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or any(not part or not all(c.isalnum() or c == "-" for c in part) for part in host.split(".")):
            raise Rejected("invalid DNS name")
    else:
        public_ip(host)
    path = u.path or "/"
    if not path.startswith("/") or "%" in path or ".." in path or "//" in path:
        raise Rejected("ambiguous path")
    return host, path


def resolve_public(host):
    # Validate ALL returned addresses; never silently skip an unsafe answer.
    answers = socket.getaddrinfo(host, 443, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
    if not answers:
        raise Rejected("empty DNS answer")
    return sorted({public_ip(item[4][0]) for item in answers})


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, ip, connections):
        super().__init__(host, 443, timeout=5, context=ssl.create_default_context())
        self.ip = public_ip(ip)
        self.connections = connections

    def connect(self):
        # No second DNS lookup: the actual socket is pinned to the checked IP.
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.settimeout(5)
        self.connections.add(raw)
        try:
            raw.connect((self.ip, 443))
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
            self.connections.replace(raw, self.sock)
            if self.sock.getpeername()[0] != self.ip:
                raise Rejected("peer mismatch")
        except BaseException:
            self.connections.remove(raw)
            raw.close()
            raise


def transmit(spec, body, invocation_id, token, connections, installed_ips):
    host, path = service_url(spec["endpoint"])
    addresses = resolve_public(host)
    if not set(addresses).issubset(installed_ips):
        raise Rejected("DNS changed outside installed firewall; operator relaunch required")
    connection = PinnedHTTPS(host, addresses[0], connections)
    def expire():
        if connection.sock is not None:
            try:
                connection.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connections.remove(connection.sock)
            connection.close()
    timer = threading.Timer(10, expire)
    timer.start()
    try:
        connection.request("POST", path, body=canonical(body), headers={
            "Host": host, "Content-Type": "application/json", "Authorization": "Bearer " + token,
            "Idempotency-Key": invocation_id, "Connection": "close"})
        # Only the bounded status line is consumed. Provider headers/bodies are
        # never parsed or forwarded; redirects cannot create a second connection.
        line = bytearray()
        while not line.endswith(b"\r\n"):
            if len(line) >= 512:
                raise Rejected("provider status line limit")
            chunk = connection.sock.recv(1)
            if not chunk:
                raise Rejected("truncated provider status")
            line.extend(chunk)
        match = re.fullmatch(rb"HTTP/1\.[01] ([0-9]{3})(?: [\x20-\x7e]*)?\r\n", bytes(line))
        if not match:
            raise Rejected("invalid provider status")
        status = int(match[1])
        if 300 <= status < 400:
            raise Rejected("redirect prohibited")
        return 200 <= status < 300
    finally:
        timer.cancel()
        if connection.sock is not None:
            connections.remove(connection.sock)
        connection.close()
