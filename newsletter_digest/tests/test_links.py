"""Tests for ``links``: link classification, ledger keys and the store re-export.

Contract: shared/plan.md AF-P5 (links.py) and shared/designs.md AF-2, AF-3.1, AF-13,
FR-030. All offline; ``links`` is pure.
"""

import random

import pytest

from newsletter_digest import links, store
from newsletter_digest.links import (
    FETCHABLE_KINDS,
    NEVER_REQUEST_KINDS,
    TRACKER_HOST_PREFIXES,
    TRACKER_HOSTS,
    LinkKind,
    classify_link,
    is_tracker_host,
    ledger_key,
)

# The tables exactly as they were in store.py at bb16231 (before the move).
ORIGINAL_TRACKER_HOSTS: frozenset[str] = frozenset(
    {
        "list-manage.com",
        "mailchi.mp",
        "flight.beehiiv.net",
        "convertkit-mail.com",
        "convertkit-mail2.com",
        "sendgrid.net",
        "mandrillapp.com",
        "hubspotlinks.com",
        "mlsend.com",
        "rs6.net",
        "t.co",
        "lnkd.in",
        "bit.ly",
        "tinyurl.com",
        "ow.ly",
        "buff.ly",
    }
)
ORIGINAL_TRACKER_HOST_PREFIXES: tuple[str, ...] = (
    "link.",
    "links.",
    "click.",
    "clicks.",
    "email.",
    "track.",
    "tracking.",
)


def original_is_tracker_host(host: str) -> bool:
    """``store._is_tracker_host`` as it was at bb16231, used as a parity oracle."""
    if host.startswith(ORIGINAL_TRACKER_HOST_PREFIXES):
        return True
    return any(h == host or host.endswith(f".{h}") for h in ORIGINAL_TRACKER_HOSTS)


# --- LinkKind and the kind sets -------------------------------------------------


def test_link_kind_members_and_lower_case_values() -> None:
    assert {kind.name for kind in LinkKind} == {
        "ARTICLE",
        "TRACKER",
        "UNSUBSCRIBE",
        "PREFERENCES",
        "VIEW_IN_BROWSER",
        "SOCIAL",
        "PIXEL",
        "SPONSOR",
    }
    for kind in LinkKind:
        assert kind.value == kind.name.lower()
        assert kind == kind.value  # StrEnum compares equal to its value


def test_fetchable_kinds_are_article_and_tracker() -> None:
    # Q1 decided (B): tracker redirect links are followed (FR-031).
    assert FETCHABLE_KINDS == frozenset({LinkKind.ARTICLE, LinkKind.TRACKER})
    assert isinstance(FETCHABLE_KINDS, frozenset)


def test_never_request_kinds_are_unsubscribe_preferences_view_in_browser() -> None:
    assert NEVER_REQUEST_KINDS == frozenset(
        {LinkKind.UNSUBSCRIBE, LinkKind.PREFERENCES, LinkKind.VIEW_IN_BROWSER}
    )
    assert isinstance(NEVER_REQUEST_KINDS, frozenset)


def test_never_request_kinds_are_never_fetchable() -> None:
    assert FETCHABLE_KINDS.isdisjoint(NEVER_REQUEST_KINDS)


@pytest.mark.parametrize(
    "kind",
    [LinkKind.SOCIAL, LinkKind.PIXEL, LinkKind.SPONSOR],
)
def test_fr030_excluded_kinds_are_not_fetchable(kind: LinkKind) -> None:
    assert kind not in FETCHABLE_KINDS


# --- classify_link: one table per kind ------------------------------------------

