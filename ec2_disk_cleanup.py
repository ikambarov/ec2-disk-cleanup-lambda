import os
import shlex
import time


DEFAULT_REQUIRED_TAG_KEY = "AutoDiskCleanup"
DEFAULT_REQUIRED_TAG_VALUE = "enabled"
DEFAULT_DISK_THRESHOLD_PERCENT = 85
DEFAULT_TMP_DAYS = 1
DEFAULT_LOG_RETENTION_DAYS = 7
DEFAULT_SSM_POLL_SECONDS = 3
DEFAULT_SSM_WAIT_SECONDS = 600
DEFAULT_SSM_TIMEOUT_SECONDS = 600

RUNNING_SSM_STATUSES = {"Pending", "InProgress", "Delayed"}
TERMINAL_SSM_STATUSES = {"Success", "Cancelled", "TimedOut", "Failed", "Cancelling"}


def lambda_handler(event, context):
    import boto3

    event = event or {}

    sns_topic_arn = os.environ["SNS_TOPIC_ARN"]
    required_tag_key = os.environ.get("REQUIRED_TAG_KEY", DEFAULT_REQUIRED_TAG_KEY)
    required_tag_value = os.environ.get("REQUIRED_TAG_VALUE", DEFAULT_REQUIRED_TAG_VALUE)
    disk_threshold_percent = int(os.environ.get("DISK_THRESHOLD_PERCENT", DEFAULT_DISK_THRESHOLD_PERCENT))
    tmp_days = int(os.environ.get("TMP_CLEANUP_OLDER_THAN_DAYS", DEFAULT_TMP_DAYS))
    log_retention_days = int(os.environ.get("LOG_RETENTION_DAYS", DEFAULT_LOG_RETENTION_DAYS))
    ssm_document_name = os.environ.get("SSM_DOCUMENT_NAME", "AWS-RunShellScript")
    ssm_poll_seconds = int(os.environ.get("SSM_POLL_SECONDS", DEFAULT_SSM_POLL_SECONDS))
    ssm_wait_seconds = int(os.environ.get("SSM_WAIT_SECONDS", DEFAULT_SSM_WAIT_SECONDS))
    ssm_timeout_seconds = int(os.environ.get("SSM_TIMEOUT_SECONDS", DEFAULT_SSM_TIMEOUT_SECONDS))

    ec2 = boto3.client("ec2")
    sns = boto3.client("sns")
    ssm = boto3.client("ssm")

    alarm_state = get_alarm_state(event)
    if alarm_state and alarm_state != "ALARM":
        print("Ignoring CloudWatch alarm state: {}".format(alarm_state))
        return {"status": "IGNORED", "reason": "Alarm state is {}".format(alarm_state)}

    instance_id = get_instance_id(event)
    requested_path = get_requested_path(event)
    alarm_name = get_alarm_name(event)

    print("Checking required tag for {}".format(instance_id))
    instance = describe_instance(ec2, instance_id)
    tag_value = get_tag_value(instance, required_tag_key)
    if tag_value != required_tag_value:
        result = {
            "status": "SKIPPED",
            "reason": "Required tag {}={} is missing".format(required_tag_key, required_tag_value),
            "instance_id": instance_id,
            "alarm_name": alarm_name,
            "requested_path": requested_path,
        }
        publish_notification(
            sns,
            sns_topic_arn,
            build_subject(result["status"], instance_id, alarm_name),
            build_notification(result, None, required_tag_key, required_tag_value),
        )
        print(result["reason"])
        return result

    print("Required tag matched. Sending SSM cleanup command to {}".format(instance_id))
    command = build_cleanup_command(
        requested_path,
        disk_threshold_percent,
        tmp_days,
        log_retention_days,
    )
    command_id = send_cleanup_command(ssm, instance_id, ssm_document_name, command, ssm_timeout_seconds)
    invocation = wait_for_command(ssm, command_id, instance_id, ssm_poll_seconds, ssm_wait_seconds)
    report = parse_cleanup_output(invocation.get("StandardOutputContent", ""))

    status = report.get("status") or ("SUCCESS" if invocation.get("Status") == "Success" else "FAILED")
    reason = report.get("reason") or invocation.get("StatusDetails") or invocation.get("Status") or "Unknown result"
    if invocation.get("Status") != "Success" and status == "SUCCESS":
        status = "FAILED"
        reason = invocation.get("StatusDetails") or "SSM command did not finish successfully"

    result = {
        "status": status,
        "reason": reason,
        "instance_id": instance_id,
        "alarm_name": alarm_name,
        "requested_path": requested_path,
        "command_id": command_id,
        "ssm_status": invocation.get("Status"),
        "ssm_status_details": invocation.get("StatusDetails"),
        "disk_used_before_percent": report.get("disk_used_before_percent"),
        "disk_used_after_percent": report.get("disk_used_after_percent"),
        "target_folder": report.get("target_folder"),
        "actions_taken": report.get("actions_taken"),
    }

    publish_notification(
        sns,
        sns_topic_arn,
        build_subject(status, instance_id, alarm_name),
        build_notification(result, invocation, required_tag_key, required_tag_value),
    )
    print("Finished with status {}: {}".format(status, reason))
    return result


