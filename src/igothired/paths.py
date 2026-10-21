"""Where private state lives.

Anything sensitive (API key, Google token, the local database, logs) goes in
~/.igothired with owner-only permissions. Nothing secret is ever written
inside the repo.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOME = Path(os.environ.get("IGOTHIRED_HOME", Path.home() / ".igothired")).expanduser()

DB_PATH = HOME / "igothired.db"
SECRETS_PATH = HOME / "secrets.env"
GOOGLE_CLIENT_PATH = HOME / "google_client_secret.json"
GOOGLE_TOKEN_PATH = HOME / "google_token.json"
SHEET_PATH = HOME / "sheet.json"
LOG_DIR = HOME / "logs"
LOCK_PATH = HOME / "run.lock"


def ensure_home() -> None:
    HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    LOG_DIR.mkdir(mode=0o700, exist_ok=True)
    os.chmod(HOME, 0o700)


def write_private(path: Path, text: str) -> None:
    """Write a file only the current user can read."""
    ensure_home()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)


def _read_env_file() -> dict[str, str]:
    values: dict[str, str] = {}
    if not SECRETS_PATH.exists():
        return values
    for line in SECRETS_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def load_secrets() -> None:
    """Load secrets.env into the environment. Real env vars win."""
    if SECRETS_PATH.exists() and SECRETS_PATH.stat().st_mode & 0o077:
        os.chmod(SECRETS_PATH, 0o600)
    for key, value in _read_env_file().items():
        os.environ.setdefault(key, value)


def save_secret(key: str, value: str) -> None:
    values = _read_env_file()
    values[key] = value
    write_private(SECRETS_PATH, "".join(f"{k}={v}\n" for k, v in values.items()))


def search_path() -> str:
    """PATH for subprocesses. launchd starts jobs with a bare PATH, so uvx isn't found without this."""
    dirs = [
        str(Path.home() / ".local/bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
    found = shutil.which("uvx")
    if found:
        dirs.insert(0, str(Path(found).parent))
    dirs += os.environ.get("PATH", "").split(os.pathsep)
    return os.pathsep.join(dict.fromkeys(d for d in dirs if d))