UNSUBSCRIBE_URLS: list[str] = [
    "https://example.com/unsubscribe?u=1",
    "https://news.example.com/list-unsubscribe",
    "https://example.com/u?action=unsubscribe",
    "https://example.com/unsubscribe.php?id=9",
    "https://example.com/opt-out",
    "https://example.com/optout",
    "https://example.com/opt_out?id=3",
    "https://example.com/email/unsub/abc",
    "https://example.substack.com/action/disable_email?token=x",
    "https://example.com/%75nsubscribe",  # percent-encoded "u"
    "https://example.com/a%2Funsubscribe",  # encoded slash
]
PREFERENCES_URLS: list[str] = [
    "https://example.com/preferences",
    "https://example.com/preference?u=1",
    "https://example.com/prefs",
    "https://example.com/manage-subscription",
    "https://example.com/manage_subscriptions",
    "https://example.com/email-settings",
    "https://example.com/subscription-center",
    "https://example.com/subscription-centre",
    "https://us1.list-manage.com/profile?u=1",  # Mailchimp update-preferences
]
VIEW_IN_BROWSER_URLS: list[str] = [
    "https://example.com/view-in-browser",
    "https://example.com/view-in-your-browser?id=1",
    "https://example.com/viewinbrowser",
    "https://example.com/webversion",
    "https://example.com/web-version/42",
    "https://example.com/view-online/123",
    "https://example.com/view-as-web-page",
    "https://example.com/browser-view",
    "https://us1.campaign-archive.com/?u=1&id=2",  # Mailchimp web version
]
SOCIAL_URLS: list[str] = [
    # Share intents.
    "https://x.com/intent/tweet?text=hi",
    "https://x.com/intent/post?url=a",
    "https://twitter.com/share?url=a",
    "https://www.facebook.com/sharer/sharer.php?u=a",
    "https://www.facebook.com/dialog/share?href=a",
    "https://www.linkedin.com/sharing/share-offsite/?url=a",
    "https://www.linkedin.com/shareArticle?url=a",
    # Home pages and profiles.
    "https://x.com/",
    "https://x.com/somebody",
    "https://twitter.com/somebody",
    "https://www.facebook.com/somepage",
    "https://www.instagram.com/someone/",
    "https://www.threads.net/@someone",
    "https://bsky.app/profile/someone.bsky.social",
    "https://www.linkedin.com/in/someone",
    "https://www.linkedin.com/company/acme",
]
PIXEL_URLS: list[str] = [
    "https://example.com/track/open.php?u=1",  # Mailchimp
    "https://example.com/wf/open?upn=1",  # SendGrid
    "https://example.com/e/open",
    "https://example.com/open",
    "https://example.com/x/pixel.png",
    "https://example.com/x/beacon",
    "https://example.com/images/1x1.gif",
    "https://example.com/spacer.gif",
    "https://example.com/tracking-pixel/abc",
    "https://pixel.example.com/x",
    "https://beacon.example.com/x",
]
SPONSOR_URLS: list[str] = [
    "https://ad.doubleclick.net/ddm/clk/1",
    "https://googleads.g.doubleclick.net/x",
    "https://www.googleadservices.com/pagead/aclk?x=1",
    "https://ads.example.com/x",
    "https://adserver.example.com/x",
    "https://example.com/sponsor/acme",
    "https://example.com/sponsors",
    "https://example.com/sponsored-post",
    "https://example.com/ads/banner",
    "https://example.com/ad/123",
    "https://example.com/post?utm_medium=sponsor",
    "https://example.com/post?utm_medium=sponsored",
    "https://example.com/post?utm_source=paid",
    "https://example.com/post?utm_medium=paid_social",
    "https://example.com/post?utm_medium=cpc",
    "https://example.com/post?utm_source=newsletter&utm_medium=ad",
]
ARTICLE_URLS: list[str] = [
    "https://example.com/posts/hello-world",
    "https://blog.example.org/2026/11/a-post",
    "https://example.com/post?utm_medium=email&utm_source=newsletter",
    # A specific post on a social site is content, not a share/profile link.
    "https://x.com/someone/status/123",
    "https://bsky.app/profile/a.bsky.social/post/3k",
    "https://www.linkedin.com/pulse/some-article",
    "https://www.instagram.com/p/abc123/",
    # Hosts outside the social table are never SOCIAL.
    "https://www.youtube.com/watch?v=abc",
    "https://www.youtube.com/@channel",
    "https://www.reddit.com/r/python",
    "https://news.ycombinator.com/item?id=1",
]


@pytest.mark.parametrize("url", UNSUBSCRIBE_URLS)
def test_classify_unsubscribe(url: str) -> None:
    assert classify_link(url) is LinkKind.UNSUBSCRIBE


@pytest.mark.parametrize("url", PREFERENCES_URLS)
def test_classify_preferences(url: str) -> None:
    assert classify_link(url) is LinkKind.PREFERENCES


@pytest.mark.parametrize("url", VIEW_IN_BROWSER_URLS)
def test_classify_view_in_browser(url: str) -> None:
    assert classify_link(url) is LinkKind.VIEW_IN_BROWSER


@pytest.mark.parametrize("url", SOCIAL_URLS)
def test_classify_social(url: str) -> None:
    assert classify_link(url) is LinkKind.SOCIAL


