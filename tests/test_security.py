"""Browser-facing hardening: self-hosted assets, CSP and headers, login lockout, minimal public health."""

import re

import pytest
from fastapi.testclient import TestClient

from app.main import STATIC, Runtime, create_app

from .fakes import ScriptedLLM, text_turn

AUTH = ("reviewer", "pw")


@pytest.fixture
def make(settings, warehouse):
    def _make(**overrides):
        settings.basic_auth_user, settings.basic_auth_password = AUTH
        for k, v in overrides.items():
            setattr(settings, k, v)
        llm = ScriptedLLM(steps=[text_turn("ok")] * 5)
        return TestClient(create_app(settings, runtime=Runtime(settings, warehouse=warehouse, llm=llm)))

    return _make


def test_page_loads_nothing_from_third_parties_and_has_no_inline_code():
    html = (STATIC / "index.html").read_text()
    assert not re.search(r"""(src|href)=["']https?://""", html), "external asset referenced"
    assert "<script>" not in html and "<style>" not in html  # CSP forbids inline script and style
    assert not re.search(r"\son[a-z]+=", html), "inline event handler (blocked by CSP)"
    for src in re.findall(r'(?:src|href)="(/static/[^"]+)"', html):
        assert (STATIC / src.removeprefix("/static/")).is_file(), src


def test_vendored_libraries_are_the_pinned_versions():
    assert "marked v12.0.2" in (STATIC / "vendor/marked-12.0.2.min.js").read_text()[:200]
    assert "DOMPurify 3.1.6" in (STATIC / "vendor/purify-3.1.6.min.js").read_text()[:200]


def test_security_headers_on_pages_api_and_errors(make):
    with make() as c:
        for r in (
            c.get("/", auth=AUTH),
            c.get("/api/memory", params={"client_id": "a" * 32}, auth=AUTH),
            c.get("/"),
            c.get("/healthz"),
        ):
            csp = r.headers["content-security-policy"]
            assert "script-src 'self'" in csp and "unsafe-inline" not in csp and "frame-ancestors 'none'" in csp
            assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
            assert r.headers["referrer-policy"] == "no-referrer"


def test_static_assets_are_served(make):
    with make() as c:
        js = c.get("/static/app.js")
        assert js.status_code == 200 and "javascript" in js.headers["content-type"]
        assert c.get("/static/vendor/purify-3.1.6.min.js").status_code == 200
        assert c.get("/static/../app/main.py").status_code == 404  # no path traversal


def test_wrong_passwords_lock_out_the_ip_even_for_the_right_password(make):
    with make(login_failures_per_window=3, trusted_proxy_hops=1) as c:
        attacker, other = {"x-forwarded-for": "203.0.113.5"}, {"x-forwarded-for": "198.51.100.9"}
        for _ in range(3):
            assert c.get("/", auth=("reviewer", "guess"), headers=attacker).status_code == 401
        locked = c.get("/", auth=AUTH, headers=attacker)  # right password, but locked out
        assert locked.status_code == 429 and locked.headers["retry-after"]
        assert c.get("/", auth=AUTH, headers=other).status_code == 200  # other people unaffected


def test_missing_credentials_do_not_count_as_failures(make):
    with make(login_failures_per_window=2, trusted_proxy_hops=1) as c:
        h = {"x-forwarded-for": "203.0.113.6"}
        for _ in range(5):  # browsers make a credential-less request before showing the prompt
            assert c.get("/", headers=h).status_code == 401
        assert c.get("/", auth=AUTH, headers=h).status_code == 200


def test_public_health_is_minimal_and_details_need_login(make):
    with make() as c:
        assert c.get("/healthz").json() in ({"status": "ok"}, {"status": "degraded"})
        assert c.get("/api/status").status_code == 401
        detail = c.get("/api/status", auth=AUTH).json()
        assert {"status", "detail", "state", "proxy"} <= detail.keys()
