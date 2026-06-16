# Receiving Stinger webhooks

This guide is for developers on the **receiving** end — you've given someone an
endpoint URL, and Stinger is now POSTing signed events to it. It covers how to
verify those signatures, handle retries, and survive a secret rotation.

## What arrives

Every delivery is an HTTP `POST` with a JSON body and three headers:

| Header              | Example                          | Meaning                                                |
| ------------------- | -------------------------------- | ------------------------------------------------------ |
| `webhook-id`        | `0193f0c2-…`                     | Unique per message. Use it to **dedupe** (see below).  |
| `webhook-timestamp` | `1718500000`                     | Unix **seconds** when the delivery was signed.         |
| `webhook-signature` | `v1,3hq… v1,Tg9…`                | One or more space-separated `v1,<base64>` tokens.       |

Your signing secret looks like `whsec_MfKQ9r…` — Stinger gives it to you when the
endpoint is created (or rotated). **Treat it like a password.**

## How verification works

The signature is `base64( HMAC-SHA256(key, signed_content) )`, where:

- `signed_content` is the exact string `{webhook-id}.{webhook-timestamp}.{raw_body}`
- `key` is the base64-decoded bytes of your secret **after** the `whsec_` prefix

To verify, you:

1. **Reject stale deliveries.** If `abs(now - webhook-timestamp)` exceeds your
   tolerance (5 minutes is standard), reject — this is replay protection.
2. **Recompute** the expected signature over the **raw request body bytes**.
3. **Compare** it, in constant time, against each token in `webhook-signature`
   (strip the `v1,` prefix). Accept if **any** token matches.

Two things cause almost all verification failures, so get them right first:

- **Verify against the raw body bytes, not re-serialized JSON.** The signature
  covers the exact bytes Stinger sent. If your framework parses the body to JSON
  and you re-serialize it, the bytes change (key order, whitespace) and the
  signature won't match. Read the raw body *before* any JSON parsing.
- **The timestamp is in seconds, not milliseconds.**

## Python

```python
import base64
import hashlib
import hmac
import time


def verify(secret: str, headers, body: bytes, tolerance: int = 300) -> bool:
    try:
        msg_id = headers["webhook-id"]
        ts = headers["webhook-timestamp"]
        sig_header = headers["webhook-signature"]
    except KeyError:
        return False

    # 1. replay protection
    if abs(time.time() - int(ts)) > tolerance:
        return False

    # 2. recompute over the RAW body bytes
    key = base64.b64decode(secret.removeprefix("whsec_"))
    signed = msg_id.encode() + b"." + ts.encode() + b"." + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()

    # 3. accept if any token matches (handles rotation)
    for token in sig_header.split():
        _, _, sig = token.partition(",")
        if hmac.compare_digest(sig, expected):
            return True
    return False
```

A FastAPI receiver — note `await request.body()` gives the raw bytes:

```python
import json
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()
WEBHOOK_SECRET = "whsec_…"          # from `add-endpoint`, kept in your secrets store
_seen: set[str] = set()             # use a real store (Redis/db) in production

@app.post("/webhooks")
async def receive(request: Request):
    body = await request.body()     # RAW bytes — verify against these
    if not verify(WEBHOOK_SECRET, request.headers, body):
        raise HTTPException(status_code=400, detail="invalid signature")

    event_id = request.headers["webhook-id"]
    if event_id in _seen:           # at-least-once: ignore duplicates
        return {"ok": True}
    _seen.add(event_id)

    event = json.loads(body)
    # ... do something with `event` (ideally async; ack fast — see below)
    return {"ok": True}
```

## Node.js

```javascript
const crypto = require("crypto");

function verify(secret, headers, body /* Buffer */, toleranceSec = 300) {
  const id = headers["webhook-id"];
  const ts = headers["webhook-timestamp"];
  const sigHeader = headers["webhook-signature"];
  if (!id || !ts || !sigHeader) return false;

  if (Math.abs(Date.now() / 1000 - Number(ts)) > toleranceSec) return false;

  const key = Buffer.from(secret.replace(/^whsec_/, ""), "base64");
  const signed = Buffer.concat([Buffer.from(`${id}.${ts}.`), body]);
  const expected = crypto.createHmac("sha256", key).update(signed).digest("base64");

  return sigHeader.split(" ").some((token) => {
    const sig = token.split(",")[1] ?? "";
    const a = Buffer.from(sig);
    const b = Buffer.from(expected);
    return a.length === b.length && crypto.timingSafeEqual(a, b);
  });
}
```

With Express, capture the raw body with `express.raw` so you verify the bytes:

```javascript
app.post("/webhooks", express.raw({ type: "application/json" }), (req, res) => {
  if (!verify(WEBHOOK_SECRET, req.headers, req.body)) {
    return res.status(400).send("invalid signature");
  }
  const event = JSON.parse(req.body.toString());
  res.sendStatus(200);
});
```

## Using a Standard Webhooks library

Stinger's scheme matches the [Standard Webhooks](https://www.standardwebhooks.com)
spec, so you can skip the hand-rolled code and use an official library in your
language. In Python:

```python
from standardwebhooks import Webhook

wh = Webhook(WEBHOOK_SECRET)            # the whsec_… string
event = wh.verify(body, dict(headers))  # raises on failure; returns the payload
```

The library handles the timestamp tolerance, the `whsec_` decoding, and the
multi-token comparison for you.

## Responding, and what Stinger does with your response

Your HTTP status code controls retries:

- **`2xx`** — the delivery is marked **succeeded**. You're done.
- **`410 Gone`** — treated as **permanent**: Stinger stops retrying this delivery
  immediately. Return this when an endpoint should no longer receive an event.
- **Anything else, a timeout, or a connection error** — a transient **failure**.
  Stinger retries on a backoff schedule (`5s → 30s → 2m → 10m → 1h → 4h → 12h`,
  then exhausted).

**Acknowledge quickly.** Stinger uses a 10-second read timeout and a 15-second
hard deadline. Verify the signature, record the `webhook-id`, return `2xx`, and
do slow processing afterward — if you process inline and exceed the deadline,
Stinger times out and retries, and you'll get a duplicate.

## Idempotency (dedupe on `webhook-id`)

Delivery is **at-least-once**. If your `2xx` response is lost, or you're slow and
Stinger times out, the same event is redelivered — with the **same `webhook-id`**.
Record the ids you've processed and skip repeats. Don't rely on payload contents
for dedupe; `webhook-id` is the stable key.

## Secret rotation

When an endpoint's secret is rotated, Stinger hands you a new `whsec_…` and, for
a **24-hour overlap window**, signs each delivery with **both** the old and the
new secret — so the `webhook-signature` header carries two tokens.

Because verification accepts *any* matching token, **your receiver keeps working
with the old secret for the entire window** — no coordinated cutover. Update your
configured secret to the new value any time before the window closes; deliveries
after that point are signed with the new secret only.
