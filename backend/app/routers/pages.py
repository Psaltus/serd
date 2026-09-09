"""The pages nginx proxies through: the front page and the error pages."""

from fastapi import APIRouter, Request
from fastapi.responses import Response

from ..cache import cache
from ..views import render_error, render_home

router = APIRouter(include_in_schema=False)


@router.get("/")
def home(request: Request) -> Response:
    return render_home(request, indexed=cache.size())


@router.get("/errors/{status}")
def error_page(request: Request, status: int) -> Response:
    """Rendered on nginx's behalf when nginx itself produces an error.

    nginx keeps the original status code and swaps in this body, so the 200
    returned here is not what the client sees. A status outside the error range
    would mean a misconfigured error_page directive, so it is normalised to 500
    rather than rendering a page claiming success.

    Always HTML: this route exists only to give nginx something to show a
    person, and the request that triggered it may have been too malformed for
    nginx to have parsed an Accept header from it.
    """
    if not 400 <= status <= 599:
        status = 500
    return render_error(request, status, force_html=True)
