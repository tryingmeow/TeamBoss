import os
from pathlib import Path

import uvicorn


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def main() -> None:
    project_dir = Path(__file__).resolve().parents[1]
    load_dotenv(project_dir / ".env")

    host = os.getenv("AUTO_TEAM_BACKEND_HOST", "127.0.0.1")
    port = int(os.getenv("AUTO_TEAM_BACKEND_PORT", "18087"))
    uvicorn.run("app.main:app", host=host, port=port, proxy_headers=True)


if __name__ == "__main__":
    main()
