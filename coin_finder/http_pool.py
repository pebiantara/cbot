"""Shared, connection-pooled HTTP session for Binance REST calls --
added 2026-11-17 after real-time investigation of sustained 170-230%
CPU usage (docker stats) on quick-scalp-alert. Every hot-path caller
(delta_map build, fade/chase candle fetches, live price lookups,
funding rate) was using a fresh urllib.request.urlopen() per call,
opening and tearing down a brand-new TCP+TLS connection every single
time -- at this container's call volume (~150-200+ requests/minute
across all the per-poll work added this session: delta_map, fade scan,
chase scan, extension checks, freshness re-checks, regime leader
prices), the repeated TLS handshakes (RSA/ECDHE key exchange) are a
real, measurable CPU cost. One shared requests.Session (HTTP keep-alive
+ connection pooling by default) reused across every hot-path caller
instead -- same connections get reused across calls to the same host
(api.binance.com / fapi.binance.com), no code elsewhere needs to change
its call signature, just swap urlopen for SESSION.get.
"""

import requests

SESSION = requests.Session()
