"""Read-only client for the ARD Sounds catalogue (formerly ARD Audiothek).

The second source behind a "Podcast" card, next to a plain RSS feed
(concept §7.4): searching shows in the ARD catalogue, listing their
episodes and resolving one episode to a downloadable audio URL.

**This is an unofficial interface.** ARD publishes no documented, stable
public API; `api.ardaudiothek.de/graphql` is the same endpoint the ARD
Sounds website itself uses and what the community tooling around it
(audiothek-downloader, ARD-Audiothek-RSS, espuino-podcast-server, …) talks
to as well. It can change without notice — everything here therefore fails
soft: an `ArdSoundsError` with a readable message, never an exception
leaking into a request handler. Please respect ARD's terms of use; this is
a private, non-commercial convenience feature.

Compared to the scraping approach of espuino-podcast-server (show page HTML
→ episode URNs → embed page → numeric id → GraphQL) a single GraphQL query
per step is enough: `search`/`programSet` already return numeric ids, and
`Item.audios` carries the audio URL directly. No HTML parsing, no regexes
over someone else's markup.
"""

import json
import re
import urllib.error
import urllib.request

from ttl_cache import TtlCache

GRAPHQL_ENDPOINT = "https://api.ardaudiothek.de/graphql"

# A UA that names the project: if ARD ever wants to block or contact us,
# they can tell who this is instead of seeing an anonymous scraper.
USER_AGENT = "ESPuino-MediaHub (+https://github.com/albert0815/ESPuino-Mediahub)"

# Short enough that an admin clicking "Search" never waits long, generous
# enough for the occasionally sluggish GraphQL endpoint.
REQUEST_TIMEOUT = 15

# Cache TTLs (seconds). Catalogue data is not time-critical; audio URLs are
# re-resolved often because they can rotate (CDN tokens/paths).
SEARCH_TTL = 300
EPISODE_LIST_TTL = 600
EPISODE_TTL = 900

MAX_SEARCH_RESULTS = 25
MAX_EPISODE_PAGE = 100

# Width to bake into the {width} placeholder ARD's image URLs carry.
IMAGE_WIDTH = 256

# "urn:ard:show:321c2c393a041ddc" — accepted anywhere in the input so an
# admin can paste a whole ardsounds.de / ardaudiothek.de show URL.
SHOW_URN_PATTERN = re.compile(r"urn:ard:show:[0-9a-f]+", re.IGNORECASE)


class ArdSoundsError(Exception):
    """Anything that went wrong talking to ARD Sounds, with a message meant
    to be shown to the admin as-is."""


_cache = TtlCache()


def _query(query, variables):
    """POSTs a GraphQL query and returns its `data`, or raises ArdSoundsError."""
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = urllib.request.Request(
        GRAPHQL_ENDPOINT,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        raise ArdSoundsError(f"ARD Sounds replied with HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ArdSoundsError(f"ARD Sounds is unreachable ({exc}).") from exc
    except ValueError as exc:
        raise ArdSoundsError("ARD Sounds sent a response that isn't valid JSON.") from exc

    if body.get("errors"):
        first = body["errors"][0].get("message", "unknown error")
        raise ArdSoundsError(f"ARD Sounds rejected the query: {first}")
    data = body.get("data")
    if data is None:
        raise ArdSoundsError("ARD Sounds sent an empty response.")
    return data


def _image_url(image):
    """ARD image URLs carry a literal `{width}` placeholder that has to be
    filled in before the browser can load them. Prefers the square variant
    (the card form shows small thumbnails)."""
    if not image:
        return None
    url = image.get("url1X1") or image.get("url")
    return url.replace("{width}", str(IMAGE_WIDTH)) if url else None


# --------------------------------------------------------------------------
# Shows
# --------------------------------------------------------------------------
_SHOW_FIELDS = """
    id
    coreId
    title
    synopsis
    numberOfElements
    lastItemAdded
    sharingUrl
    publicationService { title }
    image { url url1X1 }
"""


def _show_from_node(node):
    service = node.get("publicationService") or {}
    return {
        "id": str(node["id"]),
        "urn": node.get("coreId"),
        "title": node.get("title") or "",
        "synopsis": node.get("synopsis") or "",
        "publisher": service.get("title") or "",
        "episode_count": node.get("numberOfElements") or 0,
        "last_item_added": node.get("lastItemAdded"),
        "url": node.get("sharingUrl"),
        "image_url": _image_url(node.get("image")),
    }


def extract_show_urn(text):
    """The `urn:ard:show:…` inside a pasted show URL, or None."""
    match = SHOW_URN_PATTERN.search(text or "")
    return match.group(0).lower() if match else None


def search_shows(query, limit=MAX_SEARCH_RESULTS):
    """Shows matching a free-text query, best match first.

    A pasted show URL (or a bare `urn:ard:show:…`) is resolved directly
    instead of being run through the text search — the admin already told us
    exactly which show they mean.
    """
    query = (query or "").strip()
    if not query:
        return []
    limit = max(1, min(int(limit), MAX_SEARCH_RESULTS))

    urn = extract_show_urn(query)
    if urn:
        show = get_show_by_urn(urn)
        return [show] if show else []

    cache_key = f"search:{limit}:{query.casefold()}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    data = _query(
        "query Search($q: String!, $limit: Int!) {"
        "  search(query: $q, limit: $limit) {"
        f"    programSets {{ nodes {{ {_SHOW_FIELDS} }} }}"
        "  }"
        "}",
        {"q": query, "limit": limit},
    )
    nodes = (((data.get("search") or {}).get("programSets") or {}).get("nodes")) or []
    shows = [_show_from_node(node) for node in nodes if node.get("id")]
    _cache.set(cache_key, shows, SEARCH_TTL)
    return shows


def get_show(show_id):
    """One show by its numeric ARD id, or None if unknown."""
    cache_key = f"show:{show_id}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    data = _query(
        "query Show($id: ID!) { programSet(id: $id) { " + _SHOW_FIELDS + " } }",
        {"id": str(show_id)},
    )
    node = data.get("programSet")
    show = _show_from_node(node) if node and node.get("id") else None
    if show:
        _cache.set(cache_key, show, EPISODE_LIST_TTL)
    return show


