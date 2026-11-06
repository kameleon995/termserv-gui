# Welcome email for console users

Copy the text below, replace the placeholders and send it to each new user. Send the
password separately (another channel or a second email) if your policy requires it.

| Placeholder | Replace with |
| --- | --- |
| `{{USER}}` | the user's Linux account name on the termserv box |
| `{{IP}}` | the termserv box's IP address or host name |
| `{{PASSWORD}}` | their initial password |
| `{{PORT_LIST}}` | which device is on which port (optional; delete the section if not needed) |
| `{{ADMIN_CONTACT}}` | who to ask for help |

---

**Subject:** Your serial console access

Hi,

You now have access to the serial consoles on our console server. You can reach a
device's console from any computer with an SSH client (Linux, macOS, and Windows 10/11
all have one built in).

**Your login**

- Server: `{{IP}}`
- Username: `{{USER}}`
- Password: `{{PASSWORD}}` (the console server gives no shell, so to change it, ask {{ADMIN_CONTACT}})

**How to connect**

Each console port has its own SSH port: port 1 is 2001, port 2 is 2002, and so on
(2000 + the port number). Open a terminal and type:

    ssh -p 2001 {{USER}}@{{IP}}

Enter your password, then press Enter once or twice to wake up the console.

Always include `{{USER}}@` in the command. Without it, the login fails even with the
right password. In PuTTY, use Connection type SSH, Host `{{IP}}`, Port 2001 (and so on).

**Ports**

{{PORT_LIST}}

**Useful keys** (press Ctrl-E, then c, then the key)

- `Ctrl-E c .` disconnect
- `Ctrl-E c f` take over typing if someone else is connected
- `Ctrl-E c l 0` send a break
- `Ctrl-E c ?` help

If someone else is already on a port, you can watch but not type until you take over
with `Ctrl-E c f`. Everything on the consoles is logged.

Questions or problems: {{ADMIN_CONTACT}}

---
