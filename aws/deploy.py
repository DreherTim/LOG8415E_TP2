"""Provision LOG8415E TP2 in AWS.
"""
import argparse
import json
import os
import pathlib
import re
import sys
import time
from datetime import timezone
from urllib.parse import urlparse

ROOT = pathlib.Path(__file__).resolve().parent
STATE = ROOT / '.deployment-state.json'
TAG = 'log8415e-tp2'


def make_userdata(role, repo, ref, ips=('', '')):
    template = (ROOT / 'user_data.sh').read_text()
    substitutions = {
        '__ROLE__': role,
        '__REPO_URL__': repo,
        '__REPO_REF__': ref,
        '__WORKER1_IP__': ips[0],
        '__WORKER2_IP__': ips[1],
    }
    for key, value in substitutions.items():
        template = template.replace(key, value)
    return template


def validate_repo(repo, ref):
    parsed = urlparse(repo)
    if parsed.scheme != 'https' or parsed.hostname not in {'github.com'} or parsed.username or parsed.password:
        raise ValueError('Use an anonymous HTTPS github.com clone URL; never embed tokens.')
    if not re.fullmatch(r'[A-Za-z0-9_.\-/]+', parsed.path) or parsed.path in {'/', ''}:
        raise ValueError('Invalid repository path.')
    if not re.fullmatch(r'[A-Za-z0-9_.\-/]+', ref) or ref.startswith('-'):
        raise ValueError('Invalid git branch/tag.')


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repo-url', default='https://github.com/DreherTim/LOG8415E_TP2.git')
    p.add_argument('--repo-ref', default='main', help='Git branch or tag accessible to git clone')
    p.add_argument('--region', default=os.getenv('AWS_REGION', 'us-east-1'))
    p.add_argument('--instance-type', default='t2.large')
    p.add_argument('--subnet-id', help='Subnet for all three instances (default VPC first available subnet)')
    p.add_argument('--key-name', help='Existing EC2 key pair for SSH, if desired')
    p.add_argument('--ssh-cidr', help='Admin CIDR for port 22, e.g. 203.0.113.7/32 (optional)')
    p.add_argument('--name', default=TAG, help='Unique tag prefix for the deployment')
    return p.parse_args()


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
    # Existing Compose exposes host:5001 -> container:5000; keep it unchanged for now.
    ec2.authorize_security_group_ingress(GroupId=worker, IpPermissions=[{
        'IpProtocol': 'tcp', 'FromPort': 5001, 'ToPort': 5001,
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
                  UserData=make_userdata('worker' if role.startswith('worker') else 'orchestrator',
                                         args.repo_url, args.repo_ref, ips),
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
    validate_repo(args.repo_url, args.repo_ref)
    if args.ssh_cidr:
        import ipaddress
        ipaddress.ip_network(args.ssh_cidr, strict=False)
        if not args.key_name:
            raise ValueError('--ssh-cidr requires --key-name')
    print(json.dumps({'region': args.region, 'instance_type': args.instance_type,
                      'instances': ['worker1', 'worker2', 'orchestrator'],
                      'repository': args.repo_url, 'ref': args.repo_ref,
                      'worker_host_port': 5001, 'worker_container_port': 5000,
                      'mode': 'DEPLOY'}, indent=2))
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
        print(f'Created EC2 resources. Orchestrator public IP: {orch.get("PublicIpAddress", "unavailable")}')
        print('EC2 running does NOT mean cloud-init, Docker build, or model download succeeded.')
        print('Wait for bootstrap; then test POST http://PUBLIC_IP/new_request .')
        print('Check logs with SSH: sudo tail -f /var/log/tp2-bootstrap.log')
        print('Cleanup: python aws/cleanup.py')
    except Exception:
        # Persisted state permits safe, explicit cleanup of partially created infrastructure.
        print(f'Deploy interrupted. Inspect {STATE} and run cleanup.py.', file=sys.stderr)
        raise


if __name__ == '__main__':
    main()
