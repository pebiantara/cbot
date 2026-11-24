"""Watches both cloudflared containers' logs (via the Docker Engine API, over
the read-only mounted socket) for their current trycloudflare.com URLs:

- cbot-cloudflared (webhook-server, port 8090) -> re-registers Helius/Alchemy/
  Telegram webhooks whenever it changes.
- cbot-cloudflared-dashboard (dashboard, port 8501) -> just persists its URL to
  dashboard_url.json so webhook_server.py's /telegram/webhook handler can hand
  it back for the /status_accuracy and /list bot commands.

Also self-heals: Cloudflare's free Quick Tunnel occasionally drops a tunnel
server-side ("Unauthorized: Tunnel not found"), and cloudflared then retries
forever against that same dead tunnel ID without ever printing a new URL --
so the log-watching above never notices anything changed, even though the
tunnel is permanently dead until the container restarts and requests a fresh
one. Periodically health-checking the live public URL (not just watching for
a new one in the logs) catches that stuck state and forces the restart.
Despite the docker.sock mount being read-only, container.restart() over it
still works -- `:ro` on a bind-mounted socket blocks file-level writes, not
the connect()/send() calls a Unix socket client actually uses, so it's not a
real restriction on the Docker API itself.

Runs forever as the `webhook-registrar` service in docker-compose.yml.
"""

import json
import os
import re
import time

import docker
import requests

from register_webhooks import register_all

WEBHOOK_SERVER_CONTAINER = "cbot-cloudflared"
DASHBOARD_CONTAINER = "cbot-cloudflared-dashboard"
DASHBOARD_URL_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard_url.json")
POLL_SECONDS = 10
HEALTH_CHECK_FAILURE_THRESHOLD = 3  # ~30s of outage (POLL_SECONDS apart) before forcing a restart
# Cloudflare edge-level error codes -- these mean the tunnel itself is broken
# (wrong/expired/unregistered tunnel ID), not an app-level error from our own
# server, which would come back as a normal (if non-200) HTTP response.
CLOUDFLARE_EDGE_ERROR_CODES = {502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 530}


def get_latest_url(client, container_name):
    container = client.containers.get(container_name)
    logs = container.logs(tail=200).decode("utf-8", errors="replace")
    matches = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", logs)
    return matches[-1] if matches else None


def webhook_tunnel_is_healthy(url):
    """Hits our own /health through the public tunnel -- a real 200 means
    tunnel + backend are both actually reachable end to end."""
    try:
        resp = requests.get(f"{url}/health", timeout=8)
    except requests.RequestException:
        return False
    return resp.status_code == 200


def dashboard_tunnel_is_healthy(url):
    """Streamlit has no fixed health endpoint, so just confirm the tunnel
    itself is alive: any response that isn't a Cloudflare edge error code
    means the tunnel delivered the request to the backend."""
    try:
        resp = requests.get(url, timeout=8)
    except requests.RequestException:
        return False
    return resp.status_code not in CLOUDFLARE_EDGE_ERROR_CODES


def restart_tunnel(client, container_name, label):
    print(f"{label} tunnel failed {HEALTH_CHECK_FAILURE_THRESHOLD} health checks in a row -- "
          f"restarting {container_name} to force a fresh tunnel...", flush=True)
    try:
        client.containers.get(container_name).restart(timeout=10)
    except Exception as e:
        print(f"Restart of {container_name} failed, will retry next poll: {e}", flush=True)


def main():
    # Explicit short timeout -- added 2026-11-22 after a real incident: the
    # default docker-py client timeout left a hung restart() call (seen
    # twice: "Read timed out (read timeout=70)") blocking this ENTIRE
    # single-threaded loop for ~70s, during which BOTH tunnels' health
    # checks are frozen too -- so one stuck restart call silently extended
    # the outage well past the normal ~30s detection window. A short
    # timeout makes a stuck call fail fast instead, so the next poll (10s
    # later) gets a fresh attempt instead of waiting out a long HTTP hang.
    client = docker.from_env(timeout=15)
    print(f"Watching {WEBHOOK_SERVER_CONTAINER} and {DASHBOARD_CONTAINER} for tunnel URL changes...",
          flush=True)
    last_webhook_url = None
    last_dashboard_url = None
    webhook_failures = 0
    dashboard_failures = 0

    while True:
        try:
            webhook_url = get_latest_url(client, WEBHOOK_SERVER_CONTAINER)
        except Exception as e:
            print(f"Couldn't read {WEBHOOK_SERVER_CONTAINER} logs, will retry: {e}", flush=True)
            webhook_url = None

        if webhook_url and webhook_url != last_webhook_url:
            print(f"New webhook-server tunnel URL detected: {webhook_url}", flush=True)
            try:
                register_all(webhook_url)
                last_webhook_url = webhook_url
                webhook_failures = 0
            except Exception as e:
                print(f"Registration failed, will retry next poll: {e}", flush=True)
        elif last_webhook_url:
            if webhook_tunnel_is_healthy(last_webhook_url):
                webhook_failures = 0
            else:
                webhook_failures += 1
                print(f"webhook-server tunnel health check failed ({webhook_failures}/"
                      f"{HEALTH_CHECK_FAILURE_THRESHOLD}): {last_webhook_url}", flush=True)
                if webhook_failures >= HEALTH_CHECK_FAILURE_THRESHOLD:
                    restart_tunnel(client, WEBHOOK_SERVER_CONTAINER, "webhook-server")
                    last_webhook_url = None
                    webhook_failures = 0

        try:
            dashboard_url = get_latest_url(client, DASHBOARD_CONTAINER)
        except Exception as e:
            print(f"Couldn't read {DASHBOARD_CONTAINER} logs, will retry: {e}", flush=True)
            dashboard_url = None

        if dashboard_url and dashboard_url != last_dashboard_url:
            print(f"New dashboard tunnel URL detected: {dashboard_url}", flush=True)
            with open(DASHBOARD_URL_JSON, "w") as f:
                json.dump({"url": dashboard_url}, f)
            last_dashboard_url = dashboard_url
            dashboard_failures = 0
        elif last_dashboard_url:
            if dashboard_tunnel_is_healthy(last_dashboard_url):
                dashboard_failures = 0
            else:
                dashboard_failures += 1
                print(f"dashboard tunnel health check failed ({dashboard_failures}/"
                      f"{HEALTH_CHECK_FAILURE_THRESHOLD}): {last_dashboard_url}", flush=True)
                if dashboard_failures >= HEALTH_CHECK_FAILURE_THRESHOLD:
                    restart_tunnel(client, DASHBOARD_CONTAINER, "dashboard")
                    last_dashboard_url = None
                    dashboard_failures = 0

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
