"""Terminate only resources recorded in aws/.deployment-state.json."""
import json
import pathlib
import time

STATE = pathlib.Path(__file__).resolve().parent / '.deployment-state.json'


def main():
    if not STATE.exists():
        print('No deployment state found. Nothing to clean up.')
        return
    data = json.loads(STATE.read_text())
    print(json.dumps(data, indent=2))
    import boto3
    ec2 = boto3.client('ec2', region_name=data['region'])
    ids = data.get('instance_ids', [])
    if ids:
        ec2.terminate_instances(InstanceIds=ids)
        ec2.get_waiter('instance_terminated').wait(InstanceIds=ids, WaiterConfig={'Delay': 15, 'MaxAttempts': 80})
    for gid in reversed(data.get('security_group_ids', [])):
        for attempt in range(12):
            try:
                ec2.delete_security_group(GroupId=gid)
                break
            except ec2.exceptions.ClientError as exc:
                if exc.response.get('Error', {}).get('Code') != 'DependencyViolation' or attempt == 11:
                    raise
                time.sleep(5)
    STATE.unlink()
    print('Tracked instances terminated and security groups deleted.')


if __name__ == '__main__':
    main()
