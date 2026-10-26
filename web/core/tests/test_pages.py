"""Error pages and small template helpers."""

from __future__ import annotations

import pytest
from django.test import RequestFactory
from django.views.defaults import server_error

from accounts.templatetags.accounts_extras import browser

pytestmark = pytest.mark.django_db


def test_missing_page_uses_the_site_layout(client, settings):
    settings.DEBUG = False
    response = client.get("/no-such-page/")
    assert response.status_code == 404
    assert b"Page Not Found" in response.content and b"app.css" in response.content


def test_server_error_page_renders_without_a_request_context():
    response = server_error(RequestFactory().get("/"))
    assert response.status_code == 500
    assert b"Something went wrong on the server" in response.content


def test_browser_names_raw_user_agents():
    chrome_mac = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/155.0.0.0 Safari/537.36")
    assert browser(chrome_mac) == "Chrome on macOS"
    assert browser("") == "Unknown browser on unknown system"
