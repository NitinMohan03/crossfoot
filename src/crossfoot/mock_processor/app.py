"""The FastAPI app.

Every error leaves through one envelope -- `{"error": {"code": ..., "message": ...}}` -- so a
client can branch on a stable code instead of on prose. Codes:

| status | code             | cause                                           |
| ------ | ---------------- | ----------------------------------------------- |
| 400    | `bad_cursor`     | cursor forged, truncated, or from another list  |
| 401    | `missing_token`  | no `Authorization` header                       |
| 401    | `invalid_scheme` | header present but not `Bearer <token>`         |
| 401    | `invalid_token`  | wrong token                                     |
| 404    | `not_found`      | unknown record id                               |
| 422    | `invalid_limit`  | `limit` outside `1..max_limit`                  |
| 422    | `invalid_query`  | a query parameter failed to parse               |
| 429    | `rate_limited`   | bucket empty; `Retry-After` says how long       |
"""

import hmac
import time
from collections.abc import Mapping
from datetime import date
from typing import Annotated, Any, Final

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from crossfoot.mock_processor.cursors import BadCursor, decode, encode
from crossfoot.mock_processor.limiter import Clock, RateLimited, TokenBucket
from crossfoot.mock_processor.settings import ApiSettings
from crossfoot.mock_processor.store import Collection, Key, Store, body, load_store

SETTLEMENTS: Final = "settlements"
BATCHES: Final = "batches"
JSON: Final = "application/json"


class ApiError(Exception):
    """An error with a stable code, rendered through the one envelope."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        headers: Mapping[str, str] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = dict(headers or {})
        self.extra = dict(extra or {})

    def response(self) -> JSONResponse:
        payload: dict[str, Any] = {"code": self.code, "message": self.message, **self.extra}
        return JSONResponse(
            status_code=self.status_code, content={"error": payload}, headers=self.headers
        )


async def _handle_api_error(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, ApiError):  # pragma: no cover - only registered for ApiError
        raise exc
    return exc.response()


async def _handle_validation_error(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, RequestValidationError):  # pragma: no cover
        raise exc
    fields = ", ".join(str(error["loc"][-1]) for error in exc.errors()) or "query"
    return ApiError(422, "invalid_query", f"could not parse: {fields}").response()


def create_app(settings: ApiSettings, clock: Clock = time.monotonic) -> FastAPI:
    """Build the app. `store` is loaded once, up front: it is immutable and read-only."""
    store: Store = load_store(settings.data_dir)
    bucket = TokenBucket(settings.rate_per_second, settings.burst, clock=clock)

    app = FastAPI(
        title="crossfoot mock payment processor",
        version="1.0.0",
        summary="Phase 1 settlement data behind cursor pagination, bearer auth and a 429.",
    )
    app.add_exception_handler(ApiError, _handle_api_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)

    def authenticate(authorization: str | None) -> str:
        challenge = {"WWW-Authenticate": "Bearer"}
        if authorization is None:
            raise ApiError(401, "missing_token", "Authorization header required", challenge)
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise ApiError(
                401, "invalid_scheme", "expected `Authorization: Bearer <token>`", challenge
            )
        if not hmac.compare_digest(token, settings.token):
            raise ApiError(401, "invalid_token", "bearer token is not recognised", challenge)
        return token

    def guard(authorization: str | None) -> None:
        """Authenticate, then spend a token. Auth first: an unauthenticated caller gets 401."""
        token = authenticate(authorization)
        verdict = bucket.take(token)
        if isinstance(verdict, RateLimited):
            seconds = verdict.retry_after_seconds
            raise ApiError(
                429,
                "rate_limited",
                f"rate limit exceeded; retry in {seconds}s",
                {"Retry-After": str(seconds)},
                {"retry_after": seconds},
            )

    def resolve_limit(limit: int | None) -> int:
        if limit is None:
            return settings.default_limit
        if not 1 <= limit <= settings.max_limit:
            raise ApiError(
                422, "invalid_limit", f"limit must be between 1 and {settings.max_limit}"
            )
        return limit

    def resolve_cursor(name: str, cursor: str | None) -> Key | None:
        if cursor is None:
            return None
        try:
            return decode(name, cursor)
        except BadCursor as exc:
            raise ApiError(400, "bad_cursor", str(exc)) from exc

    def list_page(
        name: str,
        collection: Collection,
        authorization: str | None,
        limit: int | None,
        cursor: str | None,
        lo: date | None,
        hi: date | None,
    ) -> Response:
        guard(authorization)
        records, next_key = collection.page(
            after=resolve_cursor(name, cursor), limit=resolve_limit(limit), lo=lo, hi=hi
        )
        next_cursor = encode(name, next_key) if next_key is not None else None
        return Response(content=body(records, next_cursor), media_type=JSON)

    def single(collection: Collection, authorization: str | None, record_id: str) -> Response:
        guard(authorization)
        record = collection.get(record_id)
        if record is None:
            raise ApiError(404, "not_found", f"no record with id {record_id!r}")
        return Response(content=record.raw, media_type=JSON)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """Unauthenticated and unmetered, so a client can wait for the server to come up."""
        return {"status": "ok", "settlements": str(len(store.settlements))}

    @app.get("/v1/settlements")
    def list_settlements(
        authorization: Annotated[str | None, Header()] = None,
        limit: int | None = None,
        cursor: str | None = None,
        settled_from: date | None = None,
        settled_to: date | None = None,
    ) -> Response:
        return list_page(
            SETTLEMENTS, store.settlements, authorization, limit, cursor, settled_from, settled_to
        )

    @app.get("/v1/settlements/{settlement_id}")
    def get_settlement(
        settlement_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        return single(store.settlements, authorization, settlement_id)

    @app.get("/v1/batches")
    def list_batches(
        authorization: Annotated[str | None, Header()] = None,
        limit: int | None = None,
        cursor: str | None = None,
        closed_from: date | None = None,
        closed_to: date | None = None,
    ) -> Response:
        return list_page(
            BATCHES, store.batches, authorization, limit, cursor, closed_from, closed_to
        )

    @app.get("/v1/batches/{batch_id}")
    def get_batch(
        batch_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        return single(store.batches, authorization, batch_id)

    return app
