"""API-key auth for the True Markets Trading API.

One key file does two jobs (https://docs.truemarkets.co/trading-api/requests-and-signing):
  1. Sign `{key_id}.{unix_ts}` with ES256 (raw r||s, base64url) -> access + refresh token.
  2. Stamp each unsigned wallet `payload` (DER/hex inside a base64url JSON) for /execute.

The key file never leaves this machine; only signatures are sent.
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from turnkey_api_key_stamper import ApiKeyStamper, ApiKeyStamperConfig


def _b64url_bytes(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@dataclass
class KeyFile:
    key_id: str
    private_key: ec.EllipticCurvePrivateKey
    stamper: ApiKeyStamper

    @classmethod
    def load(cls, path: str | os.PathLike) -> "KeyFile":
        p = Path(os.path.expanduser(str(path)))
        if not p.exists():
            raise FileNotFoundError(f"TM key file not found: {p}")
        mode = p.stat().st_mode & 0o777
        if mode & 0o077:
            print(f"! warning: {p} is readable by other users (chmod 600 it)")
        data = json.loads(p.read_text())
        key_id, jwk = data["key_id"], data["private_key"]
        x, y, d = (_b64url_bytes(jwk[k]) for k in ("x", "y", "d"))
        private_key = ec.EllipticCurvePrivateNumbers(
            int.from_bytes(d, "big"),
            ec.EllipticCurvePublicNumbers(
                int.from_bytes(x, "big"), int.from_bytes(y, "big"), ec.SECP256R1()
            ),
        ).private_key()
        compressed_pub = bytes([2 if y[31] % 2 == 0 else 3]) + x
        stamper = ApiKeyStamper(
            ApiKeyStamperConfig(api_public_key=compressed_pub.hex(), api_private_key=d.hex())
        )
        return cls(key_id=key_id, private_key=private_key, stamper=stamper)

    def sign_challenge(self, timestamp: int | None = None) -> dict:
        ts = int(time.time()) if timestamp is None else timestamp
        der = self.private_key.sign(f"{self.key_id}.{ts}".encode(), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        sig = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        return {"key_id": self.key_id, "timestamp": ts, "signature": sig}

    def stamp(self, payload: str) -> str:
        """Sign an unsigned wallet payload exactly as returned by the API."""
        return self.stamper.stamp(payload).stamp_header_value


@dataclass
class Token:
    access: str
    refresh: str | None
    expires_at: float  # unix seconds

    def expiring(self, margin: float = 120.0) -> bool:
        return time.time() > self.expires_at - margin

    @classmethod
    def from_response(cls, body: dict) -> "Token":
        exp = body.get("expires_in")
        expires_at = time.time() + 3600
        if isinstance(exp, (int, float)):
            expires_at = time.time() + float(exp)
        elif isinstance(exp, str):
            try:
                expires_at = datetime.fromisoformat(exp.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
        return cls(access=body["access_token"], refresh=body.get("refresh_token"), expires_at=expires_at)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
