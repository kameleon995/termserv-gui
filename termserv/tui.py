"""termserv TUI: manage ports from an SSH session (sudo termserv-tui).

Same operations as the web UI, through termserv.service.
"""

from __future__ import annotations

import os
import sys

from textual import on, work
from textual.app import App, ComposeResult, SuspendNotSupported
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Checkbox, DataTable, Footer, Header, Input, Label, RichLog, Select, Static

from . import __version__, discover, model, service
from .apply import ApplyError

STATE_STYLE = {"up": "green", "down": "red", "released": "cyan", "disabled": "dim", "idle": "dim", "unknown": "yellow"}


class SocketSelect(Select):
    """Select whose entries for sockets already used by another port are greyed out
    and cannot be picked (the core validation refuses them too)."""

    def __init__(self, options, *, disabled_values=(), **kwargs) -> None:
        self.disabled_values = set(disabled_values)
        super().__init__(options, **kwargs)

    def _setup_options_renderables(self) -> None:
        super()._setup_options_renderables()
        try:
            from textual.widgets._select import SelectOverlay

            overlay = self.query_one(SelectOverlay)
            for index, (_, value) in enumerate(self._options):
                if value in self.disabled_values:
                    overlay.disable_option_at_index(index)
        except Exception:  # Textual internals changed: labels still say "used by port N"
            pass


class ArrowNavigation:
    """Up/Down (and Left/Right on button rows) move between fields like Tab/Shift-Tab.

    Priority bindings run before the focused widget sees the key, so a closed
    dropdown does not swallow Up/Down (Enter or Space opens it). While a dropdown
    list is open, the keys go to the list instead.
    """

    NAV_BINDINGS = [
        Binding("down", "focus_next_field", "Next field", show=False, priority=True),
        Binding("up", "focus_previous_field", "Previous field", show=False, priority=True),
    ]

    def check_action(self, action: str, parameters) -> bool | None:
        if action in ("focus_next_field", "focus_previous_field"):
            focused = self.focused
            # an open dropdown list: let it handle the arrows
            if focused is not None and any(type(n).__name__ == "SelectOverlay" for n in focused.ancestors_with_self):
                return False
        return True

    def action_focus_next_field(self) -> None:
        self.focus_next()

    def action_focus_previous_field(self) -> None:
        self.focus_previous()