@pytest.mark.parametrize("url", PIXEL_URLS)
def test_classify_pixel(url: str) -> None:
    assert classify_link(url) is LinkKind.PIXEL


@pytest.mark.parametrize("url", SPONSOR_URLS)
def test_classify_sponsor(url: str) -> None:
    assert classify_link(url) is LinkKind.SPONSOR


@pytest.mark.parametrize("url", ARTICLE_URLS)
def test_classify_article(url: str) -> None:
    assert classify_link(url) is LinkKind.ARTICLE


@pytest.mark.parametrize("host", sorted(TRACKER_HOSTS))
def test_classify_tracker_hosts(host: str) -> None:
    assert classify_link(f"https://{host}/r/abc123") is LinkKind.TRACKER
    assert classify_link(f"https://sub.{host}/r/abc123") is LinkKind.TRACKER


@pytest.mark.parametrize("prefix", TRACKER_HOST_PREFIXES)
def test_classify_tracker_host_prefixes(prefix: str) -> None:
    assert classify_link(f"https://{prefix}mail.example.com/c/abc") is LinkKind.TRACKER


def test_every_link_kind_is_covered_by_a_table() -> None:
    covered = {
        classify_link(url)
        for url in (
            UNSUBSCRIBE_URLS
            + PREFERENCES_URLS
            + VIEW_IN_BROWSER_URLS
            + SOCIAL_URLS
            + PIXEL_URLS
            + SPONSOR_URLS
            + ARTICLE_URLS
            + ["https://t.co/abc"]
        )
    }
    assert covered == set(LinkKind)


# --- case-insensitivity ---------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("HTTPS://EXAMPLE.COM/UNSUBSCRIBE", LinkKind.UNSUBSCRIBE),
        ("https://example.com/List-Unsubscribe", LinkKind.UNSUBSCRIBE),
        ("https://example.com/x?Action=UnSubscribe", LinkKind.UNSUBSCRIBE),
        ("https://example.com/Opt-Out", LinkKind.UNSUBSCRIBE),
        ("https://example.com/Manage-Subscription", LinkKind.PREFERENCES),
        ("https://US1.List-Manage.COM/Profile?u=1", LinkKind.PREFERENCES),
        ("https://example.com/WebVersion", LinkKind.VIEW_IN_BROWSER),
        ("HTTPS://X.COM/INTENT/TWEET", LinkKind.SOCIAL),
        ("https://WWW.LinkedIn.com/IN/Someone", LinkKind.SOCIAL),
        ("https://example.com/Track/Open.PHP", LinkKind.PIXEL),
        ("https://PIXEL.example.com/x", LinkKind.PIXEL),
        ("https://AD.DoubleClick.NET/clk", LinkKind.SPONSOR),
        ("https://example.com/p?UTM_MEDIUM=PAID", LinkKind.SPONSOR),
        ("https://example.com/Sponsored-Post", LinkKind.SPONSOR),
        ("https://T.CO/ABC", LinkKind.TRACKER),
        ("https://Click.Example.COM/abc", LinkKind.TRACKER),
    ],
)
def test_classify_is_case_insensitive(url: str, expected: LinkKind) -> None:
    assert classify_link(url) is expected
    assert classify_link(url.lower()) is expected


# --- precedence -----------------------------------------------------------------
# Each case: a URL matching two rules, the winner, and the same URL without the
# winner's marker (to prove it really matches the lower rule as well).

