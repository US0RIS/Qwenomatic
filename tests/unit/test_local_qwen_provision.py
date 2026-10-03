"""The local broker chain must verify under strict Python/OpenSSL rules."""
import shutil
import socket
import ssl
import subprocess
import threading

import pytest
from cryptography import x509

from scripts.provision_local_qwen import cert, make_ca


@pytest.mark.skipif(shutil.which('openssl') is None, reason='OpenSSL CLI unavailable')
def test_local_qwen_certificates_pass_strict_verification(tmp_path):
    ca_pem, ca_key, ca = make_ca()
    ca_path = tmp_path / 'ca.pem'
    ca_path.write_bytes(ca_pem)
    ski = ca.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value.digest
    assert ca.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value.key_identifier == ski
    for name, address in (('10.204.1.2', '10.204.1.2'), ('qwenomatic-farm', None)):
        pem, _, _ = cert(ca_key, ca, name, address)
        leaf = x509.load_pem_x509_certificate(pem)
        assert leaf.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value.key_identifier == ski
        path = tmp_path / f'{name}.pem'
        path.write_bytes(pem)
        result = subprocess.run(['openssl', 'verify', '-x509_strict', '-CAfile', str(ca_path), str(path)],
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


def test_python_mutual_tls_with_strict_verification(tmp_path):
    ca_pem, ca_key, ca = make_ca()
    (tmp_path / 'ca.pem').write_bytes(ca_pem)
    for name, address in (('broker', '10.204.1.2'), ('farm', None)):
        pem, key, _ = cert(ca_key, ca, address or 'qwenomatic-farm', address)
        (tmp_path / f'{name}.pem').write_bytes(pem)
        (tmp_path / f'{name}.key').write_bytes(key)
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(str(tmp_path / 'broker.pem'), str(tmp_path / 'broker.key'))
    server.load_verify_locations(cafile=str(tmp_path / 'ca.pem'))
    server.verify_mode = ssl.CERT_REQUIRED
    server.verify_flags |= ssl.VERIFY_X509_STRICT
    client = ssl.create_default_context(cafile=str(tmp_path / 'ca.pem'))
    client.load_cert_chain(str(tmp_path / 'farm.pem'), str(tmp_path / 'farm.key'))
    client.verify_flags |= ssl.VERIFY_X509_STRICT
    incoming, outgoing = socket.socketpair()
    incoming.settimeout(5)
    outgoing.settimeout(5)
    errors = []

    def accept():
        try:
            with server.wrap_socket(incoming, server_side=True) as peer:
                peer.sendall(b'OK')
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=accept)
    worker.start()
    try:
        with client.wrap_socket(outgoing, server_hostname='10.204.1.2') as peer:
            assert peer.recv(2) == b'OK'
    finally:
        worker.join(timeout=6)
    assert not worker.is_alive() and not errors
