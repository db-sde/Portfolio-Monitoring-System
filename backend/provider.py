"""Bound every outbound request, including nested fund-recovery searches."""

import asyncio
import random
import time
import weakref
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
import httpx

_LIMITS = weakref.WeakKeyDictionary()


def retry_delay(value, attempt):
    minimum = 0.0
    if value:
        try:
            minimum = max(0.0, float(value))
        except ValueError:
            try:
                minimum = max(
                    0.0,
                    (
                        parsedate_to_datetime(value) - datetime.now(timezone.utc)
                    ).total_seconds(),
                )
            except (ValueError, TypeError):
                pass
    return max(minimum, min(8, 2**attempt)) + random.random()


class Client(httpx.AsyncClient):
    def __init__(self, **kwargs):
        kwargs.setdefault("timeout", httpx.Timeout(10, connect=5))
        super().__init__(**kwargs)
        self.requests_by_key = {}
        self.metrics = {"requests": 0, "retries": 0, "cache_hits": 0}

    async def aclose(self):
        tasks = list(self.requests_by_key.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await super().aclose()

    async def __aexit__(self, *args):
        await self.aclose()

    async def get(self, url, **kwargs):
        loop_limits = _LIMITS.setdefault(asyncio.get_running_loop(), {})
        semaphore = loop_limits.setdefault(
            urlsplit(str(url)).netloc, asyncio.Semaphore(8)
        )
        async with semaphore:
            self.metrics["requests"] += 1
            return await super().get(url, **kwargs)


async def fetch_json(client, url, **kwargs):
    """Deduplicate a run's identical requests. Failures remain retryable next run."""
    key = (url, repr(sorted(kwargs.items())))
    cache = getattr(client, "requests_by_key", None)
    if cache is not None and key in cache:
        client.metrics["cache_hits"] += 1
        return await asyncio.shield(cache[key])

    async def run():
        deadline = time.monotonic() + 45
        for attempt in range(3):
            try:
                response = await asyncio.wait_for(
                    client.get(url, **kwargs),
                    timeout=max(0.1, deadline - time.monotonic()),
                )
                if response.status_code == 200:
                    return response.json()
                if response.status_code != 429 and response.status_code < 500:
                    return None
                delay = retry_delay(response.headers.get("Retry-After"), attempt)
            except (httpx.HTTPError, asyncio.TimeoutError):
                delay = retry_delay(None, attempt)
            except ValueError:
                return None
            if attempt == 2 or time.monotonic() + delay >= deadline:
                return None
            if hasattr(client, "metrics"):
                client.metrics["retries"] += 1
            await asyncio.sleep(delay)
        return None

    task = asyncio.create_task(run())
    if cache is not None:
        cache[key] = task
    return await asyncio.shield(task)
