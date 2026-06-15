**Function to verify webhooks**

```python
def verify(payload: str, headers: Mapping[str, str], *, secret: str, tolerance_seconds: int = 300) -> bool:
    """Consumer-side verification: constant-time, with replay tolerance.

    This is the reference snippet to ship in the docs so receivers can verify
    Stinger deliveries themselves.
    """
    try:
        message_id = headers["webhook-id"]
        ts = int(headers["webhook-timestamp"])
        header_tokens = headers["webhook-signature"].split()
    except (KeyError, ValueError):
        return False

    if abs(time.time() - ts) > tolerance_seconds:
        return False

    expected = _sig(secret, message_id, ts, payload)
    for token in header_tokens:
        _, _, sig = token.partition(",")
        if hmac.compare_digest(sig, expected):
            return True
    return False
```
This shows receiver-side verification for Stinger deliveries. It extracts `webhook-id`, `webhook-timestamp`, and `webhook-signature` from the request headers, checks the timestamp against the configured replay tolerance, and compares the payload signature in constant time. Receivers should also deduplicate on `webhook-id` to support at-least-once delivery semantics, rejecting duplicate or already-processed webhook IDs within their dedupe window, and should reject any requests whose timestamps fall outside the allowed tolerance.