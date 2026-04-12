from dataclasses import dataclass
from pathlib import Path
import json

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from agent_log_server.web_pages import (
    build_codex_agent_manifest,
    build_codex_agent_sw,
    render_appserver_ui,
    render_codex_agent_ui,
)


@dataclass(frozen=True)
class PageRoutesDeps:
    package_root: Path


def register_page_routes(app: FastAPI, deps: PageRoutesDeps) -> None:
    app.mount('/static', StaticFiles(directory=deps.package_root / 'static'), name='static')
    templates = Jinja2Templates(directory=str(deps.package_root / 'templates'))

    @app.get('/agent-log', response_class=HTMLResponse)
    @app.get('/agent-log/', response_class=HTMLResponse)
    async def get_index(request: Request):
        return templates.TemplateResponse('template.html', {'request': request})

    @app.get('/appserver')
    async def appserver_ui() -> HTMLResponse:
        return render_appserver_ui(deps.package_root)

    @app.get('/')
    async def codex_agent_ui() -> HTMLResponse:
        return render_codex_agent_ui(deps.package_root)

    @app.get('/codex-agent')
    @app.get('/codex-agent/')
    async def codex_agent_redirect(request: Request) -> RedirectResponse:
        query = request.url.query
        target = '/' if not query else f'/?{query}'
        return RedirectResponse(url=target, status_code=307)

    @app.get('/manifest.json')
    @app.get('/codex-agent/manifest.json')
    async def codex_agent_manifest() -> Response:
        return Response(
            content=json.dumps(build_codex_agent_manifest(deps.package_root), ensure_ascii=False),
            media_type='application/manifest+json',
            headers={'Cache-Control': 'no-cache, no-store, must-revalidate'},
        )

    @app.get('/sw.js')
    @app.get('/codex-agent/sw.js')
    async def codex_agent_sw() -> Response:
        return Response(
            content=build_codex_agent_sw(deps.package_root),
            media_type='application/javascript',
            headers={'Cache-Control': 'no-cache, no-store, must-revalidate'},
        )
