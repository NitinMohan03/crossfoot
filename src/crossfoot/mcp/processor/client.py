"""The client of the processor API: the bearer token, the URLs, the 429, and what a status means.

Everything between a tool and `http.py`'s one GET. Four endpoints, all reads.

**What an answer maps to.** The envelope's code table is closed, so each upstream outcome lands
on a code that already says what the caller can do about it:

| the API said                          | code               | why                            |
| ------------------------------------- | ------------------ | ------------------------------ |
| 200                                   | (the body)         |                                |
| 404 `not_found`                       | `not_found`        | no record with that id         |
| 400 `bad_cursor`                      | `invalid_argument` | the cursor is the caller's     |
| 422 `invalid_limit`                   | `limit_exceeded`   | a page above the API's own cap |
| 401, any of its three codes           | `unavailable`      | `retryable: false`, see below  |
| 429, still, after waiting             | `unavailable`      | carries `retry_after`          |
| 5xx, or no answer at all              | `unavailable`      | `retryable: true`              |
| anything else: a 3xx, any other 4xx   | `malformed_source` | an answer nobody documented    |

A 401 is `unavailable` rather than the caller's `invalid_argument` because no argument caused
it and no argument fixes it: the server's own credential was refused, which makes the source
unreachable until a person corrects `CROSSFOOT_PROCESSOR_TOKEN`. `retryable: false` says the
one thing that differs from an outage. A 422 `invalid_query` is `malformed_source` for the
mirror-image reason: dates and limits are checked before a request is built, so the API
refusing one means the contract moved.

**The 429 is waited out, a bounded number of times.** `Retry-After` is honoured exactly, at most
`max_retries` times, so one call makes at most `max_retries + 1` requests. A wait longer than
`max_retry_wait` is not slept at all -- the call answers `unavailable` with the `retry_after` it
was told, and whoever is calling decides whether that is worth their time. A header that is
missing or is not whole seconds counts as one second. Nothing else is retried: a timeout or a
500 is reported once, because an agent with four steps is better served by a code now than by
this server spending its patience for it. The worst case for one request is therefore
`(max_retries + 1) * timeout + max_retries * max_retry_wait` seconds, 25 with the defaults.

**The token** goes into the `Authorization` header and nowhere else: not a URL, not a message.
Messages are built from the API's stable `code`, never from its prose or its headers, so
nothing the far side writes is repeated to the agent unread.
"""

import json
import re
import time
from collections.abc import Callable, Mapping
from datetime import date
from typing import Final
from urllib.parse import quote, urlencode

from crossfoot.mcp.common.errors import (
    INVALID_ARGUMENT,
    LIMIT_EXCEEDED,
    MALFORMED_SOURCE,
    NOT_FOUND,
    UNAVAILABLE,
    ToolError,
)
from crossfoot.mcp.processor.http import MAX_BODY, Reply, SourceDown, Transport, UrllibTransport
from crossfoot.mcp.processor.settings import TOKEN_ENV, ProcessorSettings
from crossfoot.mcp.processor.wire import Page, decode_body, split_page

SETTLEMENTS_PATH: Final = "/v1/settlements"
BATCHES_PATH: Final = "/v1/batches"

#: Seconds to wait on a 429 whose `Retry-After` is missing or is not whole seconds.
FALLBACK_WAIT: Final = 1
#: An upstream error code is a short lower-case word; anything else is not repeated.
_CODE: Final = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_SECONDS: Final = re.compile(r"^\d{1,6}$")

#: `time.sleep`, or a test's stand-in that moves a fake clock instead.
Sleep = Callable[[int], None]


def upstream_code(reply: Reply) -> str | None:
    """The API's own error code, if the body is its envelope. Never its message."""
    try:
        parsed = json.loads(reply.body.decode("utf-8"), parse_float=str)
    except ValueError:
        return None
    error = parsed.get("error") if isinstance(parsed, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) and _CODE.match(code) else None


def retry_after(reply: Reply) -> int:
    """Whole seconds the API asked for, or the fallback when it did not say."""
    value = reply.headers.get("retry-after", "").strip()
    return int(value) if _SECONDS.match(value) else FALLBACK_WAIT


