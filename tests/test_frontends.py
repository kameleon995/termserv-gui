"""Web UI / Cockpit on-off switches, web login settings, and the JSON CLI the
Cockpit plugin uses."""

import json
from pathlib import Path

import pytest

from termserv import apply as applymod
from termserv import cli, frontends, model, service

PORTS = """
settings: {base_port: 2000, ssh_group: serial}
ports:
  - {number: 1, name: router1}
"""


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(model, "RELEASE_DIR", tmp_path / "released")
    monkeypatch.setattr(frontends, "_systemd", lambda: False)  # never touch real services
    f = tmp_path / "ports.yaml"
    f.write_text(PORTS)
    (tmp_path / "cockpit" / "base1").mkdir(parents=True)  # pretend Cockpit is installed
    return service.Store(f, applymod.Paths(
        conserver_cf=tmp_path / "conserver.cf", sshd_config=tmp_path / "sshd_config",
        udev_rules=tmp_path / "99-termserv.rules", banner=tmp_path / "banner",
        cockpit_dir=tmp_path / "cockpit"))


def test_defaults():
    s = model.Settings()
    assert (s.web_enabled, s.cockpit_enabled) == (True, False)
    assert (s.web_idle_minutes, s.web_max_failures, s.web_lockout_minutes) == (60, 5, 10)


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "ports.yaml"


def test_first_install_leaves_cockpit_plugin_off(store):
    # install.sh starts from examples/ports.yaml (or no file at all) and runs apply,
    # which calls sync: even with Cockpit installed, the plugin must not appear.
    texts = [""] + ([EXAMPLE.read_text()] if EXAMPLE.exists() else [])
    for text in texts:
        store.ports_file.write_text(text)
        cfg = store.load()
        assert cfg.settings.cockpit_enabled is False
        frontends.sync(cfg, store.paths.cockpit_dir)
        assert not (store.paths.cockpit_dir / "termserv").exists()


@pytest.mark.parametrize("key,value,msg", [
    ("web_idle_minutes", "0", "1..1440"),
    ("web_lockout_minutes", "2000", "1..1440"),
    ("web_max_failures", "0", "1..100"),
    ("web_max_failures", "lots", "must be a number"),
    ("web_port", "1", "unknown setting"),
])
def test_save_settings_rejects_bad_values(store, key, value, msg):
    before = store.ports_file.read_text()
    with pytest.raises(model.ConfigError, match=msg):
        service.save_settings(store, {key: value})
    assert store.ports_file.read_text() == before


def test_save_settings_roundtrip(store):
    service.save_settings(store, {"web_idle_minutes": "15", "web_max_failures": 3, "web_lockout_minutes": "30"})
    s = store.load().settings
    assert (s.web_idle_minutes, s.web_max_failures, s.web_lockout_minutes) == (15, 3, 30)


def test_cockpit_plugin_installed_and_removed(store):
    plugin = store.paths.cockpit_dir / "termserv"
    log = service.save_settings(store, {"cockpit_enabled": True})
    assert (plugin / "manifest.json").exists() and (plugin / "termserv.js").exists()
    assert any("installed" in line for line in log)
    assert service.save_settings(store, {"cockpit_enabled": True}) == []  # already up to date
    log = service.save_settings(store, {"cockpit_enabled": False})
    assert not plugin.exists() and log == ["Cockpit plugin removed"]


def test_cockpit_plugin_without_cockpit(store):
    (store.paths.cockpit_dir / "base1").rmdir()
    log = service.save_settings(store, {"cockpit_enabled": "on"})
    assert "not installed" in log[0]
    assert store.load().settings.cockpit_enabled is True  # remembered for when it is installed


def test_plugin_manifest_is_valid():
    m = json.loads((frontends.PLUGIN_SOURCE / "manifest.json").read_text())
    assert m["menu"]["index"]["label"] == "Serial ports"
    html = (frontends.PLUGIN_SOURCE / "index.html").read_text()
    assert "../base1/cockpit.js" in html and "termserv.js" in html


