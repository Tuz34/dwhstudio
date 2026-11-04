from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = Path(__file__).with_name("odi_connection.properties.generated")


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def main() -> int:
    load_env_file(ROOT / ".env")
    load_env_file(ROOT / ".env.dev")

    repo_dsn = require_env("ODI_REPO_DSN")
    repo_user = require_env("ODI_REPO_USER")
    repo_password = require_env("ODI_REPO_PASSWORD")
    odi_user = require_env("ODI_SUPERVISOR_USER")
    odi_password = require_env("ODI_SUPERVISOR_PASSWORD")
    work_name = os.getenv("ODI_WORK_REPOSITORY", "WORKREP").strip() or "WORKREP"

    jdbc_url = repo_dsn if repo_dsn.lower().startswith("jdbc:oracle:thin:@") else f"jdbc:oracle:thin:@{repo_dsn}"
    output = Path(os.getenv("ODI_CONNECTION_PROPERTIES_OUT", str(DEFAULT_OUTPUT)))
    output.write_text(
        "\n".join(
            [
                "# Generated local ODI connection properties. Do not commit or paste.",
                f"master_jdbc={jdbc_url}",
                "master_jdbc_driver=oracle.jdbc.OracleDriver",
                f"master_username={repo_user}",
                f"master_password={repo_password}",
                f"work_name={work_name}",
                f"odi_username={odi_user}",
                f"odi_password={odi_password}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(f"Wrote local ODI properties: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
