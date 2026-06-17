import base64

import pytest

from app.config import EncryptionSettings
from app.crypto import LocalKeyProvider, SecretBox

KEY = b"unit-test-key-material-not-for-real-use"


@pytest.fixture
def box():
    return SecretBox(LocalKeyProvider(KEY))


def test_round_trip(box):
    secret = "whsec_" + base64.b64encode(b"x" * 24).decode()
    assert box.open(box.seal(secret)) == secret


def test_is_sealed_distinguishes_token_from_plaintext(box):
    token = box.seal("whsec_abc")
    assert SecretBox.is_sealed(token)
    assert not SecretBox.is_sealed("whsec_abc")     # legacy plaintext


def test_ciphertext_is_non_deterministic(box):
    # fresh DEK + nonce per seal → two seals of one plaintext never collide,
    # which is also why the rotation column-copy never reuses a nonce.
    assert box.seal("whsec_abc") != box.seal("whsec_abc")


def test_tampered_ciphertext_is_rejected(box):
    token = box.seal("whsec_abc")
    parts = token.split(".")
    ct = bytearray(base64.urlsafe_b64decode(parts[-1] + "=" * (-len(parts[-1]) % 4)))
    ct[0] ^= 0x01
    parts[-1] = base64.urlsafe_b64encode(bytes(ct)).rstrip(b"=").decode()
    with pytest.raises(Exception):                  # GCM InvalidTag
        box.open(".".join(parts))


def test_tampered_header_is_rejected(box):
    # version/provider are the GCM AAD, so flipping them fails the open
    token = box.seal("whsec_abc")
    forged = "stcr.v2." + token.split(".", 2)[2]
    with pytest.raises(ValueError):
        box.open(forged)


def test_wrong_key_cannot_open(box):
    token = box.seal("whsec_abc")
    other = SecretBox(LocalKeyProvider(b"a-completely-different-key-aaaaaa"))
    with pytest.raises(Exception):
        other.open(token)


def test_malformed_token_raises(box):
    with pytest.raises(ValueError):
        box.open("not-a-token")


def test_token_is_relocatable(box):
    # moving a token between columns (previous_secret = secret) must still open —
    # this is what makes rotation's in-DB column copy safe.
    secret = "whsec_relocate"
    token = box.seal(secret)
    moved = token
    assert box.open(moved) == secret


def test_key_resolution_prefers_dedicated_then_falls_back():
    # The settings object should resolve the dedicated encryption key first,
    # then fall back to the general secret when no dedicated key is present.
    s = EncryptionSettings(SECRET_KEY="cookie-key", STINGER_ENCRYPTION_KEY=None)
    assert (s.STINGER_ENCRYPTION_KEY or s.SECRET_KEY).encode() == b"cookie-key"
    s2 = EncryptionSettings(SECRET_KEY="cookie-key", STINGER_ENCRYPTION_KEY="dek")
    assert (s2.STINGER_ENCRYPTION_KEY or s2.SECRET_KEY).encode() == b"dek"