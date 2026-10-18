"""`claude-bridge --config path/to/config.toml`"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import stat
import sys
from pathlib import Path

from .config import ConfigError, load_config
from .server import serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="claude-bridge", description=__doc__)
    parser.add_argument(
        "--config", type=Path,
        default=Path(os.environ.get("CLAUDE_BRIDGE_CONFIG", "~/.config/claude-bridge/config.toml")).expanduser(),
        help="TOML config (default: $CLAUDE_BRIDGE_CONFIG or ~/.config/claude-bridge/config.toml)",
    )
    parser.add_argument("--log-level", default=os.environ.get("CLAUDE_BRIDGE_LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper(), format="%(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    log = logging.getLogger("claude_bridge")
    try:
        cfg = load_config(args.config if args.config.exists() else None)
        cfg.read_token()
        cfg.resolved_binary()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    for secret in (cfg.token_file, cfg.oauth_token_file, cfg.api_key_file):
        if secret is not None and secret.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            log.warning("%s is readable by group/others; chmod 600 it", secret)

    try:
        asyncio.run(serve(cfg))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
