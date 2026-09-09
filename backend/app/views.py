"""HTML rendering, and one definition of what each error looks like.

Two things need error pages and they must agree: nginx, when it generates an
error itself and fetches a page from `/errors/<status>`, and this app, when a
request reaches it and fails. Both end up in `render_error`, so there is a
single place to change the wording.
"""

from pathlib import Path
from typing import Optional

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

# Wording for the statuses nginx is configured to hand over, plus those this
# app raises. Anything else falls back to the class default below.
ERRORS: dict[int, tuple[str, str]] = {
    400: ("Bad request", "That request could not be understood."),
    401: ("Sign-in required", "You need to be signed in to open this."),
    403: ("Not allowed", "You do not have access to this file."),
    404: (
        "Not found",
        "There is nothing at this address. Check the link you were sent — it may "
        "have been mistyped, or the file may have been removed.",
    ),
    405: ("Method not allowed", "That is not something you can do at this address."),
    408: ("Request timed out", "The request took too long to arrive. Please try again."),
    413: ("File too large", "That file is bigger than this service accepts."),
    414: ("Address too long", "That address is too long to process."),
    429: ("Too many requests", "You have made a lot of requests. Please slow down."),
    431: ("Headers too large", "The request headers were too large to process."),
    500: ("Something went wrong", "An unexpected problem stopped this request."),
    502: ("Storage unavailable", "The file store could not be reached just now."),
    503: ("Service unavailable", "The service is temporarily unable to respond."),
    504: ("Timed out", "The service took too long to respond."),
}

CLASS_DEFAULTS: dict[int, tuple[str, str]] = {
    4: ("Request problem", "That request could not be completed."),
    5: ("Service problem", "Something went wrong at our end."),
}


def error_details(status: int) -> tuple[str, str]:
    if status in ERRORS:
        return ERRORS[status]
    return CLASS_DEFAULTS.get(status // 100, CLASS_DEFAULTS[5])


def wants_html(request: Request) -> bool:
    """Whether to answer with a page rather than JSON.

    Anything under /api is a programmatic caller and gets JSON regardless of
    what it claims to accept; everything else follows the Accept header, so
    curl and fetch() get JSON while a browser gets the page.
    """
    if request.url.path.startswith("/api/"):
        return False
    return "text/html" in request.headers.get("accept", "")


def render_error(
    request: Request, status: int, detail: Optional[str] = None, force_html: bool = False
) -> Response:
    """Render an error, as a page or as JSON depending on who is asking.

    `force_html` is for the /errors/<status> route nginx fetches. nginx may be
    handling a request it could not even parse — an over-long URI arrives with
    no usable Accept header — so negotiation there would quietly hand a browser
    a blob of JSON.
    """
    title, message = error_details(status)
    if not force_html and not wants_html(request):
        return JSONResponse(status_code=status, content={"detail": detail or title})
    return TEMPLATES.TemplateResponse(
        request=request,
        name="error.html",
        context={"status": status, "title": title, "message": message},
        status_code=status,
    )


def render_home(request: Request, indexed: int) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request=request, name="home.html", context={"indexed": indexed}
    )