PRECEDENCE_CASES: list[tuple[str, LinkKind, str, LinkKind]] = [
    (
        "https://example.com/preferences/unsubscribe",
        LinkKind.UNSUBSCRIBE,
        "https://example.com/preferences",
        LinkKind.PREFERENCES,
    ),
    (
        "https://example.com/webversion/preferences",
        LinkKind.PREFERENCES,
        "https://example.com/webversion",
        LinkKind.VIEW_IN_BROWSER,
    ),
    (
        "https://example.com/view-online/open",
        LinkKind.VIEW_IN_BROWSER,
        "https://example.com/open",
        LinkKind.PIXEL,
    ),
    (
        "https://ads.example.com/open",
        LinkKind.PIXEL,
        "https://ads.example.com/x",
        LinkKind.SPONSOR,
    ),
    (
        "https://x.com/someone?utm_medium=paid",
        LinkKind.SPONSOR,
        "https://x.com/someone",
        LinkKind.SOCIAL,
    ),
    (
        "https://click.linkedin.com/in/someone",
        LinkKind.SOCIAL,
        "https://click.example.com/in/someone",
        LinkKind.TRACKER,
    ),
    (
        "https://t.co/abc",
        LinkKind.TRACKER,
        "https://example.com/abc",
        LinkKind.ARTICLE,
    ),
    # Non-adjacent pairs.
    (
        "https://us1.list-manage.com/unsubscribe?u=1",
        LinkKind.UNSUBSCRIBE,
        "https://us1.list-manage.com/track/click?u=1",
        LinkKind.TRACKER,
    ),
    (
        "https://click.example.com/preferences",
        LinkKind.PREFERENCES,
        "https://click.example.com/abc",
        LinkKind.TRACKER,
    ),
    (
        "https://t.co/webversion",
        LinkKind.VIEW_IN_BROWSER,
        "https://t.co/abc",
        LinkKind.TRACKER,
    ),
    (
        "https://doubleclick.net/unsubscribe",
        LinkKind.UNSUBSCRIBE,
        "https://doubleclick.net/x",
        LinkKind.SPONSOR,
    ),
    (
        "https://x.com/opt-out",
        LinkKind.UNSUBSCRIBE,
        "https://x.com/someone",
        LinkKind.SOCIAL,
    ),
    (
        "https://sendgrid.net/wf/open?upn=1",
        LinkKind.PIXEL,
        "https://sendgrid.net/ls/click?upn=1",
        LinkKind.TRACKER,
    ),
    (
        "https://x.com/pixel.gif",
        LinkKind.PIXEL,
        "https://x.com/someone",
        LinkKind.SOCIAL,
    ),
    (
        "https://bit.ly/abc?utm_source=sponsor",
        LinkKind.SPONSOR,
        "https://bit.ly/abc",
        LinkKind.TRACKER,
    ),
]


@pytest.mark.parametrize(
    "url,winner,lower_url,lower",
    PRECEDENCE_CASES,
    ids=[f"{w.value}>{lo.value}" for _, w, _, lo in PRECEDENCE_CASES],
)
def test_classify_precedence(
    url: str, winner: LinkKind, lower_url: str, lower: LinkKind
) -> None:
    assert classify_link(lower_url) is lower
    assert classify_link(url) is winner


def test_precedence_cases_cover_every_adjacent_pair() -> None:
    order = [
        LinkKind.UNSUBSCRIBE,
        LinkKind.PREFERENCES,
        LinkKind.VIEW_IN_BROWSER,
        LinkKind.PIXEL,
        LinkKind.SPONSOR,
        LinkKind.SOCIAL,
        LinkKind.TRACKER,
        LinkKind.ARTICLE,
    ]
    pairs = {(winner, lower) for _, winner, _, lower in PRECEDENCE_CASES}
    for higher, lower in zip(order, order[1:], strict=False):
        assert (higher, lower) in pairs


# --- decoding and fragments -----------------------------------------------------


def test_encoded_hash_does_not_hide_unsubscribe_in_fragment() -> None:
    # Split before decoding: %23 stays in the path, where the keyword is matched.
    assert classify_link("https://example.com/a%23unsubscribe") is LinkKind.UNSUBSCRIBE


def test_encoded_question_mark_keeps_keyword_matched() -> None:
    assert classify_link("https://example.com/a%3Funsubscribe") is LinkKind.UNSUBSCRIBE


def test_fragment_is_ignored() -> None:
    # The fragment is never sent, so it cannot make a request an unsubscribe.
    assert classify_link("https://example.com/a#unsubscribe") is LinkKind.ARTICLE


def test_trailing_dot_host_is_normalised() -> None:
    assert classify_link("https://x.com./someone") is LinkKind.SOCIAL
    assert classify_link("https://t.co./abc") is LinkKind.TRACKER


def test_surrounding_whitespace_is_ignored() -> None:
    assert classify_link("  https://example.com/unsubscribe \n") is LinkKind.UNSUBSCRIBE


# --- known false-positive policy (AF-13) ----------------------------------------


