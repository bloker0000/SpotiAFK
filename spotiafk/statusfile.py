"""status reads the heartbeat file, it never talks to the running farm"""

import datetime
import json
import os

STATUS_FILE = "status.json"


def save(state_dir: str, name: str, payload: dict) -> None:
    os.makedirs(state_dir, exist_ok=True)
    path = os.path.join(state_dir, name)
    temporary_path = path + ".tmp"
    with open(temporary_path, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False)
    os.replace(temporary_path, path)


def load(state_dir: str, name: str) -> dict | None:
    try:
        with open(os.path.join(state_dir, name), encoding="utf-8") as file:
            return json.load(file)
    except (OSError, ValueError):
        return None


def write(state_dir: str, state: str, **fields) -> None:
    save(
        state_dir,
        STATUS_FILE,
        {
            "state": state,
            "pid": os.getpid(),
            "updated": datetime.datetime.now().isoformat(timespec="seconds"),
            **fields,
        },
    )


def read(state_dir: str) -> dict | None:
    return load(state_dir, STATUS_FILE)


def measure_age_seconds(status: dict) -> float | None:
    try:
        updated = datetime.datetime.fromisoformat(status["updated"])
    except (KeyError, ValueError):
        return None
    return (datetime.datetime.now() - updated).total_seconds()
