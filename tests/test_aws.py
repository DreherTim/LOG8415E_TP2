"""AWS integration tests for LOG8415E TP2.

Run: python3 tests/test_aws.py
Reads the deployment state and resolves the orchestrator public IP with boto3.
Creates tests/results_aws.json.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
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


def main():
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

    report["summary"] = {
        "all_http_and_response_checks_passed": all([
            single["passed"], invalid["passed"], parallel["passed"], burst["passed"]
        ]),
        "both_workers_used_during_tests": len(set(parallel["workers_observed"] + burst["workers_observed"])) == 2,
        "fifo_queue_verified": False,
        "note": "End-to-end HTTP checks only; FIFO order is not proven by these tests.",
    }
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nResults saved to: {OUTPUT_FILE}")
    print("All response checks passed:", report["summary"]["all_http_and_response_checks_passed"])
    print("Both workers observed:", report["summary"]["both_workers_used_during_tests"])
    print("FIFO proven: no (requires controlled instrumentation/slow workers)")
    if not report["summary"]["all_http_and_response_checks_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
