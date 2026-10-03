"""Disposable HTTP/data parser. Executed without site loading or credentials."""
import ctypes
import errno
import resource
import sys
from .protocol import Rejected, canonical, decode


def parse(raw, expected_host):
    if len(raw) > 73728:
        raise Rejected("request too large")
    try:
        head, body = raw.split(b"\r\n\r\n", 1)
        if len(head) > 8192:
            raise Rejected("headers too large")
        lines = head.decode("ascii").split("\r\n")
        if lines[0] not in ("POST /v1/execute HTTP/1.1", "POST /v1/chat/completions HTTP/1.1"):
            raise Rejected("operation not exposed")
        headers = {}
        for line in lines[1:]:
            if ":" not in line or line.startswith((" ", "\t")):
                raise Rejected("invalid header")
            name, value = line.split(":", 1)
            name = name.lower()
            if name in headers or name not in {"host", "content-type", "content-length", "connection", "user-agent", "accept", "accept-encoding"}:
                raise Rejected("duplicate or unsupported header")
            value = value.strip()
            if any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise Rejected("invalid header value")
            headers[name] = value
        if headers.get("host") != expected_host or headers.get("content-type") != "application/json":
            raise Rejected("Host/content type mismatch")
        size = headers.get("content-length", "")
        if not size.isascii() or not size.isdecimal() or len(size) > 6 or int(size) != len(body):
            raise Rejected("invalid framing")
        return {"path": lines[0].split()[1], "body": decode(body)}
    except (ValueError, UnicodeError) as exc:
        raise Rejected("invalid HTTP") from exc


def sandbox(tls=False):
    """No file opens, sockets, process creation or exec after trusted imports.

    A fresh interpreter holds no broker credentials. libseccomp is mandatory;
    unsupported kernels/libraries refuse parsing rather than remove isolation.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0):  # PR_SET_NO_NEW_PRIVS
        raise Rejected("parser no_new_privs unavailable")
    lib = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    context = lib.seccomp_init(0x00050000 | errno.EPERM)
    if not context:
        raise Rejected("parser seccomp unavailable")
    try:
        calls = ("read", "write", "close", "fstat", "newfstatat", "lseek", "ioctl",
                     "brk", "mmap", "munmap", "mprotect", "mremap", "madvise",
                     "rt_sigaction", "rt_sigprocmask", "rt_sigreturn", "sigaltstack",
                     "futex", "clock_gettime", "getpid", "gettid", "getrandom",
                     "exit", "exit_group")
        if tls:
            calls += ("recvfrom", "sendto", "recvmsg", "sendmsg", "getsockopt", "setsockopt",
                      "getpeername", "getsockname", "fcntl", "poll", "ppoll", "select", "pselect6", "shutdown")
        for name in calls:
            number = lib.seccomp_syscall_resolve_name(name.encode())
            if number < 0 or lib.seccomp_rule_add(context, 0x7fff0000, number, 0):
                raise Rejected("parser seccomp rule unavailable")
        if lib.seccomp_load(context):
            raise Rejected("parser seccomp installation failed")
    finally:
        lib.seccomp_release(context)


def main():
    resource.setrlimit(resource.RLIMIT_CPU, (2, 2))
    resource.setrlimit(resource.RLIMIT_AS, (192 * 1024 * 1024, 192 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (16, 16))
    sandbox()
    raw = sys.stdin.buffer.read(73729)
    try:
        value = decode(raw) if sys.argv[1] == "--json" else parse(raw, sys.argv[1])
        sys.stdout.buffer.write(canonical(value))
    except Exception:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
