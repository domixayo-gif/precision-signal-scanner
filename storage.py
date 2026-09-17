import base64
import json
import os
import requests


GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
REPO = "domixayo-gif/precision-signal-scanner"
FILE_PATH = "tracker.json"

API = (
    "https://api.github.com/repos/"
    + REPO
    + "/contents/"
    + FILE_PATH
)


def headers():
    return {
        "Authorization": "Bearer " + GITHUB_TOKEN,
        "Accept": "application/vnd.github+json",
    }


def read_tracker():
    response = requests.get(
        API,
        headers=headers(),
        timeout=30,
    )

    if response.status_code == 404:
        data = {
            "signals": [],
            "offset": 0,
        }
        return data, None

    response.raise_for_status()

    payload = response.json()
    content = payload.get("content", "")
    sha = payload.get("sha")

    decoded = base64.b64decode(
        content.replace("\n", "")
    ).decode("utf-8")

    if not decoded.strip():
        data = {
            "signals": [],
            "offset": 0,
        }
    else:
        data = json.loads(decoded)

    if "signals" not in data:
        data["signals"] = []

    if "offset" not in data:
        data["offset"] = 0

    return data, sha


def update_tracker(mutate, message):
    data, sha = read_tracker()

    new_data = mutate(data)

    content = json.dumps(
        new_data,
        indent=2,
    )

    encoded = base64.b64encode(
        content.encode("utf-8")
    ).decode("utf-8")

    payload = {
        "message": message,
        "content": encoded,
    }

    if sha:
        payload["sha"] = sha

    response = requests.put(
        API,
        headers=headers(),
        json=payload,
        timeout=30,
    )

    response.raise_for_status()

    return response.json()
