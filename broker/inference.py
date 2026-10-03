"""Only a fixed chat operation reaches an inference server with blocked egress."""
import http.client
import math
import socket
import threading
from urllib.parse import urlsplit
from .protocol import Rejected, canonical, decode, fields


class InferenceProxy:
    def __init__(self, config):
        required = {"endpoint", "model", "max_tokens", "max_messages", "max_content_bytes", "requests_per_period", "period_seconds", "concurrency"}
        if not isinstance(config, dict) or not required.issubset(config) or set(config) - required - {"timeout_seconds"}:
            raise Rejected("invalid inference configuration")
        u = urlsplit(config["endpoint"])
        import ipaddress
        if u.scheme != "http" or u.username or u.password or u.query or u.fragment or u.path != "/v1/chat/completions":
            raise Rejected("fixed chat inference endpoint required")
        ipaddress.IPv4Address(u.hostname)
        if not u.port or any(type(config[k]) is not int or config[k] < 1 for k in required - {"endpoint", "model"}):
            raise Rejected("explicit inference limits required")
        if not isinstance(config["model"], str) or not config["model"]:
            raise Rejected("fixed model required")
        timeout = config.get("timeout_seconds", 10)
        if type(timeout) is not int or not 10 <= timeout <= 180:
            raise Rejected("bounded inference timeout required")
        self.config = config
        self.timeout_seconds = timeout
        self.host, self.port, self.path = u.hostname, u.port, u.path
        self.lock = threading.Lock()
        self.active = 0

    def validate(self, body):
        required = {"model", "messages", "max_tokens", "temperature"}
        if not isinstance(body, dict) or not required.issubset(body) or set(body) - required - {"response_format", "seed", "chat_template_kwargs"}:
            raise Rejected("inference schema")
        if "chat_template_kwargs" in body:
            kwargs = body["chat_template_kwargs"]
            if not isinstance(kwargs, dict) or set(kwargs) != {"enable_thinking"} or type(kwargs["enable_thinking"]) is not bool:
                raise Rejected("invalid chat template option")
        if body["model"] != self.config["model"] or type(body["max_tokens"]) is not int or not 1 <= body["max_tokens"] <= self.config["max_tokens"]:
            raise Rejected("model/token scope")
        t = body["temperature"]
        if isinstance(t, bool) or not isinstance(t, (float, int)) or not math.isfinite(t) or not 0 <= t <= 2:
            raise Rejected("invalid temperature")
        if "seed" in body and (type(body["seed"]) is not int or not 0 <= body["seed"] <= 2**31 - 1):
            raise Rejected("invalid seed")
        if "response_format" in body and body["response_format"] != {"type": "json_object"}:
            raise Rejected("invalid response format")
        messages = body["messages"]
        if not isinstance(messages, list) or not 1 <= len(messages) <= self.config["max_messages"]:
            raise Rejected("messages limit")
        for message in messages:
            fields(message, ("role", "content"))
            if message["role"] not in ("system", "user", "assistant") or not isinstance(message["content"], str):
                raise Rejected("plain text chat only; no remote images/tools")
        if sum(len(m["content"].encode()) for m in messages) > self.config["max_content_bytes"]:
            raise Rejected("content limit")

    def generate(self, farm, body, broker):
        self.validate(body)
        authority = broker.authority
        # Durable shared counter, not per-agent identity or an in-memory reset.
        with authority.lock, self.lock:
            if broker.connections.halted or self.active >= self.config["concurrency"]:
                raise Rejected("inference halted/busy")
            period = int(authority.checked_time()) // self.config["period_seconds"]
            scope = "inference:all"
            row = authority.db.execute("SELECT value FROM usage WHERE scope=? AND period=? AND dimension='requests'", (scope, period)).fetchone()
            if (row[0] if row else 0) >= self.config["requests_per_period"]:
                raise Rejected("inference rate limit")
            authority.db.execute("INSERT INTO usage VALUES(?,?,'requests',1) ON CONFLICT(scope,period,dimension) DO UPDATE SET value=value+1", (scope, period))
            authority._audit("inference_request", {"farm": farm, "content_hash": __import__('hashlib').sha256(canonical(body)).hexdigest()})
            self.active += 1
        connection = http.client.HTTPConnection(self.host, self.port, timeout=min(5, self.timeout_seconds))
        def expire():
            if connection.sock is not None:
                try:
                    connection.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                broker.connections.remove(connection.sock)
                connection.close()
        timer = threading.Timer(self.timeout_seconds, expire)
        try:
            timer.start()
            connection.connect()
            connection.sock.settimeout(self.timeout_seconds)
            broker.connections.add(connection.sock)
            connection.request("POST", self.path, body=canonical(body), headers={"Host": f"{self.host}:{self.port}", "Content-Type": "application/json", "Connection": "close"})
            response = connection.getresponse()
            if response.status != 200:
                raise Rejected("inference refused")
            raw = response.read(65537)
            if len(raw) > 65536:
                raise Rejected("inference output limit")
            from .server import parse_isolated
            value = parse_isolated(raw, "--json")
            if not isinstance(value.get("choices"), list) or not isinstance(value.get("usage"), dict):
                raise Rejected("inference response schema")
            # Return only data used by the farm; never upstream URLs/headers.
            if not isinstance(value["choices"], list) or len(value["choices"]) != 1:
                raise Rejected("inference choices")
            choice = value["choices"][0]
            if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict) or not isinstance(choice["message"].get("content"), str):
                raise Rejected("inference content")
            usage = value["usage"]
            if not isinstance(usage, dict) or any(type(usage.get(k)) is not int or usage[k] < 0 for k in ("prompt_tokens", "completion_tokens")):
                raise Rejected("inference usage")
            return {"choices": [{"message": {"content": choice["message"]["content"]}}],
                    "usage": {k: usage[k] for k in ("prompt_tokens", "completion_tokens")}}
        finally:
            timer.cancel()
            if connection.sock is not None:
                broker.connections.remove(connection.sock)
            connection.close()
            with self.lock:
                self.active -= 1
