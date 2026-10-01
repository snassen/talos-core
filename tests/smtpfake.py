"""A fake SMTP server on the loopback, for the send tests and the demo. It delivers nothing.

It speaks enough SMTP for smtplib (EHLO, AUTH PLAIN and LOGIN, MAIL, RCPT, DATA, RSET, NOOP,
QUIT) and keeps what it was given in `received`: the envelope, the login and the bytes. It
never opens a connection anywhere, so no real mail can leave through it.

    with FakeSmtp() as smtp:
        ... SmtpTransport("127.0.0.1", smtp.port, "none", user, lambda: "pw") ...
        smtp.received[0].data
"""

from __future__ import annotations

import base64
import socketserver
import threading
from dataclasses import dataclass, field


@dataclass
class Received:
    mail_from: str
    rcpt_to: list[str]
    data: bytes
    user: str | None
    password: str | None


@dataclass
class _State:
    user: str | None = None
    password: str | None = None
    mail_from: str | None = None
    rcpt: list[str] = field(default_factory=list)


class FakeSmtp:
    def __init__(self, *, refuse: set[str] | None = None, fail_login: bool = False):
        self.received: list[Received] = []
        self.refuse = {r.lower() for r in (refuse or set())}
        self.fail_login = fail_login
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def reply(self, line: str) -> None:
                self.wfile.write((line + "\r\n").encode())

            def read(self) -> str:
                return self.rfile.readline().decode("utf-8", "replace").rstrip("\r\n")

            def handle(self) -> None:
                st = _State()
                self.reply("220 fake.smtp.test ESMTP ready")
                while True:
                    line = self.read()
                    if not line and self.rfile.closed:
                        return
                    cmd, _, arg = line.partition(" ")
                    cmd = cmd.upper()
                    if cmd in ("EHLO", "HELO"):
                        self.wfile.write(b"250-fake.smtp.test\r\n250-AUTH PLAIN LOGIN\r\n250-8BITMIME\r\n250 SMTPUTF8\r\n")
                    elif cmd == "AUTH":
                        mech, _, initial = arg.partition(" ")
                        if mech.upper() == "PLAIN":
                            if not initial:
                                self.reply("334 ")
                                initial = self.read()
                            parts = base64.b64decode(initial).split(b"\0")
                            st.user, st.password = parts[1].decode(), parts[2].decode()
                        else:
                            self.reply("334 VXNlcm5hbWU6")
                            st.user = base64.b64decode(self.read()).decode()
                            self.reply("334 UGFzc3dvcmQ6")
                            st.password = base64.b64decode(self.read()).decode()
                        if outer.fail_login:
                            self.reply("535 5.7.8 Username and Password not accepted")
                        else:
                            self.reply("235 2.7.0 Accepted")
                    elif cmd == "MAIL":
                        st.mail_from, st.rcpt = arg.split(":", 1)[1].strip().split(" ")[0].strip("<>"), []
                        self.reply("250 OK")
                    elif cmd == "RCPT":
                        addr = arg.split(":", 1)[1].strip().split(" ")[0].strip("<>")
                        if addr.lower() in outer.refuse:
                            self.reply("550 5.1.1 No such user")
                        else:
                            st.rcpt.append(addr)
                            self.reply("250 OK")
                    elif cmd == "DATA":
                        self.reply("354 End data with <CR><LF>.<CR><LF>")
                        chunks = []
                        while True:
                            raw = self.rfile.readline()
                            if raw in (b".\r\n", b".\n", b""):
                                break
                            chunks.append(raw[1:] if raw.startswith(b"..") else raw)
                        outer.received.append(Received(st.mail_from or "", list(st.rcpt), b"".join(chunks),
                                                       st.user, st.password))
                        self.reply("250 OK queued")
                    elif cmd in ("RSET", "NOOP"):
                        self.reply("250 OK")
                    elif cmd == "QUIT":
                        self.reply("221 Bye")
                        return
                    elif not line:
                        return
                    else:
                        self.reply("502 Command not implemented")

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "FakeSmtp":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()
