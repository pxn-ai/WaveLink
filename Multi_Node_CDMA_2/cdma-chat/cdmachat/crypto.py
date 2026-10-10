"""Optional end-to-end encryption (bonus feature): AES-256-GCM.

All stations share a passphrase (``--key``).  A 256-bit key is derived with
scrypt; every message gets a random 96-bit nonce and the (src, dst) addresses
are authenticated as associated data, so a ciphertext replayed to a different
station fails authentication.  Requires the ``cryptography`` package.
"""
from __future__ import annotations

import hashlib
import os

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except Exception:            # pragma: no cover - optional dependency
    AESGCM = None

PLAIN, SEALED = 0x01, 0x02


class Crypto:
    def __init__(self, passphrase: str | None):
        self.enabled = bool(passphrase)
        self.key_id = ""
        if not self.enabled:
            return
        if AESGCM is None:
            raise RuntimeError("encryption needs:  pip install cryptography")
        key = hashlib.scrypt(passphrase.encode(), salt=b"EN2130-cdma-chat", n=2 ** 14, r=8, p=1, dklen=32)
        self.aead = AESGCM(key)
        self.key_id = hashlib.sha256(key).hexdigest()[:8]     # shown in UI to compare keys

    def seal(self, data: bytes, src: int, dst: int) -> bytes:
        if not self.enabled:
            return bytes([PLAIN]) + data
        nonce = os.urandom(12)
        return bytes([SEALED]) + nonce + self.aead.encrypt(nonce, data, bytes([src, dst]))

    def open(self, blob: bytes, src: int, dst: int) -> tuple[bytes, bool]:
        """Returns (plaintext, was_encrypted).  Raises ValueError on failure."""
        if not blob:
            raise ValueError("empty")
        if blob[0] == PLAIN:
            return blob[1:], False
        if blob[0] == SEALED:
            if not self.enabled:
                raise ValueError("encrypted message but no key configured")
            try:
                return self.aead.decrypt(blob[1:13], blob[13:], bytes([src, dst])), True
            except Exception:
                raise ValueError("decryption failed (wrong key?)")
        raise ValueError("unknown envelope")
