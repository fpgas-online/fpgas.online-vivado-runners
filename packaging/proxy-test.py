#!/usr/bin/python3
"""Start squid with host/squid.conf and check what it lets through.

Run as root in a clean debian:trixie container with the repository at the
current directory. The only change made to the configuration is the address:
the proxy listens on, and accepts clients from, 127.0.0.1.
"""

import pathlib
import resource
import shutil
import subprocess
import sys
import time

ALLOWED = [
    "https://github.com/",
    "https://api.github.com/zen",
    "https://codeload.github.com/",
    "https://pipelines.actions.githubusercontent.com/",
]
REFUSED = [
    "https://pypi.org/simple/",
    "https://files.pythonhosted.org/",
    "https://example.com/",
    "https://evilgithub.com/",
    "https://github.com.example.com/",
    "https://140.82.112.3/",
    "https://[2606:50c0:8000::154]/",
    "https://github.com:8443/",
    "http://github.com/",
]
PROXY = "http://127.0.0.1:3128"
LOG = pathlib.Path("/var/log/vivado-runners/proxy-access.log")


def status(url: str) -> tuple[str, str]:
    out = subprocess.run(
        ["curl", "-s", "-o", "/dev/null", "-m", "20", "-w", "%{http_connect} %{http_code}", "-x", PROXY, url],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    return out[0], out[1]


def main() -> int:
    etc = pathlib.Path("/etc/vivado-runners")
    etc.mkdir(parents=True, exist_ok=True)
    for name in ("allowed-hosts", "allowed-blob-regex"):
        shutil.copy2(f"host/{name}", etc / name)
    for directory in ("/var/log/vivado-runners", "/run/vivado-runners-proxy"):
        pathlib.Path(directory).mkdir(parents=True, exist_ok=True)
        shutil.chown(directory, "proxy", "proxy")
    conf = pathlib.Path("host/squid.conf").read_text()
    for old, new in (("http_port 192.168.76.1:3128", "http_port 127.0.0.1:3128"),
                     ("acl runners src 192.168.76.0/24", "acl runners src 127.0.0.1/32")):  # fmt: skip
        if old not in conf:
            sys.exit(f"host/squid.conf no longer contains {old!r}; update this test")
        conf = conf.replace(old, new)
    pathlib.Path("/root/squid-test.conf").write_text(conf)

    # A container's file-descriptor limit can be ~1e9, and squid sizes a table by it.
    resource.setrlimit(resource.RLIMIT_NOFILE, (4096, 4096))
    squid = subprocess.Popen(["squid", "--foreground", "-f", "/root/squid-test.conf"])
    time.sleep(5)
    if squid.poll() is not None:
        sys.exit(f"squid exited with {squid.returncode}")

    failed = 0
    try:
        for url in ALLOWED:
            connect, _ = status(url)
            ok = connect == "200"
            failed += not ok
            print(f"{'ok  ' if ok else 'FAIL'} allowed  {url} (CONNECT {connect})")
        for url in REFUSED:
            connect, http = status(url)
            ok = "403" in (connect, http) and "200" not in (connect, http)
            failed += not ok
            print(f"{'ok  ' if ok else 'FAIL'} refused  {url} (CONNECT {connect}, HTTP {http})")
    finally:
        squid.terminate()
        squid.wait(timeout=30)

    log = LOG.read_text()
    print(log)
    for needle in ("TCP_DENIED/403 CONNECT pypi.org:443", "127.0.0.1 TCP_TUNNEL/200 CONNECT github.com:443"):
        ok = needle in log
        failed += not ok
        print(f"{'ok  ' if ok else 'FAIL'} access log has: {needle}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
