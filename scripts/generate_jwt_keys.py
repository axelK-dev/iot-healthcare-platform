# scripts/generate_jwt_keys.py
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

out_dir = Path("src/config")
out_dir.mkdir(parents=True, exist_ok=True)

# Generate private key (2048-bit RSA)
private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

# Private key: PKCS#8 PEM (-----BEGIN PRIVATE KEY-----)
private_pem = private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
)
(out_dir / "jwt_private.pem").write_bytes(private_pem)

# Public key: SubjectPublicKeyInfo PEM (-----BEGIN PUBLIC KEY-----)
public_pem = private_key.public_key().public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
)
(out_dir / "jwt_public.pem").write_bytes(public_pem)

print("✅ Keys written to src/config/jwt_private.pem and src/config/jwt_public.pem")