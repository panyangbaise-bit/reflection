"""Run ON the intended mainland host; DNS/TCP/TLS evidence only, no paid API calls.

No credentials, user portfolios, HTTP redirects, proxies or response bodies are used.
A successful handshake is not API/authentication/data-rights/SLA verification.
"""

import argparse
import json
import socket
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


def endpoint(value):
    url = urlsplit(value)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise argparse.ArgumentTypeError(
            "Use an HTTPS endpoint without credentials, query or fragment"
        )
    return value


def probe(value, timeout):
    url = urlsplit(value)
    host, port = url.hostname, url.port or 443
    started = time.monotonic()
    result = {"endpoint": value, "ok": False}
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        result["resolved_addresses"] = sorted({a[4][0] for a in addresses})
        context = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as tcp:
            with context.wrap_socket(tcp, server_hostname=host) as tls:
                result.update(ok=True, tls=tls.version(), connected_address=tls.getpeername()[0])
    except (OSError, ValueError) as exc:
        result["error_type"] = type(exc).__name__
    result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", required=True, help="Label only, e.g. aliyun-cn-hangzhou")
    parser.add_argument("--endpoint", type=endpoint, action="append", required=True)
    parser.add_argument("--samples", type=int, default=3, choices=range(1, 11))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    results = [probe(url, 5) for url in args.endpoint for _ in range(args.samples)]
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "deployment_declared": args.deployment,
        "scope": "DNS/TCP/TLS only; deployment label is not independently verified",
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "handshakes_ok": sum(r["ok"] for r in results),
                "samples": len(results),
            }
        )
    )
    raise SystemExit(0 if all(r["ok"] for r in results) else 1)


if __name__ == "__main__":
    main()