@pytest.mark.parametrize(
    "url,kind",
    [
        ("https://example.com/why-users-opt-out", LinkKind.UNSUBSCRIBE),
        ("https://example.com/the-unsubscribe-button-is-broken", LinkKind.UNSUBSCRIBE),
        ("https://example.com/developer-preferences-survey", LinkKind.PREFERENCES),
        ("https://example.com/sponsor/how-we-pick-sponsors", LinkKind.SPONSOR),
    ],
)
def test_known_false_positives_are_skipped_not_fetched(
    url: str, kind: LinkKind
) -> None:
    """AF-13 accepted risk: an article slug with a keyword is a quiet skip.

    Matching favours recall on unsubscribe/preferences, because a false negative
    could unsubscribe the user; a false positive only drops the fetch (blurb kept).
    """
    assert classify_link(url) is kind
    assert kind not in FETCHABLE_KINDS


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/reviews/google-pixel-10-review",
        "https://example.com/adventures-in-rust",
        "https://example.com/headlines",
        "https://example.com/downloads/tool",
        "https://example.com/address-book",
        "https://example.com/post?utm_campaign=ad-regulation",
        "https://example.com/post?utm_content=sponsor",
        "https://example.com/open-source-roundup",
        "https://example.com/opening-keynote",
    ],
)
def test_near_miss_keywords_stay_article(url: str) -> None:
    assert classify_link(url) is LinkKind.ARTICLE


# --- totality -------------------------------------------------------------------

GARBAGE_INPUTS: list[str] = [
    "",
    " ",
    "\n\t",
    "not a url",
    "//",
    "?",
    "#",
    "%",
    "%%%zz",
    "\x00\x01",
    "http://",
    "https://",
    "http://[",
    "http://[::1",
    "http://[::1/x",
    "https://a.com:99999/x",
    "https://a.com:-1/x",
    "https://a.com:port/x",
    "http://a＃b.com/",  # NFKC-normalised netloc raises in urlsplit
    "mailto:someone@example.com",
    "javascript:alert(1)",
    "data:text/html,<b>x</b>",
    "ftp://example.com/file",
    "file:///etc/passwd",
    "☃\U0001f600",
    "https://example.com/" + "a" * 10_000,
    "https://example.com/?" + "&".join(f"k{i}=v" for i in range(2_000)),
]


@pytest.mark.parametrize("value", GARBAGE_INPUTS)
def test_classify_link_never_raises(value: str) -> None:
    assert isinstance(classify_link(value), LinkKind)


@pytest.mark.parametrize("value", GARBAGE_INPUTS)
def test_ledger_key_never_raises(value: str) -> None:
    assert isinstance(ledger_key(value), str)


@pytest.mark.parametrize(
    "value",
    ["", "not a url", "http://[::1/x", "mailto:a@b.c", "javascript:void(0)"],
)
def test_unparseable_or_non_http_without_keywords_is_article(value: str) -> None:
    assert classify_link(value) is LinkKind.ARTICLE


@pytest.mark.parametrize(
    "value,expected",
    [
        ("http://[unsubscribe", LinkKind.UNSUBSCRIBE),
        ("http://[x/preferences", LinkKind.PREFERENCES),
        ("http://[x/webversion", LinkKind.VIEW_IN_BROWSER),
        ("http://a＃b.com/unsubscribe", LinkKind.UNSUBSCRIBE),
    ],
)
def test_unparseable_input_falls_back_to_keyword_rules(
    value: str, expected: LinkKind
) -> None:
    assert classify_link(value) is expected


def test_classify_and_ledger_key_survive_random_strings() -> None:
    rng = random.Random(20261101)
    alphabet = "abcxyz019:/?#[]@!$&'()*+,;=%.-_~ \t＃℀é"
    for _ in range(3_000):
        value = "".join(rng.choices(alphabet, k=rng.randint(0, 40)))
        for candidate in (value, f"http://{value}", f"https://[{value}"):
            assert isinstance(classify_link(candidate), LinkKind)
            assert isinstance(ledger_key(candidate), str)


# --- ledger_key -----------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("HTTPS://Example.COM/A?B=C#frag", "https://example.com/A?B=C"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("http://example.com:80/a", "http://example.com/a"),
        ("http://example.com:8080/a", "http://example.com:8080/a"),
        ("http://example.com:443/a", "http://example.com:443/a"),
        ("https://example.com:80/a", "https://example.com:80/a"),
        ("https://example.com", "https://example.com/"),
        ("https://example.com?q=1", "https://example.com/?q=1"),
        ("https://example.com/a?b=2&a=1", "https://example.com/a?b=2&a=1"),
        ("https://example.com/a?x=%2F", "https://example.com/a?x=%2F"),
        ("https://example.com/Path/CASE", "https://example.com/Path/CASE"),
        ("https://example.com/a#", "https://example.com/a"),
        ("https://user:pw@Example.com/p", "https://example.com/p"),
        ("https://[::1]:443/x", "https://[::1]/x"),
        ("https://[2001:DB8::1]:8443/x", "https://[2001:db8::1]:8443/x"),
    ],
)
def test_ledger_key_normalisation(url: str, expected: str) -> None:
    assert ledger_key(url) == expected


