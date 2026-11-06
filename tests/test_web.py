import re
import shutil

import pytest
from fastapi.testclient import TestClient

from termserv import apply as applymod
from termserv import model, service
from termserv.web.app import COOKIE, create_app
from termserv.web.sessions import LoginThrottle, SessionStore

PORTS = """
settings: {base_port: 2000, ssh_group: serial, admin_group: admins}
ports:
  - {number: 1, name: router1, device: /dev/null, baud: 115200}
  - {number: 2, name: switch1, baud: 9600, enabled: false}
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(model, "RELEASE_DIR", tmp_path / "released")
    (tmp_path / "ports.yaml").write_text(PORTS)
    store = service.Store(tmp_path / "ports.yaml", applymod.Paths(
        conserver_cf=tmp_path / "conserver.cf", sshd_config=tmp_path / "sshd_config",
        udev_rules=tmp_path / "99-termserv.rules", banner=tmp_path / "banner",
        cockpit_dir=tmp_path / "cockpit"))
    monkeypatch.setattr(service, "is_admin", lambda cfg, user: user == "boss")
    monkeypatch.setattr(service, "group_members", lambda g: {"boss"} if g == "serial" else set())
    monkeypatch.setattr(service, "login_users", lambda: ["boss", "eve"])
    app = create_app(store, authenticate=lambda u, p: p == "secret", https_only=False)
    return store, TestClient(app)


def token(client, url="/"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(url).text).group(1)


def login(client, user="boss", password="secret"):
    r = client.post("/login", data={"username": user, "password": password, "csrf": token(client, "/login")})
    return r


def test_requires_login(env):
    _, c = env
    for url in ("/", "/users", "/ports/1/edit", "/ports/1/log"):
        r = c.get(url)
        assert r.url.path == "/login", url
    r = c.get("/partials/ports", headers={"HX-Request": "true"})
    assert r.headers.get("HX-Redirect") == "/login"


def test_login_rejects_bad_password_and_non_admins(env, monkeypatch):
    import asyncio

    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))  # skip the anti-guessing delay
    _, c = env
    assert login(c, password="nope").url.path == "/login"
    r = login(c, user="eve")  # right password, not an admin
    assert r.url.path == "/login"
    assert "&#39;admins&#39; group" in r.text  # the error names the admin group
    assert login(c).url.path == "/"


def test_dashboard_lists_ports_and_pending_changes(env):
    _, c = env
    login(c)
    html = c.get("/").text
    assert "router1" in html and "2001" in html and "disabled" in html
    assert "Apply changes" in html  # nothing generated yet


def test_csrf_required(env):
    store, c = env
    login(c)
    r = c.post("/ports", data={"number": "5", "baud": "9600", "parity": "none", "csrf": "forged"})
    assert r.status_code == 403
    assert store.load().port(5) is None


def test_add_edit_delete_port(env):
    store, c = env
    login(c)
    tok = token(c)
    r = c.post("/ports", data={"csrf": tok, "number": "3", "name": "fw1", "baud": "9600",
                               "parity": "even", "enabled": "1", "groups": "lab, ops"})
    assert r.url.path == "/"
    p = store.load().port(3)
    assert (p.name, p.baud, p.parity, p.groups, p.enabled, p.ondemand) == ("fw1", 9600, "even", ["lab", "ops"], True, False)

    # renumber 3 -> 4 and turn on ondemand
    c.post("/ports/3", data={"csrf": tok, "number": "4", "name": "fw1", "baud": "9600", "parity": "even",
                             "enabled": "1", "ondemand": "1"})
    cfg = store.load()
    assert cfg.port(3) is None and cfg.port(4).ondemand and cfg.port(4).log

    # the form sends a hidden log=0 before the checkbox; unchecked turns logging off
    form = c.get("/ports/4/edit").text
    assert '<input type="hidden" name="log" value="0">' in form
    c.post("/ports/4", data={"csrf": tok, "number": "4", "name": "fw1", "baud": "9600", "parity": "even",
                             "enabled": "1", "log": "0"})
    assert store.load().port(4).log is False
    assert "no log" in c.get("/").text
    assert "Logging is off" in c.get("/ports/4/log").text
    c.post("/ports/4", data={"csrf": tok, "number": "4", "name": "fw1", "baud": "9600", "parity": "even",
                             "enabled": "1", "log": ["0", "1"]})
    assert store.load().port(4).log is True

    c.post("/ports/4/delete", data={"csrf": tok})
    assert store.load().port(4) is None


def test_invalid_input_shows_error_and_keeps_file(env):
    store, c = env
    login(c)
    before = store.ports_file.read_text()
    r = c.post("/ports", data={"csrf": token(c), "number": "1", "baud": "9600", "parity": "none"})
    assert "already exists" in r.text
    r = c.post("/ports", data={"csrf": token(c), "number": "7", "name": "bad name!", "baud": "9600", "parity": "none"})
    assert "may use letters" in r.text
    # renumbering onto an existing port must not overwrite it
    r = c.post("/ports/1", data={"csrf": token(c), "number": "2", "baud": "9600", "parity": "none", "enabled": "1"})
    assert "already exists" in r.text
    assert store.ports_file.read_text() == before


def test_release_and_grab(env, monkeypatch):
    store, c = env
    monkeypatch.setattr(applymod, "reload_conserver", lambda: "reloaded")
    monkeypatch.setattr(shutil, "which", lambda name: None)  # skip conserver -S
    login(c)
    tok = token(c)
    r = c.post("/ports/1/release", data={"csrf": tok})
    assert "released" in r.text
    assert model.released_ports() == {1}
    assert "console port1 {" not in store.paths.conserver_cf.read_text()
    c.post("/ports/1/grab", data={"csrf": tok})
    assert model.released_ports() == set()
    r = c.post("/ports/2/release", data={"csrf": tok})
    assert "disabled" in r.text  # error flashed, nothing changed


def test_log_page_strips_control_codes(env, tmp_path):
    store, c = env
    cfg = store.load()
    cfg.settings.log_dir = str(tmp_path)
    store.save(cfg)
    (tmp_path / "port1.log").write_bytes(b"\x1b[1mboot\x1b[0m ok\r\nrouter1> <script>\r\n")
    login(c)
    html = c.get("/ports/1/log").text
    assert "boot ok" in html and "\x1b" not in html
    assert "&lt;script&gt;" in html  # escaped


def test_users_page_and_toggle(env, monkeypatch):
    store, c = env
    calls = []
    monkeypatch.setattr(service, "set_membership", lambda cfg, u, g, m: calls.append((u, g, m)))
    login(c)
    html = c.get("/users").text
    assert "eve" in html and "boss" in html
    c.post("/users/eve/groups/serial", data={"csrf": token(c, "/users"), "member": "1"})
    assert calls == [("eve", "serial", True)]


def test_logout(env):
    _, c = env
    login(c)
    c.post("/logout", data={"csrf": token(c)})
    assert c.get("/").url.path == "/login"


# --- session hardening -------------------------------------------------------

class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clocked(env, tmp_path, monkeypatch):
    import asyncio

    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))
    store, _ = env
    clock = FakeClock()
    sessions = SessionStore(idle_seconds=3600, clock=clock)
    throttle = LoginThrottle(max_failures=3, window_seconds=600, clock=clock)
    app = create_app(store, authenticate=lambda u, p: p == "secret", https_only=True,
                     sessions=sessions, throttle=throttle)
    return TestClient(app, base_url="https://testserver"), clock, sessions


def test_cookie_is_browser_session_only_and_locked_down(clocked):
    c, _, _ = clocked
    r = c.get("/login")
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie
    assert "max-age" not in cookie and "expires" not in cookie  # dies with the browser


def test_session_id_changes_at_login_and_dies_at_logout(clocked):
    c, _, _ = clocked
    c.get("/login")
    before = c.cookies.get(COOKIE)
    login(c)
    after = c.cookies.get(COOKIE)
    assert after and after != before  # no session fixation
    c.post("/logout", data={"csrf": token(c)})
    # replaying the old cookie after logout gets nothing
    c.cookies.set(COOKIE, after)
    assert c.get("/").url.path == "/login"


def test_idle_timeout(clocked):
    c, clock, _ = clocked
    login(c)
    clock.t += 59 * 60
    assert c.get("/").url.path == "/"  # activity resets the idle timer
    clock.t += 59 * 60
    assert c.get("/").url.path == "/"
    clock.t += 61 * 60
    r = c.get("/")
    assert r.url.path == "/login" and "ended after inactivity" in r.text


def test_background_refresh_does_not_keep_session_alive(clocked):
    c, clock, _ = clocked
    login(c)
    bg = {"HX-Request": "true", "X-Termserv-Background": "1"}
    for _ in range(12):  # an open tab polling for an hour and a bit
        clock.t += 6 * 60
        r = c.get("/partials/ports", headers=bg)
    assert r.headers.get("HX-Redirect") == "/login"


def test_absolute_session_lifetime(clocked):
    c, clock, _ = clocked
    login(c)
    for _ in range(9):  # active every 55 minutes for over 8 hours
        clock.t += 55 * 60
        r = c.get("/")
    assert r.url.path == "/login"


def test_login_lockout(clocked):
    c, clock, _ = clocked
    for _ in range(3):
        login(c, password="bad")
    r = login(c)  # right password, but locked out
    assert r.url.path == "/login" and "Too many failed logins" in r.text
    clock.t += 601
    assert login(c).url.path == "/"


def test_security_headers(clocked):
    c, _, _ = clocked
    r = c.get("/login")
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp and "frame-ancestors 'none'" in csp
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["cache-control"] == "no-store"
    assert "<script>" not in r.text and "onsubmit" not in r.text


def test_session_store_caps_anonymous_sessions(monkeypatch):
    from termserv.web import sessions as smod

    monkeypatch.setattr(smod, "MAX_SESSIONS", 10)
    store = SessionStore()
    keep = store.new()
    keep.data["user"] = "boss"
    for _ in range(50):
        store.new()
    assert len(store) <= 11
    assert store.get(keep.id)[0] is keep  # logged-in sessions are never evicted for space


def test_connect_hint_shows_ip_not_hostname(env, monkeypatch):
    import socket

    from termserv import discover

    _, client = env
    login(client)
    monkeypatch.setattr(discover, "primary_ip", lambda fallback="<address>": "10.1.2.3")
    page = client.get("/").text
    assert "user@10.1.2.3" in page
    assert socket.gethostname() not in page


def test_connect_link_and_copy_button_no_web_shell(env, monkeypatch):
    from termserv import discover

    _, client = env
    login(client)
    monkeypatch.setattr(discover, "primary_ip", lambda fallback="<address>": "10.1.2.3")
    page = client.get("/").text
    # port 1 is enabled: the link opens the user's own SSH client, nothing runs in the browser
    assert 'href="ssh://boss@10.1.2.3:2001"' in page
    assert 'data-copy="ssh -p 2001 boss@10.1.2.3"' in page
    assert "ssh://boss@10.1.2.3:2002" not in page  # port 2 is disabled


def test_login_limits_follow_ports_yaml_without_restart(env, monkeypatch):
    import asyncio

    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))
    store, client = env
    service.save_settings(store, {"web_max_failures": "2", "web_lockout_minutes": "3", "web_idle_minutes": "7"})
    for _ in range(2):
        client.post("/login", data={"username": "boss", "password": "nope", "csrf": token(client, "/login")})
    r = client.post("/login", data={"username": "boss", "password": "secret", "csrf": token(client, "/login")})
    assert "Too many failed logins. Try again in 3 minute(s)." in r.text  # locked after 2, not 5
    assert client.app.state.sessions.idle_seconds == 7 * 60


def test_restart_services(env, monkeypatch):
    from termserv.web import app as appmod

    store, c = env
    login(c)
    calls = []
    monkeypatch.setattr(service, "restart_services", lambda s, web=True: calls.append(web) or ["restarted x"])
    monkeypatch.setattr(service, "restart_web", lambda: calls.append("web"))
    monkeypatch.setattr(appmod.asyncio, "sleep", lambda s: asyncio_noop())
    assert "Restart services" in c.get("/").text
    r = c.post("/restart", data={"csrf": token(c)})
    assert "Restarting services" in r.text and "restarted x" in r.text
    assert 'url=/login' in r.text
    assert calls == [False, "web"]  # others first, the web UI itself after answering


def test_restart_services_without_web_restart_or_on_error(env, monkeypatch):
    store, c = env
    login(c)
    cfg = store.load()
    cfg.settings.web_enabled = False
    store.save(cfg)
    monkeypatch.setattr(service, "restart_services", lambda s, web=True: ["restarted x"])
    r = c.post("/restart", data={"csrf": token(c)})
    assert r.url.path == "/" and "Services restarted: restarted x" in r.text

    def broken(s, web=True):
        raise applymod.ApplyError("conserver.service failed to restart")

    monkeypatch.setattr(service, "restart_services", broken)
    r = c.post("/restart", data={"csrf": token(c)})
    assert "failed to restart" in r.text


def test_restart_needs_csrf(env, monkeypatch):
    _, c = env
    login(c)
    monkeypatch.setattr(service, "restart_services", lambda s, web=True: pytest.fail("restarted"))
    assert c.post("/restart", data={"csrf": "nope"}).status_code == 403


async def asyncio_noop():
    return None
