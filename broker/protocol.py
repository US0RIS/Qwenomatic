"""Bounded, canonical data protocol; no model-generated HTTP or code."""
import hashlib
import json
import re
import struct

MAX_FRAME = 65536


class Rejected(Exception):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise Rejected("duplicate field")
            result[key] = value
        return result
    def number(_):
        raise Rejected("nonfinite constant")
    if len(raw) > MAX_FRAME:
        raise Rejected("frame too large")
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=number)
        canonical(value)  # catches overflow-to-infinity numbers too
    except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
        raise Rejected("invalid data") from exc
    if not isinstance(value, dict):
        raise Rejected("object required")
    return value


def fields(value, expected):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise Rejected("unexpected fields")


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        raise Rejected("invalid identifier")
    return value


def receive(sock):
    def exact(count):
        out = bytearray()
        while len(out) < count:
            chunk = sock.recv(count - len(out))
            if not chunk:
                raise Rejected("truncated frame")
            out.extend(chunk)
        return bytes(out)
    size = struct.unpack("!I", exact(4))[0]
    if not 1 <= size <= MAX_FRAME:
        raise Rejected("invalid frame size")
    return decode(exact(size))


def send(sock, value):
    raw = canonical(value)
    if len(raw) > MAX_FRAME:
        raise Rejected("frame too large")
    sock.sendall(struct.pack("!I", len(raw)) + raw)
