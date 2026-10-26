"""Mutating demo site: synthetic tender notices behind a switchable page layout.

Every layout serves the same records at the same URLs; only the HTML markup
changes. That makes it a controlled target for testing breakage detection and repair.

Run:     python -m examples.mutating_site --layout 2 --chaos flaky
         (or: uvicorn examples.mutating_site.app:app, configured by LAYOUT / CHAOS env vars)
Switch:  POST /admin/layout/{n} and POST /admin/chaos/{modes} while running.
"""

import math
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.requests import Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from .chaos import DEFAULT_FAILURE_RATE, Chaos, parse_modes
from .records import Notice, format_date, format_money, generate_notices

LAYOUTS = (1, 2, 3)
PAGE_SIZE = 20
ROBOTS_TXT = "User-agent: *\nDisallow: /admin/\n"

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["day"] = format_date
templates.env.filters["money"] = format_money


def _check_layout(layout: int) -> int:
    if layout not in LAYOUTS:
        raise ValueError(f"layout must be one of {LAYOUTS}, got {layout}")
    return layout


def create_app(
    layout: int | None = None,
    chaos: str | None = None,
    failure_rate: float | None = None,
    notices: list[Notice] | None = None,
) -> FastAPI:
    """Build the site. Unset arguments fall back to the LAYOUT, CHAOS and
    CHAOS_FAILURE_RATE env vars, then to layout 1 with no chaos. `notices`
    replaces the 200 generated ones, e.g. to try a larger source."""
    if layout is None:
        layout = int(os.environ.get("LAYOUT", "1"))
    if chaos is None:
        chaos = os.environ.get("CHAOS", "off")
    if failure_rate is None:
        failure_rate = float(os.environ.get("CHAOS_FAILURE_RATE", DEFAULT_FAILURE_RATE))

    app = FastAPI(title="scrapemend demo site")
    app.state.layout = _check_layout(layout)
    app.state.chaos = Chaos(parse_modes(chaos), failure_rate)

    if notices is None:
        notices = generate_notices()
    notices = sorted(notices, key=lambda n: (n.published, n.id), reverse=True)
    by_id = {n.id: n for n in notices}
    pages = math.ceil(len(notices) / PAGE_SIZE)

    def render(request: Request, template: str, context: dict[str, Any]) -> HTMLResponse:
        return templates.TemplateResponse(request, f"v{app.state.layout}/{template}", context)

    # Chaos only hits content pages, so /admin, /docs and /robots.txt always answer.
    @app.middleware("http")
    async def inject_chaos(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path.startswith("/notices"):
            chaos: Chaos = app.state.chaos
            failure = chaos.intercept()
            if failure is not None:
                return failure
        return await call_next(request)

    @app.get("/", include_in_schema=False)
    def home() -> RedirectResponse:
        return RedirectResponse("/notices")

    @app.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
    def robots() -> str:
        return ROBOTS_TXT

    @app.get("/notices", response_class=HTMLResponse)
    def listing(request: Request, page: int = 1) -> HTMLResponse:
        if not 1 <= page <= pages:
            raise HTTPException(404, "No such page")
        start = (page - 1) * PAGE_SIZE
        chunk = notices[start : start + PAGE_SIZE]
        return render(
            request,
            "listing.html",
            {
                "notices": chunk,
                "page": page,
                "pages": pages,
                "first": start + 1,
                "last": start + len(chunk),
                "total": len(notices),
            },
        )

    @app.get("/notices/{notice_id}", response_class=HTMLResponse)
    def detail(request: Request, notice_id: str) -> HTMLResponse:
        notice = by_id.get(notice_id)
        if notice is None:
            raise HTTPException(404, "No such notice")
        return render(request, "detail.html", {"notice": notice})

    @app.get("/admin/layout")
    def get_layout() -> dict[str, int]:
        return {"layout": app.state.layout}

    @app.post("/admin/layout/{layout}")
    def set_layout(layout: int) -> dict[str, int]:
        if layout not in LAYOUTS:
            raise HTTPException(422, f"layout must be one of {LAYOUTS}")
        app.state.layout = layout
        return {"layout": layout}

    @app.get("/admin/chaos")
    def get_chaos() -> dict[str, object]:
        chaos: Chaos = app.state.chaos
        return chaos.describe()

    @app.post("/admin/chaos/{modes}")
    def set_chaos(modes: str, failure_rate: float | None = None) -> dict[str, object]:
        """Set chaos modes, comma-separated (flaky, ratelimit, blocked), or "off"."""
        current: Chaos = app.state.chaos
        if failure_rate is None:
            failure_rate = current.failure_rate
        try:
            chaos = Chaos(parse_modes(modes), failure_rate)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        app.state.chaos = chaos
        return chaos.describe()

    return app


app = create_app()
