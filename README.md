# LOG8415E — Lab Assignment 2

## Project overview

This project deploys a Flask-based orchestrator and two Dockerized Flask workers in AWS Academy Learner Lab. The orchestrator accepts HTTP POST requests on port 80 and forwards them to an available worker on port 5000. If both workers are busy, incoming requests are queued and dispatched as workers become available. The workers use a lightweight DistilBERT sentiment-classification model and return JSON responses.

The automated deployment uses Python and `boto3` to create three Ubuntu EC2 instances (one orchestrator and two workers), configure their security groups, transfer the local application files, and start Docker containers.

## 1. How to launch the system

Open the project folder in a terminal (VS code for us), start AWS Learner Lab, and run the following commands from the **project root**:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r aws/requirements.txt
python3 -m pip install -r orchestrator/requirements.txt
python3 aws/deploy.py --check-only
```
`--check-only` validates the local source files and their size for EC2 user data; it does **not** create AWS resources.

Once the check succeeds, launch the system:

```bash
python3 aws/deploy.py --region us-east-1 --instance-type t2.large --skip-tests
```

The deployment script creates two worker instances and one orchestrator instance, installs Docker using EC2 bootstrap scripts, builds and starts the containers, and performs a readiness request. It prints the orchestrator's public IP address and saves deployment details locally in `aws/.deployment-state.json`. The `--skip-tests` option skips running the test suite automatically, allowing tests to be run separately.

Do not rerun the deployment command if resources have already been created. Check the existing deployment or clean it up first.

## 2. How to run the automation scripts

The main automation scripts are:

| Script | Purpose |
| --- | --- |
| `aws/deploy.py` | Provisions and configures three EC2 instances, transfers the local application source, and checks readiness. |
| `aws/user_data.sh` | Installs Docker and starts the application during each EC2 instance's first boot; called automatically by `deploy.py`. |
| `tests/test_aws.py` | Checks HTTP responses, concurrent requests, worker utilization, and local/AWS FIFO queue behaviour. |
| `aws/cleanup.py` | Terminates tracked EC2 instances and deletes their project security groups. |

After the deployment reports that the system is responding, run:

```bash
python3 tests/test_aws.py
```

This test suite runs: a single valid request (expected HTTP 200), an invalid request missing `text` (expected HTTP 400), two concurrent requests, eight concurrent requests, an AWS queue test, and a local simulated FIFO test. It saves results in `tests/results_aws.json`.

To run only one test category:

```bash
python3 tests/test_aws.py --aws-only
python3 tests/test_aws.py --local-only
```

The AWS FIFO test requires both workers to be occupied long enough for requests to enter the queue. The worker configuration in `worker/docker-compose.yml` sets `PROCESSING_DELAY: "2"` for this controlled test. Set it to `"0"` for operation without an artificial delay; with no delay, the FIFO test may not observe any queued requests even if the queue logic is correct. Changes to this configuration require redeployment to take effect.

## 3. Expected runtime

In one successful course run, the system became ready after approximately **184 seconds (about 3 minutes)**. This is an observed result for our test. Deployment time depends on EC2 startup, Docker installation, container builds, and the model download.

The test suite normally runs after the application is ready; its duration varies with worker processing time and queueing. A 2-second artificial delay is enabled in the test configuration to make queue behaviour observable.

## 4. How to send a test request

Replace `ORCHESTRATOR_PUBLIC_IP` with the IP printed by `deploy.py`, and submit a JSON POST request:

```bash
curl -X POST http://ORCHESTRATOR_PUBLIC_IP/new_request \
  -H "Content-Type: application/json" \
  -d '{"text":"I love cloud computing"}'
```
A successful response is JSON containing the input text, a predicted sentiment label and score, and the worker that handled the request. Depending on the application version, it may also contain queue metrics such as `arrival_seq`, `dispatch_seq`, `was_queued`, and `wait_seconds`.

For a negative test, send a JSON object with no `text` field; the expected HTTP response is **400 Bad Request**.

## 5. How to stop and clean up AWS resources

After saving your test results, run from the **same project folder**:

```bash
python3 aws/cleanup.py
```

This reads `aws/.deployment-state.json`, terminates the three EC2 instances created by the deployment, and removes their associated security groups. Wait for the success message:

```text
Tracked instances terminated and security groups deleted.
```

Verify in AWS EC2 that the instances are terminated. This command **terminates** the instances rather than temporarily stopping them. Never delete or rename `.deployment-state.json` before cleanup unless the resources have already been independently verified as removed. If AWS reports an expired token, renew your Learner Lab credentials before retrying cleanup.

