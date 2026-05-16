# EC2 Disk Cleanup Lambda

Simple AWS Lambda that runs an SSM cleanup command, and sends an SNS notification for high disk usage.

The Lambda only inspects and cleans instances tagged:

```text
AutoDiskCleanup=enabled
```

Instances without the required tag are skipped before any SSM command is sent.

## Environment Variables

Required:

- `SNS_TOPIC_ARN`

Optional:

- `REQUIRED_TAG_KEY` default `AutoDiskCleanup`
- `REQUIRED_TAG_VALUE` default `enabled`
- `DISK_THRESHOLD_PERCENT` default `85`
- `TMP_CLEANUP_OLDER_THAN_DAYS` default `1`
- `LOG_RETENTION_DAYS` default `7`
- `SSM_DOCUMENT_NAME` default `AWS-RunShellScript`
- `SSM_POLL_SECONDS` default `3`
- `SSM_WAIT_SECONDS` default `600`
- `SSM_TIMEOUT_SECONDS` default `600`

## Cleanup Rules

- `/tmp`: deletes files older than `TMP_CLEANUP_OLDER_THAN_DAYS`
- `/var/log`: vacuums old systemd journal entries and deletes old rotated log files

The run is marked `SUCCESS` only when:

- the target folder was `/tmp` or `/var/log`
- at least one cleanup action was taken
- disk usage after cleanup is below `DISK_THRESHOLD_PERCENT`

Otherwise, the run is marked `FAILED`.

## Package

```bash
./package_lambda.sh
```

Upload `dist/ec2-disk-cleanup-lambda.zip` to Lambda.

Use this handler:

```text
ec2_disk_cleanup.lambda_handler
```

## EventBridge

Create a CloudWatch alarm for the CloudWatch Agent metric `disk_used_percent`, then route alarm state changes to Lambda with EventBridge.

The Lambda ignores non-`ALARM` state changes.

## Manual Lambda Test

```json
{
  "instance_id": "i-0123456789abcdef0",
  "mount_path": "/"
}
```

CloudWatch alarm events are also supported when the alarm metric dimensions include `InstanceId` and optionally `path`.

## IAM Role Requirements

Lambda role:

```json
{
  "Effect": "Allow",
  "Action": [
    "ec2:DescribeInstances",
    "ssm:SendCommand",
    "ssm:GetCommandInvocation",
    "sns:Publish"
  ],
  "Resource": "*"
}
```

The Lambda role also needs normal CloudWatch Logs permissions.

EC2 instance profile:

```text
AmazonSSMManagedInstanceCore
```

## Instance Requirements

- SSM Agent installed and running
- CloudWatch Agent
- Instance profile with SSM permissions
- Network access to Systems Manager endpoints
- CloudWatch Agent publishing `disk_used_percent`
- Required tag: `AutoDiskCleanup=enabled`
