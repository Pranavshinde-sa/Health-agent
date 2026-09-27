# Container Health Monitor

A primary health agent that polls HTTP `/health` endpoints and alerts
the developer (only) when a target goes unhealthy or recovers, plus
two demo services to watch it work against.

This checks health over **HTTP**, not the Docker socket — so the same
agent code works unchanged whether the target is a container on your
local Docker host, a task on AWS ECS, or anything else with a health
endpoint. That was a deliberate choice: `docker.sock` isn't available
to you on ECS, but a health endpoint is.

## What's inside

```
health-monitor/
├── docker-compose.yml
├── health-agent/          # the health agent
│   ├── Dockerfile
│   ├── health_agent.py
│   └── requirements.txt
├── demo-service-1/         # dummy Flask service #1
│   ├── Dockerfile
│   └── app.py
└── demo-service-2/         # dummy Flask service #2
    ├── Dockerfile
    └── app.py
```

## How the agent works

- Every `CHECK_INTERVAL_SECONDS` (default 15s), it sends an HTTP GET to
  each URL in `TARGETS` and classifies the result:
  - `healthy` — got a 2xx response
  - `unhealthy` — got a response, but a non-2xx status
  - `unreachable` — connection refused / timed out / DNS failure
    (what you'll see if the container/task is down or not yet up)
- Every check is logged to **stdout** (`docker logs health-agent`) and
  to a **log file** at `LOG_FILE` (default
  `/var/log/health_agent/health_agent.log`, mounted to
  `./health-agent-logs/` on the host by docker-compose).
- It only fires an **alert** when a target's state *changes*
  (healthy → unhealthy/unreachable, or back to healthy), so you don't
  get spammed every poll.
- Alerts always land in the log/stdout. If you set `ALERT_WEBHOOK_URL`
  (a Slack/Discord/generic incoming webhook), the same alert is also
  POSTed there. This is a one-way channel to you, the developer — it
  never reaches end users of the demo services.

## Run it

```bash
docker compose up --build
```

This starts:
- `demo-service-1` on http://localhost:5001
- `demo-service-2` on http://localhost:5002
- `health-agent`, which starts polling both `/health` endpoints immediately

Watch it work:

```bash
docker compose logs -f health-agent
```

## Trigger an alert

Stop one of the demo containers and watch the agent notice and alert:

```bash
docker stop demo-service-1
docker compose logs -f health-agent
```

Within one check interval you should see something like:
`[ALERT] Target 'demo-service-1' (http://demo-service-1:5000/health) is UNREACHABLE ...`

Bring it back:

```bash
docker start demo-service-1
```

and you'll see a `RECOVERED` alert.

## Configuration (env vars, set in docker-compose.yml)

| Variable                   | Default                                   | Meaning                                          |
|-----------------------------|--------------------------------------------|----------------------------------------------------|
| `TARGETS`                  | *(required)*                              | Comma-separated `name=url` pairs to health-check   |
| `CHECK_INTERVAL_SECONDS`   | `15`                                       | Poll interval                                      |
| `REQUEST_TIMEOUT_SECONDS`  | `5`                                        | Per-request timeout                                |
| `LOG_FILE`                 | `/var/log/health_agent/health_agent.log`  | Where status/alerts are logged                     |
| `ALERT_WEBHOOK_URL`        | *(empty)*                                 | Optional webhook for push alerts                   |

## Moving this to AWS ECS later

The migration is small precisely because detection and alerting are
decoupled:

1. **Targets**: instead of `http://demo-service-1:5000/health`, point
   `TARGETS` at each ECS service's load balancer DNS name (or a
   service-discovery/Cloud Map name), e.g.
   `payments-api=http://payments-api.internal:8080/health`.
2. **Where the agent itself runs**: as its own small ECS task/service
   (Fargate is fine — it's just a Python loop, no privileged access
   needed since it no longer touches `docker.sock`).
3. **Alerting**: the webhook path works as-is on ECS with zero changes.
   If you want to go "properly AWS," swap the webhook POST in
   `send_alert()` for a `boto3` SNS `publish()` call — there's a
   commented-out example already in `health_agent.py` showing exactly
   where. Subscribe your email/SMS to that SNS topic once in the
   console, and every alert lands there. The IAM role attached to the
   task just needs `sns:Publish` on that topic.
4. Everything else — the "only alert on state change" logic, the log
   file, the healthy/unhealthy/unreachable classification — needs no
   changes at all.

## Notes

- The demo services' `/health` endpoint returns a simple JSON
  `{"status": "ok", ...}` with a 200 — good enough to prove the check
  works; for a real service, make `/health` actually verify DB
  connections, dependencies, etc. before returning 200.
- Swap the webhook POST in `send_alert()` for SMTP/email, PagerDuty,
  or Telegram if you'd rather get alerts there instead of Slack/Discord.
