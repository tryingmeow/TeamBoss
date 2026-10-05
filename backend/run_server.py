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
    # proxy_headers=False：客户端 IP 只由 app/client_ip.py 按 AUTO_TEAM_TRUSTED_PROXIES
    # 认定（可信反代来源才采信 X-Real-IP）。uvicorn 自己的 proxy headers 处理会在那之前
    # 按 X-Forwarded-For 改写 request.client，是另一套信任配置，两层叠加只会更难推理。
    uvicorn.run("app.main:app", host=host, port=port, proxy_headers=False)


if __name__ == "__main__":
    main()
