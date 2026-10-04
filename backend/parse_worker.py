"""Isolated PDF process. Credentials travel through stdin, never arguments."""

import base64
import json
import os
import sys

try:
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
    if sys.platform == "linux":
        resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
except (ImportError, ValueError, OSError):
    pass
from upload_parser import _parse_upload

if __name__ == "__main__":
    try:
        payload = json.load(sys.stdin)
        data = _parse_upload(
            base64.b64decode(payload["content"]),
            payload["filename"],
            payload.get("password", ""),
        )
        print(json.dumps({"data": data.model_dump(mode="json", by_alias=True)}))
    except Exception as exc:
        print(json.dumps({"error": str(exc)}))
        sys.exit(1)
