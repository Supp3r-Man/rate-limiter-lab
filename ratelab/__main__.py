"""CLI entry: python -m ratelab demo. Exit codes: 0 ok, 2 crash."""

from __future__ import annotations

import sys


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv != ["demo"]:
        print("usage: python -m ratelab demo", file=sys.stderr)
        return 2
    try:
        from .demo import main as demo_main

        demo_main()
    except Exception as exc:  # fail closed: a crash must not look like success
        print(f"demo failed: {exc!r}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
