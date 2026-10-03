"""The local broker chain must verify under strict Python/OpenSSL rules."""
import shutil
import subprocess

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
