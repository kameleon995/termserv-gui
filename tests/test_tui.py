import asyncio

import pytest

from termserv import apply as applymod
from termserv import model, service
from termserv.tui import Confirm, LogScreen, PortForm, TermservTUI, UsersScreen

PORTS = """
settings: {base_port: 2000, ssh_group: serial}
ports:
  - {number: 1, name: router1, baud: 115200}
  - {number: 2, name: switch1, baud: 9600}
"""


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(model, "RELEASE_DIR", tmp_path / "released")
    (tmp_path / "ports.yaml").write_text(PORTS)
    return service.Store(tmp_path / "ports.yaml", applymod.Paths(
        conserver_cf=tmp_path / "conserver.cf", sshd_config=tmp_path / "sshd_config",
        udev_rules=tmp_path / "99-termserv.rules"))


def run(coro):
    return asyncio.run(coro)


def test_lists_ports_and_pending(store):
    async def go():
        app = TermservTUI(store, live=False)
        async with app.run_test(size=(140, 32)) as pilot:
            await pilot.pause()
            table = app.query_one("#ports")
            assert table.row_count == 2
            assert "router1" in str(table.get_row_at(0))
            assert "Press p to apply" in str(app.query_one("#status").render())
    run(go())


def test_add_port_through_form(store):
    async def go():
        app = TermservTUI(store, live=False)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.press("a")
            await pilot.pause()
            form = app.screen
            assert isinstance(form, PortForm)
            assert form.query_one("#number").value == "3"  # next free number
            form.query_one("#name").value = "fw1"
            form.query_one("#groups").value = "lab"
            form.query_one("#save").press()
            await pilot.pause()
            assert not isinstance(app.screen, PortForm)
            p = store.load().port(3)
            assert (p.name, p.groups) == ("fw1", ["lab"])
            assert app.query_one("#ports").row_count == 3
    run(go())


def test_form_shows_validation_error(store):
    async def go():
        app = TermservTUI(store, live=False)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.press("e")  # edit port 1
            await pilot.pause()
            form = app.screen
            form.query_one("#number").value = "2"  # clashes with port 2
            form.query_one("#save").press()
            await pilot.pause()
            assert isinstance(app.screen, PortForm)
            assert "already exists" in str(form.query_one("#error").render())
            assert store.load().port(2).name == "switch1"  # not overwritten
            assert store.load().port(1).name == "router1"
    run(go())


def test_delete_with_confirm(store):
    async def go():
        app = TermservTUI(store, live=False)
        async with app.run_test(size=(140, 32)) as pilot:
            await pilot.press("down", "d")
            await pilot.pause()
            assert isinstance(app.screen, Confirm)
            app.screen.query_one("#yes").press()
            await pilot.pause()
            assert [p.number for p in store.load().ports] == [1]
    run(go())


def test_release_and_grab(store, monkeypatch):
    monkeypatch.setattr(applymod, "reload_conserver", lambda: "reloaded")
    monkeypatch.setattr(applymod.shutil, "which", lambda name: None)

    async def go():
        app = TermservTUI(store, live=False)
        async with app.run_test(size=(140, 32)) as pilot:
            await pilot.press("r")
            await pilot.pause()
            app.screen.query_one("#yes").press()
            await pilot.pause()
            assert model.released_ports() == {1}
            assert "released" in str(app.query_one("#ports").get_row_at(0))
            await pilot.press("r")  # grab needs no confirmation
            await pilot.pause()
            assert model.released_ports() == set()
    run(go())


def test_log_and_users_screens(store, monkeypatch, tmp_path):
    cfg = store.load()
    cfg.settings.log_dir = str(tmp_path)
    store.save(cfg)
    (tmp_path / "port1.log").write_text("router1> show version\n")
    monkeypatch.setattr(service, "login_users", lambda: ["alice"])
    monkeypatch.setattr(service, "group_members", lambda g: set())
    calls = []
    monkeypatch.setattr(service, "set_membership", lambda cfg, u, g, m: calls.append((u, g, m)))

    async def go():
        app = TermservTUI(store, live=False)
        async with app.run_test(size=(140, 32)) as pilot:
            await pilot.press("l")
            await pilot.pause()
            assert isinstance(app.screen, LogScreen)
            await pilot.press("escape", "u")
            await pilot.pause()
            assert isinstance(app.screen, UsersScreen)
            await pilot.press("right", "space")
            await pilot.pause()
            assert calls == [("alice", "serial", True)]
    run(go())


def test_arrow_keys_move_between_fields(store):
    async def go():
        app = TermservTUI(store, live=False)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.press("e")
            await pilot.pause()
            form = app.screen
            form.query_one("#number").focus()
            await pilot.pause()
            await pilot.press("down")
            assert app.focused.id == "name"
            await pilot.press("down")
            assert app.focused.id == "usb_path"  # a closed dropdown does not swallow Down
            await pilot.press("down")
            assert app.focused.id == "device"
            await pilot.press("up", "up")
            assert app.focused.id == "name"
            # Enter opens a dropdown; arrows then move inside the list, not between fields
            sel = form.query_one("#baud")
            sel.focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert sel.expanded
            await pilot.press("down", "enter")
            await pilot.pause()
            assert not sel.expanded and app.focused is sel
            assert sel.value != 115200  # moved inside the list
            await pilot.press("down")
            assert app.focused.id == "parity"
    run(go())


def test_confirm_left_right(store):
    async def go():
        app = TermservTUI(store, live=False)
        async with app.run_test(size=(140, 32)) as pilot:
            await pilot.press("d")
            await pilot.pause()
            assert app.focused.id == "yes"
            await pilot.press("right")
            assert app.focused.id == "no"
            await pilot.press("left")
            assert app.focused.id == "yes"
            await pilot.press("right", "enter")
            await pilot.pause()
            assert not isinstance(app.screen, Confirm)
            assert len(store.load().ports) == 2  # "No" kept the port
    run(go())
