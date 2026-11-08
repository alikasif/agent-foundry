"""Render a ``ReadingList`` to one self-contained HTML file.

Everything the page needs (CSS, the small "mark read" script) is inline; the page makes
no network requests. All model/email-derived text is HTML-escaped by Jinja2 autoescape,
link targets pass through ``safe_url`` (http/https only), and a Content-Security-Policy
allows only the static script below, identified by its sha256 hash.

Imports only jinja2, the standard library and ``newsletter_digest.models``.
"""

import base64
import hashlib
import re
from pathlib import Path
from urllib.parse import urlsplit

from jinja2 import Environment, PackageLoader, StrictUndefined

from newsletter_digest.models import ReadingList

TEMPLATE_NAME = "reading_list.html.j2"
SAFE_URL_SCHEMES = frozenset({"http", "https"})
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

# Static, trusted script, emitted byte-for-byte between <script> and </script>. The
# CSP hash is computed from this exact string, so it stays valid if the script changes.
SCRIPT_BODY = """
(function () {
  "use strict";
  var PREFIX = "nd-read:";
  function isRead(key) {
    try {
      return window.localStorage.getItem(PREFIX + key) === "1";
    } catch (err) {
      return false;
    }
  }
  function remember(key, read) {
    try {
      if (read) {
        window.localStorage.setItem(PREFIX + key, "1");
      } else {
        window.localStorage.removeItem(PREFIX + key);
      }
    } catch (err) {
      /* storage unavailable: the checkbox still works for this visit */
    }
  }
  var items = document.querySelectorAll(".item[data-key]");
  Array.prototype.forEach.call(items, function (item) {
    var key = item.getAttribute("data-key");
    var box = item.querySelector("input.mark-read");
    if (!key || !box) {
      return;
    }
    box.checked = isRead(key);
    item.classList.toggle("is-read", box.checked);
    box.addEventListener("change", function () {
      item.classList.toggle("is-read", box.checked);
      remember(key, box.checked);
    });
  });
})();
"""
SCRIPT_HASH = base64.b64encode(
    hashlib.sha256(SCRIPT_BODY.encode("utf-8")).digest()
).decode("ascii")


def safe_url(url: str | None) -> str | None:
    """Return ``url`` cleaned for use in an ``href``, or ``None`` if it is unsafe.

    Removes ASCII control characters and surrounding whitespace, then accepts only
    absolute ``http``/``https`` URLs with a host. Rejects ``javascript:``, ``data:``,
    ``vbscript:``, obfuscated variants (``" javascript:"``, ``"java\\tscript:"``),
    protocol-relative ``//host`` and malformed URLs.
    """
    if not url:
        return None
    cleaned = _CONTROL_CHARS.sub("", url).strip()
    try:
        parts = urlsplit(cleaned)
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme.lower() not in SAFE_URL_SCHEMES or not host:
        return None
    return cleaned


def slug(value: str) -> str:
    """Lowercase ``value`` and join its alphanumeric runs with '-' (an HTML id)."""
    return "-".join(re.findall(r"[a-z0-9]+", value.lower())) or "section"


ENVIRONMENT = Environment(
    loader=PackageLoader("newsletter_digest", "templates"),
    autoescape=True,
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)
ENVIRONMENT.filters["safe_url"] = safe_url
ENVIRONMENT.filters["slug"] = slug


def render_html(reading_list: ReadingList) -> str:
    """Render ``reading_list`` to a complete, self-contained HTML document."""
    return ENVIRONMENT.get_template(TEMPLATE_NAME).render(
        reading_list=reading_list,
        meta=reading_list.meta,
        items=reading_list.items,
        must_read=reading_list.must_read(),
        by_topic=reading_list.by_topic(),
        script_body=SCRIPT_BODY,
        script_hash=SCRIPT_HASH,
    )


def write_reading_list(reading_list: ReadingList, out_dir: Path) -> Path:
    """Write the HTML to ``out_dir/reading-list-<end_date>.html`` and return the path.

    Creates ``out_dir`` (with parents) if needed and overwrites a same-day file.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"reading-list-{reading_list.meta.end_date.isoformat()}.html"
    path.write_text(render_html(reading_list), encoding="utf-8")
    return path
