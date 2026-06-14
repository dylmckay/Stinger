import os
from slowapi import Limiter
from slowapi.util import get_remote_address

rlimiter = Limiter(
    key_func=get_remote_address,
    enabled=not bool(os.getenv("TESTING")),
)
