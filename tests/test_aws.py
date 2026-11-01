"""LOG8415E TP2: AWS integration tests, live AWS FIFO, local FIFO simulation.

Run all:        python3 tests/test_aws.py
Run local only: python3 tests/test_aws.py --local-only
Run AWS only:   python3 tests/test_aws.py --aws-only

Live FIFO requires new orchestrator/app.py and worker/app.py deployed on AWS.
Results are saved to tests/results_aws.json.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse
import importlib.util
import sys
import tempfile
from threading import Lock
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

STATE_FILE = Path(__file__).resolve().parents[1] / "aws" / ".deployment-state.json"
ORCHESTRATOR_PORT = 80
REQUEST_TIMEOUT_SECONDS = 240
BURST_REQUESTS = 8  # More requests than the two available workers.
OUTPUT_FILE = Path(__file__).resolve().parent / "results_aws.json"


def get_orchestrator_ip():
    """Look up the public IP of the orchestrator launched by aws/deploy.py."""
    if not STATE_FILE.is_file():
        raise RuntimeError(
            f"Deployment state not found: {STATE_FILE}. "
            "Run python aws/deploy.py first, from the same project directory."
        )
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        instance_id = state["instance_ids"][-1]
        region = state["region"]
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Invalid deployment state in {STATE_FILE}: {exc}") from exc

    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("Install boto3 with: pip install -r aws/requirements.txt") from exc

    ec2 = boto3.client("ec2", region_name=region)
    response = ec2.describe_instances(InstanceIds=[instance_id])
    instances = [
        instance
        for reservation in response.get("Reservations", [])
        for instance in reservation.get("Instances", [])
    ]
    if len(instances) != 1:
        raise RuntimeError(f"Expected orchestrator instance {instance_id}, found {len(instances)}")
    instance = instances[0]
    tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
    if tags.get("Role") != "orchestrator" or tags.get("Project") != state.get("name"):
        raise RuntimeError(f"Instance {instance_id} does not match this deployment's tags")
    if instance.get("State", {}).get("Name") != "running":
        raise RuntimeError(f"Orchestrator {instance_id} is not running")
    ip = instance.get("PublicIpAddress")
    if not ip:
        raise RuntimeError(f"Orchestrator {instance_id} has no public IP")
    return ip


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def post_json(url, payload):
    body = json.dumps(payload).encode("utf-8")
    req = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    started = time.perf_counter()
    start_utc = timestamp()
    try:
        with urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            code = response.status
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        code = exc.code
        raw = exc.read().decode("utf-8", errors="replace")
    except (URLError, TimeoutError, OSError) as exc:
        return {
            "http_status": None,
            "response": None,
            "error": str(exc),
            "started_at": start_utc,
            "finished_at": timestamp(),
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
    try:
        response = json.loads(raw)
    except ValueError:
        response = None
    return {
        "http_status": code,
        "response": response,
        "raw_body": raw if response is None else None,
        "error": None,
        "started_at": start_utc,
        "finished_at": timestamp(),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }


def check_success(result, text):
    """Check the real worker/orchestrator response contract."""
    body = result.get("response")
    reasons = []
    if result["http_status"] != 200:
        reasons.append(f"expected HTTP 200, got {result['http_status']}")
    if not isinstance(body, dict):
        reasons.append("response is not a JSON object")
        return reasons
    if body.get("text") != text:
        reasons.append("response does not echo the original text")
    if body.get("label") not in {"POSITIVE", "NEGATIVE"}:
        reasons.append("missing or invalid sentiment label")
    score = body.get("score")
    if isinstance(score, bool) or not isinstance(score, (float, int)) or not 0 <= score <= 1:
        reasons.append("missing or invalid sentiment score")
    if body.get("worker") not in {"worker1", "worker2"}:
        reasons.append("missing or invalid worker name")
    return reasons


def send_valid(url, text, request_id, gate=None):
    if gate is not None:
        gate.wait()
    result = post_json(url, {"text": text})
    result["request_id"] = request_id
    result["text_sent"] = text
    result["validation_errors"] = check_success(result, text)
    result["passed"] = not result["validation_errors"]
    return result


def run_concurrent(url, count, prefix):
    """Release worker threads together to approximate simultaneous arrivals."""
    gate = Event()
    texts = [f"{prefix} request {i}: This is a wonderful cloud computing course!" for i in range(1, count + 1)]
    results = []
    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(send_valid, url, text, i, gate) for i, text in enumerate(texts, 1)]
        gate.set()
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda item: item["request_id"])
    observed_workers = sorted({r["response"]["worker"] for r in results
                               if r["passed"] and isinstance(r["response"], dict)})
    return {
        "passed": all(r["passed"] for r in results),
        "requests_sent": count,
        "successful_responses": sum(r["passed"] for r in results),
        "workers_observed": observed_workers,
        "both_workers_observed": len(observed_workers) == 2,
        "requests": results,
    }


def run_invalid_payload(url):
    result = post_json(url, {"unexpected": "value"})
    body = result.get("response")
    result["passed"] = (result["http_status"] == 400 and isinstance(body, dict)
                        and isinstance(body.get("error"), str))
    return result



def run_live_fifo_test(url):
    """End-to-end FIFO check using slow *real* AWS workers.

    Two requests occupy both workers, then A-E are submitted one after
    another. Orchestrator response metadata captures actual server arrival
    and dispatch order; HTTP response completion order is not used.
    """
    queued_names = ["A", "B", "C", "D", "E"]
    gate = Event()
    with ThreadPoolExecutor(max_workers=7) as pool:
        futures = {}
        for name in ("warmup1", "warmup2"):
            text = f"live FIFO {name}: A wonderful movie."
            futures[name] = pool.submit(send_valid, url, text, name, gate)
        gate.set()
        # Give the first pair time to reserve worker1 and worker2.
        time.sleep(0.50)
        for name in queued_names:
            text = f"live FIFO {name}: A wonderful movie."
            futures[name] = pool.submit(send_valid, url, text, name)
            # Stagger queued arrivals so the server can observe their order.
            time.sleep(0.12)
        results = {name: future.result() for name, future in futures.items()}

    errors = []
    for name, result in results.items():
        if not result.get("passed"):
            errors.append(f"{name} HTTP/JSON response failed: {result.get('validation_errors')}")
        metric = (result.get("response") or {}).get("queue_metrics") if isinstance(result.get("response"), dict) else None
        if not isinstance(metric, dict) or any(
            not isinstance(metric.get(field), int) or isinstance(metric.get(field), bool)
            for field in ("arrival_seq", "dispatch_seq")
        ) or not isinstance(metric.get("was_queued"), bool) or not isinstance(metric.get("wait_seconds"), (int, float)):
            errors.append(f"{name} missing queue_metrics (deploy new orchestrator/app.py)")

    arrivals = []
    dispatches = []
    queued_count = 0
    if not errors:
        for name, result in results.items():
            m = result["response"]["queue_metrics"]
            arrivals.append((m["arrival_seq"], name))
            dispatches.append((m["dispatch_seq"], name))
        # FIFO is proved by *server-observed* order, not thread launch order.
        arrival_order = [name for _, name in sorted(arrivals)]
        dispatch_order = [name for _, name in sorted(dispatches)]
        if len(set(seq for seq, _ in arrivals)) != 7 or len(set(seq for seq, _ in dispatches)) != 7:
            errors.append("Duplicate arrival or dispatch sequence numbers")
        if arrival_order != dispatch_order:
            errors.append("FIFO violation: server arrival order != dispatch order")
        # Thread/network scheduling may reorder A-E before they reach AWS.
        # FIFO means dispatch order equals ACTUAL server arrival order,
        # not necessarily the order in which the client launched threads.
        observed_queued_order = [name for name in arrival_order if name in queued_names]
        observed_queued_dispatch = [name for name in dispatch_order if name in queued_names]
        if observed_queued_order != observed_queued_dispatch:
            errors.append("FIFO violation among queued A-E requests")
        queued_count = sum(bool(results[name]["response"]["queue_metrics"]["was_queued"])
                           for name in queued_names)
        max_wait = max(results[name]["response"]["queue_metrics"]["wait_seconds"]
                       for name in queued_names)
        if queued_count < 3 or max_wait < 0.5:
            errors.append("Insufficient evidence of a real queue: fewer than 3 A-E requests waited "
                          "or maximum wait < 0.5s; verify PROCESSING_DELAY on AWS")
        warmup_workers = {results[name]["response"]["worker"] for name in ("warmup1", "warmup2")}
        if len(warmup_workers) != 2:
            errors.append("Warmup requests did not use both workers")
    else:
        arrival_order = []
        dispatch_order = []
        max_wait = 0

    return {
        "passed": not errors,
        "environment": "AWS real workers with processing delay",
        "requests_sent": len(results),
        "staggered_queue_order": queued_names,
        "server_arrival_order": arrival_order,
        "server_dispatch_order": dispatch_order,
        "queued_requests_A_to_E": queued_count,
        "max_queue_wait_seconds": max_wait,
        "fifo_proven_on_aws": not errors,
        "errors": errors,
        "requests": results,
    }

def run_aws_tests():
    try:
        orchestrator_ip = get_orchestrator_ip()
    except Exception as exc:
        raise SystemExit(f"Could not resolve orchestrator IP: {exc}") from exc
    url = f"http://{orchestrator_ip}:{ORCHESTRATOR_PORT}/new_request"
    print(f"Testing {url}\n")
    report = {
        "tested_at": timestamp(),
        "endpoint": url,
        "configuration": {"request_timeout_seconds": REQUEST_TIMEOUT_SECONDS,
                          "parallel_requests": 2, "burst_requests": BURST_REQUESTS},
        "tests": {},
    }

    print("[1/4] Single request")
    single = send_valid(url, "I really enjoy working with Docker and AWS!", "single")
    report["tests"]["single"] = single
    print(f"  {'PASS' if single['passed'] else 'FAIL'}: HTTP {single['http_status']}, "
          f"worker={(single.get('response') or {}).get('worker') if isinstance(single.get('response'), dict) else None}")

    print("[2/4] Invalid JSON payload (missing text)")
    invalid = run_invalid_payload(url)
    report["tests"]["invalid_payload"] = invalid
    print(f"  {'PASS' if invalid['passed'] else 'FAIL'}: HTTP {invalid['http_status']} (expected 400)")

    print("[3/4] Two concurrent requests")
    parallel = run_concurrent(url, 2, "parallel")
    report["tests"]["parallel"] = parallel
    print(f"  {'PASS' if parallel['passed'] else 'FAIL'}: "
          f"{parallel['successful_responses']}/2 valid; workers: {parallel['workers_observed']}")

    print(f"[4/4] Burst of {BURST_REQUESTS} concurrent requests (queue stress)")
    burst = run_concurrent(url, BURST_REQUESTS, "burst")
    burst["fifo_queue_proven"] = False
    burst["queue_evidence_note"] = (
        "More than two concurrent client requests were submitted. Success shows "
        "that they were handled, but does not prove that both workers were "
        "simultaneously busy or establish FIFO order. Confirm FIFO with a "
        "controlled slow-worker test or server-side queue instrumentation."
    )
    report["tests"]["burst"] = burst
    print(f"  {'PASS' if burst['passed'] else 'FAIL'}: "
          f"{burst['successful_responses']}/{BURST_REQUESTS} valid; "
          f"workers: {burst['workers_observed']}")

    print("[5/5] Real AWS FIFO queue verification (delayed workers + arrival/dispatch sequence)")
    live_fifo = run_live_fifo_test(url)
    report["tests"]["live_fifo"] = live_fifo
    print(f"  {'PASS' if live_fifo['passed'] else 'FAIL'}: "
          f"{live_fifo['queued_requests_A_to_E']}/5 queued A-E requests; "
          f"max wait={live_fifo['max_queue_wait_seconds']}s")
    print("  Arrival order:", live_fifo["server_arrival_order"])
    print("  Dispatch order:", live_fifo["server_dispatch_order"])
    for error in live_fifo["errors"]:
        print("  ERROR:", error)

    report["summary"] = {
        "all_http_and_response_checks_passed": all([
            single["passed"], invalid["passed"], parallel["passed"], burst["passed"]
        ]),
        "both_workers_used_during_tests": len(set(parallel["workers_observed"] + burst["workers_observed"])) == 2,
        "fifo_queue_verified_on_aws": live_fifo["passed"],
        "note": "Live FIFO uses orchestrator arrival/dispatch metrics and delayed AWS workers.",
    }
    print("AWS response checks passed:", report["summary"]["all_http_and_response_checks_passed"])
    print("Both workers observed:", report["summary"]["both_workers_used_during_tests"])
    print("FIFO proven on AWS:", report["summary"]["fifo_queue_verified_on_aws"])
    return report


# Local FIFO test: no AWS requests and no modification to deployed workers.
class FakeWorkerResponse:
    status_code = 200

    def __init__(self, text):
        self.text = text

    def json(self):
        return {"text": self.text, "label": "POSITIVE", "score": 0.99}


def run_local_fifo_test():
    """Verify queue dispatch order using the team's actual orchestrator code.

    Mocks the worker HTTP calls so results are deterministic and do not
    depend on AWS or on any real ML inference.
    """
    orchestrator_file = Path(__file__).resolve().parents[1] / "orchestrator" / "app.py"
    if not orchestrator_file.is_file():
        raise RuntimeError(f"Orchestrator source not found: {orchestrator_file}")
    spec = importlib.util.spec_from_file_location("tp2_orchestrator_for_test", orchestrator_file)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load the team's orchestrator source")
    orchestrator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(orchestrator)

    names = ["A", "B", "C", "D", "E"]
    dispatch_order = []
    dispatch_lock = Lock()

    def fake_post(url, json, timeout):
        with dispatch_lock:
            dispatch_order.append(json["text"])
        time.sleep(0.05)
        return FakeWorkerResponse(json["text"])

    def send_request(text):
        with orchestrator.app.test_client() as client:
            response = client.post("/new_request", json={"text": text})
            return response.status_code, response.get_json()

    def wait_until_queued(expected, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with orchestrator.queue_condition:
                count = len(orchestrator.request_queue)
            if count == expected:
                return
            time.sleep(0.01)
        raise AssertionError(f"Expected {expected} queued requests, found {count}")

    with tempfile.TemporaryDirectory(prefix="tp2_fifo_") as folder:
        status_path = Path(folder) / "status.json"
        status_path.write_text(json.dumps({
            "worker1": {"ip": "127.0.0.1", "port": "5000", "status": "busy"},
            "worker2": {"ip": "127.0.0.1", "port": "5000", "status": "busy"},
        }), encoding="utf-8")
        orchestrator.STATUS_FILE = str(status_path)

        with patch.object(orchestrator.requests, "post", side_effect=fake_post):
            with ThreadPoolExecutor(max_workers=len(names)) as pool:
                futures = {}
                for number, name in enumerate(names, start=1):
                    futures[name] = pool.submit(send_request, name)
                    wait_until_queued(number)

                print("Five requests queued:", names)
                if dispatch_order:
                    raise AssertionError("A request started while both workers were busy")

                orchestrator.set_worker_status("worker1", "free")
                with orchestrator.queue_condition:
                    orchestrator.queue_condition.notify_all()

                results = {name: futures[name].result(timeout=15) for name in names}

        print("Actual dispatch order:", dispatch_order)
        if dispatch_order != names:
            raise AssertionError(f"FIFO mismatch: expected {names}, got {dispatch_order}")
        if not all(code == 200 and data.get("text") == name
                   for name, (code, data) in results.items()):
            raise AssertionError("Some requests failed or responses were mismatched")
        if orchestrator.request_queue:
            raise AssertionError("Queue was not drained")
        if orchestrator.read_status()["worker1"]["status"] != "free":
            raise AssertionError("Worker 1 was not released at the end")
    print("PASS: all 5 requests dispatched in FIFO order and completed.")
    print("NOTE: This checks local queue logic with simulated workers, not AWS FIFO.")
    return {"passed": True, "queued_order": names,
            "dispatch_order": dispatch_order,
            "environment": "local simulated workers",
            "fifo_proven_on_aws": False}



def main():
    parser = argparse.ArgumentParser(description="AWS response tests, live FIFO verification, and local FIFO verification")
    choices = parser.add_mutually_exclusive_group()
    choices.add_argument("--local-only", action="store_true", help="Run FIFO simulation without AWS")
    choices.add_argument("--aws-only", action="store_true", help="Run AWS integration tests including live FIFO")
    args = parser.parse_args()

    report = {"tested_at": timestamp(), "tests": {}, "summary": {}}
    # When running a single test group, preserve results from earlier runs.
    if (args.local_only or args.aws_only) and OUTPUT_FILE.is_file():
        try:
            previous = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
            if isinstance(previous, dict) and isinstance(previous.get("tests"), dict):
                report = previous
                report["last_updated_at"] = timestamp()
        except (OSError, ValueError):
            pass
    overall_passed = True

    if not args.local_only:
        print("===== AWS integration tests =====")
        try:
            aws = run_aws_tests()
            # Preserve original AWS test names in results_aws.json.
            report.update({key: value for key, value in aws.items() if key != "tests"})
            report["tests"].update(aws["tests"])
            aws_ok = (aws["summary"]["all_http_and_response_checks_passed"]
                      and aws["summary"]["both_workers_used_during_tests"]
                      and aws["summary"]["fifo_queue_verified_on_aws"])
        except Exception as exc:
            aws_ok = False
            report["tests"]["aws_error"] = str(exc)
            print(f"FAIL: AWS tests could not run: {exc}")
        overall_passed = overall_passed and aws_ok

    if not args.aws_only:
        print("\n[6/6] Local FIFO queue verification (simulated workers)" if not args.local_only
              else "\nLocal FIFO queue verification (simulated workers)")
        try:
            fifo = run_local_fifo_test()
        except Exception as exc:
            fifo = {"passed": False, "error": str(exc), "environment": "local simulated workers",
                    "fifo_proven_on_aws": False}
            print(f"FAIL: {exc}")
        report["tests"]["local_fifo"] = fifo
        overall_passed = overall_passed and fifo["passed"]

    # Distinguish local FIFO proof from unproven ordering on AWS.
    old_summary = report.get("summary", {})
    report["summary"] = {
        **old_summary,
        "local_fifo_test_passed": report["tests"].get("local_fifo", {}).get("passed"),
        "fifo_queue_verified_on_aws": report["tests"].get("live_fifo", {}).get("passed", False),
        "all_selected_tests_passed": overall_passed,
        "note": "Live FIFO runs on delayed AWS workers; local FIFO test is separate.",
    }
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nResults saved to: {OUTPUT_FILE}")
    print("OVERALL:", "PASS" if overall_passed else "FAIL")
    if not overall_passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

