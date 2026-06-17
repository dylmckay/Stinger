"""Envelope encryption for secrets at rest.

Signing secrets must exist as plaintext bytes at delivery time (HMAC needs the
key), so unlike API keys they can't be hashed — they have to be *encrypted* and
recoverable. This module seals them under envelope encryption and hands the
worker a `SecretBox` that opens them just before signing.

Envelope, not a single static key:

  Each secret is sealed under its own fresh 256-bit data key (DEK); the DEK is
  wrapped under a long-lived key-encryption key (KEK) held by a provider. This
  gives (a) KEK rotation without rewriting ciphertext — rewrap the small DEKs,
  leave ciphertext alone — and (b) a KMS seam: the provider is the only thing
  that touches the KEK, so a future `KmsKeyProvider` keeps the KEK out of the
  process entirely. The default `LocalKeyProvider` wraps DEKs with AES-256-GCM
  under a KEK derived from app config — dependency-free beyond `cryptography`,
  in keeping with "Postgres is the only stateful dependency."

Both layers (DEK-over-plaintext, KEK-over-DEK) use AES-256-GCM, so tampering is
rejected at open time rather than silently decrypting to garbage. The token's
header (version + provider id) is the GCM additional authenticated data, binding
each ciphertext to the scheme that produced it.

Token format (opaque, stored verbatim in the existing Text column):

    stcr.v1.<provider>.<wrapped_dek>.<dek_nonce>.<nonce>.<ct>     (each b64url)

The `stcr.` prefix distinguishes encrypted values from legacy plaintext
`whsec_…`, which the migration relies on to seal idempotently.
"""
from __future__ import annotations

import base64
import os
from functools import lru_cache
from typing import Protocol

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_MAGIC = "stcr"
_VERSION = "v1"
_DEK_LEN = 32          # AES-256
_NONCE_LEN = 12        # GCM standard nonce


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class KeyProvider(Protocol):
    """Wraps/unwraps data keys — the only thing that ever touches the KEK.

    `id` is stamped into the token so a later multi-provider deployment can
    route an unwrap to the KEK that sealed it.
    """

    id: str

    def wrap(self, dek: bytes, *, aad: bytes) -> tuple[bytes, bytes]:
        """Return (wrapped_dek, wrap_nonce)."""
        ...

    def unwrap(self, wrapped_dek: bytes, wrap_nonce: bytes, *, aad: bytes) -> bytes:
        ...


class LocalKeyProvider:
    """Default provider: wrap DEKs with AES-256-GCM under a config-derived KEK.

    The KEK is HKDF-SHA256 over the supplied key material with a fixed,
    scheme-specific info string — so even when the caller falls back to passing
    `SECRET_KEY`, the wrapping key is a *distinct derived value*, never the
    cookie-signing key reused verbatim.
    """

    id = "local"
    _INFO = b"stinger-secret-encryption-kek-v1"

    def __init__(self, key_material: bytes) -> None:
        kek = HKDF(
            algorithm=hashes.SHA256(), length=_DEK_LEN, salt=None, info=self._INFO
        ).derive(key_material)
        self._aead = AESGCM(kek)

    def wrap(self, dek: bytes, *, aad: bytes) -> tuple[bytes, bytes]:
        nonce = os.urandom(_NONCE_LEN)
        return self._aead.encrypt(nonce, dek, aad), nonce

    def unwrap(self, wrapped_dek: bytes, wrap_nonce: bytes, *, aad: bytes) -> bytes:
        return self._aead.decrypt(wrap_nonce, wrapped_dek, aad)


class SecretBox:
    """Seals/opens secrets, wrapping each DEK via a KeyProvider."""

    def __init__(self, provider: KeyProvider) -> None:
        self._provider = provider

    @staticmethod
    def is_sealed(value: str) -> bool:
        """True if `value` is one of our tokens (vs legacy plaintext)."""
        return value.startswith(f"{_MAGIC}.")

    def seal(self, plaintext: str) -> str:
        header = f"{_MAGIC}.{_VERSION}.{self._provider.id}"
        aad = header.encode()

        dek = os.urandom(_DEK_LEN)
        nonce = os.urandom(_NONCE_LEN)
        ct = AESGCM(dek).encrypt(nonce, plaintext.encode(), aad)
        wrapped_dek, dek_nonce = self._provider.wrap(dek, aad=aad)

        return ".".join(
            [header, _b64(wrapped_dek), _b64(dek_nonce), _b64(nonce), _b64(ct)]
        )

    def open(self, token: str) -> str:
        try:
            magic, version, provider, wrapped_dek, dek_nonce, nonce, ct = token.split(".")
        except ValueError as e:
            raise ValueError("malformed secret token") from e
        if magic != _MAGIC or version != _VERSION:
            raise ValueError(f"unsupported secret token: {magic}.{version}")
        if provider != self._provider.id:
            raise ValueError(
                f"token sealed by provider {provider!r}, have {self._provider.id!r}"
            )
        aad = f"{magic}.{version}.{provider}".encode()
        dek = self._provider.unwrap(_unb64(wrapped_dek), _unb64(dek_nonce), aad=aad)
        return AESGCM(dek).decrypt(_unb64(nonce), _unb64(ct), aad).decode()


@lru_cache
def get_secret_box() -> SecretBox:
    """Process-wide SecretBox built from settings.

    Uses STINGER_ENCRYPTION_KEY when set, else derives from SECRET_KEY (a
    distinct HKDF sub-key, not the cookie key itself). A dedicated key is
    recommended — see config.encryption_key_material.
    """
    from app.config import encryption_key_material

    return SecretBox(LocalKeyProvider(encryption_key_material()))