def get_alarm_state(event):
    return str(event.get("detail", {}).get("state", {}).get("value") or event.get("alarm_state") or "")


def get_alarm_name(event):
    return str(event.get("detail", {}).get("alarmName") or event.get("alarm_name") or "manual-invocation")


def get_instance_id(event):
    if event.get("instance_id"):
        return event["instance_id"]
    if event.get("InstanceId"):
        return event["InstanceId"]

    dimensions = get_metric_dimensions(event)
    instance_id = dimensions.get("InstanceId") or dimensions.get("instanceId") or dimensions.get("instance_id")
    if not instance_id:
        raise ValueError("Could not find InstanceId in event")
    return instance_id


def get_requested_path(event):
    if event.get("mount_path"):
        return event["mount_path"]
    if event.get("path"):
        return event["path"]

    dimensions = get_metric_dimensions(event)
    return dimensions.get("path") or dimensions.get("Path") or dimensions.get("mount_path") or "/"


def get_metric_dimensions(event):
    dimensions = {}
    detail = event.get("detail", {})

    for metric in detail.get("configuration", {}).get("metrics", []):
        metric_dimensions = metric.get("metricStat", {}).get("metric", {}).get("dimensions", {})
        if metric_dimensions:
            dimensions.update(metric_dimensions)

    trigger_dimensions = detail.get("Trigger", {}).get("Dimensions", [])
    for dimension in trigger_dimensions:
        name = dimension.get("name") or dimension.get("Name")
        value = dimension.get("value") or dimension.get("Value")
        if name and value:
            dimensions[name] = value

    dimensions.update(event.get("dimensions", {}))
    return dimensions


def describe_instance(ec2, instance_id):
    response = ec2.describe_instances(InstanceIds=[instance_id])
    reservations = response.get("Reservations", [])
    if not reservations or not reservations[0].get("Instances"):
        raise ValueError("Instance not found: {}".format(instance_id))
    return reservations[0]["Instances"][0]


def get_tag_value(instance, key):
    for tag in instance.get("Tags", []):
        if tag.get("Key") == key:
            return tag.get("Value")
    return None


def send_cleanup_command(ssm, instance_id, document_name, command, timeout_seconds):
    response = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName=document_name,
        Parameters={"commands": [command]},
        TimeoutSeconds=timeout_seconds,
        Comment="EC2 disk cleanup automation",
    )
    return response["Command"]["CommandId"]