class ProcessorApi:
    """The four read endpoints, each returning the body's own text or raising a coded error."""

    def __init__(
        self,
        settings: ProcessorSettings,
        transport: Transport | None = None,
        sleep: Sleep = time.sleep,
    ) -> None:
        self._settings = settings
        self._transport: Transport = transport or UrllibTransport()
        self._sleep = sleep

    # -- the endpoints ----------------------------------------------------------------------

    def settlement(self, settlement_id: str) -> str:
        """One settlement line's JSON text. `settlement_id` is already validated by the caller."""
        return self._get(
            f"{SETTLEMENTS_PATH}/{quote(settlement_id, safe='')}",
            {},
            missing=f"settlement {settlement_id!r}",
        )

    def batch(self, batch_id: str) -> str:
        """One batch's JSON text. `batch_id` is already validated by the caller."""
        return self._get(
            f"{BATCHES_PATH}/{quote(batch_id, safe='')}", {}, missing=f"batch {batch_id!r}"
        )

    def settlements(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        settled_from: date | None = None,
        settled_to: date | None = None,
    ) -> Page:
        """One page of settlement lines, in the API's order: `settled_on`, then id."""
        return self._page(
            SETTLEMENTS_PATH,
            limit=limit,
            cursor=cursor,
            dates={"settled_from": settled_from, "settled_to": settled_to},
        )

    def batches(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        closed_from: date | None = None,
        closed_to: date | None = None,
    ) -> Page:
        """One page of batches, in the API's order: `closed_on`, then id."""
        return self._page(
            BATCHES_PATH,
            limit=limit,
            cursor=cursor,
            dates={"closed_from": closed_from, "closed_to": closed_to},
        )

    # -- one request, and what its answer means ---------------------------------------------

    def _page(
        self, path: str, *, limit: int, cursor: str | None, dates: Mapping[str, date | None]
    ) -> Page:
        query = {"limit": str(limit)}
        if cursor is not None:
            query["cursor"] = cursor
        query.update({name: day.isoformat() for name, day in dates.items() if day is not None})
        return split_page(self._get(path, query, missing=None))

    def _get(self, path: str, query: Mapping[str, str], *, missing: str | None) -> str:
        """GET one path, waiting out a 429 within bounds. `missing` names the thing a 404 means."""
        url = self._settings.base_url + path + (f"?{urlencode(query)}" if query else "")
        headers = {
            "Authorization": f"Bearer {self._settings.token}",
            "Accept": "application/json",
        }
        attempts = 0
        while True:
            attempts += 1
            try:
                reply = self._transport.get(url, headers, self._settings.timeout)
            except SourceDown as exc:
                raise ToolError(
                    UNAVAILABLE,
                    f"the processor API at {self._settings.base_url} did not answer "
                    f"({exc.reason}); is `just proc-api` running?",
                    retryable=True,
                ) from exc
            if reply.status != 429:
                return self._answer(reply, missing)
            wait = retry_after(reply)
            if attempts > self._settings.max_retries or wait > self._settings.max_retry_wait:
                raise ToolError(
                    UNAVAILABLE,
                    f"the processor API is rate limiting this server and asks for {wait}s more "
                    f"after {attempts} request(s); try again after that",
                    upstream_status=429,
                    retry_after=wait,
                    attempts=attempts,
                    retryable=True,
                )
            self._sleep(wait)

    def _answer(self, reply: Reply, missing: str | None) -> str:
        status = reply.status
        if len(reply.body) > MAX_BODY:
            raise ToolError(
                MALFORMED_SOURCE,
                f"the processor API sent more than {MAX_BODY} bytes in one response",
                upstream_status=status,
            )
        if status == 200:
            return decode_body(reply.body)

        code = upstream_code(reply)
        named = f"{status} {code}" if code else str(status)
        if status == 404 and missing is not None:
            raise ToolError(NOT_FOUND, f"no {missing}")
        if status == 401:
            raise ToolError(
                UNAVAILABLE,
                f"the processor API refused this server's credentials ({named}); check {TOKEN_ENV}",
                upstream_status=status,
                upstream_code=code,
                retryable=False,
            )
        if status == 400 and code == "bad_cursor":
            raise ToolError(
                INVALID_ARGUMENT,
                "cursor: not a cursor the processor API issued for this list; "
                "start again without one",
            )
        if status == 422 and code == "invalid_limit":
            raise ToolError(
                LIMIT_EXCEEDED,
                "limit is above the processor API's own maximum page size",
                upstream_status=status,
            )
        if status >= 500:
            raise ToolError(
                UNAVAILABLE,
                f"the processor API failed ({named})",
                upstream_status=status,
                retryable=True,
            )
        raise ToolError(
            MALFORMED_SOURCE,
            f"the processor API answered {named}, which this client has no reading for",
            upstream_status=status,
        )