def get_show_by_urn(urn):
    """One show by its `urn:ard:show:…` core id, or None — the lookup behind
    pasting a show URL into the search box."""
    cache_key = f"show-urn:{urn}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    data = _query(
        "query ShowByUrn($urn: String!) {"
        "  programSets(condition: { coreId: $urn }, first: 1) {"
        f"    nodes {{ {_SHOW_FIELDS} }}"
        "  }"
        "}",
        {"urn": urn},
    )
    nodes = ((data.get("programSets") or {}).get("nodes")) or []
    show = _show_from_node(nodes[0]) if nodes and nodes[0].get("id") else None
    if show:
        _cache.set(cache_key, show, EPISODE_LIST_TTL)
    return show


# --------------------------------------------------------------------------
# Episodes
# --------------------------------------------------------------------------
_EPISODE_FIELDS = """
    id
    coreId
    title
    publishDate
    duration
    audios { url mimeType allowDownload }
"""


def _episode_from_node(node, show_id=None):
    audio = _pick_audio(node.get("audios") or [])
    return {
        "id": str(node["id"]),
        "urn": node.get("coreId"),
        "title": node.get("title") or "",
        "publish_date": node.get("publishDate"),
        "duration": node.get("duration") or 0,
        "audio_url": audio.get("url") if audio else None,
        "mime_type": audio.get("mimeType") if audio else None,
        "show_id": str(show_id) if show_id else None,
    }


def _pick_audio(audios):
    """ARD lists the same file several times, differing in `allowDownload`.
    Prefer an entry that says downloading is allowed; fall back to the first
    one with a URL so a show that doesn't set the flag still works."""
    for audio in audios:
        if audio.get("url") and audio.get("allowDownload"):
            return audio
    for audio in audios:
        if audio.get("url"):
            return audio
    return None


def list_episodes(show_id, limit=50, offset=0):
    """Episodes of a show, newest first, plus the total number available.

    `isPublished` filters out the expired/unpublished items ARD keeps in the
    catalogue — those have no audio at all and would just be dead rows in
    the picker (and unplayable cards).
    """
    show_id = str(show_id)
    limit = max(1, min(int(limit), MAX_EPISODE_PAGE))
    offset = max(0, int(offset))

    cache_key = f"episodes:{show_id}:{limit}:{offset}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    data = _query(
        "query Episodes($id: ID!, $limit: Int!, $offset: Int!) {"
        "  programSet(id: $id) {"
        "    id"
        "    title"
        "    items(first: $limit, offset: $offset, orderBy: PUBLISH_DATE_DESC,"
        "          filter: { isPublished: { equalTo: true } }) {"
        "      totalCount"
        f"      nodes {{ {_EPISODE_FIELDS} }}"
        "    }"
        "  }"
        "}",
        {"id": show_id, "limit": limit, "offset": offset},
    )
    program_set = data.get("programSet")
    if not program_set:
        raise ArdSoundsError(f"ARD Sounds doesn't know a show with id {show_id}.")

    items = program_set.get("items") or {}
    episodes = [
        _episode_from_node(node, show_id)
        for node in (items.get("nodes") or [])
        if node.get("id")
    ]
    result = {
        "show_id": show_id,
        "show_title": program_set.get("title") or "",
        "total": items.get("totalCount") or len(episodes),
        "episodes": episodes,
    }
    _cache.set(cache_key, result, EPISODE_LIST_TTL)
    return result


def get_episode(episode_id):
    """One episode including its current audio URL.

    Cached only briefly: the audio URL is what gets downloaded, and ARD can
    move it (CDN paths carry dates/tokens), so a stale hit would produce a
    404 at download time.
    """
    episode_id = str(episode_id)
    cache_key = f"episode:{episode_id}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    data = _query(
        "query Episode($id: ID!) {"
        "  item(id: $id) {"
        f"    {_EPISODE_FIELDS}"
        "    programSet { id title }"
        "  }"
        "}",
        {"id": episode_id},
    )
    node = data.get("item")
    if not node or not node.get("id"):
        raise ArdSoundsError(f"ARD Sounds doesn't know an episode with id {episode_id}.")

    program_set = node.get("programSet") or {}
    episode = _episode_from_node(node, program_set.get("id"))
    episode["show_title"] = program_set.get("title") or ""
    _cache.set(cache_key, episode, EPISODE_TTL)
    return episode
