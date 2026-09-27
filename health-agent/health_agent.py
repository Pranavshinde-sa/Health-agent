#!/usr/bin/env python3
"""
Primary Health Agent (HTTP health-check edition)
-------------------------------------------------
Watches a list of HTTP health endpoints and reports whether each one
is healthy. This works the same way whether the target is a container
on your local Docker host, a task on AWS ECS, or anything else that
exposes a health endpoint over HTTP — there is no dependency on the
Docker socket, so this design carries straight over to ECS.

Every check is written to stdout and to a log file. When a watched
target goes DOWN (or comes back UP after being down), an alert is
sent to the developer only.

Alert delivery is pluggable:
  - If ALERT_WEBHOOK_URL is set, the alert is POSTed there (Slack /
    Discord / generic incoming webhook format both work fine since we
    send a plain {"text": "..."} JSON body).
  - If ALERT_WEBHOOK_URL is NOT set, the alert is still guaranteed to
    reach the developer: it is printed to stdout with an "[ALERT]"
    tag and written to the same log file, so `docker logs` or the log
    file itself is always a valid alert channel with zero setup.
  - On AWS, swap send_alert()'s webhook POST for a boto3 SNS publish
    call and subscribe your email/SMS to the topic — same trigger
    logic, different delivery call. See the comment in send_alert().

Configuration is via environment variables (see docker-compose.yml):
  TARGETS                 comma-separated "name=url" pairs to health-check,
                           e.g. "demo-service-1=http://demo-service-1:5000/health,demo-service-2=http://demo-service-2:5000/health"
  CHECK_INTERVAL_SECONDS   how often to poll (default: 15)
  REQUEST_TIMEOUT_SECONDS  per-request timeout (default: 5)
  LOG_FILE                 path to the log file (default: /var/log/health_agent/health_agent.log)
  ALERT_WEBHOOK_URL        optional webhook URL for push alerts to the developer
"""

import os
import sys
import time
import logging
from datetime import datetime, timezone
import smtplib
from email.message import EmailMessage

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _parse_targets(raw: str) -> dict:
    """
    Parses "name=url,name=url" into {name: url}. Skips malformed entries
    with a startup error rather than crashing, so one typo doesn't take
    down monitoring for every other target.
    """
    targets = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            print(f"Ignoring malformed TARGETS entry (expected name=url): {entry}", file=sys.stderr)
            continue
        name, url = entry.split("=", 1)
        name, url = name.strip(), url.strip()
        if name and url:
            targets[name] = url
    return targets


TARGETS = _parse_targets(os.environ.get("TARGETS", ""))
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "15"))
REQUEST_TIMEOUT_SECONDS = int(os.environ.get("REQUEST_TIMEOUT_SECONDS", "5"))
LOG_FILE = os.environ.get("LOG_FILE", "/var/log/health_agent/health_agent.log")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "").strip()
ALERT_EMAIL_TO = os.environ.get("ALERT_EMAIL_TO", "").strip()

# ---------------------------------------------------------------------------
# Logging: stdout (so `docker logs` shows it) + a persistent log file
# ---------------------------------------------------------------------------

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

logger = logging.getLogger("health_agent")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(_fmt)
logger.addHandler(_stream_handler)

_file_handler = logging.FileHandler(LOG_FILE)
_file_handler.setFormatter(_fmt)
logger.addHandler(_file_handler)


def send_alert(message: str) -> None:
    """
    Send a health alert to the developer via email.
    The alert is always logged locally as well.
    """

    logger.warning("[ALERT] %s", message)

    if not SMTP_USERNAME or not SMTP_PASSWORD or not ALERT_EMAIL_TO:
        logger.warning(
            "Email alerting is not configured. "
            "Set SMTP_USERNAME, SMTP_PASSWORD and ALERT_EMAIL_TO."
        )
        return

    try:
        email = EmailMessage()

        email["Subject"] = "Health Agent Alert"
        email["From"] = SMTP_USERNAME
        email["To"] = ALERT_EMAIL_TO

        email.set_content(
            f"""Health Agent Alert

{message}

This alert was generated automatically by the Health Agent.
"""
        )

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as smtp:
            smtp.starttls()
            smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(email)

        logger.info("Email alert sent successfully to %s", ALERT_EMAIL_TO)

    except Exception as exc:
        logger.error("Failed to send email alert: %s", exc)

def check_target(name: str, url: str) -> str:
    """
    Calls the target's HTTP health endpoint and returns one of:
    "healthy", "unhealthy", "unreachable"

    - "healthy": got a 2xx response
    - "unhealthy": got a response, but a non-2xx status code
    - "unreachable": connection refused, timed out, DNS failure, etc.
      (the equivalent of "container not found/not running" when you
      have no Docker socket to ask directly — on ECS this is exactly
      what you'll see if a task is down or hasn't registered yet)
    """
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        return "healthy" if resp.ok else "unhealthy"
    except requests.RequestException as exc:
        logger.debug("Target '%s' unreachable: %s", name, exc)
        return "unreachable"


def main() -> None:
    if not TARGETS:
        logger.error(
            "No targets configured. Set TARGETS to a comma-separated "
            "'name=url' list, e.g. "
            "'demo-service-1=http://demo-service-1:5000/health'. Exiting."
        )
        sys.exit(1)

    logger.info(
        "Health agent starting. Monitoring %s every %ss. Log file: %s",
        TARGETS, CHECK_INTERVAL_SECONDS, LOG_FILE,
    )
    if SMTP_USERNAME and SMTP_PASSWORD and ALERT_EMAIL_TO:
        logger.info("Email alerting is ENABLED.")
    else:
        logger.info("Email alerting is DISABLED.")

    # Tracks the last known status per target so we only alert on a
    # *change* of state, not on every single poll.
    last_status = {name: None for name in TARGETS}

    while True:
        timestamp = datetime.now(timezone.utc).isoformat()

        for name, url in TARGETS.items():
            status = check_target(name, url)
            logger.info("Check: target=%s url=%s status=%s", name, url, status)

            if last_status[name] is None:
                # First observation — just record it, no alert on startup
                # unless it's already unhealthy, which is worth flagging.
                if status != "healthy":
                    send_alert(
                        f"Target '{name}' ({url}) is {status.upper()} at "
                        f"startup ({timestamp})."
                    )
            elif status != last_status[name]:
                if status == "healthy":
                    send_alert(
                        f"Target '{name}' ({url}) RECOVERED and is now "
                        f"healthy ({timestamp})."
                    )
                else:
                    send_alert(
                        f"Target '{name}' ({url}) is {status.upper()} "
                        f"({timestamp})."
                    )

            last_status[name] = status

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
