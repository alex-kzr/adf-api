"""Bound request bodies while consuming ASGI chunks, including chunked JSON uploads."""

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RequestTooLarge(HTTPException):
    def __init__(self) -> None:
        super().__init__(
            status_code=413, detail="Request body exceeds the configured limit."
        )


class BodyLimit:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        maximum = (
            30 * 1024 * 1024 if scope["path"].endswith("/artifacts") else 1024 * 1024
        )
        consumed = 0

        async def bounded_receive() -> Message:
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > maximum:
                    raise RequestTooLarge
            return message

        try:
            await self.app(scope, bounded_receive, send)
        except RequestTooLarge:
            response = JSONResponse(
                status_code=413,
                content={
                    "code": "QUOTA_EXCEEDED",
                    "message": "Request body exceeds the configured limit.",
                    "details": {},
                    "request_id": scope.get("state", {}).get(
                        "request_id", "diagnostic"
                    ),
                },
            )
            await response(scope, receive, send)
