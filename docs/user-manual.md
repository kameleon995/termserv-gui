# termserv user manual

termserv gives you the serial console of lab and network devices (routers, switches,
servers, boards) over SSH. You don't need any special software: any SSH client works.

This manual is for people who connect to consoles. If you install or run the
termserv box, see the [administrator guide](admin-guide.md).

## What you need

From your administrator:

- the **address** of the termserv box (an IP address or host name),
- your **username** and **password** (or permission to log in with your SSH key),
- the list of **ports**: which device is on which port number.

Each port has its own SSH port number: **port N is SSH port 2000 + N**. Port 1 is
2001, port 2 is 2002, port 12 is 2012.

## Connecting

### Linux, macOS, Windows 10/11 (built-in ssh)

Open a terminal (on Windows: PowerShell or Windows Terminal) and type:

```sh
ssh -p 2001 alice@192.0.2.10
```

Replace `alice` with your username, `192.0.2.10` with the termserv address and `2001`
with 2000 + the port number. Enter your password when asked.

> **Always type your username and `@`.** Without it, ssh sends the name you are logged
> in as on your own computer, and the login fails with "Permission denied" even if the
> password is right.

The first time you connect, ssh asks you to confirm the server's fingerprint; type
`yes`. You will then see:

```
[termserv] port1 on consolehost. Escape: Ctrl-E c ?  help, Ctrl-E c .  disconnect
```

You are now on the device's console. **Press Enter** once or twice to get a prompt;
a quiet console prints nothing until you do.

### PuTTY (Windows)

1. **Host Name**: the termserv address. **Port**: 2000 + the port number.
   **Connection type**: SSH.
2. Under **Connection → Data**, set **Auto-login username** to your username
   (optional, saves typing).
3. Click **Open**, log in, and press Enter.

Choose SSH, not "Serial": the serial settings (speed, parity) are already set on the
termserv side.

### From the web UI

Administrators can also use the **Connect** button next to each port in the web UI
or Cockpit. It opens an `ssh://` link in your computer's SSH client. **Copy ssh**
copies the command to paste into a terminal.

### With an SSH key

If your administrator has added your public key, ssh logs you in without a password.
To save typing, add the box to `~/.ssh/config`:

```
Host lab-console
    HostName 192.0.2.10
    User alice
```

Then `ssh -p 2001 lab-console` is enough.

## Changing your password

The console server never gives you a shell, so you can't run `passwd` through it.
Ask your administrator to change your password, or use an SSH key instead.

## Using the console

Everything you type goes to the device, exactly as if you were plugged in with a
console cable. termserv commands start with the escape sequence **Ctrl-E, then c**,
followed by one key:

| Keys | What it does |
| --- | --- |
| `Ctrl-E c .` | disconnect (leave the console) |
| `Ctrl-E c ?` | list all escape commands |
| `Ctrl-E c f` | take over write access from whoever has it |
| `Ctrl-E c w` | show who else is connected |
| `Ctrl-E c r` | replay the last lines of output |
| `Ctrl-E c l 0` | send a serial break (for ROMMON, password recovery, magic SysRq) |

Hold Ctrl, press E, release both, then press c, then the key. Closing the terminal
window also disconnects you; the device does not notice.

### Sharing a port

Several people can be on the same port at once:

- The **first** person to connect can type. Everyone who joins later **watches** in
  read-only mode and sees the same output live.
- To type while someone else has write access, use `Ctrl-E c f`. They drop to
  read-only. Tell them first.
- When the writer disconnects, the next person can take over with `Ctrl-E c f`.

### Logging

Ports are logged on the termserv box by default, including what you type, with
timestamps. Assume a password you type at a device's console is recorded.
Administrators can read the logs; ask them if you need to see what happened on a
port while you were away.

An administrator can turn logging off for a single port. Nothing done on such a
port is recorded, so there is also nothing to look up later. In the web UI, TUI and
Cockpit those ports show **no log** next to their line settings. Administrators can
also clear a port's log, so don't rely on an old log still being there.

## Common problems

| You see | What to do |
| --- | --- |
| `Permission denied` | Check you typed `username@` before the address, and the password. If it still fails, you may not have access to that port; ask your administrator. |
| `Connection refused` or a timeout | Check the address and the port number (2000 + N). The port may be disabled, or a firewall is in the way. |
| Connected, but the screen stays blank | Press Enter a few times. The device may be off or stuck; try `Ctrl-E c l 0` only if you know the device handles a break safely. |
| Garbled characters | The port's speed doesn't match the device. Ask your administrator to check the baud rate. |
| Your typing does nothing | Someone else has write access; `Ctrl-E c w` shows who, `Ctrl-E c f` takes over. |
| `port N is released for local use` | An administrator is using the port directly (cable test, firmware update). Try again later. |
| `a terminal is required (use ssh -t)` | Your client didn't ask for a terminal. Add `-t`: `ssh -t -p 2001 alice@192.0.2.10`. |

## Good habits

- Log out of the device (`exit`, `logout`, `end`) before you disconnect, so the next
  person doesn't land in your privileged session.
- Disconnect with `Ctrl-E c .` when you are done, so others can get write access.
- Don't send a break to a production device unless you mean it: on many devices it
  drops into a boot monitor.