def wait_for_command(ssm, command_id, instance_id, poll_seconds, wait_seconds):
    deadline = time.time() + wait_seconds
    last_invocation = None

    while time.time() < deadline:
        try:
            last_invocation = ssm.get_command_invocation(
                CommandId=command_id,
                InstanceId=instance_id,
            )
        except ssm.exceptions.InvocationDoesNotExist:
            time.sleep(poll_seconds)
            continue

        status = last_invocation.get("Status")
        if status in TERMINAL_SSM_STATUSES or status not in RUNNING_SSM_STATUSES:
            return last_invocation

        time.sleep(poll_seconds)

    if last_invocation:
        return last_invocation
    raise TimeoutError("Timed out waiting for SSM command invocation")


def build_cleanup_command(requested_path, disk_threshold_percent, tmp_days, log_retention_days):
    requested_path = str(requested_path or "/")
    return """#!/bin/sh
set -u

REQUESTED_PATH={requested_path}
THRESHOLD_PERCENT={threshold}
TMP_DAYS={tmp_days}
LOG_RETENTION_DAYS={log_retention_days}

ACTION_COUNT=0
DELETED_COUNT=0
SKIPPED_OPEN_COUNT=0
SKIPPED_UNCHECKED_COUNT=0
TARGET_FOLDER="none"
FAIL_REASON=""

disk_percent() {{
  df -P "$MOUNT_PATH" 2>/dev/null | awk 'NR == 2 {{ gsub(/%/, "", $5); print $5 }}'
}}

top_child_path() {{
  find "$1" -xdev -mindepth 1 -maxdepth 1 -exec du -xsd {{}} + 2>/dev/null | sort -rn | head -n 1 | cut -f 2-
}}

print_top_folders() {{
  echo "TOP_FOLDERS_$1:"
  du -xhd1 "$MOUNT_PATH" 2>/dev/null | sort -hr | head -20
  if [ "$MOUNT_PATH" = "/" ] && [ -d /var ]; then
    echo "TOP_FOLDERS_$1_VAR:"
    du -xhd1 /var 2>/dev/null | sort -hr | head -20
  fi
}}

open_detector() {{
  if command -v lsof >/dev/null 2>&1; then
    echo "lsof"
    return 0
  fi
  if command -v fuser >/dev/null 2>&1; then
    echo "fuser"
    return 0
  fi
  echo "none"
  return 0
}}

is_open_file() {{
  if [ "$OPEN_DETECTOR" = "lsof" ]; then
    lsof -- "$1" >/dev/null 2>&1
    return $?
  fi
  if [ "$OPEN_DETECTOR" = "fuser" ]; then
    fuser -- "$1" >/dev/null 2>&1
    return $?
  fi
  return 2
}}

delete_file_if_safe() {{
  file_path="$1"
  require_detector="$2"

  is_open_file "$file_path"
  open_status=$?
  if [ "$open_status" -eq 0 ]; then
    SKIPPED_OPEN_COUNT=$((SKIPPED_OPEN_COUNT + 1))
    echo "SKIPPED in-use file: $file_path"
    return 0
  fi

  if [ "$open_status" -eq 2 ] && [ "$require_detector" = "yes" ]; then
    SKIPPED_UNCHECKED_COUNT=$((SKIPPED_UNCHECKED_COUNT + 1))
    echo "SKIPPED cannot verify file is closed: $file_path"
    return 0
  fi

  if rm -f -- "$file_path"; then
    ACTION_COUNT=$((ACTION_COUNT + 1))
    DELETED_COUNT=$((DELETED_COUNT + 1))
    echo "DELETED file: $file_path"
  else
    echo "FAILED delete file: $file_path"
  fi
}}

cleanup_tmp() {{
  echo "ACTION cleaning /tmp files older than $TMP_DAYS days"
  candidate_file="$(mktemp)"
  find /tmp -xdev -type f -mtime +"$TMP_DAYS" -print 2>/dev/null > "$candidate_file"
  while IFS= read -r file_path; do
    [ -n "$file_path" ] || continue
    delete_file_if_safe "$file_path" "no"
  done < "$candidate_file"
  rm -f "$candidate_file"

  candidate_file="$(mktemp)"
  find /tmp -xdev -depth -mindepth 1 -type d -empty -mtime +"$TMP_DAYS" -print 2>/dev/null > "$candidate_file"
  while IFS= read -r dir_path; do
    [ -n "$dir_path" ] || continue
    if rmdir -- "$dir_path" 2>/dev/null; then
      ACTION_COUNT=$((ACTION_COUNT + 1))
      DELETED_COUNT=$((DELETED_COUNT + 1))
      echo "DELETED empty directory: $dir_path"
    fi
  done < "$candidate_file"
  rm -f "$candidate_file"
}}

cleanup_logs() {{
  if command -v journalctl >/dev/null 2>&1; then
    echo "ACTION vacuuming systemd journal older than $LOG_RETENTION_DAYS days"
    if journalctl --vacuum-time="${{LOG_RETENTION_DAYS}}d" 2>&1 | sed 's/^/JOURNAL: /'; then
      ACTION_COUNT=$((ACTION_COUNT + 1))
    else
      echo "SKIPPED journal vacuum failed"
    fi
  fi

  if [ "$OPEN_DETECTOR" = "none" ]; then
    echo "SKIPPED rotated log deletion because lsof/fuser is unavailable"
    return 0
  fi

  echo "ACTION deleting rotated /var/log files older than $LOG_RETENTION_DAYS days after in-use check"
  candidate_file="$(mktemp)"
  find /var/log -xdev -type f \\( \\
    -name "*.gz" -o \\
    -name "*.xz" -o \\
    -name "*.bz2" -o \\
    -name "*.old" -o \\
    -name "*.1" -o \\
    -name "*.log.[0-9]*" -o \\
    -name "*.log-*" \\
  \\) -mtime +"$LOG_RETENTION_DAYS" -print 2>/dev/null > "$candidate_file"

  while IFS= read -r file_path; do
    [ -n "$file_path" ] || continue
    delete_file_if_safe "$file_path" "yes"
  done < "$candidate_file"
  rm -f "$candidate_file"
}}

MOUNT_PATH="$(df -P "$REQUESTED_PATH" 2>/dev/null | awk 'NR == 2 {{ print $6 }}')"
if [ -z "$MOUNT_PATH" ]; then
  echo "STATUS: FAILED"
  echo "REASON: Requested path is not available: $REQUESTED_PATH"
  exit 1
fi

OPEN_DETECTOR="$(open_detector)"
DISK_USED_BEFORE="$(disk_percent)"
ROOT_TOP="$(top_child_path "$MOUNT_PATH")"

echo "REQUESTED_PATH: $REQUESTED_PATH"
echo "MOUNT_PATH: $MOUNT_PATH"
echo "OPEN_FILE_DETECTOR: $OPEN_DETECTOR"
echo "DISK_USED_BEFORE_PERCENT: $DISK_USED_BEFORE"
echo "HIGHEST_FOLDER: $ROOT_TOP"
print_top_folders "BEFORE"

if [ "$MOUNT_PATH" = "/tmp" ] || [ "$ROOT_TOP" = "/tmp" ]; then
  TARGET_FOLDER="/tmp"
  cleanup_tmp
elif [ "$MOUNT_PATH" = "/var/log" ]; then
  TARGET_FOLDER="/var/log"
  cleanup_logs
else
  FAIL_REASON="Highest folder is not /tmp or /var/log"
fi

DISK_USED_AFTER="$(disk_percent)"
echo "DISK_USED_AFTER_PERCENT: $DISK_USED_AFTER"
echo "TARGET_FOLDER: $TARGET_FOLDER"
echo "ACTIONS_TAKEN: $ACTION_COUNT"
echo "FILES_DELETED: $DELETED_COUNT"
echo "FILES_SKIPPED_IN_USE: $SKIPPED_OPEN_COUNT"
echo "FILES_SKIPPED_UNCHECKED: $SKIPPED_UNCHECKED_COUNT"
print_top_folders "AFTER"

if [ "$ACTION_COUNT" -gt 0 ] && [ "$DISK_USED_AFTER" -lt "$THRESHOLD_PERCENT" ] && \\
   {{ [ "$TARGET_FOLDER" = "/tmp" ] || [ "$TARGET_FOLDER" = "/var/log" ]; }}; then
  echo "STATUS: SUCCESS"
  echo "REASON: Cleaned $TARGET_FOLDER and disk usage is below $THRESHOLD_PERCENT percent"
  exit 0
fi

if [ -z "$FAIL_REASON" ]; then
  if [ "$ACTION_COUNT" -eq 0 ]; then
    FAIL_REASON="No cleanup actions were taken"
  elif [ "$DISK_USED_AFTER" -ge "$THRESHOLD_PERCENT" ]; then
    FAIL_REASON="Disk usage is still at or above $THRESHOLD_PERCENT percent"
  else
    FAIL_REASON="Cleanup target was not an approved folder"
  fi
fi

echo "STATUS: FAILED"
echo "REASON: $FAIL_REASON"
exit 1
""".format(
        requested_path=shlex.quote(requested_path),
        threshold=int(disk_threshold_percent),
        tmp_days=int(tmp_days),
        log_retention_days=int(log_retention_days),
    )