def test_web_service_toggle_uses_systemctl(store, monkeypatch):
    calls = []

    class R:
        def __init__(self, out=""):
            self.stdout, self.stderr, self.returncode = out, "", 0

    def fake(*args):
        calls.append(args)
        return R("active" if args[0] == "is-active" else "enabled")

    monkeypatch.setattr(frontends, "_systemd", lambda: True)
    monkeypatch.setattr(frontends, "_systemctl", fake)
    assert service.save_settings(store, {"web_enabled": False}) == ["web UI stopped and disabled"]
    assert ("disable", "--now", frontends.WEB_SERVICE) in calls
    calls.clear()
    service.save_settings(store, {"web_enabled": True})
    assert ("enable", frontends.WEB_SERVICE) in calls and ("start", frontends.WEB_SERVICE) in calls


# --- termservctl -------------------------------------------------------------

def ctl(store, *args):
    return cli.main(["-f", str(store.ports_file), *args])


def test_cli_web_and_cockpit_switches(store, monkeypatch, capsys):
    monkeypatch.setattr(applymod.Paths, "__init__", _paths_like(store))
    assert ctl(store, "web", "disable") == 0
    assert store.load().settings.web_enabled is False
    assert ctl(store, "cockpit", "enable") == 0
    assert (store.paths.cockpit_dir / "termserv" / "manifest.json").exists()
    capsys.readouterr()
    assert ctl(store, "cockpit", "status") == 0
    assert "enabled, installed" in capsys.readouterr().out
    assert ctl(store, "web", "status") == 0
    assert "web UI: disabled" in capsys.readouterr().out


def test_cli_settings(store, monkeypatch, capsys):
    monkeypatch.setattr(applymod.Paths, "__init__", _paths_like(store))
    assert ctl(store, "settings", "--web-idle-minutes", "20", "--web-max-failures", "4",
               "--web-lockout-minutes", "15") == 0
    out = capsys.readouterr().out
    assert "20 min" in out and "15 min after 4 failed tries" in out
    assert ctl(store, "settings", "--web-idle-minutes", "0") == 2  # rejected
    assert store.load().settings.web_idle_minutes == 20


def test_cli_state_and_save_port(store, monkeypatch, capsys):
    monkeypatch.setattr(service.discover, "console_status", lambda port=782: {})
    monkeypatch.setattr(service.discover, "usb_serial_devices", lambda: [])
    assert ctl(store, "state", "--sockets-for", "0") == 0
    state = json.loads(capsys.readouterr().out)
    assert state["ports"][0]["name"] == "router1" and state["ports"][0]["ssh_port"] == 2001
    assert state["next_free"] == 2 and state["sockets"] == [] and "address" in state
    assert "hostname" not in state

    monkeypatch.setattr("sys.stdin", _stdin({"number": "2", "name": "sw1", "baud": "9600", "enabled": True}))
    assert ctl(store, "save-port") == 0
    assert store.load().port(2).baud == 9600
    # same validation as the web UI: renumbering onto an existing port is refused
    monkeypatch.setattr("sys.stdin", _stdin({"number": "1", "name": "sw1", "enabled": True}))
    assert ctl(store, "save-port", "--original", "2") == 2
    monkeypatch.setattr("sys.stdin", _stdin_raw("not json"))
    assert ctl(store, "save-port") == 2


def _stdin(data):
    return _stdin_raw(json.dumps(data))


def _stdin_raw(text):
    import io

    return io.StringIO(text)


def _paths_like(store):
    """termservctl builds a default Store; point its Paths at the test dir."""
    real_init = applymod.Paths.__init__
    target = store.paths

    def init(self, *a, **kw):
        real_init(self, *a, **kw)
        for k in ("conserver_cf", "sshd_config", "udev_rules", "banner", "cockpit_dir"):
            setattr(self, k, getattr(target, k))

    return init
