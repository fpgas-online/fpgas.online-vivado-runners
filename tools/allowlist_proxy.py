#!/usr/bin/env python3
"""A logging CONNECT proxy for experiments. NOT the production proxy (that is squid).

    uv run python tools/allowlist_proxy.py --port 8876 --allow github.com \\
        --allow .githubusercontent.com --log tmp/proxy.log

Every CONNECT is logged as `ALLOW host:port` or `DENY host:port`. `--allow .x`
matches x and its subdomains; `--allow-all` allows and logs everything.
"""

import argparse
import select
import socket
import socketserver
import threading


def allowed(host: str, rules: list[str]) -> bool:
    for rule in rules:
        if rule.startswith("."):
            if host == rule[1:] or host.endswith(rule):
                return True
        elif host == rule:
            return True
    return False


class Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        line = self.rfile.readline().decode(errors="replace").split()
        while self.rfile.readline() not in (b"\r\n", b"\n", b""):
            pass
        if len(line) < 2 or line[0] != "CONNECT":
            self.wfile.write(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            return
        host, _, port = line[1].rpartition(":")
        ok = self.server.allow_all or allowed(host, self.server.rules)
        with self.server.lock, open(self.server.log, "a") as log:
            log.write(f"{'ALLOW' if ok else 'DENY'} {host}:{port}\n")
        if not ok:
            self.wfile.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            return
        try:
            upstream = socket.create_connection((host, int(port)), timeout=30)
        except OSError:
            self.wfile.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return
        self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        self.wfile.flush()
        pair = {self.connection: upstream, upstream: self.connection}
        with upstream:
            while True:
                ready, _, _ = select.select(list(pair), [], [], 300)
                if not ready:
                    return
                for sock in ready:
                    data = sock.recv(65536)
                    if not data:
                        return
                    pair[sock].sendall(data)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8876)
    parser.add_argument("--allow", action="append", default=[])
    parser.add_argument("--allow-all", action="store_true")
    parser.add_argument("--log", required=True)
    args = parser.parse_args()
    server = Server(("127.0.0.1", args.port), Handler)
    server.rules, server.allow_all, server.log, server.lock = args.allow, args.allow_all, args.log, threading.Lock()
    server.serve_forever()


if __name__ == "__main__":
    main()
