"""
vite.py — `{% vite_entry "src/workspace/main.ts" %}` emits the built script and
stylesheet tags for one frontend entry point, read from Vite's manifest.

There's deliberately no Vite dev-server mode: the CSP only allows same-origin
scripts, and `npm run watch` (vite build --watch) rebuilds into static/dist fast
enough that a second code path isn't worth having.
"""

from __future__ import annotations

import json
from functools import lru_cache

from django import template
from django.conf import settings
from django.templatetags.static import static
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

register = template.Library()


@lru_cache(maxsize=1)
def _cached_manifest(mtime: float) -> dict:
    return json.loads(settings.VITE_MANIFEST_PATH.read_text())


def _manifest() -> dict:
    path = settings.VITE_MANIFEST_PATH
    if not path.exists():
        raise template.TemplateSyntaxError(
            f"{path} not found — run `npm run build` in web/frontend first")
    return _cached_manifest(path.stat().st_mtime)


def _collect_css(manifest: dict, key: str, seen: set[str]) -> list[str]:
    chunk = manifest[key]
    css = list(chunk.get("css", []))
    for imported in chunk.get("imports", []):
        if imported not in seen:
            seen.add(imported)
            css.extend(_collect_css(manifest, imported, seen))
    return css


@register.simple_tag(takes_context=True)
def vite_entry(context, entry: str) -> str:
    manifest = _manifest()
    if entry not in manifest:
        raise template.TemplateSyntaxError(f"{entry!r} is not a Vite entry point")
    chunk = manifest[entry]
    css_files = list(dict.fromkeys(_collect_css(manifest, entry, set())))
    links = format_html_join(
        "", '<link rel="stylesheet" href="{}">', ((static(f"dist/{href}"),) for href in css_files))
    script = format_html('<script type="module" src="{}"></script>', static(f"dist/{chunk['file']}"))
    return mark_safe(links + script)
