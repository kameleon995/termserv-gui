"""termserv web UI: FastAPI + server-rendered Jinja templates + htmx.

Runs as root (it edits /etc and restarts services) behind HTTPS. Only members
of settings.admin_group can log in, with their Linux password (PAM).
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
from starlette.middleware.sessions import SessionMiddleware

from .. import __version__, discover, model, service
from ..apply import ApplyError

HERE = Path(__file__).parent
SECRET_FILE = Path("/etc/termserv/web-secret")
PAM_SERVICE = "termserv-web"


def pam_authenticate(user: str, password: str) -> bool:
    import pam  # python-pam; imported lazily so tests need no PAM

    return pam.pam().authenticate(user, password, service=PAM_SERVICE)


def load_secret(path: Path = SECRET_FILE) -> str:
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        secret = secrets.token_hex(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o600)
        path.write_text(secret)
        return secret


class NotLoggedIn(Exception):
    pass


def create_app(
    store: service.Store | None = None,
    authenticate: Callable[[str, str], bool] = pam_authenticate,
    secret: str | None = None,
    https_only: bool = True,
) -> FastAPI:
    store = store or service.Store()
    app = FastAPI(title="termserv", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret or load_secret(),
        session_cookie="termserv_session",
        max_age=8 * 3600,
        same_site="strict",
        https_only=https_only,
    )
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals["version"] = __version__

    # --- helpers -----------------------------------------------------------

    def current_user(request: Request) -> str:
        user = request.session.get("user")
        if not user:
            raise NotLoggedIn()
        return user

    def csrf_token(request: Request) -> str:
        tok = request.session.get("csrf")
        if not tok:
            tok = request.session["csrf"] = secrets.token_urlsafe(32)
        return tok

    async def check_csrf(request: Request) -> None:
        sent = request.headers.get("x-csrf-token")
        if sent is None:
            form = await request.form()
            sent = form.get("csrf", "")
        if not hmac.compare_digest(str(sent), request.session.get("csrf", "")):
            raise HTTPException(403, "bad CSRF token, reload the page")

    def flash(request: Request, text: str, kind: str = "ok") -> None:
        request.session.setdefault("flash", []).append({"text": text, "kind": kind})

    def render(request: Request, name: str, **ctx) -> HTMLResponse:
        ctx.setdefault("user", request.session.get("user"))
        ctx["csrf"] = csrf_token(request)
        ctx["flashes"] = request.session.pop("flash", [])
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
        return render(request, "login.html", user=None)

    @app.post("/login")
    async def login(request: Request, username: str = Form(...), password: str = Form(...)):
        await check_csrf(request)
        username = username.strip()
        cfg = store.load()
        ok = await asyncio.to_thread(authenticate, username, password)
        if not ok or not service.is_admin(cfg, username):
            await asyncio.sleep(1.5)  # slow down guessing
            flash(request, f"Login failed. Use a Linux account in the '{cfg.settings.admin_group}' group.", "error")
            return back("/login")
        request.session.clear()
        request.session["user"] = username
        return back("/")

    @app.post("/logout")
    async def logout(request: Request):
        await check_csrf(request)
        request.session.clear()
        return back("/login")

    # --- ports -------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        current_user(request)
        cfg = store.load()
        return render(request, "ports.html", cfg=cfg, views=service.port_views(cfg),
                      services=service.services_status(), hostname=_hostname())

    @app.get("/partials/ports", response_class=HTMLResponse)
    async def ports_partial(request: Request):
        current_user(request)
        cfg = store.load()
        return render(request, "_ports_table.html", cfg=cfg, views=service.port_views(cfg), hostname=_hostname())

    @app.get("/ports/new", response_class=HTMLResponse)
    async def new_port(request: Request):
        current_user(request)
        cfg = store.load()
        port = model.Port(number=service.next_free_number(cfg))
        return render(request, "port_form.html", cfg=cfg, port=port, is_new=True,
                      devices=discover.usb_serial_devices(), bauds=model.BAUDS, parities=model.PARITIES)

    @app.get("/ports/{number}/edit", response_class=HTMLResponse)
    async def edit_port(request: Request, number: int):
        current_user(request)
        cfg = store.load()
        port = cfg.port(number)
        if port is None:
            raise HTTPException(404, "no such port")
        return render(request, "port_form.html", cfg=cfg, port=port, is_new=False,
                      devices=discover.usb_serial_devices(), bauds=model.BAUDS, parities=model.PARITIES)

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
                      devices=discover.usb_serial_devices(), bauds=model.BAUDS, parities=model.PARITIES)

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
        return back(request.headers.get("referer") or "/")

    # --- logs --------------------------------------------------------------

    @app.get("/ports/{number}/log", response_class=HTMLResponse)
    async def port_log(request: Request, number: int):
        current_user(request)
        cfg = store.load()
        port = cfg.port(number)
        if port is None:
            raise HTTPException(404, "no such port")
        return render(request, "log.html", cfg=cfg, port=port, log=service.log_tail(cfg, number))

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


def _hostname() -> str:
    import socket

    return socket.gethostname()


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
