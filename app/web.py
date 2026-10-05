from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import util
from .auth import csrf_token
from .config import settings

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
g = templates.env.globals
g.update(settings=settings, money=util.money, fmt_dt=util.fmt_dt, fmt_range=util.fmt_range,
         render_text=util.render_text, utc_to_local=util.utc_to_local, input_value=util.input_value,
         row_letter=util.row_letter)


def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    ctx.setdefault("user", getattr(request.state, "user", None))
    ctx["csrf"] = csrf_token(request)
    ctx["is_htmx"] = request.headers.get("hx-request") == "true"
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def redirect(request: Request, url: str) -> Response:
    """303 for normal forms; HX-Redirect for htmx so the whole page navigates."""
    if request.headers.get("hx-request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


def flash_url(url: str, msg: str) -> str:
    from urllib.parse import quote
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}msg={quote(msg)}"
