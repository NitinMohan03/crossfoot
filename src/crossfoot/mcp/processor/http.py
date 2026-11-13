"""The processor server's read-only access to the API. Rule 6, for a source that is HTTP.

`mcp-erp` has a database role that cannot write; `mcp-bank` has one `open`, mode `"rb"`. An HTTP
API has neither, so the boundary here is the verb: **this module issues the only request in the
package, and its method is the literal `"GET"`.** There is no body argument, no other verb, and
no second place a request could be made -- `tests/test_mcp/processor/test_no_write.py` checks
each of those against the source, and then checks what actually goes over a socket.

Two things the opener is built *without*, both because of the bearer token:

* **No redirects.** urllib follows a 3xx by default and would carry the `Authorization` header
  to wherever `Location` points. The token is for the configured host and nowhere else, so a
  redirect is returned as the answer it is and the client reads it as an unexpected one.
* **No proxy.** The environment's proxy settings would put a third machine between this server
  and its source, holding the token. The URL in the settings is the whole route.

A status that is not 200 is still an answer and comes back as a `Reply`: only the absence of any
answer -- refused, timed out, cut off -- is `SourceDown`.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from http.client import HTTPException
from typing import Any, Final, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

#: More than any page this API serves. A body beyond it is not read to the end.
MAX_BODY: Final = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Reply:
    """What the API answered: status, headers with lower-case names, and the body as bytes."""

    status: int
    headers: Mapping[str, str]
    body: bytes


class SourceDown(Exception):
    """No HTTP answer at all. `reason` is a few words, safe to show a caller."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Transport(Protocol):
    """The one thing the client needs from the network: a GET."""

    def get(self, url: str, headers: Mapping[str, str], timeout: float) -> Reply: ...


class _NoRedirect(HTTPRedirectHandler):
    """A 3xx is handed back, not followed."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _reply(status: int, headers: Any, body: bytes) -> Reply:
    return Reply(
        status=status,
        headers={str(name).lower(): str(value) for name, value in headers.items()},
        body=body,
    )


class UrllibTransport:
    """GET over the standard library, and nothing else."""

    def __init__(self) -> None:
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def get(self, url: str, headers: Mapping[str, str], timeout: float) -> Reply:
        """The package's one request. One byte past `MAX_BODY` is read so the caller can tell."""
        request = Request(url, headers=dict(headers), method="GET")
        try:
            with self._opener.open(request, timeout=timeout) as response:
                return _reply(response.status, response.headers, response.read(MAX_BODY + 1))
        except HTTPError as exc:
            # A 4xx or 5xx (and, with no redirect handling, a 3xx): an answer, not a failure.
            with exc:
                return _reply(exc.code, exc.headers, exc.read(MAX_BODY + 1))
        except TimeoutError as exc:
            raise SourceDown("timed out") from exc
        except URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise SourceDown("timed out") from exc
            raise SourceDown(f"connection failed: {type(exc.reason).__name__}") from exc
        except (OSError, HTTPException) as exc:
            raise SourceDown(f"connection failed: {type(exc).__name__}") from exc
