"""One outbound boundary for AI clients, including health checks and CLI tools.

AI_TRUSTED_ORIGINS is an administrator-owned comma-separated list of exact HTTPS
origins (scheme, host, port). API requests cannot extend it. Remote DNS names
must resolve exclusively to public addresses; private services require an
explicit trusted IP origin. Connections pin an approved address while retaining
the original TLS hostname. No proxy environment or redirects are consulted.
"""

import ipaddress
import json as json_module
import os
import re
import socket
import threading
from urllib.parse import SplitResult, urlsplit

import httpx
import requests
import urllib3

CONFIG_LOCK = threading.RLock()
CLAUDE_ORIGIN = "https://api.anthropic.com"


class EgressPolicyError(ValueError):
    def __init__(self) -> None:
        super().__init__("AI endpoint is not permitted.")


def _parse(url: str) -> tuple[SplitResult, str, int, str]:
    if (
        not isinstance(url, str)
        or not url
        or not url.isascii()
        or any(ord(c) <= 32 or ord(c) == 127 for c in url)
        or any(c in url for c in "\\%?#@")
    ):
        raise EgressPolicyError()
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if parts.scheme not in {"http", "https"} or not host or not 1 <= port <= 65535:
            raise EgressPolicyError()
        try:
            address = ipaddress.ip_address(host)
            if address.is_unspecified or address.is_multicast or address.is_link_local:
                raise EgressPolicyError()
            host = str(address)
        except ValueError:
            if (
                not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", host)
                or host.replace(".", "").isdigit()
                or host.startswith("0x")
            ):
                raise EgressPolicyError() from None
        authority = f"[{host}]" if ":" in host else host
        if port != (443 if parts.scheme == "https" else 80):
            authority += f":{port}"
        # Reject alternate authority spellings rather than guessing parser intent.
        supplied = parts.netloc.lower()
        default = ":443" if parts.scheme == "https" else ":80"
        if supplied not in {authority, authority + default}:
            raise EgressPolicyError()
        if "//" in parts.path or any(p in {".", ".."} for p in parts.path.split("/")):
            raise EgressPolicyError()
        origin = f"{parts.scheme}://{authority}"
        return parts, host, port, origin
    except (ValueError, UnicodeError):
        raise EgressPolicyError() from None


def validate_url(url: str, *, cloud: bool = False) -> str:
    parts, host, _, origin = _parse(url)
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    trusted = set()
    for item in os.getenv("AI_TRUSTED_ORIGINS", "").split(","):
        if not item.strip():
            continue
        entry, _, _, canonical = _parse(item.strip())
        if entry.scheme != "https" or entry.path not in {"", "/"}:
            raise EgressPolicyError()
        trusted.add(canonical)
    if cloud:
        trusted.add(CLAUDE_ORIGIN)
    if not loopback and (parts.scheme != "https" or origin not in trusted):
        raise EgressPolicyError()
    return origin + parts.path


def endpoint_origin(url: str) -> str:
    return _parse(url)[3]


def local_openai_key(url: str, default_url: str, default_key: str = "", *, explicit_key: str | None = None) -> str:
    """Bind inherited credentials to their origin; empty explicit keys disable auth."""
    with CONFIG_LOCK:
        url = validate_endpoint(url)
        if explicit_key is not None:
            return explicit_key
        key = os.getenv("LOCAL_OPENAI_API_KEY")
        if key is None:
            key, bound_url = default_key, default_url
        else:
            # An explicit empty key suppresses imported fallback credentials.
            bound_url = os.getenv("LOCAL_OPENAI_API_KEY_ORIGIN") or default_url
        if key and endpoint_origin(url) != endpoint_origin(bound_url):
            raise EgressPolicyError()
        return key or ""


def local_openai_settings(default_url: str, default_key: str = "") -> tuple[str, dict[str, str]]:
    with CONFIG_LOCK:
        url = (os.getenv("LOCAL_OPENAI_BASE_URL") or default_url).rstrip("/")
        key = local_openai_key(url, default_url, default_key)
        url = validate_url(url)
        return url, {"Authorization": f"Bearer {key}"} if key else {}


def _address(host: str, port: int) -> str:
    if host == "localhost":
        return "127.0.0.1"
    # _parse already canonicalized literal IPs; DNS labels cannot match here.
    if ":" in host or host.replace(".", "").isdigit():
        return str(ipaddress.ip_address(host))
    try:
        addresses = {ipaddress.ip_address(row[4][0]) for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
    except (OSError, ValueError):
        raise EgressPolicyError() from None
    if not addresses or any(
        not address.is_global or address.is_multicast or address.is_reserved for address in addresses
    ):
        raise EgressPolicyError()
    return str(sorted(addresses, key=str)[0])


def validate_endpoint(url: str, *, cloud: bool = False) -> str:
    """Preflight before reading credentials; request() independently pins at use."""
    url = validate_url(url, cloud=cloud)
    _, host, port, _ = _parse(url)
    _address(host, port)
    return url


def request(method, url, *, headers=None, json=None, data=None, timeout=30, cloud=False) -> requests.Response:
    url = validate_url(url, cloud=cloud)
    parts, host, port, _ = _parse(url)
    address = _address(host, port)
    outgoing = dict(headers or {})
    # The URL, not a caller-supplied header, determines the destination identity.
    outgoing = {k: v for k, v in outgoing.items() if k.lower() not in {"host", "proxy-authorization"}}
    outgoing["Host"] = parts.netloc
    if json is not None:
        data = json_module.dumps(json).encode("utf-8")
        outgoing["Content-Type"] = "application/json"
    if isinstance(timeout, tuple):
        timeout = urllib3.Timeout(connect=timeout[0], read=timeout[1])
    pool_type = urllib3.HTTPSConnectionPool if parts.scheme == "https" else urllib3.HTTPConnectionPool
    tls = (
        {"cert_reqs": "CERT_REQUIRED", "assert_hostname": host, "server_hostname": host}
        if parts.scheme == "https"
        else {}
    )
    try:
        with pool_type(address, port=port, **tls) as pool:
            raw = pool.urlopen(
                method, parts.path or "/", body=data, headers=outgoing, timeout=timeout, redirect=False, retries=False
            )
            if 300 <= raw.status < 400:
                raise EgressPolicyError()
            response = requests.Response()
            response.status_code = raw.status
            response.headers.update(raw.headers)
            response._content = raw.data
            response.url = url
            return response
    except EgressPolicyError:
        raise
    except (urllib3.exceptions.HTTPError, OSError, ValueError):
        raise RuntimeError("AI endpoint request failed.") from None


def get(url, **kwargs) -> requests.Response:
    return request("GET", url, **kwargs)


def post(url, **kwargs) -> requests.Response:
    return request("POST", url, **kwargs)


class ClaudeTransport(httpx.BaseTransport):
    def handle_request(self, req) -> httpx.Response:
        response = request(req.method, str(req.url), headers=dict(req.headers), data=req.read(), timeout=90, cloud=True)
        headers = {k: v for k, v in response.headers.items() if k.lower() not in {"content-encoding", "content-length"}}
        return httpx.Response(response.status_code, headers=headers, content=response.content, request=req)
