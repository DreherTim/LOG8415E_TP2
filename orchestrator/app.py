"""
Orchestrator (inital version): receives requests on port 80 and forwards
them to a free worker which are tracked in status.json.
No queue yet: if both workers are not available, the request is rejected with 503.
"""
import json
import os
import threading

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

STATUS_FILE = os.getenv("STATUS_FILE", "status.json")
status_lock = threading.Lock()  # protects status.json from concurrent writes


def read_status():
    with open(STATUS_FILE) as f:
        return json.load(f)


def write_status(status):
    with open(STATUS_FILE, "w") as f:
        json.dump(status, f, indent=2)


def set_worker_status(name, value):
    with status_lock:
        status = read_status()
        status[name]["status"] = value
        write_status(status)


def find_free_worker():
    """Return (name, info) of a free worker and mark it busy, or none."""
    with status_lock:
        status = read_status()
        for name, info in status.items():
            if info["status"] == "free":
                info["status"] = "busy"
                write_status(status)
                return name, info
    return None


@app.route("/new_request", methods=["POST"])
def new_request():
    incoming_data = request.get_json(silent=True)
    if not incoming_data or "text" not in incoming_data:
        return jsonify(error="JSON body with a 'text' field is required"), 400

    worker = find_free_worker()
    if worker is None:
        # TODO: queue the request instead of just rejecting it -> Tim, Dalya or isabela
        return jsonify(error="all workers are busy"), 503

    name, info = worker
    try:
        resp = requests.post(f"http://{info['ip']}:{info['port']}/process",
                             json=incoming_data, timeout=120)
        result = resp.json()
        result["worker"] = name
        return jsonify(result), resp.status_code
    except (requests.RequestException, ValueError) as e:
        return jsonify(error=f"worker {name} failed: {e}"), 502
    finally:
        set_worker_status(name, "free")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80, threaded=True)