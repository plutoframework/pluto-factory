"""Loopback-only dashboard command, separate from coordinator operations."""

import os
from pathlib import Path

from .config import factory_root


def register(commands, common):
    command = commands.add_parser(
        "ui", help="Serve the local factory dashboard"
    )
    common(command)
    command.add_argument("--port", type=int, default=8765)
    command.add_argument("--root", type=Path, help="Existing factory registry")
    command.add_argument(
        "--tailscale",
        action="store_true",
        help="Also serve over HTTPS inside your tailnet (tailscale serve)",
    )
    command.add_argument(
        "--tailscale-port",
        type=int,
        default=443,
        help="Tailnet HTTPS port (default 443)",
    )
    command.add_argument(
        "--tailscale-allow",
        action="append",
        default=[],
        metavar="LOGIN",
        help="Extra tailnet login allowed besides this node's owner",
    )


def execute_ui(args):
    from .dashboard_server import serve

    home = args.home or os.environ.get("PLUTO_FACTORY_HOME")
    if args.factory:
        home = (args.root or factory_root()) / args.factory
    return serve(
        args.root or factory_root(),
        extra_home=Path(home) if home else None,
        port=args.port,
        tailscale=args.tailscale,
        tailscale_port=args.tailscale_port,
        tailscale_allow=args.tailscale_allow,
    )
