"""Shared HTTP helpers and the launcher version constant."""

import json
import ssl
import urllib.error
from urllib import request

ALVERSION = "v1.8.7.2"


class AstroRequests:
    """SSL-verified HTTP helpers backed by urllib."""

    # Modern default TLS context: verifies certificates against the system
    # CA store (the old code used ssl.SSLContext() with no protocol/verify).
    _CTX = ssl.create_default_context()

    @classmethod
    def _opener(cls):
        handler = request.HTTPSHandler(context=cls._CTX)
        proxies = request.ProxyHandler(request.getproxies())
        return request.build_opener(handler, proxies)

    @classmethod
    def get(cls, url, timeout=5):
        return cls._opener().open(url, timeout=timeout)

    @classmethod
    def post(cls, url, headers=None, jsonD=None, timeout=5):
        headers = headers or {}
        jsonD = jsonD or {}
        req = request.Request(url)
        if jsonD:
            req.data = json.dumps(jsonD).encode("utf-8")
            req.add_header("Content-Type", "application/json; charset=utf-8")
        for header, value in headers.items():
            req.add_header(header, value)
        try:
            return cls._opener().open(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            return exc
