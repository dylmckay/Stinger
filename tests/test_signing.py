import time

import pytest

from app.delivery import signing


def test_sign_verify_roundtrip():
    secret = signing.generate_secret()
    headers = signing.sign('{"a":1}', message_id="msg_1", secret=secret)
    assert signing.verify('{"a":1}', headers, secret=secret)


def test_tampered_payload_rejected():
    secret = signing.generate_secret()
    headers = signing.sign('{"a":1}', message_id="msg_1", secret=secret)
    assert not signing.verify('{"a":2}', headers, secret=secret)


def test_tampered_id_rejected():
    secret = signing.generate_secret()
    headers = signing.sign('{"a":1}', message_id="msg_1", secret=secret)
    assert not signing.verify('{"a":1}', {**headers, "webhook-id": "msg_x"}, secret=secret)


def test_stale_timestamp_rejected():
    secret = signing.generate_secret()
    old = signing.sign('{"a":1}', message_id="msg_1", secret=secret, timestamp=int(time.time()) - 9999)
    assert not signing.verify('{"a":1}', old, secret=secret, tolerance_seconds=300)


def test_wrong_secret_rejected():
    headers = signing.sign('{"a":1}', message_id="msg_1", secret=signing.generate_secret())
    assert not signing.verify('{"a":1}', headers, secret=signing.generate_secret())


def test_rotation_accepts_both_secrets():
    old, new = signing.generate_secret(), signing.generate_secret()
    headers = signing.sign('{"a":1}', message_id="msg_1", secret=new, previous_secret=old)
    assert len(headers["webhook-signature"].split()) == 2
    assert signing.verify('{"a":1}', headers, secret=new)   # migrated consumer
    assert signing.verify('{"a":1}', headers, secret=old)   # not-yet-migrated consumer


def test_interop_with_reference_library():
    """Our signature is byte-identical to standardwebhooks."""
    from datetime import datetime, timezone
    Webhook = pytest.importorskip("standardwebhooks").Webhook
    secret, msg_id, payload, ts = signing.generate_secret(), "msg_1", '{"a":1}', int(time.time())
    ours = signing.sign(payload, message_id=msg_id, secret=secret, timestamp=ts)
    ref = Webhook(secret).sign(msg_id, datetime.fromtimestamp(ts, timezone.utc), payload)
    assert ours["webhook-signature"] == ref