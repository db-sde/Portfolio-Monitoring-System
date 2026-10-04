"""Single-owner authentication. No credential is shipped in the frontend."""

import base64
import hashlib
import hmac
import json
import os
import time
import ipaddress
from urllib.parse import urlsplit
from cryptography.fernet import Fernet
from fastapi import HTTPException

COOKIE = "portfolioiq_session"
MAX_AGE = 12 * 60 * 60


def access_mode():
    mode = os.environ.get("ACCESS_MODE", "password").lower()
    if mode not in {"password", "local"}:
        raise RuntimeError("ACCESS_MODE must be 'password' or 'local'.")
    return mode


def local_request(request):
    """Password-free local installations accept loopback peers and hosts only.

    Check the actual peer (not a forwarded header) as well as Host, so a
    hostname resolving to loopback cannot read the local portfolio.
    """
    if not request.client:
        return False
    try:
        peer = ipaddress.ip_address(request.client.host)
        host = request.url.hostname
        local_host = host == "localhost" or ipaddress.ip_address(host).is_loopback
        origin = request.headers.get("origin")
        if origin:
            origin_host = urlsplit(origin).hostname
            if (
                origin_host != "localhost"
                and not ipaddress.ip_address(origin_host).is_loopback
            ):
                return False
        return peer.is_loopback and local_host
    except ValueError:
        return False


def secret():
    value = os.environ.get("APP_SECRET", "")
    if len(value) < 32:
        raise RuntimeError(
            "APP_SECRET must contain at least 32 characters. Configure the same value on API and worker."
        )
    return value.encode()


def cipher():
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret() + b"jobs").digest()))


def password_matches(value):
    expected = os.environ.get("OWNER_PASSWORD", "")
    if len(expected) < 12:
        raise HTTPException(
            503, "Owner access is not configured. Set OWNER_PASSWORD (12+ characters)."
        )
    return hmac.compare_digest(
        hashlib.sha256(value.encode()).digest(),
        hashlib.sha256(expected.encode()).digest(),
    )


def make_session():
    timestamp = str(int(time.time()))
    signature = hmac.new(secret(), timestamp.encode(), hashlib.sha256).hexdigest()
    return f"{timestamp}.{signature}"


def valid_session(value):
    try:
        timestamp, signature = value.split(".")
        age = time.time() - int(timestamp)
        expected = hmac.new(secret(), timestamp.encode(), hashlib.sha256).hexdigest()
        return 0 <= age < MAX_AGE and hmac.compare_digest(signature, expected)
    except (ValueError, TypeError, AttributeError):
        return False


def seal(value):
    return cipher().encrypt(json.dumps(value).encode()).decode()


def unseal(value):
    return json.loads(cipher().decrypt(value.encode(), ttl=3600))
