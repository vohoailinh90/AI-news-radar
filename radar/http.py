"""Fetching, kept small: stdlib only, a User-Agent, a timeout, one retry, a size cap."""

from __future__ import annotations

import time
import urllib.error
import urllib.request

USER_AGENT = "ai-news-radar/0.1 (+https://github.com/vohoailinh90/ai-news-radar)"
DEFAULT_MAX_BYTES = 5_000_000


class FetchError(RuntimeError):
    pass


def fetch(url: str, *, timeout: float = 20, max_bytes: int = DEFAULT_MAX_BYTES, retries: int = 1) -> bytes:
    """GET `url` and return the body. Raises FetchError (never a bare urllib error) so a caller
    can record the failure against the source and carry on."""
    if not url.startswith("https://"):
        raise FetchError(f"{url}: only https:// sources are fetched")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    last = ""
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise FetchError(f"{url}: response is larger than {max_bytes} bytes")
            return body
        except FetchError:
            raise
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
            if exc.code < 500 and exc.code != 429:
                break                      # 403/404 will not change on a second try
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = str(getattr(exc, "reason", exc))
        if attempt < retries:
            time.sleep(1.5)
    raise FetchError(f"{url}: {last}")
