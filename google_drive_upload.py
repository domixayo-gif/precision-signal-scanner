"""Upload private EMA audit CSV snapshots to a Google Drive Apps Script endpoint.

No Google credentials are stored in this repository. Configure the endpoint URL and
shared token as GitHub Actions repository secrets.
"""
import base64
import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

AUDIT_FILES = ("ema_trade_history.csv", "ema_scan_audit.csv")


def collect_audit_files(run_id, directory="."):
    files = []
    for source_name in AUDIT_FILES:
        path = Path(directory) / source_name
        if not path.is_file():
            continue
        content = path.read_bytes()
        if not content:
            continue
        stem = source_name[:-4]
        files.append({
            "name": f"{stem}_run_{run_id}.csv",
            "content_base64": base64.b64encode(content).decode("ascii"),
        })
    return files


def upload(url, token, run_id, repository, directory="."):
    files = collect_audit_files(run_id, directory)
    if not files:
        print("Google Drive upload skipped: no EMA audit CSV files were produced.")
        return False

    payload = {
        "token": token,
        "run_id": str(run_id),
        "repository": repository,
        "uploaded_at_utc": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        result = json.loads(response.read().decode("utf-8"))
        if response.status != 200 or not result.get("ok"):
            raise RuntimeError(f"Google Drive rejected audit upload: {result}")
    print("Google Drive audit upload successful:")
    for item in result.get("files", []):
        print(f"- {item.get('name')}: {item.get('action')}")
    return True


if __name__ == "__main__":
    endpoint = os.environ.get("GDRIVE_WEB_APP_URL", "").strip()
    token = os.environ.get("GDRIVE_UPLOAD_TOKEN", "").strip()
    if not endpoint or not token:
        print("Google Drive upload not configured yet; CSVs remain only in this runner.")
    else:
        upload(
            endpoint,
            token,
            os.environ.get("GITHUB_RUN_ID", "local"),
            os.environ.get("GITHUB_REPOSITORY", "local"),
        )
