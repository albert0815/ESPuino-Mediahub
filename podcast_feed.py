"""Reads episodes from an ordinary podcast RSS feed.

Many ARD Sounds shows are also published as plain podcasts (feeds.br.de,
podcast.hr.de, deutschlandfunkkultur.de, …). Where such a feed exists, the
hub resolves "the newest episode(s)" through it rather than through ARD's
internal app API, because that feed is the channel ARD publishes *for
downloading* — it exists so podcast clients can fetch episodes, which is
exactly what MediaHub does (concept §7.3).

It is the same content either way: for a spot-checked BR show the feed's
`<guid>` is the very UUID that appears in the API's audio URL. What differs
is the delivery endpoint — the feed points at a dedicated podcast URL
(`media.neuland.br.de/…/feed/…`) instead of the app CDN path.

Parsing is stdlib-only (`xml.etree`), in keeping with the project's
no-dependencies stance. Feeds are fetched with a size cap: the URL always
comes from ARD's own catalogue rather than from a browser, but a parser
that will happily chew through an unbounded download is a bad idea either
way.
"""

import email.utils
import hashlib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree

from ard_sounds import TtlCache

REQUEST_TIMEOUT = 15
FEED_TTL = 600

# Comfortably more than any real podcast feed (the BR one above is 35 KB);
# small enough that a runaway response can't exhaust memory.
MAX_FEED_BYTES = 8 * 1024 * 1024

ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"

_cache = TtlCache()


class PodcastFeedError(Exception):
    """A feed that could not be fetched or parsed."""


def _fetch(feed_url):
    request = urllib.request.Request(
        feed_url,
        headers={
            "User-Agent": "ESPuino-MediaHub (+https://github.com/albert0815/ESPuino-Mediahub)",
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            # read(n+1) so an oversized body is detected rather than silently
            # truncated into invalid XML.
            body = response.read(MAX_FEED_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PodcastFeedError(f"Podcast feed unreachable ({exc}).") from exc
    if len(body) > MAX_FEED_BYTES:
        raise PodcastFeedError("Podcast feed is implausibly large — ignoring it.")
    return body


def _parse_date(value):
    """RFC 822 `pubDate` -> ISO 8601, or None."""
    if not value:
        return None
    try:
        return email.utils.parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError):
        return None


def _parse_duration(value):
    """iTunes durations come as seconds, `MM:SS` or `HH:MM:SS`."""
    if not value:
        return 0
    parts = value.strip().split(":")
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return 0
    seconds = 0
    for number in numbers:
        seconds = seconds * 60 + number
    return seconds


def _episode_id(guid, audio_url):
    """A short, stable, filesystem-safe id for a feed episode.

    Feed GUIDs are free-form (UUIDs, URLs, sentences), so they are hashed
    rather than sanitized — that keeps cache filenames bounded and
    collision-free. The `rss-` prefix keeps the namespace visibly separate
    from ARD's numeric API ids: a show that gains or loses a public feed
    re-downloads its episodes once under the other naming, and the old
    copies are pruned like any other unreferenced file.
    """
    seed = (guid or audio_url or "").strip()
    return "rss-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _item_to_episode(item):
    enclosure = item.find("enclosure")
    if enclosure is None:
        return None
    audio_url = (enclosure.get("url") or "").strip()
    if not audio_url:
        return None

    guid = (item.findtext("guid") or "").strip()
    return {
        "id": _episode_id(guid, audio_url),
        "urn": guid or None,
        "title": (item.findtext("title") or "").strip(),
        "publish_date": _parse_date(item.findtext("pubDate")),
        "duration": _parse_duration(item.findtext(f"{{{ITUNES_NS}}}duration")),
        "audio_url": audio_url,
        "mime_type": enclosure.get("type"),
    }


def fetch_episodes(feed_url, limit=20):
    """Episodes of a podcast feed, newest first.

    Raises PodcastFeedError for anything unusable, so the caller can fall
    back to another source.
    """
    if not (feed_url or "").startswith(("http://", "https://")):
        raise PodcastFeedError("Not a usable podcast feed URL.")

    cache_key = f"feed:{feed_url}"
    episodes = _cache.get(cache_key)
    if episodes is None:
        body = _fetch(feed_url)
        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError as exc:
            raise PodcastFeedError(f"Podcast feed is not valid XML ({exc}).") from exc

        channel = root.find("channel")
        if channel is None:
            raise PodcastFeedError("Podcast feed has no <channel> element.")

        episodes = [
            episode
            for episode in (_item_to_episode(item) for item in channel.findall("item"))
            if episode is not None
        ]
        # Most feeds are already newest-first, but say so explicitly — the
        # cache filenames and playback order depend on it.
        episodes.sort(key=lambda episode: episode["publish_date"] or "", reverse=True)
        if not episodes:
            raise PodcastFeedError("Podcast feed lists no downloadable episode.")
        _cache.set(cache_key, episodes, FEED_TTL)

    return episodes[: max(1, int(limit))]