def parse_cleanup_output(output):
    report = {}
    fields = {
        "STATUS": "status",
        "REASON": "reason",
        "DISK_USED_BEFORE_PERCENT": "disk_used_before_percent",
        "DISK_USED_AFTER_PERCENT": "disk_used_after_percent",
        "TARGET_FOLDER": "target_folder",
        "ACTIONS_TAKEN": "actions_taken",
    }

    for line in output.splitlines():
        for prefix, key in fields.items():
            marker = prefix + ":"
            if line.startswith(marker):
                report[key] = line[len(marker):].strip()

    for key in ("disk_used_before_percent", "disk_used_after_percent", "actions_taken"):
        if key in report:
            try:
                report[key] = int(report[key])
            except ValueError:
                pass

    return report


def build_subject(status, instance_id, alarm_name):
    subject = "EC2 disk cleanup {} {}".format(status, instance_id)
    if alarm_name and alarm_name != "manual-invocation":
        subject = "{} {}".format(subject, alarm_name)
    return subject[:100]


def build_notification(result, invocation, required_tag_key, required_tag_value):
    lines = [
        "EC2 Disk Cleanup Automation",
        "",
        "Status: {}".format(result.get("status")),
        "Reason: {}".format(result.get("reason")),
        "InstanceId: {}".format(result.get("instance_id")),
        "Alarm: {}".format(result.get("alarm_name")),
        "Requested path: {}".format(result.get("requested_path")),
        "Required tag: {}={}".format(required_tag_key, required_tag_value),
    ]

    if result.get("target_folder"):
        lines.extend([
            "Target folder: {}".format(result.get("target_folder")),
            "Disk used before: {}%".format(result.get("disk_used_before_percent")),
            "Disk used after: {}%".format(result.get("disk_used_after_percent")),
            "Actions taken: {}".format(result.get("actions_taken")),
        ])

    if result.get("command_id"):
        lines.extend([
            "SSM command id: {}".format(result.get("command_id")),
            "SSM status: {}".format(result.get("ssm_status")),
            "SSM details: {}".format(result.get("ssm_status_details")),
        ])

    if invocation:
        stdout = invocation.get("StandardOutputContent", "")
        stderr = invocation.get("StandardErrorContent", "")
        if stdout:
            lines.extend(["", "Command output:", truncate(stdout, 18000)])
        if stderr:
            lines.extend(["", "Command errors:", truncate(stderr, 4000)])

    return "\n".join(lines)


def publish_notification(sns, topic_arn, subject, message):
    print("Publishing notification: {}".format(subject))
    sns.publish(
        TopicArn=topic_arn,
        Subject=subject,
        Message=message,
    )


def truncate(value, max_chars):
    value = str(value)
    if len(value) <= max_chars:
        return value
    return value[:max_chars] + "\n... truncated ..."
