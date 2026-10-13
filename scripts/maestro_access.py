"""Manage Maestro's optional sign-in gate from the command line.

Maestro is open on loopback by default. As soon as the backend is reachable from
another device (Pinokio's LAN share, or a Tailscale route for your phone), turn
this on so only you can drive it.

    python scripts/maestro_access.py status      # is it on? which mode?
    python scripts/maestro_access.py enable      # create a password (printed once)
    python scripts/maestro_access.py reset       # new password, old sessions die
    python scripts/maestro_access.py disable     # open again (loopback only!)

The password is stored as a PBKDF2 hash in app/settings/access_gate.json; the
plaintext is printed exactly once and never kept. Setting MAESTRO_ACCESS_TOKEN
overrides everything (that mode writes nothing to disk).
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from services.access_gate import AccessGate, AccessGateError  # noqa: E402

_SETTINGS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "settings"
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Maestro sign-in gate")
    parser.add_argument("action", choices=["status", "enable", "reset", "disable"])
    parser.add_argument(
        "--password",
        default="",
        help="Use this password instead of a generated one (at least 8 characters).",
    )
    args = parser.parse_args(argv)
    gate = AccessGate(_SETTINGS_DIR)

    if args.action == "status":
        status = gate.status()
        print(f"enabled: {status['enabled']} (mode: {status['mode']})")
        print(f"settings file: {status['settings_file']}")
        print(f"session length: {status['session_days']} days")
        if args.action == "status" and not status["enabled"]:
            print(
                "\nThe gate is OFF: anyone who can reach this port can use Maestro.\n"
                "That is fine on a single computer. If you enabled Pinokio's LAN\n"
                "share or a Tailscale route, run: python scripts/maestro_access.py enable"
            )
        return 0

    if args.action == "disable":
        gate.disable()
        print("Sign-in gate disabled. Maestro is reachable without a password again.")
        print("Only do this when the backend is bound to 127.0.0.1.")
        return 0

    try:
        password = gate.set_password(args.password or None)
    except AccessGateError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    print("Sign-in gate enabled.")
    print("Password (shown once, write it down):")
    print(f"    {password}")
    print("\nEnter it once per device. Sessions last 30 days.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
