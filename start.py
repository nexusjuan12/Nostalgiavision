"""Startup script — initialises the database and launches the Flask server."""
import os
import sys
import webbrowser
import threading

# Make sure the working directory is the project root
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import database as db
db.init_db()

import app as flask_app
import json

cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
if not os.path.exists(cfg_path):
    print("ERROR: config.json not found.")
    print("Copy config.example.json to config.json and fill in your Plex URL and token.")
    sys.exit(1)
with open(cfg_path) as f:
    cfg = json.load(f)

host = cfg.get("host", "127.0.0.1")
port = int(cfg.get("port", 5000))
url = f"http://127.0.0.1:{port}"

print(f"Nostalgiavision -> {url}")
print("Press Ctrl+C to quit.\n")

# Open browser after a short delay
def _open_browser():
    import time; time.sleep(1.2)
    webbrowser.open(url)
threading.Thread(target=_open_browser, daemon=True).start()

flask_app.app.run(host=host, port=port, debug=False, threaded=True)
