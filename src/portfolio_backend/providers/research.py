"""Public research in a killable process with shared cache; no private inputs."""

import json
import subprocess
import sys

from redis import Redis

from portfolio_backend.errors import DomainError
from portfolio_backend.services.public_data import SharedFetch


class ResearchMarket:
    def __init__(self, cfg):
        self.cfg = cfg

    def fetch(self, name, args, timeout):
        def load():
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "portfolio_backend.providers.research_worker",
                        name,
                        json.dumps(args),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=True,
                )
                return json.loads(result.stdout)
            except subprocess.TimeoutExpired as exc:
                raise DomainError("tool_timeout", "行情查询超时", 503) from exc
            except (subprocess.CalledProcessError, ValueError) as exc:
                raise DomainError("research_unavailable", "该市场或资料暂不可用", 503) from exc

        with Redis.from_url(self.cfg.cache_url) as cache:
            return SharedFetch(cache).get(
                "research:" + name + ":" + json.dumps(args, sort_keys=True), 3600, load
            ) or {"error": "research_unavailable"}
