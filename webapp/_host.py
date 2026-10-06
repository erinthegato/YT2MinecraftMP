"""_host.py - serve the converter to a phone, or to the whole internet.

``python -m webapp.app`` binds ``127.0.0.1``, which is exactly right for the
desktop shortcut and exactly wrong for a phone.  This helper is the other half
of that choice: it widens the bind, opens the Windows Firewall for the port,
prints the addresses to type on the phone, and - when cloudflared is around -
also puts the converter on a public https URL that works from anywhere, cellular
included.

    python -m webapp._host                  # this network: Wi-Fi or a hotspot
    python -m webapp._host --lan-only       # the same, with no tunnel attempt
    python -m webapp._host --tunnel         # also ask cloudflared for a URL
    python -m webapp._host --fetch-cloudflared   # put cloudflared in bin/

The serving itself is not written here: the app is started through
``webapp.app``'s own ``main()``, so the desktop and the phone share one code
path.

Environment:
    PORT                listen port (default 8000), the same as app.py
    YT2DISC_WEB_HOST    bind address; 0.0.0.0 here, not 127.0.0.1
    YT2DISC_WEB_TOKEN   shared key; an exported one wins over --key's default
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for _entry in (HERE, ROOT):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

DEFAULT_PORT = 8000
BIN = ROOT / "bin"
CLOUDFLARED_URL = (
    "https://github.com/cloudflare/cloudflared/releases/latest/download/"
    "cloudflared-windows-amd64.exe"
)
TUNNEL_URL = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")
LOOPBACK = {"127.0.0.1", "localhost", "::1", "::"}


def port_from_env(default: int = DEFAULT_PORT) -> int:
    """``$PORT`` when it is a number, the default when it is not."""
    try:
        value = int(str(os.environ.get("PORT", default)).strip())
    except (TypeError, ValueError):
        return default
    return max(1, min(65535, value))


def primary_address() -> str | None:
    """The address this machine would leave by.

    Asking the routing table is the only way that cannot pick a VPN, a Docker
    switch or a stale adapter: nothing is sent, but the socket knows which local
    address a packet to the internet would come from.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def lan_addresses() -> list[str]:
    """Every local IPv4 a phone on this network could try, best first."""
    found: list[str] = []
    first = primary_address()
    if first:
        found.append(first)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if address not in found:
                found.append(address)
    except OSError:
        pass
    return [
        address
        for address in found
        if not address.startswith("127.") and not address.startswith("169.254.")
    ]


def is_admin() -> bool:
    """Windows will not change the firewall for anyone else."""
    if os.name != "nt":
        get_uid = getattr(os, "geteuid", None)
        return bool(get_uid and get_uid() == 0)
    import ctypes

    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def rule_name(port: int) -> str:
    """What the rule is called in the firewall, so it can be found again."""
    return f"yt2disc converter (TCP {port})"


