"""Optional tailnet access for the loopback dashboard via ``tailscale serve``.

The dashboard never binds a network interface. ``tailscale serve`` runs in
the foreground as an HTTPS proxy to the loopback port, reachable only inside
the tailnet; its configuration disappears when that process exits. The
dashboard accepts proxied requests only for this node's tailnet name and only
from allowed tailnet logins (``Tailscale-User-Login``).
"""

import json
import queue
import shutil
import subprocess
import threading
from dataclasses import dataclass, field


class TailnetError(RuntimeError):
    pass


@dataclass(frozen=True)
class TailnetAccess:
    """Hosts and logins the dashboard accepts through the tailnet proxy."""

    dns_name: str
    https_port: int
    logins: frozenset[str] = field(default_factory=frozenset)

    @property
    def url(self) -> str:
        suffix = "" if self.https_port == 443 else f":{self.https_port}"
        return f"https://{self.dns_name}{suffix}/"

    @property
    def hosts(self) -> frozenset[str]:
        hosts = {f"{self.dns_name}:{self.https_port}"}
        if self.https_port == 443:
            hosts.add(self.dns_name)
        return frozenset(hosts)


def _tailscale(*arguments: str) -> subprocess.CompletedProcess:
    if not shutil.which("tailscale"):
        raise TailnetError(
            "--tailscale needs the tailscale CLI; install Tailscale and log in"
        )
    return subprocess.run(
        ["tailscale", *arguments],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def discover(https_port: int, allow: list[str]) -> TailnetAccess:
    """Read this node's tailnet name and owner; refuse unusable setups."""
    if not 1 <= https_port <= 65535:
        raise TailnetError("--tailscale-port must be between 1 and 65535")
    result = _tailscale("status", "--json")
    if result.returncode != 0:
        raise TailnetError("tailscale status failed; is tailscaled running?")
    status = json.loads(result.stdout)
    if status.get("BackendState") != "Running":
        raise TailnetError("Tailscale is not connected; run tailscale up")
    me = status.get("Self") or {}
    dns_name = (me.get("DNSName") or "").rstrip(".")
    if not dns_name or dns_name not in (status.get("CertDomains") or []):
        raise TailnetError(
            "this tailnet has no HTTPS certificates; enable HTTPS in the "
            "Tailscale admin console (DNS > HTTPS Certificates)"
        )
    owner = (status.get("User") or {}).get(str(me.get("UserID")), {})
    logins = {owner["LoginName"]} if owner.get("LoginName") else set()
    logins.update(login.strip() for login in allow if login.strip())
    if not logins:
        raise TailnetError(
            "could not determine this node's owner; pass --tailscale-allow"
        )
    serving = _tailscale("serve", "status", "--json")
    if serving.returncode == 0 and serving.stdout.strip():
        config = json.loads(serving.stdout)
        ports = set((config.get("TCP") or {}).keys())
        for session in (config.get("Foreground") or {}).values():
            ports.update((session.get("TCP") or {}).keys())
        if str(https_port) in ports:
            raise TailnetError(
                f"tailscale serve already uses HTTPS port {https_port}; "
                "choose another with --tailscale-port"
            )
    return TailnetAccess(dns_name, https_port, frozenset(logins))


class TailnetProxy:
    """Foreground ``tailscale serve``; stopping it removes the exposure."""

    def __init__(self, access: TailnetAccess, local_port: int):
        self.access = access
        self.local_port = local_port
        self.process: subprocess.Popen | None = None

    def start(self, timeout: float = 30) -> None:
        self.process = subprocess.Popen(
            [
                "tailscale",
                "serve",
                f"--https={self.access.https_port}",
                f"http://127.0.0.1:{self.local_port}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        lines: queue.Queue = queue.Queue()

        def pump():
            for line in self.process.stdout:
                lines.put(line)
            lines.put(None)

        threading.Thread(target=pump, daemon=True).start()
        output = []
        while True:
            try:
                line = lines.get(timeout=timeout)
            except queue.Empty:
                self.stop()
                raise TailnetError("tailscale serve did not start in time")
            if line is None:
                break
            output.append(line)
            if "Available" in line:
                return
        self.stop()
        detail = "".join(output).strip()
        if "access denied" in detail.lower() or "operator" in detail.lower():
            detail += (
                "\nAllow your user once with: sudo tailscale set "
                "--operator=$USER"
            )
        raise TailnetError(f"tailscale serve failed: {detail}")

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