def test_ledger_key_same_request_same_key() -> None:
    variants = [
        "https://example.com/a?x=1",
        "HTTPS://EXAMPLE.COM/a?x=1",
        "https://example.com:443/a?x=1",
        "https://example.com/a?x=1#section-2",
    ]
    assert len({ledger_key(url) for url in variants}) == 1


@pytest.mark.parametrize(
    "first,second",
    [
        ("https://example.com/a?x=1", "https://example.com/a?x=2"),
        ("https://example.com/a", "https://example.com/A"),
        ("https://example.com/a", "http://example.com/a"),
        ("https://example.com/a", "https://example.com:8443/a"),
        ("https://example.com/a?b=1&c=2", "https://example.com/a?c=2&b=1"),
    ],
)
def test_ledger_key_different_requests_different_keys(first: str, second: str) -> None:
    assert ledger_key(first) != ledger_key(second)


@pytest.mark.parametrize(
    "url",
    [
        "HTTPS://Example.COM:443/A?B=C#frag",
        "http://example.com:8080",
        "https://[::1]:443/x",
        "https://example.com/a?b=2&a=1",
    ],
)
def test_ledger_key_is_idempotent(url: str) -> None:
    assert ledger_key(ledger_key(url)) == ledger_key(url)


@pytest.mark.parametrize("url", ["http://[::1/x", "https://a.com:99999/x"])
def test_ledger_key_returns_unparseable_input_unchanged(url: str) -> None:
    assert ledger_key(url) == url


# --- tracker tables and is_tracker_host -----------------------------------------


def test_tracker_tables_moved_verbatim() -> None:
    assert TRACKER_HOSTS == ORIGINAL_TRACKER_HOSTS
    assert isinstance(TRACKER_HOSTS, frozenset)
    assert TRACKER_HOST_PREFIXES == ORIGINAL_TRACKER_HOST_PREFIXES
    assert isinstance(TRACKER_HOST_PREFIXES, tuple)


@pytest.mark.parametrize("host", sorted(ORIGINAL_TRACKER_HOSTS))
def test_is_tracker_host_entries_and_parity(host: str) -> None:
    assert is_tracker_host(host)
    assert is_tracker_host(f"www.{host}")
    assert is_tracker_host(f"a.b.{host}")
    assert not is_tracker_host(f"not{host}")
    assert not is_tracker_host(f"{host}.evil.example")
    for candidate in (
        host,
        f"www.{host}",
        f"a.b.{host}",
        f"not{host}",
        f"{host}.evil.example",
        f"x{host}",
        host.upper(),
    ):
        assert is_tracker_host(candidate) == original_is_tracker_host(candidate)


@pytest.mark.parametrize("prefix", ORIGINAL_TRACKER_HOST_PREFIXES)
def test_is_tracker_host_prefixes_and_parity(prefix: str) -> None:
    assert is_tracker_host(f"{prefix}example.com")
    assert is_tracker_host(prefix)
    bare = prefix.rstrip(".")
    assert not is_tracker_host(f"{bare}example.com")
    assert not is_tracker_host(f"www.{prefix}example.com")
    for candidate in (
        f"{prefix}example.com",
        prefix,
        f"{bare}example.com",
        f"www.{prefix}example.com",
        f"{prefix.upper()}example.com",
    ):
        assert is_tracker_host(candidate) == original_is_tracker_host(candidate)


@pytest.mark.parametrize(
    "host",
    ["", "example.com", "co", "t.com", "ly", "list-manage.org", "mailchimp.com"],
)
def test_is_tracker_host_non_trackers_and_parity(host: str) -> None:
    assert not is_tracker_host(host)
    assert is_tracker_host(host) == original_is_tracker_host(host)


# --- store re-export ------------------------------------------------------------


def test_store_reexports_tracker_hosts_identity() -> None:
    assert store.TRACKER_HOSTS is links.TRACKER_HOSTS


def test_store_reexports_tracker_host_prefixes_identity() -> None:
    assert store.TRACKER_HOST_PREFIXES is links.TRACKER_HOST_PREFIXES


def test_store_private_tracker_check_is_links_function() -> None:
    assert store._is_tracker_host is links.is_tracker_host
