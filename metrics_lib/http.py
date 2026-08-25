"""Shared, leak-safe HTTP transport for the read-only YouTube API clients.

One seam - Transport.get / Transport.post / Transport.get_bytes - attaches the
bearer token, issues the request, and turns HTTP failures into MetricsApiError
whose message never includes the URL, the token, or any secret. The Reporting
and Analytics clients build params and shape responses on top of this; their
tests inject a fake transport, and Transport's own handling is tested by
monkeypatching urllib.request.urlopen (mirrors the radar's yt_api).
"""

import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

# macOS python.org builds ship without a wired-up CA bundle, so default SSL
# verification can fail. certifi is used when present, never required - same
# approach as the radar's yt_api.
try:
    import certifi

    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:  # pragma: no cover
    _SSL_CONTEXT = ssl.create_default_context()


class MetricsApiError(Exception):
    """A YouTube API request failed. Message never includes the URL or token."""

    def __init__(self, status: int, reason: str) -> None:
        self.status = status
        self.reason = reason
        super().__init__(f"YouTube API error (status {status}): {reason}")


def _error_reason(body: bytes) -> str | None:
    """Pull the human message out of a Google API JSON error body, if present."""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
    return None


class Transport:
    """Holds a token provider and issues authorized requests through one seam."""

    def __init__(self, token_provider, timeout: int = 20) -> None:
        self._token_provider = token_provider
        self.timeout = timeout

    def _authorize(self, params: dict, headers: dict) -> None:
        """Put this transport's credential on the request, in the place it belongs.

        The private APIs authorize with a bearer token in a header. The PUBLIC Data API
        authorizes with a key in the query string instead (see ApiKeyTransport), which is a
        different PLACE rather than a different request - so it is one overridable step here
        rather than a second copy of _raw with its own error handling to keep in step.
        """
        headers["Authorization"] = f"Bearer {self._token_provider.bearer()}"

    def get(self, url: str, params: dict | None = None) -> dict:
        return self._request("GET", url, params=params)

    def post(self, url: str, body: dict | None = None) -> dict:
        return self._request("POST", url, body=body)

    def get_bytes(self, url: str) -> bytes:
        """Download raw bytes (a report CSV lives at an absolute downloadUrl)."""
        return self._raw("GET", url, want="bytes")

    def _request(self, method: str, url: str, params=None, body=None) -> dict:
        return self._raw(method, url, params=params, body=body, want="json")

    def _raw(self, method, url, params=None, body=None, want="json"):
        params = dict(params or {})
        headers: dict = {}
        # Authorizing BEFORE the query string is built is what lets a credential live in
        # either place without a second copy of everything below it.
        self._authorize(params, headers)
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        data_bytes = None
        if body is not None:
            data_bytes = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=_SSL_CONTEXT) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            reason = _error_reason(exc.read()) or exc.reason or "request failed"
            raise MetricsApiError(exc.code, reason) from None
        except urllib.error.URLError as exc:
            raise MetricsApiError(0, str(exc.reason) or "request failed") from None
        except (TimeoutError, socket.timeout, OSError):
            # A timeout or dropped connection during the response READ (not
            # the send) is not wrapped in URLError - CPython's do_open only
            # wraps the send phase, so this raises a raw TimeoutError/
            # socket.timeout/OSError. Caught here, after the two more
            # specific except clauses above, since URLError (and its HTTPError
            # subclass) are themselves OSError subclasses and must be handled
            # by their own clauses first.
            raise MetricsApiError(0, "request timed out or connection failed") from None

        if want == "bytes":
            return raw
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise MetricsApiError(0, "could not parse response body") from None


class ApiKeyTransport(Transport):
    """The same seam for the PUBLIC YouTube Data API, which takes a key, not a bearer token.

    Everything about a request that can go wrong - the timeouts, the read-phase OSError, the
    error body parsing, the promise that a MetricsApiError never carries the URL - is shared
    with Transport rather than written twice, so the key can never leak through an error path
    the other transport hardened and this one forgot. It holds no OAuth token provider: this
    reads only what the channel already shows the public.
    """

    def __init__(self, api_key: str, timeout: int = 20) -> None:
        super().__init__(token_provider=None, timeout=timeout)
        self._api_key = api_key

    def _authorize(self, params: dict, headers: dict) -> None:
        params["key"] = self._api_key
