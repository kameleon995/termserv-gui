"""End-to-end test: ssh -p 2001 -> termserv sshd -> serial-attach -> conserver -> virtual serial port.

DESTRUCTIVE: writes /etc/termserv, creates users, starts daemons. Run only in a
throwaway container or VM, as root:

    TERMSERV_E2E=1 python3 -m pytest -v tests/e2e

Uses socat pty pairs as serial ports, so no hardware is needed.
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pexpect = pytest.importorskip("pexpect")

pytestmark = pytest.mark.skipif(
    os.environ.get("TERMSERV_E2E") != "1" or os.geteuid() != 0,
    reason="set TERMSERV_E2E=1 and run as root in a throwaway container",
)

REPO = Path(__file__).resolve().parents[2]
WORK = Path("/tmp/termserv-e2e")
PORTS_YAML = """
settings:
  base_port: 2000
  ssh_group: serial
  log_dir: /var/log/conserver
ports:
  - {number: 1, name: router1, device: /dev/port1, baud: 115200}
  - {number: 2, name: switch1, device: /dev/port2, baud: 9600, groups: [serial-lab], ondemand: true}
  - {number: 3, name: spare, device: /dev/port3, enabled: false}
"""
USERS = {"alice": "serial", "bob": "serial", "carol": "serial-lab"}


def sh(*cmd, check=True):
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def wait_port(port, timeout=10):
    import socket

    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise TimeoutError(f"port {port} never opened")


@pytest.fixture(scope="module")
def stack():
    procs = []
    shutil.rmtree(WORK, ignore_errors=True)
    shutil.rmtree("/run/termserv/released", ignore_errors=True)  # leftovers from an aborted run
    WORK.mkdir(parents=True)
    os.makedirs("/run/sshd", exist_ok=True)
    os.makedirs("/var/log/conserver", exist_ok=True)
    sh("ssh-keygen", "-A")

    # users + keys
    for g in ("serial", "serial-lab"):
        sh("groupadd", "-f", g)
    for user, group in USERS.items():
        sh("useradd", "-m", "-s", "/bin/bash", "-G", group, user, check=False)
        key = WORK / f"{user}_key"
        sh("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key))
        home = Path(f"/home/{user}/.ssh")
        home.mkdir(mode=0o700, exist_ok=True)
        (home / "authorized_keys").write_text((key.with_suffix(".pub")).read_text())
        shutil.chown(home, user, user)
        shutil.chown(home / "authorized_keys", user, user)
        # unlock the account (useradd leaves it locked with "!", which sshd refuses)
        sh("usermod", "-p", "*", user)

    # virtual serial ports: /dev/portN <-> WORK/farN, with a fake device on the far end
    for n, host in ((1, "router1"), (2, "switch1")):
        procs.append(subprocess.Popen([
            "socat", f"pty,raw,echo=0,link=/dev/port{n}", f"pty,raw,echo=0,link={WORK}/far{n}",
        ]))
    for n in (1, 2):
        for _ in range(50):
            if Path(f"/dev/port{n}").exists() and Path(f"{WORK}/far{n}").exists():
                break
            time.sleep(0.1)
    for n, host in ((1, "router1"), (2, "switch1")):
        procs.append(subprocess.Popen([sys.executable, str(REPO / "tests/e2e/fake_device.py"), f"{WORK}/far{n}", host]))

    # install helper + generate config through the real CLI
    os.makedirs("/usr/local/lib/termserv", exist_ok=True)
    shutil.copy(REPO / "scripts/serial-attach", "/usr/local/lib/termserv/serial-attach")
    os.chmod("/usr/local/lib/termserv/serial-attach", 0o755)
    os.makedirs("/etc/termserv", exist_ok=True)
    Path("/etc/termserv/ports.yaml").write_text(PORTS_YAML)
    Path("/etc/termserv/conserver.passwd").touch()
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    r = subprocess.run([sys.executable, "-m", "termserv.cli", "-f", "/etc/termserv/ports.yaml", "apply", "--no-reload"],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr

    # start daemons the way the systemd units do
    procs.append(subprocess.Popen([
        "conserver", "-M", "127.0.0.1", "-C", "/etc/termserv/conserver.cf",
        "-P", "/etc/termserv/conserver.passwd", "-o", "-O", "1",
    ], stdout=open(WORK / "conserver.log", "w"), stderr=subprocess.STDOUT))
    wait_port(782)
    procs.append(subprocess.Popen(["/usr/sbin/sshd", "-D", "-e", "-f", "/etc/termserv/sshd_config"],
                                  stdout=open(WORK / "sshd.log", "w"), stderr=subprocess.STDOUT))
    wait_port(2001)
    wait_port(2002)
    yield
    for p in reversed(procs):
        p.terminate()
    for p in procs:
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()


def ssh(user, port, *extra, command=()):
    return [
        "ssh", "-p", str(port), "-i", str(WORK / f"{user}_key"),
        "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
        "-o", "BatchMode=yes", "-o", "LogLevel=ERROR", *extra, f"{user}@127.0.0.1", *command,
    ]


def attach(user, port):
    cmd = ssh(user, port, "-tt")
    child = pexpect.spawn(cmd[0], cmd[1:], timeout=10, encoding="utf-8")
    child.expect(r"\[termserv\] port\d")
    child.expect(r"\[(Enter `\^Ec\?' for help|attached|spy|no, \S+ is attached)")
    child.logfile_read = open(WORK / f"{user}-{port}.transcript", "a")
    return child


def detach(child):
    child.send("\x05c.")
    child.expect(pexpect.EOF)


def test_port_1_reaches_serial_port_1(stack):
    c = attach("alice", 2001)
    c.send("show version\r")
    c.expect("router1# you said: show version")
    detach(c)


def test_port_2_reaches_serial_port_2(stack):
    c = attach("carol", 2002)
    c.send("hello\r")
    c.expect("switch1# you said: hello")
    detach(c)


def test_per_port_group_restriction(stack):
    # alice is in "serial" but port 2 only allows "serial-lab"
    r = subprocess.run(ssh("alice", 2002, "-tt"), capture_output=True, text=True, timeout=15)
    assert r.returncode == 255
    assert "Permission denied" in r.stderr


def test_disabled_port_not_listening(stack):
    r = subprocess.run(ssh("alice", 2003, "-tt"), capture_output=True, text=True, timeout=15)
    assert r.returncode == 255 and "refused" in r.stderr.lower()


def test_no_shell_or_commands(stack):
    r = subprocess.run(ssh("alice", 2001, command=("id",)), capture_output=True, text=True, timeout=15)
    assert "uid=" not in r.stdout
    assert "terminal is required" in r.stderr


def test_no_port_forwarding(stack):
    # -W would let a user reach conserver directly; DisableForwarding must stop it
    r = subprocess.run(ssh("alice", 2001, "-W", "127.0.0.1:782"), capture_output=True, text=True,
                       timeout=15, input="")
    assert r.returncode != 0


def test_second_user_watches_read_only(stack):
    a = attach("alice", 2001)
    b = attach("bob", 2001)
    # conserver tells bob someone else holds write access, so he is read-only
    b.expect("alice@localhost is attached")
    a.send("ping\r")
    a.expect("router1# you said: ping")
    b.expect("router1# you said: ping")  # bob sees alice's session live
    detach(b)
    detach(a)


def test_session_is_logged(stack):
    c = attach("alice", 2001)
    c.send("log-me-please\r")
    c.expect("you said: log-me-please")
    detach(c)
    time.sleep(0.5)
    assert "log-me-please" in Path("/var/log/conserver/port1.log").read_text()


def holders(dev):
    """conserver PIDs that have the device open (pty slave behind /dev/portN).

    socat itself also keeps the slave open, so only conserver processes count.
    """
    target = os.path.realpath(dev)
    pids = set()
    for fd in Path("/proc").glob("[0-9]*/fd/*"):
        try:
            if os.path.realpath(fd) == target:
                pid = fd.parts[2]
                if Path(f"/proc/{pid}/comm").read_text().strip() == "conserver":
                    pids.add(int(pid))
        except OSError:
            pass
    return pids


def ctl(*args):
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    r = subprocess.run([sys.executable, "-m", "termserv.cli", "-f", "/etc/termserv/ports.yaml", *args],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


def wait_for(cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.2)
    return False


def test_release_frees_device_and_grab_takes_it_back(stack):
    assert holders("/dev/port1"), "conserver should hold port 1 while it is managed"
    ctl("release", "1")
    assert wait_for(lambda: not holders("/dev/port1")), "device still held after release"
    assert "released" in ctl("list")
    # SSH users get a clear message instead of a hang
    r = subprocess.run(ssh("alice", 2001, "-tt"), capture_output=True, text=True, timeout=15)
    assert "released for local use" in r.stdout + r.stderr  # -tt: stderr arrives on the pty
    # port 2 is untouched by releasing port 1
    c = attach("carol", 2002)
    c.send("still-here\r")
    c.expect("switch1# you said: still-here")
    detach(c)
    ctl("grab", "1")
    assert wait_for(lambda: bool(holders("/dev/port1"))), "device not reopened after grab"
    c = attach("alice", 2001)
    c.send("back\r")
    c.expect("router1# you said: back")
    detach(c)


def test_ondemand_port_only_open_while_connected(stack):
    assert wait_for(lambda: not holders("/dev/port2")), "ondemand port held with nobody attached"
    c = attach("carol", 2002)
    c.send("hi\r")
    c.expect("switch1# you said: hi")
    assert holders("/dev/port2")
    detach(c)
    assert wait_for(lambda: not holders("/dev/port2")), "ondemand port not closed after last user left"


def test_banner_tells_user_to_give_a_username(stack):
    # what a user sees after `ssh -p 2001 host` from a machine whose local user doesn't exist here
    cmd = ssh("alice", 2001)
    cmd[cmd.index("LogLevel=ERROR")] = "LogLevel=INFO"  # ssh's default; ERROR hides banners
    cmd[cmd.index("alice@127.0.0.1")] = "nosuchuser@127.0.0.1"
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    assert r.returncode == 255
    assert "ssh <username>@" in r.stderr and "login will fail" in r.stderr


# --- escape commands that would leave the port -------------------------------

def test_escape_cannot_run_a_local_command(stack):
    # Ctrl-E c | asks the console client to run a command on this server: a shell
    # escape past ForceCommand. conserver's "limited *" refuses it.
    marker = Path("/tmp/termserv-e2e-escape")
    marker.unlink(missing_ok=True)
    c = attach("alice", 2001)
    c.send("\x05c|")
    c.expect("unknown -- use")
    c.send(f"id > {marker}\r")
    c.expect("router1# you said: id >")  # it went to the device instead
    detach(c)
    assert not marker.exists()


def test_escape_cannot_switch_to_another_port(stack):
    # Ctrl-E c ; would hop to any console, past port 2's group restriction
    c = attach("alice", 2001)
    c.send("\x05c;")
    c.expect("unknown -- use")
    c.send("port2\r")
    c.expect("router1# you said: port2")
    detach(c)


def test_escape_cannot_suspend_client(stack):
    c = attach("alice", 2001)
    c.send("\x05cz")
    c.expect("unknown -- use")
    detach(c)


def test_allowed_escapes_still_work(stack):
    a = attach("alice", 2001)
    b = attach("bob", 2001)
    b.expect("alice@localhost is attached")
    b.send("\x05cf")  # force write access
    b.expect(r"bumped alice@localhost")
    b.send("\x05cl0")  # serial break
    b.expect(r"\]")
    b.send("\x05c?")
    b.expect("disconnect")
    b.send("typed-after-break\r")
    b.expect("router1# you said: typed-after-break")
    detach(b)
    detach(a)
