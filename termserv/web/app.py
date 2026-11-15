"""termserv web UI: FastAPI + server-rendered Jinja templates + htmx.

Runs as root (it edits /etc and restarts services) behind HTTPS. Only members
of settings.admin_group can log in, with their Linux password (PAM).

Sessions are kept on the server (see sessions.py): the cookie is a random id
without an expiry date, so closing the browser ends it; an idle session ends
after settings.web_idle_minutes (default 60) and any session after 8 hours.
"""

from __future__ import annotations

import asyncio
import hmac
import secrets
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

from .. import __version__, model, service
from ..apply import ApplyError
from .sessions import LoginThrottle, Session, SessionStore

HERE = Path(__file__).parent
PAM_SERVICE = "termserv-web"
COOKIE = "termserv_session"
# Sent by the auto-refreshing parts of the page; such requests do not count as
# activity, so an open but unattended tab still times out.
BACKGROUND_HEADER = "x-termserv-background"
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), usb=(), serial=()",
}


def pam_authenticate(user: str, password: str) -> bool:
    import pam  # python-pam; imported lazily so tests need no PAM

    return pam.pam().authenticate(user, password, service=PAM_SERVICE)


class NotLoggedIn(Exception):
    pass


def create_app(
    store: service.Store | None = None,
    authenticate: Callable[[str, str], bool] = pam_authenticate,
    https_only: bool = True,
    sessions: SessionStore | None = None,
    throttle: LoginThrottle | None = None,
) -> FastAPI:
    store = store or service.Store()
    # Unless the caller passes its own, the session store and login throttle follow
    # settings.web_idle_minutes / web_lockout_minutes / web_max_failures, picked up
    # from ports.yaml whenever it changes (no restart, nobody gets logged out).
    follow_settings = sessions is None and throttle is None
    if sessions is None:  # not `or`: an empty SessionStore is falsy (__len__)
        sessions = SessionStore()
    if throttle is None:
        throttle = LoginThrottle()
    seen_mtime: list[float | None] = [None]

    def refresh_settings() -> None:
        if not follow_settings:
            return
        try:
            mtime = store.ports_file.stat().st_mtime
        except OSError:
            mtime = 0.0
        if mtime == seen_mtime[0]:
            return
        try:
            s = store.load().settings
        except model.ConfigError:
            return  # keep the last good values
        seen_mtime[0] = mtime
        sessions.idle_seconds = s.web_idle_minutes * 60
        throttle.max_failures = s.web_max_failures
        throttle.window = s.web_lockout_minutes * 60

    refresh_settings()
    app = FastAPI(title="termserv", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.sessions = sessions
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals["version"] = __version__
    templates.env.globals["restart_question"] = service.restart_question

    @app.middleware("http")
    async def session_middleware(request: Request, call_next):
        refresh_settings()
        sid = request.cookies.get(COOKIE)
        background = request.headers.get(BACKGROUND_HEADER) == "1"
        sess, timed_out = sessions.get(sid, touch=not background)
        if sess is None:
            sess = sessions.new()
            sess.expired_notice = timed_out
            if timed_out:
                sess.data.setdefault("flash", []).append(
                    {"text": "Your session ended after inactivity. Please log in again.", "kind": "error"})
        request.state.session = sess
        response = await call_next(request)
        sess = request.state.session  # login/logout swap it
        if sess.id != sid:
            # No max_age/expires: a browser-session cookie, gone when the browser closes.
            response.set_cookie(COOKIE, sess.id, httponly=True, secure=https_only, samesite="strict", path="/")
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    # --- helpers -----------------------------------------------------------

    def S(request: Request) -> dict:
        return request.state.session.data

    def current_user(request: Request) -> str:
        user = S(request).get("user")
        if not user:
            raise NotLoggedIn()
        return user

    def csrf_token(request: Request) -> str:
        tok = S(request).get("csrf")
        if not tok:
            tok = S(request)["csrf"] = secrets.token_urlsafe(32)
        return tok

    async def check_csrf(request: Request) -> None:
        sent = request.headers.get("x-csrf-token")
        if sent is None:
            form = await request.form()
            sent = form.get("csrf", "")
        expected = S(request).get("csrf", "")
        if not expected or not hmac.compare_digest(str(sent), expected):
            raise HTTPException(403, "bad CSRF token, reload the page")

    def flash(request: Request, text: str, kind: str = "ok") -> None:
        S(request).setdefault("flash", []).append({"text": text, "kind": kind})

    def render(request: Request, name: str, **ctx) -> HTMLResponse:
        ctx.setdefault("user", S(request).get("user"))
        ctx["csrf"] = csrf_token(request)
        ctx["flashes"] = S(request).pop("flash", [])
        ctx["request"] = request
        if ctx["user"]:
            ctx.setdefault("pending", service.pending_changes(store))
        return templates.TemplateResponse(request, name, ctx)

    def back(url: str = "/") -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    def run_action(request: Request, fn, ok_text: str | None = None) -> bool:
        try:
            result = fn()
        except (model.ConfigError, ApplyError) as e:
            flash(request, str(e), "error")
            return False
        if ok_text:
            flash(request, ok_text)
        elif isinstance(result, list) and result:
            flash(request, "; ".join(result))
        return True

    @app.exception_handler(NotLoggedIn)
    async def _login_redirect(request: Request, exc: NotLoggedIn):
        if request.headers.get("hx-request"):
            return Response(status_code=204, headers={"HX-Redirect": "/login"})
        return back("/login")

    # --- auth --------------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if S(request).get("user"):
            return back("/")
        return render(request, "login.html", user=None)

    @app.post("/login")
    async def login(request: Request, username: str = Form(...), password: str = Form(...)):
        await check_csrf(request)
        client = request.client.host if request.client else "?"
        wait = throttle.locked_for(client)
        if wait:
            flash(request, f"Too many failed logins. Try again in {(wait + 59) // 60} minute(s).", "error")
            return back("/login")
        username = username.strip()
        cfg = store.load()
        ok = await asyncio.to_thread(authenticate, username, password)
        if not ok or not service.is_admin(cfg, username):
            throttle.failed(client)
            await asyncio.sleep(1.5)  # slow down guessing
            flash(request, f"Login failed. Use a Linux account in the '{cfg.settings.admin_group}' group.", "error")
            return back("/login")
        throttle.succeeded(client)
        request.state.session = sessions.login(request.state.session, username)
        return back("/")

    @app.post("/logout")
    async def logout(request: Request):
        await check_csrf(request)
        sessions.delete(request.state.session)  # the old id is now worthless, even if copied
        request.state.session = sessions.new()
        return back("/login")

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        current_user(request)
        cfg = store.load()
        return render(request, "ports.html", cfg=cfg, views=service.port_views(cfg),
                      services=service.services_status(), address=_address(request))

    @app.get("/partials/ports", response_class=HTMLResponse)
    async def ports_partial(request: Request):
        current_user(request)
        cfg = store.load()
        return render(request, "_ports_table.html", cfg=cfg, views=service.port_views(cfg), address=_address(request))

    @app.get("/ports/new", response_class=HTMLResponse)
    async def new_port(request: Request):
        current_user(request)
        cfg = store.load()
        port = model.Port(number=service.next_free_number(cfg))
        return render(request, "port_form.html", cfg=cfg, port=port, is_new=True,
                      sockets=service.socket_choices(cfg), bauds=model.BAUDS, parities=model.PARITIES, flows=model.FLOWS)

    @app.get("/ports/{number}/edit", response_class=HTMLResponse)
    async def edit_port(request: Request, number: int):
        current_user(request)
        cfg = store.load()
        port = cfg.port(number)
        if port is None:
            raise HTTPException(404, "no such port")
        return render(request, "port_form.html", cfg=cfg, port=port, is_new=False,
                      sockets=service.socket_choices(cfg, number, port.usb_path), bauds=model.BAUDS, parities=model.PARITIES, flows=model.FLOWS)

    @app.post("/ports")
    async def create_port(request: Request):
        current_user(request)
        await check_csrf(request)
        data = dict(await request.form())
        if run_action(request, lambda: service.save_port(store, data), f"Port {data.get('number')} added. Apply to make it live."):
            return back("/")
        return _form_again(request, data, is_new=True)

    @app.post("/ports/{number}")
    async def update_port(request: Request, number: int):
        current_user(request)
        await check_csrf(request)
        data = dict(await request.form())
        if run_action(request, lambda: service.save_port(store, data, original_number=number),
                      f"Port {data.get('number')} saved. Apply to make it live."):
            return back("/")
        return _form_again(request, data, is_new=False, original=number)

    def _form_again(request: Request, data: dict, is_new: bool, original: int | None = None):
        cfg = store.load()
        try:
            port = service.parse_port(data)
        except model.ConfigError:
            port = cfg.port(original) if original else model.Port(number=service.next_free_number(cfg))
        return render(request, "port_form.html", cfg=cfg, port=port, is_new=is_new, original=original,
                      sockets=service.socket_choices(cfg, original, port.usb_path), bauds=model.BAUDS, parities=model.PARITIES, flows=model.FLOWS)

    @app.post("/ports/{number}/delete")
    async def remove_port(request: Request, number: int):
        current_user(request)
        await check_csrf(request)
        run_action(request, lambda: service.delete_port(store, number), f"Port {number} deleted. Apply to make it live.")
        return back("/")

    @app.post("/ports/{number}/release")
    async def release_port(request: Request, number: int):
        current_user(request)
        await check_csrf(request)
        run_action(request, lambda: service.release(store, number),
                   f"Port {number} released: its device is free for local tools until you grab it back.")
        return back("/")

    @app.post("/ports/{number}/grab")
    async def grab_port(request: Request, number: int):
        current_user(request)
        await check_csrf(request)
        run_action(request, lambda: service.grab(store, number), f"Port {number} is back under termserv.")
        return back("/")

    @app.post("/apply")
    async def apply_now(request: Request):
        current_user(request)
        await check_csrf(request)
        run_action(request, lambda: service.apply_changes(store), "Changes applied.")
        return back("/")

    @app.post("/restart")
    async def restart_services(request: Request):
        current_user(request)
        await check_csrf(request)
        log: list[str] = []
        # This process is the web UI: restart the other services first, answer,
        # and only then restart ourselves (sessions live in memory, so everyone
        # has to log in again afterwards).
        if not run_action(request, lambda: log.extend(service.restart_services(store, web=False))):
            return back("/")
        if not store.load().settings.web_enabled:
            flash(request, "Services restarted: " + "; ".join(log))
            return back("/")
        response = render(request, "restarting.html", user=None, log=log)
        response.background = BackgroundTask(_restart_web_soon)
        return response

    # --- logs --------------------------------------------------------------

    @app.get("/ports/{number}/log", response_class=HTMLResponse)
    async def port_log(request: Request, number: int):
        current_user(request)
        cfg = store.load()
        port = cfg.port(number)
        if port is None:
            raise HTTPException(404, "no such port")
        return render(request, "log.html", cfg=cfg, port=port, log=service.log_tail(cfg, number))

    @app.post("/ports/{number}/log/clear")
    async def clear_port_log(request: Request, number: int):
        current_user(request)
        await check_csrf(request)
        run_action(request, lambda: service.clear_log(store.load(), number), f"Port {number}'s log cleared.")
        return back(f"/ports/{number}/log")

    @app.get("/partials/ports/{number}/log", response_class=HTMLResponse)
    async def port_log_partial(request: Request, number: int):
        current_user(request)
        cfg = store.load()
        return render(request, "_log.html", log=service.log_tail(cfg, number))

    # --- users -------------------------------------------------------------

    @app.get("/users", response_class=HTMLResponse)
    async def users_page(request: Request):
        current_user(request)
        cfg = store.load()
        groups, users = service.users_matrix(cfg)
        return render(request, "users.html", cfg=cfg, groups=groups, users=users)

    @app.post("/users/{username}/groups/{group}")
    async def set_group(request: Request, username: str, group: str, member: str = Form(...)):
        current_user(request)
        await check_csrf(request)
        add = member == "1"
        run_action(request, lambda: service.set_membership(store.load(), username, group, add),
                   f"{username} {'added to' if add else 'removed from'} {group}.")
        return back("/users")

    return app


async def _restart_web_soon() -> None:
    await asyncio.sleep(1)  # let the browser receive the page first
    await asyncio.to_thread(service.restart_web)


def _address(request: Request) -> str:
    """The IP address this request arrived on, i.e. one the user can reach us at."""
    server = request.scope.get("server")
    ip = server[0] if server else ""
    if not ip or ip in ("0.0.0.0", "::", "127.0.0.1", "::1") or not any(c.isdigit() for c in ip):
        from ..discover import primary_ip

        return primary_ip()
    return f"[{ip}]" if ":" in ip else ip


def main() -> None:
    """Entry point for termserv-web.service."""
    import argparse

    import uvicorn

    ap = argparse.ArgumentParser(prog="termserv-web")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=None, help="default: settings.web_port")
    ap.add_argument("--cert", default="/etc/termserv/web.crt")
    ap.add_argument("--key", default="/etc/termserv/web.key")
    ap.add_argument("--no-tls", action="store_true", help="plain HTTP (testing only)")
    args = ap.parse_args()
    store = service.Store()
    port = args.port or store.load().settings.web_port
    app = create_app(store, https_only=not args.no_tls)
    tls = {} if args.no_tls else {"ssl_certfile": args.cert, "ssl_keyfile": args.key}
    uvicorn.run(app, host=args.host, port=port, proxy_headers=False, server_header=False, **tls)


if __name__ == "__main__":
    main()
