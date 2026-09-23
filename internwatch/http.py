from __future__ import annotations

import html
import re

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"

_session: requests.Session | None = None


def session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        retry = Retry(total=3, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=frozenset({"GET", "POST", "PUT"}))
        s.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=32))
        s.headers.update({"User-Agent": UA, "Accept": "application/json, text/html;q=0.9, */*;q=0.8"})
        _session = s
    return _session


def get_json(url: str, **kw):
    r = session().get(url, timeout=kw.pop("timeout", 25), **kw)
    r.raise_for_status()
    return r.json()


def post_json(url: str, payload: dict, **kw):
    r = session().post(url, json=payload, timeout=kw.pop("timeout", 25), **kw)
    r.raise_for_status()
    return r.json()


def get_text(url: str, **kw) -> str:
    r = session().get(url, timeout=kw.pop("timeout", 25), **kw)
    r.raise_for_status()
    return r.text


_BLOCK = re.compile(r"</?(p|div|br|li|ul|ol|h[1-6]|tr|section|article)[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")
_DROP = re.compile(r"<(script|style|noscript|svg)[^>]*>.*?</\1>", re.I | re.S)


def html_to_text(s: str | None) -> str:
    if not s:
        return ""
    s = html.unescape(s) if "&lt;" in s else s   # Greenhouse double-escapes its content
    s = _DROP.sub(" ", s)
    s = re.sub(r"<li[^>]*>", "\n• ", s, flags=re.I)
    s = _BLOCK.sub("\n", s)
    s = _TAG.sub("", s)
    for _ in range(2):   # some ATSs double-escape entities (&amp;#xa;)
        s = html.unescape(s)
    s = re.sub(r"[ \t\xa0]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    return s.strip()
