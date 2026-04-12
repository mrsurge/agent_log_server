from dataclasses import dataclass
from typing import Any

import socketio
from fastapi import FastAPI


@dataclass(frozen=True)
class AppIndex:
    app: FastAPI
    socketio_server: socketio.AsyncServer
    socketio_app: socketio.ASGIApp


def create_app_index(*, lifespan: Any) -> AppIndex:
    app = FastAPI(lifespan=lifespan)
    socketio_server = socketio.AsyncServer(
        async_mode='asgi',
        cors_allowed_origins='*',
        max_http_buffer_size=64 * 1024 * 1024,
    )
    socketio_app = socketio.ASGIApp(socketio_server, other_asgi_app=app)
    return AppIndex(
        app=app,
        socketio_server=socketio_server,
        socketio_app=socketio_app,
    )
