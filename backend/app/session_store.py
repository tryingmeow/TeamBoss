import json
import os
from typing import Any

from .database import get_sessions_dir


def _session_file(team_id: str) -> str:
    return os.path.join(get_sessions_dir(), f"{team_id}.json")


def write_session_file(team_id: str, session_data: dict[str, Any]) -> None:
    sessions_dir = get_sessions_dir()
    os.makedirs(sessions_dir, mode=0o700, exist_ok=True)
    os.chmod(sessions_dir, 0o700)
    path = _session_file(team_id)
    # session 文件里是明文 accessToken/sessionToken，只允许属主读写
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(session_data, f, ensure_ascii=False, indent=2)
    os.chmod(path, 0o600)


def update_session_file_tokens(team_id: str, access_token: str, session_token: str) -> bool:
    path = _session_file(team_id)
    if not os.path.exists(path):
        return False

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    data["accessToken"] = access_token
    data["sessionToken"] = session_token
    write_session_file(team_id, data)
    return True
