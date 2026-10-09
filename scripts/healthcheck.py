import json
import os
import urllib.request

port = int(os.environ.get("CAMMON_PORT", "18080"))
with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=4) as response:
    status = json.load(response)
if not status["ok"]:
    raise SystemExit(1)
