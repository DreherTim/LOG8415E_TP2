import json
import os
import threading
import time
from collections import deque

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

STATUS_FILE = os.getenv("STATUS_FILE", "status.json")
status_lock = threading.Lock()  # protects status.json from concurrent writes
request_queue = deque()
queue_condition = threading.Condition()  # coordinates waiting requests

# Protected by queue_condition: assignment and dispatch order for FIFO tests.
next_arrival_seq = 0
next_dispatch_seq = 0


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
    global next_arrival_seq, next_dispatch_seq
    incoming_data = request.get_json(silent=True)
    if not incoming_data or "text" not in incoming_data:
        return jsonify(error="JSON body with a 'text' field is required"), 400

    # Join the FIFO queue. Only the request at the front may claim a free worker.
    ticket = object()
    queued_at = time.monotonic()
    with queue_condition:
        next_arrival_seq += 1
        arrival_seq = next_arrival_seq
        request_queue.append(ticket)
        was_queued = False
        while True:
            if request_queue[0] is ticket:
                worker = find_free_worker()
                if worker is not None:
                    request_queue.popleft()
                    next_dispatch_seq += 1
                    dispatch_seq = next_dispatch_seq
                    queue_wait_seconds = round(time.monotonic() - queued_at, 3)
                    queue_condition.notify_all()
                    break
            # The request waits because other requests are ahead or no worker is free.
            was_queued = True
            queue_condition.wait()

    name, info = worker
    try:
        resp = requests.post(f"http://{info['ip']}:{info['port']}/process",
                             json=incoming_data, timeout=120)
        result = resp.json()
        result["worker"] = name
        # Test metadata: captured at dispatch, not at HTTP response completion.
        # Allows tests to distinguish dispatch order from finish order.
        result["queue_metrics"] = {
            "arrival_seq": arrival_seq,
            "dispatch_seq": dispatch_seq,
            "was_queued": was_queued,
            "wait_seconds": queue_wait_seconds,
        }
        return jsonify(result), resp.status_code
    except (requests.RequestException, ValueError) as e:
        return jsonify(error=f"worker {name} failed: {e}"), 502
    finally:
        set_worker_status(name, "free")
        with queue_condition:
            queue_condition.notify_all()  # dispatch the next queued request


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80, threaded=True)
