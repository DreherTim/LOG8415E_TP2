import argparse
import base64
import io
import tarfile
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
TEST_SCRIPT = ROOT.parent / 'tests' / 'test_aws.py'
STATE = ROOT / '.deployment-state.json'
TAG = 'log8415e-tp2'


def make_userdata(role, ips=("", "")):
    """Embed a compressed copy of the *local* role directory in EC2 user data.

    """
    project_root = ROOT.parent
    role_dir = project_root / role
    required = ["app.py", "Dockerfile", "docker-compose.yml", "requirements.txt"]
    if role == "orchestrator":
        required.append("status.json")
    missing = [filename for filename in required if not (role_dir / filename).is_file()]
    if missing:
        raise FileNotFoundError(f"Missing local {role} files: {', '.join(missing)}")

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for filename in required:
            # Archive paths are only in worker/ or orchestrator/, no secrets.
            archive.add(role_dir / filename, arcname=f"{role}/{filename}")
    payload = base64.b64encode(buffer.getvalue()).decode("ascii")
    template = (ROOT / "user_data.sh").read_text(encoding="utf-8")
    result = (template.replace("__ROLE__", role)
                      .replace("__WORKER1_IP__", ips[0])
                      .replace("__WORKER2_IP__", ips[1])
                      .replace("__APP_ARCHIVE_BASE64__", payload))
    data_size = len(result.encode("utf-8"))
    if data_size > 16 * 1024:
        raise ValueError(f"EC2 user data is {data_size} bytes; 16 KiB max. "
                         "Reduce role files or use S3 for deployment instead.")
    print(f"  Embedded local {role} files in EC2 user data ({data_size} bytes)")
    return result


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--region', default=os.getenv('AWS_REGION', 'us-east-1'))
    p.add_argument('--instance-type', default='t2.large')
    p.add_argument('--subnet-id', help='Subnet for all three instances (default VPC first available subnet)')
    p.add_argument('--key-name', help='Existing EC2 key pair for SSH, if desired')
    p.add_argument('--ssh-cidr', help='Admin CIDR for port 22, e.g. 203.0.113.7/32 (optional)')
    p.add_argument('--name', default=TAG, help='Unique tag prefix for the deployment')
    p.add_argument('--ready-timeout', type=int, default=1200,
                   help='Seconds to wait for Docker builds and model download (default 1200)')
    p.add_argument('--skip-tests', action='store_true', help='Do not run tests/test_aws.py after deploy')
    p.add_argument('--check-only', action='store_true', help='Validate local source and user-data sizes without using AWS')
    return p.parse_args()


def wait_until_ready(ip, timeout):
    """Wait until the orchestrator successfully processes a real POST request."""
    url = f'http://{ip}/new_request'
    deadline = time.time() + timeout
    start = time.time()
    while time.time() < deadline:
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps({'text': 'health check'}).encode('utf-8'),
                headers={'Content-Type': 'application/json'},
                method='POST',
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status == 200:
                    print(f'System responding after {int(time.time() - start)}s: '
                          f'{resp.read().decode()}')
                    return True
        except (urllib.error.URLError, OSError, TimeoutError):
            pass  # App may be booting or workers may not be ready yet.
        print(f'  waiting for bootstrap... {int(time.time() - start)}s', flush=True)
        time.sleep(20)
    return False


def latest_ubuntu(ec2):
    images = ec2.describe_images(Owners=['099720109477'], Filters=[
        {'Name': 'name', 'Values': ['ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*',
                                    'ubuntu/images/hvm-ssd-gp3/ubuntu-jammy-22.04-amd64-server-*']},
        {'Name': 'state', 'Values': ['available']},
    ])['Images']
    if not images:
        raise RuntimeError('No Ubuntu 22.04 AMD64 AMI found in this region.')
    return max(images, key=lambda x: x['CreationDate'])['ImageId']


def choose_network(ec2, subnet_id):
    if subnet_id:
        subnets = ec2.describe_subnets(SubnetIds=[subnet_id])['Subnets']
    else:
        vpcs = ec2.describe_vpcs(Filters=[{'Name': 'isDefault', 'Values': ['true']}])['Vpcs']
        if not vpcs:
            raise RuntimeError('No default VPC; provide --subnet-id.')
        subnets = ec2.describe_subnets(Filters=[{'Name': 'vpc-id', 'Values': [vpcs[0]['VpcId']]},
                                                {'Name': 'default-for-az', 'Values': ['true']}])['Subnets']
    if not subnets:
        raise RuntimeError('No suitable subnet.')
    subnet = sorted(subnets, key=lambda s: s['SubnetId'])[0]
    return subnet['VpcId'], subnet['SubnetId']


def create_groups(ec2, vpc, name, ssh_cidr):
    def group(suffix, description):
        resp = ec2.create_security_group(GroupName=f'{name}-{suffix}', Description=description, VpcId=vpc)
        gid = resp['GroupId']
        ec2.create_tags(Resources=[gid], Tags=[{'Key': 'Project', 'Value': name}])
        return gid

    orchestrator = group('orchestrator', 'TP2 public orchestrator')
    worker = group('worker', 'TP2 private workers')
    ec2.authorize_security_group_ingress(GroupId=orchestrator, IpPermissions=[{
        'IpProtocol': 'tcp', 'FromPort': 80, 'ToPort': 80,
        'IpRanges': [{'CidrIp': '0.0.0.0/0', 'Description': 'Client HTTP'}]
    }])
    # Workers listen on port 5000, reachable only from the orchestrator's security group.
    ec2.authorize_security_group_ingress(GroupId=worker, IpPermissions=[{
        'IpProtocol': 'tcp', 'FromPort': 5000, 'ToPort': 5000,
        'UserIdGroupPairs': [{'GroupId': orchestrator, 'Description': 'Orchestrator only'}]
    }])
    if ssh_cidr:
        for gid in (orchestrator, worker):
            ec2.authorize_security_group_ingress(GroupId=gid, IpPermissions=[{
                'IpProtocol': 'tcp', 'FromPort': 22, 'ToPort': 22,
                'IpRanges': [{'CidrIp': ssh_cidr, 'Description': 'Admin SSH'}]
            }])
    return orchestrator, worker


