"""The client entry point — `atf remote …` and `atf mcp` without the engine. The `atf.pyz` the
server hands out runs this; the full `atf` CLI offers the same commands."""
from __future__ import annotations

import argparse
import sys


def main(argv=None) -> int:
    from atf import remote
    ap = argparse.ArgumentParser(prog="atf.pyz", description="Anytest Framework client: drive a running "
                                 "atf server from a terminal (remote) or an AI client (mcp)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    remote.add_parser(sub)
    sub.add_parser("mcp", help="MCP server (stdio) for a running atf server — uses the `remote login` session")
    args = ap.parse_args(argv)
    if args.cmd == "remote":
        return remote.run(args)
    from atf import mcp
    return mcp.run()


if __name__ == "__main__":
    sys.exit(main())
