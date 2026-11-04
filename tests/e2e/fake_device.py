"""Pretend to be a network device on the far end of a virtual serial line.

Usage: fake_device.py <tty> <hostname>
Answers every line with "<hostname># you said: <line>" and a fresh prompt.
"""

import os
import sys
import termios
import tty

path, hostname = sys.argv[1], sys.argv[2]
fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
tty.setraw(fd)
attrs = termios.tcgetattr(fd)
attrs[3] &= ~termios.ECHO
termios.tcsetattr(fd, termios.TCSANOW, attrs)

buf = b""
while True:
    try:
        data = os.read(fd, 1024)
    except OSError:
        continue  # the other end may be briefly closed while conserver reopens it
    if not data:
        continue
    os.write(fd, data)  # echo like a real console
    buf += data
    while b"\r" in buf or b"\n" in buf:
        i = min(x for x in (buf.find(b"\r"), buf.find(b"\n")) if x >= 0)
        line, buf = buf[:i].strip(), buf[i + 1 :]
        reply = f"\r\n{hostname}# you said: {line.decode(errors='replace')}\r\n{hostname}> "
        os.write(fd, reply.encode())
