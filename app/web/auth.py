"""Login / logout for the dashboard — paste an API key, get a session cookie."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from starlette.responses import RedirectResponse

from app.auth import authenticate
from app.web.deps import verify_csrf

router = APIRouter(tags=["dashboard-auth"])


@router.get("/login")
async def login_form(request: Request):
    if request.session.get("application_id"):
        return RedirectResponse("/dashboard/deliveries", status_code=303)
    return request.app.state.templates.TemplateResponse(
        request, "login.html", {"error": None}
    )


@router.post("/login")
async def login(request: Request, api_key: str = Form(...), _csrf: None = Depends(verify_csrf)):
    async with request.app.state.session_factory() as session:
        application = await authenticate(session, api_key.strip())
    if application is None:
        return request.app.state.templates.TemplateResponse(
            request,
            "login.html",
            {"error": "That key didn't match any application. Check it and try again."},
            status_code=401,
        )
    request.session["application_id"] = str(application.id)
    request.session["application_name"] = application.name
    return RedirectResponse("/dashboard/deliveries", status_code=303)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
