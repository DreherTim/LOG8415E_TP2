"""
Orchestrator (inital version): receives requests on port 80 and forwards
them to a free worker which are tracked in status.json.
Requests wait in a FIFO queue when both workers are busy.
"""
import json
import os
import threading
from collections import deque

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

STATUS_FILE = os.getenv("STATUS_FILE", "status.json")
status_lock = threading.Lock()  # protects status.json from concurrent writes
request_queue = deque()
queue_condition = threading.Condition()  # coordinates waiting requests


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

    # Join the FIFO queue. Only the request at the front may claim a free worker.
    ticket = object()
    with queue_condition:
        request_queue.append(ticket)
        while True:
            if request_queue[0] is ticket:
                worker = find_free_worker()
                if worker is not None:
                    request_queue.popleft()
                    queue_condition.notify_all()
                    break
            queue_condition.wait()

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
        with queue_condition:
            queue_condition.notify_all()  # dispatch the next queued request


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80, threaded=True)