def start_instance(ec2, args, ami, subnet, sg, role, ips=('', '')):
    kwargs = dict(ImageId=ami, InstanceType=args.instance_type, MinCount=1, MaxCount=1,
                  SubnetId=subnet, SecurityGroupIds=[sg],
                  UserData=make_userdata('worker' if role.startswith('worker') else 'orchestrator', ips),
                  NetworkInterfaces=None,
                  TagSpecifications=[{'ResourceType': 'instance', 'Tags': [
                      {'Key': 'Name', 'Value': f'{args.name}-{role}'},
                      {'Key': 'Project', 'Value': args.name},
                      {'Key': 'Role', 'Value': role},
                  ]}])
    kwargs.pop('NetworkInterfaces')
    if args.key_name:
        kwargs['KeyName'] = args.key_name
    return ec2.run_instances(**kwargs)['Instances'][0]['InstanceId']


def save_state(data):
    STATE.write_text(json.dumps(data, indent=2) + '\n')
    STATE.chmod(0o600)


def main():
    args = parse_args()
    if args.ssh_cidr:
        import ipaddress
        ipaddress.ip_network(args.ssh_cidr, strict=False)
        if not args.key_name:
            raise ValueError('--ssh-cidr requires --key-name')
    print(json.dumps({'region': args.region, 'instance_type': args.instance_type,
                      'instances': ['worker1', 'worker2', 'orchestrator'],
                      'source': 'local files in worker/ and orchestrator/',
                      'worker_port': 5000,
                      'mode': 'DEPLOY'}, indent=2))
    # Validate locally before creating billable AWS resources.
    make_userdata('worker')
    make_userdata('orchestrator', ('10.0.0.1', '10.0.0.2'))
    if args.check_only:
        print('Local source check passed. No AWS resources created.')
        return
    if STATE.exists():
        raise SystemExit(f'{STATE} exists. Clean up previous deployment before recreating.')
    import boto3
    ec2 = boto3.client('ec2', region_name=args.region)
    state = {'region': args.region, 'name': args.name, 'instance_ids': [], 'security_group_ids': []}
    try:
        ami = latest_ubuntu(ec2)
        vpc, subnet = choose_network(ec2, args.subnet_id)
        orch_sg, worker_sg = create_groups(ec2, vpc, args.name, args.ssh_cidr)
        state['security_group_ids'] = [orch_sg, worker_sg]
        save_state(state)
        worker_ids = []
        for role in ('worker1', 'worker2'):
            iid = start_instance(ec2, args, ami, subnet, worker_sg, role)
            worker_ids.append(iid)
            state['instance_ids'].append(iid)
            save_state(state)
        ec2.get_waiter('instance_running').wait(InstanceIds=worker_ids, WaiterConfig={'Delay': 10, 'MaxAttempts': 36})
        instances = ec2.describe_instances(InstanceIds=worker_ids)['Reservations']
        by_id = {instance['InstanceId']: instance for reservation in instances for instance in reservation['Instances']}
        ips = tuple(by_id[iid]['PrivateIpAddress'] for iid in worker_ids)
        orch_id = start_instance(ec2, args, ami, subnet, orch_sg, 'orchestrator', ips)
        state['instance_ids'].append(orch_id)
        save_state(state)
        ec2.get_waiter('instance_running').wait(InstanceIds=[orch_id], WaiterConfig={'Delay': 10, 'MaxAttempts': 36})
        orch = ec2.describe_instances(InstanceIds=[orch_id])['Reservations'][0]['Instances'][0]
        public_ip = orch.get('PublicIpAddress')
        state['orchestrator_public_ip'] = public_ip
        state['worker_private_ips'] = list(ips)
        save_state(state)
        print(f'Created EC2 resources. Orchestrator public IP: {public_ip}')
        print('Installing Docker and building images on the instances (takes several minutes)...')
        if not public_ip or not wait_until_ready(public_ip, args.ready_timeout):
            print('System did not become ready in time. Check logs via SSH: '
                  'sudo tail -f /var/log/tp2-bootstrap.log', file=sys.stderr)
            sys.exit(1)
        if not args.skip_tests:
            # test_aws.py reads the orchestrator IP from the deployment state file.
            subprocess.run([sys.executable, str(TEST_SCRIPT)], check=False)
        print(f'\nSend your own request: curl -X POST http://{public_ip}/new_request '
              '-H "Content-Type: application/json" -d \'{"text": "I love cloud computing"}\'')
        print('Cleanup: python aws/cleanup.py')
    except Exception:
        # Persisted state permits safe, explicit cleanup of partially created infrastructure.
        print(f'Deploy interrupted. Inspect {STATE} and run cleanup.py.', file=sys.stderr)
        raise


if __name__ == '__main__':
    main()