class Confirm(ArrowNavigation, ModalScreen[bool]):
    BINDINGS = [
        *ArrowNavigation.NAV_BINDINGS,
        Binding("right", "focus_next_field", show=False, priority=True),
        Binding("left", "focus_previous_field", show=False, priority=True),
    ]

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.question, id="question", classes="wrap")
            with Horizontal(classes="buttons"):
                yield Button("Yes", id="yes", variant="error")
                yield Button("No", id="no")

    @on(Button.Pressed)
    def done(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def key_escape(self) -> None:
        self.dismiss(False)


class PortForm(ArrowNavigation, ModalScreen[bool]):
    """Add or edit one port. Dismisses True when saved."""

    BINDINGS = [*ArrowNavigation.NAV_BINDINGS]

    def __init__(self, store: service.Store, port: model.Port, is_new: bool) -> None:
        super().__init__()
        self.store, self.port, self.is_new = store, port, is_new
        self.original = None if is_new else port.number
        self.sockets = service.socket_choices(store.load(), None if is_new else port.number, port.usb_path)

    def compose(self) -> ComposeResult:
        p = self.port
        title = "Add port" if self.is_new else f"Edit port {p.number}"
        with VerticalScroll(classes="dialog form"):
            yield Label(f"[b]{title}[/b]")
            with Grid(classes="fields"):
                yield Label("Port number")
                yield Input(str(p.number), id="number", type="integer", compact=True)
                yield Label("Name")
                yield Input(p.name, id="name", placeholder="router1", compact=True)
                yield Label("USB socket")
                options = [(c.label + (f"  (used by port {c.used_by})" if c.used_by else ""), c.id_path)
                           for c in self.sockets]
                yield SocketSelect(options, id="usb_path", prompt="none (use device path)",
                                   value=p.usb_path if p.usb_path else Select.NULL, compact=True,
                                   disabled_values=[c.id_path for c in self.sockets if c.used_by])
                yield Label("Device path")
                yield Input(p.device, id="device", placeholder=f"/dev/port{p.number} (default)", compact=True)
                yield Label("Baud rate")
                yield Select([(str(b), b) for b in model.BAUDS], id="baud", value=p.baud, allow_blank=False, compact=True)
                yield Label("Parity")
                yield Select([(x, x) for x in model.PARITIES], id="parity", value=p.parity, allow_blank=False, compact=True)
                yield Label("Flow control")
                yield Select([(x, x) for x in model.FLOWS], id="flow", value=p.flow, allow_blank=False, compact=True)
                yield Label("Allowed groups")
                yield Input(", ".join(p.groups), id="groups", placeholder="empty = everyone in the serial group", compact=True)
            yield Checkbox("Enabled", p.enabled, id="enabled", compact=True)
            yield Checkbox("Open only while someone is connected (no logging while idle)", p.ondemand, id="ondemand", compact=True)
            yield Checkbox("Log console output", p.log, id="log", compact=True)
            yield Static("", id="error", classes="error")
            with Horizontal(classes="buttons"):
                yield Button("Save", id="save", variant="primary")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(False)

    def key_escape(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#save")
    def save(self) -> None:
        usb = self.query_one("#usb_path", Select).value
        data = {
            "number": self.query_one("#number", Input).value,
            "name": self.query_one("#name", Input).value,
            "usb_path": "" if usb is Select.NULL else usb,
            "device": self.query_one("#device", Input).value,
            "baud": self.query_one("#baud", Select).value,
            "parity": self.query_one("#parity", Select).value,
            "flow": self.query_one("#flow", Select).value,
            "groups": self.query_one("#groups", Input).value,
            "enabled": self.query_one("#enabled", Checkbox).value,
            "ondemand": self.query_one("#ondemand", Checkbox).value,
            "log": self.query_one("#log", Checkbox).value,
        }
        try:
            service.save_port(self.store, data, original_number=self.original)
        except model.ConfigError as e:
            self.query_one("#error", Static).update(str(e))
            return
        self.dismiss(True)


class SettingsForm(ArrowNavigation, ModalScreen[list]):
    """Web UI / Cockpit on-off and web login limits. Dismisses with the change log
    (a list) when saved, None when cancelled."""

    BINDINGS = [*ArrowNavigation.NAV_BINDINGS]

    def __init__(self, store: service.Store) -> None:
        super().__init__()
        self.store = store

    def compose(self) -> ComposeResult:
        s = self.store.load().settings
        st = service.frontend_status(self.store)
        with VerticalScroll(classes="dialog form"):
            yield Label("[b]Settings[/b]")
            yield Checkbox(f"Web UI (https://{discover.primary_ip()}:{s.web_port})", s.web_enabled,
                           id="web_enabled", compact=True)
            yield Checkbox("Cockpit plugin" + ("" if st.cockpit_installed else "  (Cockpit not installed)"),
                           s.cockpit_enabled, id="cockpit_enabled", compact=True)
            yield Static("Web UI login", classes="hint section")
            with Grid(classes="fields"):
                yield Label("Idle timeout (min)")
                yield Input(str(s.web_idle_minutes), id="web_idle_minutes", type="integer", compact=True)
                yield Label("Failed tries")
                yield Input(str(s.web_max_failures), id="web_max_failures", type="integer", compact=True)
                yield Label("Lockout (min)")
                yield Input(str(s.web_lockout_minutes), id="web_lockout_minutes", type="integer", compact=True)
            yield Static("Changes take effect at once. The web UI keeps open sessions. "
                         "Cockpit has its own login and idle timeout (/etc/cockpit/cockpit.conf).",
                         classes="hint")
            yield Static("", id="error", classes="error")
            with Horizontal(classes="buttons"):
                yield Button("Save", id="save", variant="primary")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(None)

    def key_escape(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#save")
    def save(self) -> None:
        data = {k: self.query_one(f"#{k}", Checkbox).value for k in ("web_enabled", "cockpit_enabled")}
        data |= {k: self.query_one(f"#{k}", Input).value
                 for k in ("web_idle_minutes", "web_max_failures", "web_lockout_minutes")}
        try:
            log = service.save_settings(self.store, data)
        except model.ConfigError as e:
            self.query_one("#error", Static).update(str(e))
            return
        self.dismiss(log)


class LogScreen(Screen):
    BINDINGS = [Binding("escape,q", "app.pop_screen", "Back")]

    def __init__(self, store: service.Store, number: int) -> None:
        super().__init__()
        self.store, self.number = store, number
        self.last = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield RichLog(id="log", wrap=True, highlight=False, markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"Port {self.number} log"
        self.refresh_log()
        self.set_interval(3, self.refresh_log)

    def refresh_log(self) -> None:
        cfg = self.store.load()
        port = cfg.port(self.number)
        text = service.log_tail(cfg, self.number) or (
            "(logging is off for this port)" if port and not port.log else "(nothing logged yet)")
        if text != self.last:
            log = self.query_one(RichLog)
            log.clear()
            log.write(text)
            self.last = text


class UsersScreen(Screen):
    BINDINGS = [
        Binding("escape,q", "app.pop_screen", "Back"),
        Binding("space,enter", "toggle", "Add/remove"),
    ]

    def __init__(self, store: service.Store) -> None:
        super().__init__()
        self.store = store

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Space toggles group membership. Takes effect on the next SSH login. "
                     "Create accounts with 'sudo adduser'.", classes="hint")
        yield DataTable(id="users", cursor_type="cell")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Users"
        self.load()

    def load(self) -> None:
        table = self.query_one(DataTable)
        keep = table.cursor_coordinate
        table.clear(columns=True)
        cfg = self.store.load()
        self.groups, users = service.users_matrix(cfg)
        table.add_columns("User", *self.groups)
        for u in users:
            table.add_row(u.name, *[("[green]yes[/]" if g in u.groups else "[dim]no[/]") for g in self.groups], key=u.name)
        if users:
            table.cursor_coordinate = keep

    def action_toggle(self) -> None:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return
        row, col = table.cursor_coordinate
        if col == 0:
            self.notify("Move to a group column first")
            return
        user = table.coordinate_to_cell_key((row, 0)).row_key.value
        group = self.groups[col - 1]
        member = "no" in str(table.get_cell_at((row, col)))
        try:
            service.set_membership(self.store.load(), user, group, member)
        except model.ConfigError as e:
            self.notify(str(e), severity="error")
            return
        self.notify(f"{user} {'added to' if member else 'removed from'} {group}")
        self.load()


class TermservTUI(App):
    TITLE = "termserv"
    CSS = """
    #status { height: auto; min-height: 1; padding: 0 1; background: $boost; }
    /* Long messages wrap to the terminal width and reflow on resize. */
    .wrap, .error, .hint, #status { width: 100%; text-wrap: wrap; }
    .dialog > Label { width: 100%; }
    #status.pending { background: $warning 30%; }
    .hint { padding: 0 1; color: $text-muted; }
    .dialog { width: 90; max-width: 95%; height: auto; max-height: 90%; padding: 1 2; border: thick $primary; background: $surface; }
    Confirm, PortForm, SettingsForm { align: center middle; }
    .section { margin-top: 1; }
    .fields { grid-size: 2; grid-columns: 18 1fr; grid-rows: 1; grid-gutter: 1 1; height: auto; margin: 1 0; }
    .fields Input, .fields Select { background: $boost; }
    .buttons { height: auto; margin-top: 1; }
    .buttons Button { margin-right: 2; }
    .error { color: $error; height: auto; }
    DataTable { height: 1fr; }
    """
    BINDINGS = [
        Binding("a", "add", "Add"),
        Binding("e,enter", "edit", "Edit"),
        Binding("d", "delete", "Delete"),
        Binding("r", "release", "Release/grab"),
        Binding("c", "connect", "Connect"),
        Binding("l", "log", "Log"),
        Binding("u", "users", "Users"),
        Binding("s", "settings", "Settings"),
        Binding("p", "apply", "Apply"),
        Binding("R", "restart", "Restart services"),
        Binding("f5", "refresh", "Refresh", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, store: service.Store | None = None, live: bool = True) -> None:
        super().__init__()
        self.store = store or service.Store()
        self.live = live
        self.views: list[service.PortView] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="ports", cursor_type="row", zebra_stripes=True)
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = f"v{__version__}"
        table = self.query_one("#ports", DataTable)
        table.add_columns("Port", "SSH", "Name", "Adapter", "Line", "Access", "State", "Connected")
        self.action_refresh()
        self.set_interval(5, self.action_refresh)

    # --- data --------------------------------------------------------------

    def action_refresh(self) -> None:
        try:
            cfg = self.store.load()
        except model.ConfigError as e:
            self.query_one("#status", Static).update(f"ports.yaml is invalid: {e}")
            return
        self.views = service.port_views(cfg, live=self.live)
        table = self.query_one("#ports", DataTable)
        row = table.cursor_row
        table.clear()
        for v in self.views:
            p = v.port
            device = (f"{v.socket}" + (f" → {v.tty}" if v.tty else "")) if v.socket else p.device_path()
            if not v.device_present and v.state != "disabled":
                device += "  [red]empty socket[/]" if v.socket else "  [red]missing[/]"
            line = f"{p.baud} 8{p.parity[0].upper()}1" + (f" {p.flow}" if p.flow != "none" else "") + ("  on demand" if p.ondemand else "") + ("  no log" if not p.log else "")
            state = f"[{STATE_STYLE.get(v.state, '')}]{v.state}[/]"
            table.add_row(str(p.number), str(v.ssh_port), p.name or "-", device, line,
                          ", ".join(p.groups) or cfg.settings.ssh_group, state, ", ".join(v.users) or "-",
                          key=str(p.number))
        if self.views:
            table.move_cursor(row=min(row, len(self.views) - 1))
        pending = service.pending_changes(self.store)
        status = self.query_one("#status", Static)
        status.set_class(bool(pending), "pending")
        status.update(f"Unapplied changes in {', '.join(pending)}. Press p to apply." if pending
                      else f"{len(self.views)} port(s). Connect with ssh -p {cfg.settings.base_port}+N user@{discover.primary_ip()}")

    def selected(self) -> service.PortView | None:
        table = self.query_one("#ports", DataTable)
        if not self.views or table.cursor_row < 0:
            return None
        return self.views[min(table.cursor_row, len(self.views) - 1)]

    def _run(self, fn, ok: str) -> None:
        try:
            fn()
        except (model.ConfigError, ApplyError) as e:
            self.notify(str(e), severity="error", timeout=8)
        else:
            self.notify(ok)
        self.action_refresh()

    # --- actions -----------------------------------------------------------

    def action_add(self) -> None:
        cfg = self.store.load()
        port = model.Port(number=service.next_free_number(cfg))
        self.push_screen(PortForm(self.store, port, is_new=True), self._after_form)

    def action_edit(self) -> None:
        v = self.selected()
        if v:
            self.push_screen(PortForm(self.store, v.port, is_new=False), self._after_form)

    def _after_form(self, saved: bool | None) -> None:
        if saved:
            self.notify("Saved. Press p to apply.")
        self.action_refresh()

    def action_delete(self) -> None:
        v = self.selected()
        if not v:
            return

        def go(yes: bool | None) -> None:
            if yes:
                self._run(lambda: service.delete_port(self.store, v.number), f"Port {v.number} deleted. Press p to apply.")

        self.push_screen(Confirm(f"Delete port {v.number}{' (' + v.port.name + ')' if v.port.name else ''}?"), go)

    def action_release(self) -> None:
        v = self.selected()
        if not v:
            return
        if v.state == "released":
            self._run(lambda: service.grab(self.store, v.number), f"Port {v.number} is back under termserv")
            return

        def go(yes: bool | None) -> None:
            if yes:
                self._run(lambda: service.release(self.store, v.number),
                          f"Port {v.number} released; press r again to grab it back")

        self.push_screen(Confirm(f"Release port {v.number}? Anyone connected is disconnected "
                                 "and the device is freed for local tools."), go)

    @work(thread=True, exclusive=True)
    def action_apply(self) -> None:
        try:
            log = service.apply_changes(self.store)
        except (model.ConfigError, ApplyError) as e:
            self.call_from_thread(self.notify, str(e), severity="error", timeout=10)
        else:
            self.call_from_thread(self.notify, "Applied: " + "; ".join(log), timeout=6)
        self.call_from_thread(self.action_refresh)

    def action_restart(self) -> None:
        def go(yes: bool | None) -> None:
            if yes:
                self._restart()

        self.push_screen(Confirm(service.restart_question(service.pending_changes(self.store))), go)

    @work(thread=True, exclusive=True)
    def _restart(self) -> None:
        self.call_from_thread(self.notify, "Restarting services...")
        try:
            log = service.restart_services(self.store)
        except (model.ConfigError, ApplyError) as e:
            self.call_from_thread(self.notify, str(e), severity="error", timeout=10)
        else:
            self.call_from_thread(self.notify, "Services restarted: " + "; ".join(log), timeout=6)
        self.call_from_thread(self.action_refresh)

    def action_connect(self) -> None:
        """Open the selected port's console in this terminal; the TUI comes back when
        the session ends (Ctrl-E c .)."""
        v = self.selected()
        if not v:
            return
        try:
            cfg = self.store.load()
            user = service.connect_user()
            service.connect_command(cfg, v.number, user)  # same checks, before leaving the screen
        except model.ConfigError as e:
            self.notify(str(e), severity="error", timeout=8)
            return
        try:
            with self.suspend():
                print(f"[termserv] port {v.number} as {user}; Ctrl-E c . disconnects and returns to the TUI",
                      flush=True)
                code = service.connect(cfg, v.number, user)
        except SuspendNotSupported:
            self.notify("This terminal cannot hand over to ssh; run: termservctl connect "
                        f"{v.number}", severity="error", timeout=8)
            return
        except model.ConfigError as e:
            self.notify(str(e), severity="error", timeout=8)
            return
        if code == 130:
            self.notify(f"Connection to port {v.number} cancelled")
        elif code not in (0, None):
            self.notify(f"ssh ended with exit code {code}", severity="warning")
        self.action_refresh()

    def action_log(self) -> None:
        v = self.selected()
        if v:
            self.push_screen(LogScreen(self.store, v.number))

    def action_users(self) -> None:
        self.push_screen(UsersScreen(self.store))

    def action_settings(self) -> None:
        def done(log: list | None) -> None:
            if log is not None:
                self.notify("Settings saved" + (": " + "; ".join(log) if log else ""), timeout=6)

        self.push_screen(SettingsForm(self.store), done)


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="termserv-tui", description="Manage termserv serial ports")
    ap.add_argument("-f", "--file", default=str(model.DEFAULT_PORTS_FILE), help="ports.yaml path")
    args = ap.parse_args(argv)
    if os.geteuid() != 0:
        print("termserv-tui changes system config; run it with sudo", file=sys.stderr)
        return 1
    TermservTUI(service.Store(args.file)).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
