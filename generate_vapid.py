#!/usr/bin/env python3
"""
generate_vapid.py — VERSION FINALE
Utilise directement la librairie cryptography (pas py-vapid)

Installation si pas déjà fait :
    pip install cryptography
"""

import base64
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import (
    Encoding, PublicFormat, PrivateFormat, NoEncryption
)

def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")

# Générer la paire de clés EC P-256 (standard VAPID)
private_key = ec.generate_private_key(ec.SECP256R1())
public_key  = private_key.public_key()

# Clé privée en PEM (format attendu par pywebpush)
private_pem = private_key.private_bytes(
    encoding=Encoding.PEM,
    format=PrivateFormat.TraditionalOpenSSL,
    encryption_algorithm=NoEncryption()
).decode("utf-8")

# Clé publique en base64url non compressé (format attendu par le navigateur)
public_b64 = b64url(
    public_key.public_bytes(
        encoding=Encoding.X962,
        format=PublicFormat.UncompressedPoint
    )
)

print("\n" + "="*60)
print("  🔑 CLÉS VAPID GÉNÉRÉES")
print("="*60)
print(f"\nVAPID_PRIVATE_KEY (→ variable Render) :")
print(private_pem)
print(f"VAPID_PUBLIC_KEY  (→ src/supabase.js + variable Render) :")
print(public_b64)
print("="*60)
print("⚠️  Copie ces valeurs, ne relance pas ce script sans")
print("    effacer les abonnements push dans Supabase.\n")