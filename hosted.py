"""Single-worker FastAPI entry point for the controller on a Compose host."""

from contextlib import asynccontextmanager
import os

from fastapi import FastAPI, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool
import uvicorn

from app import dispatch, json_reply
from controller import Controller


@asynccontextmanager
async def lifespan(app):
    app.state.controller = await run_in_threadpool(Controller)
    try:
        yield
    finally:
        await run_in_threadpool(app.state.controller.close)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.api_route("/{path:path}", methods=["GET", "POST"])
async def route(request: Request, path: str):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 65536:
            status, headers, data = json_reply(413, {"error": "Request body too large"})
            return Response(data, status_code=status, headers=headers)
    status, headers, data = await run_in_threadpool(
        dispatch, request.app.state.controller, request.method, request.url.path, request.headers,
        bytes(body), request.client.host if request.client else "",
    )
    headers["X-Content-Type-Options"] = "nosniff"
    headers["Referrer-Policy"] = "no-referrer"
    return Response(data, status_code=status, headers=headers)


if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("RG_BIND", "127.0.0.1"), port=int(os.getenv("RG_PORT", "8787")),
                workers=1, proxy_headers=False, access_log=False)
