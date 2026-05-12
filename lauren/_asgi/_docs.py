"""Interactive API documentation endpoints.

Serves Swagger UI, ReDoc, and the raw OpenAPI 3.1 JSON document. The HTML
assets are loaded from public CDNs so lauren stays dependency-free; callers
that need offline assets can override the CDN URLs when calling
:meth:`LaurenFactory.create`.
"""

from __future__ import annotations

from typing import Any

from ..types import Response


DEFAULT_SWAGGER_JS = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"
DEFAULT_SWAGGER_CSS = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css"
DEFAULT_REDOC_JS = "https://cdn.jsdelivr.net/npm/redoc@next/bundles/redoc.standalone.js"


def swagger_ui_html(
    *,
    openapi_url: str,
    title: str = "API \u2014 Swagger UI",
    js_url: str = DEFAULT_SWAGGER_JS,
    css_url: str = DEFAULT_SWAGGER_CSS,
    oauth2_redirect_url: str | None = None,
) -> str:
    oauth2_line = ""
    if oauth2_redirect_url:
        oauth2_line = f'            oauth2RedirectUrl: "{oauth2_redirect_url}",\n'
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>{title}</title>
  <link rel="stylesheet" href="{css_url}" />
  <link rel="icon" type="image/png" href="https://fastapi.tiangolo.com/img/favicon.png" />
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="{js_url}"></script>
  <script>
    window.onload = () => {{
      window.ui = SwaggerUIBundle({{
        url: "{openapi_url}",
        dom_id: "#swagger-ui",
        deepLinking: true,
        presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],
        layout: "BaseLayout",
{oauth2_line}      }});
    }};
  </script>
</body>
</html>
"""


def redoc_html(
    *,
    openapi_url: str,
    title: str = "API \u2014 ReDoc",
    js_url: str = DEFAULT_REDOC_JS,
) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>{title}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <link href="https://fonts.googleapis.com/css?family=Montserrat:300,400,700|Roboto:300,400,700" rel="stylesheet" />
  <style>body {{ margin: 0; padding: 0; }}</style>
</head>
<body>
  <redoc spec-url="{openapi_url}"></redoc>
  <script src="{js_url}"></script>
</body>
</html>
"""


def html_response(body: str) -> Response:
    resp = Response.text(body)
    # Response.text defaults to text/plain; flip to text/html.
    return resp.with_header("content-type", "text/html; charset=utf-8")


def json_response(data: Any) -> Response:
    return Response.json(data)
