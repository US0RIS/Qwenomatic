"""Farm transport contains session credentials only, never provider credentials."""
import base64
import http.client
import ssl
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from .protocol import Rejected, canonical, decode, digest, fields


class Client:
    def __init__(self, config):
        self.config = config
        self.context = ssl.create_default_context(cafile=config["ca_file"])
        self.context.minimum_version = ssl.TLSVersion.TLSv1_3
        self.context.load_cert_chain(config["client_cert"], config["client_key"])
        self.key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(config["audit_public_key"]))

    def submit(self, service, args, invocation_id):
        connection = http.client.HTTPSConnection(self.config["host"], self.config["port"], timeout=12, context=self.context)
        request = {"service": service, "args": args, "invocation_id": invocation_id}
        try:
            connection.request("POST", "/v1/execute", canonical(request),
                               {"Host": f"{self.config['host']}:{self.config['port']}", "Content-Type": "application/json", "Connection": "close"})
            response = connection.getresponse()
            raw = response.read(65537)
            if response.status != 200 or len(raw) > 65536:
                raise Rejected("broker refused")
            receipt = decode(raw)
            fields(receipt, ("record", "signature"))
            self.key.verify(base64.b64decode(receipt["signature"], validate=True), canonical(receipt["record"]))
            payload = receipt["record"]["payload"]
            if receipt["record"]["author"] != "broker" or receipt["record"]["kind"] != "result" or payload["invocation_id"] != invocation_id or payload["request_digest"] != digest(request):
                raise Rejected("broker receipt mismatch")
            return payload["status"] == "accepted", receipt
        finally:
            connection.close()
