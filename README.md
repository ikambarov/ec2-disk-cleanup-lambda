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
- `TMP_FILE_RETENTION_DAYS` default `1`
- `LOG_FILE_RETENTION_DAYS` default `7`
- `SSM_DOCUMENT_NAME` default `AWS-RunShellScript`
- `SSM_POLL_SECONDS` default `3`
- `SSM_WAIT_SECONDS` default `600`
- `SSM_TIMEOUT_SECONDS` default `600`

## Cleanup Rules

- `/tmp`: deletes files older than `TMP_FILE_RETENTION_DAYS`
- `/var/log`: vacuums old systemd journal entries and deletes old rotated log files, older than `LOG_FILE_RETENTION_DAYS`

The run is marked `SUCCESS` only when:

- at least one cleanup action was taken in `/tmp` or `/var/log`
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

## Manual Lambda Test

```json
{
  "instance_id": "i-0123456789abcdef0",
  "mount_path": "/"
}
```

## IAM Requirements

Attach these AWS managed policies to the Lambda execution role:

- `arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole`
- `arn:aws:iam::aws:policy/AmazonEC2ReadOnlyAccess`
- `arn:aws:iam::aws:policy/AmazonSSMFullAccess`
- `arn:aws:iam::aws:policy/AmazonSNSFullAccess`

## Instance Requirements

- SSM Agent installed and running
- CloudWatch Agent publishing `disk_used_percent`
- Instance profile with SSM permissions
- Network access to Systems Manager endpoints
- Required tag: `AutoDiskCleanup=enabled`
