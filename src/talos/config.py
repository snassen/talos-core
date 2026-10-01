"""Where Talos keeps things.

Two knobs, both environment variables so tests and a future VM can move them:

- TALOS_HOME: the data directory (vault, logs, exports). Default ~/TalosData.
- TALOS_DSN: the PostgreSQL connection string. Default is the Homebrew server on
  port 5433 over its Unix socket; 5432 is left to another PostgreSQL server.

Secrets are never configured here. They live in the macOS Keychain (see secrets.py).

One optional file, TALOS_HOME/web.json, lets other devices on the tailnet reach the web UI
through `tailscale serve` (see docs/tailscale.md):

    {"tailnet_hosts": ["mac.example.ts.net"], "tailnet_users": ["you@example.org"]}
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DSN = "host=/tmp port=5433 dbname=talos"


@dataclass(frozen=True)
class Settings:
    home: Path
    dsn: str

    @property
    def vault(self) -> Path:
        return self.home / "vault"

    @property
    def exports(self) -> Path:
        return self.home / "exports"

    @property
    def logs(self) -> Path:
        return self.home / "logs"

    @property
    def web(self) -> dict:
        """Web settings from web.json; empty (localhost only) when the file is absent."""
        path = self.home / "web.json"
        return json.loads(path.read_text()) if path.exists() else {}


def load() -> Settings:
    home = Path(os.environ.get("TALOS_HOME", Path.home() / "TalosData")).expanduser()
    return Settings(home=home, dsn=os.environ.get("TALOS_DSN", DEFAULT_DSN))