def firewall_rule_exists(port: int) -> bool:
    """True when a rule for this port is already there."""
    if os.name != "nt":
        return True
    result = subprocess.run(
        ["netsh", "advfirewall", "firewall", "show", "rule",
         f"name={rule_name(port)}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode == 0


def firewall_allow(port: int) -> bool:
    """Open the port for inbound connections.

    The private and public profiles both, because a home network is usually
    private and a phone hotspot is usually public, and the phone is the point.
    False means the console was not elevated; the caller prints what to run.
    """
    for profile in ("private,public", "any"):
        result = subprocess.run(
            ["netsh", "advfirewall", "firewall", "add", "rule",
             f"name={rule_name(port)}", "dir=in", "action=allow",
             "protocol=TCP", f"localport={port}", f"profile={profile}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode == 0:
            return True
    return False


def find_cloudflared() -> Path | None:
    """The tunnel helper, from PATH or from bin/ next to ffmpeg."""
    on_path = shutil.which("cloudflared")
    if on_path:
        return Path(on_path)
    for candidate in (BIN / "cloudflared.exe", BIN / "cloudflared"):
        if candidate.is_file():
            return candidate
    return None


def fetch_cloudflared() -> Path | None:
    """Download it into bin/, where this project already keeps ffmpeg."""
    target = BIN / ("cloudflared.exe" if os.name == "nt" else "cloudflared")
    BIN.mkdir(parents=True, exist_ok=True)
    print(f"  fetching  : cloudflared -> {target}")
    try:
        with urllib.request.urlopen(CLOUDFLARED_URL, timeout=120) as response:
            blob = response.read()
    except (urllib.error.URLError, OSError) as exc:
        print(f"  failed    : {exc}")
        print(f"  download it by hand from {CLOUDFLARED_URL}")
        return None
    target.write_bytes(blob)
    if os.name != "nt":
        target.chmod(0o755)
    print(f"  fetched   : {len(blob) // (1024 * 1024)} MB")
    return target


def start_tunnel(
    cloudflared: Path, port: int
) -> tuple[subprocess.Popen | None, str | None]:
    """Run a quick tunnel and wait for the public URL it announces.

    A quick tunnel needs no account, no login and no configuration: cloudflared
    is handed a local address and answers with a throwaway https hostname, which
    is why a phone can reach this over cellular with nothing else arranged.
    """
    process = subprocess.Popen(
        [str(cloudflared), "tunnel", "--no-autoupdate",
         "--url", f"http://127.0.0.1:{port}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    found: dict[str, str | None] = {"url": None}
    said: list[str] = []
    announced = threading.Event()

    def listen() -> None:
        """cloudflared talks on stderr, which is where the URL turns up."""
        for line in process.stdout or []:
            said.append(line.rstrip())
            if found["url"] is None:
                match = TUNNEL_URL.search(line)
                if match:
                    found["url"] = match.group(0)
                    announced.set()
        announced.set()

    threading.Thread(target=listen, daemon=True).start()
    announced.wait(timeout=60)
    if found["url"] is None:
        print("  the tunnel did not come up.  cloudflared said:")
        for line in said[-6:]:
            print(f"    {line}")
        stop_tunnel(process)
        return None, None
    return process, found["url"]


def stop_tunnel(process: subprocess.Popen | None) -> None:
    """Leave nothing running, whatever happened to the server."""
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def choose_key(choice: str | None, tunnel: bool) -> str:
    """Which shared key to demand, if any.

    A public URL with no key is an ffmpeg job for whoever finds it, so a tunnel
    generates one unless told otherwise.  On your own network the default stays
    open, exactly like the desktop shortcut.
    """
    exported = (os.environ.get("YT2DISC_WEB_TOKEN") or "").strip()
    if choice is None:
        return exported or (secrets.token_urlsafe(12) if tunnel else "")
    if choice.lower() == "none":
        return ""
    if choice.lower() == "auto":
        return exported or secrets.token_urlsafe(12)
    return choice


def main(argv: list[str] | None = None) -> int:
    """Widen the bind, open the door, print the addresses, then serve."""
    parser = argparse.ArgumentParser(
        prog="python -m webapp._host",
        description="Serve the yt2disc converter so a phone can reach it.",
    )
    parser.add_argument("--port", type=int, default=port_from_env(),
                        help=f"listen port (default {DEFAULT_PORT}, or $PORT)")
    parser.add_argument("--bind", default=os.environ.get("YT2DISC_WEB_HOST")
                        or "0.0.0.0",
                        help="bind address (default 0.0.0.0: every interface)")
    parser.add_argument("--tunnel", action="store_true",
                        help="also ask cloudflared for a public https URL")
    parser.add_argument("--lan-only", action="store_true",
                        help="stay on this network, never start a tunnel")
    parser.add_argument("--fetch-cloudflared", action="store_true",
                        help="download cloudflared into bin/ before starting")
    parser.add_argument("--key", metavar="KEY|auto|none", default=None,
                        help="shared key visitors must give "
                             "(default: auto with --tunnel, none without)")
    parser.add_argument("--no-firewall", action="store_true",
                        help="do not touch the Windows Firewall")
    args = parser.parse_args(argv)

    port = max(1, min(65535, args.port))
    want_tunnel = args.tunnel and not args.lan_only
    key = choose_key(args.key, want_tunnel)
    reachable = args.bind not in LOOPBACK

    # The app reads these when it is imported below, not when this file starts,
    # so setting them here is what the served app will actually see.
    os.environ["PORT"] = str(port)
    os.environ["YT2DISC_WEB_HOST"] = args.bind
    if key:
        os.environ["YT2DISC_WEB_TOKEN"] = key
    else:
        os.environ.pop("YT2DISC_WEB_TOKEN", None)

    cloudflared = find_cloudflared()
    if args.fetch_cloudflared and cloudflared is None:
        cloudflared = fetch_cloudflared()

    phones = lan_addresses()
    print()
    print("  yt2disc converter - reachable from your phone")
    print("  ------------------------------------------------------------")
    print(f"  this pc   : http://127.0.0.1:{port}/")
    if phones:
        for address in phones:
            suffix = f"?key={key}" if key else ""
            print(f"  phone     : http://{address}:{port}/{suffix}")
    else:
        print("  phone     : no network address yet - join Wi-Fi or a hotspot")
    print(f"  key       : {key or 'not required'}")

    if reachable and os.name == "nt" and not args.no_firewall:
        if firewall_rule_exists(port):
            print(f"  firewall  : already open for TCP {port}")
        elif firewall_allow(port):
            print(f"  firewall  : opened TCP {port} inbound, private and public")
        else:
            print("  firewall  : could not open it from here - Windows wants")
            print("              an administrator.  Run this once in a prompt")
            print("              opened with 'Run as administrator':")
            print(f'                netsh advfirewall firewall add rule '
                  f'name="{rule_name(port)}" dir=in action=allow '
                  f'protocol=TCP localport={port} profile=private,public')

    if want_tunnel:
        if cloudflared is None:
            print("  anywhere  : cloudflared is not installed, so this stays")
            print("              on your network.  Run this once to get it:")
            print("                python -m webapp._host --fetch-cloudflared")
        else:
            print("  anywhere  : asking cloudflared for a public URL ...")

    print()
    print("  stop with Ctrl+C")
    print()

    tunnel = None
    try:
        if want_tunnel and cloudflared is not None:
            # The tunnel dials 127.0.0.1, so it works even when the firewall
            # rule is missing and the LAN address is not reachable yet.
            tunnel, public = start_tunnel(cloudflared, port)
            if public:
                suffix = f"?key={key}" if key else ""
                print(f"  anywhere  : {public}/{suffix}")
                print("              open that once on the phone; it remembers")
                print("              the key, and a new address comes each run")
                print()
        from webapp import app as webapp_app  # reads the env set above
        return webapp_app.main()
    except KeyboardInterrupt:
        return 0
    finally:
        stop_tunnel(tunnel)


if __name__ == "__main__":
    raise SystemExit(main())


