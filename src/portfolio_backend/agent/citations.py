import ipaddress
import re
from urllib.parse import urlsplit


def safe_url(value):
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "https"
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not re.search(r"[\s<>\[\]()]", value)
            and "." in parsed.hostname
            and not parsed.hostname.endswith((".local", ".internal", ".localhost"))
            and not _is_ip(parsed.hostname)
        )
    except (ValueError, AttributeError):
        return False


def _is_ip(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def resolve(text, sources):
    def replace(match):
        source = sources.get(match[1])
        if not source:
            return "[来源未核实]"
        label = re.sub(r"[\[\]()]", "", source.get("title", "数据依据"))[:100]
        url = source.get("url")
        return (
            f"[{label} · {urlsplit(url).hostname}]({url})"
            if url and safe_url(url)
            else f"[{label} · {match[1]}]"
        )

    # Model-written arbitrary links cannot bypass the registry.
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1（请核对数据依据）", text)
    # Bare URLs and Markdown autolinks must not become a second citation channel.
    text = re.sub(r"https?://[^\s<>]+", "（未登记来源，请核对数据依据）", text, flags=re.IGNORECASE)
    return re.sub(r"\[\[cite:([\w:.-]+)\]\]", replace, text)
