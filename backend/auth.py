"""Encryption for queued statement payloads and PDF passwords."""

import base64
import hashlib
import json
import os
from cryptography.fernet import Fernet


def secret():
    value = os.environ.get("APP_SECRET", "")
    if len(value) < 32:
        raise RuntimeError(
            "APP_SECRET must contain at least 32 characters. Configure the same value on API and worker."
        )
    return value.encode()


def cipher():
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret() + b"jobs").digest()))


def seal(value):
    return cipher().encrypt(json.dumps(value).encode()).decode()


def unseal(value):
    return json.loads(cipher().decrypt(value.encode(), ttl=3600))
