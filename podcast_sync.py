"""Background sync of ARD Sounds cards: resolve → download → manifest.

A podcast card stores *intent* ("the newest episode of this show", "these
three episodes"), not a file list. Turning that into the concrete
`files[]` the manifest needs takes a catalogue query and one download per
episode — far too slow to do inside a request, and completely off-limits
inside a manifest request (concept §3.2: an ESPuino must never wait on the
hub doing slow work).

So all of it happens here, in a background worker, and the ESPuino-facing
endpoint only ever serves what is already cached. A card that just gained a
newer episode keeps serving the previous one until the download finished —
the same "content changes take effect on the next tap" behaviour the
`version`/stale mechanism already has (§9).

**Exactly one worker across all gunicorn workers.** The container runs
several processes (see Dockerfile), and two of them downloading the same
episode would be wasteful at best. An `flock` on a file in DATA_DIR elects
one of them; if that process dies, the OS drops the lock and another takes
over on its next poll — no heartbeat bookkeeping, no stale-lock cleanup.
"""

import fcntl
import os
import threading
from datetime import datetime, timedelta, timezone

import ard_sounds
import podcast_cache
import podcast_feed
import store as store_lib

# Episodes per card. "Latest N" plus a fixed selection both cap here: every
# episode is a full download onto the hub's data volume, and a card whose
# manifest lists 200 files is not a plausible ESPuino assignment either.
MAX_EPISODES = 20

STATE_PENDING = "pending"    # intent saved, nothing resolved/downloaded yet
STATE_SYNCING = "syncing"    # this worker is on it right now
STATE_READY = "ready"        # files are cached, manifest servable
STATE_ERROR = "error"        # last attempt failed, message says why

LOCK_FILENAME = "podcast-sync.lock"
POLL_SECONDS = 5
ERROR_RETRY_SECONDS = 600


def _now():
    return datetime.now(timezone.utc)


def _parse_ts(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def is_podcast(card):
    return card.get("kind") == "podcast"


def sync_state(card):
    """The card's sync bookkeeping, with defaults for a card that has none
    yet (freshly switched to a podcast, or written by an older version)."""
    state = dict(card.get("podcast_sync") or {})
    state.setdefault("state", STATE_PENDING)
    state.setdefault("message", "")
    state.setdefault("done", 0)
    state.setdefault("total", 0)
    state.setdefault("last_checked", None)
    state.setdefault("last_synced", None)
    state.setdefault("episodes", [])
    state.setdefault("source", None)
    return state


def is_due(card, refresh_minutes, now=None):
    """Whether the worker should (re-)sync this card right now."""
    if not is_podcast(card) or card.get("status") != "assigned":
        return False

    now = now or _now()
    state = sync_state(card)
    if state["state"] in (STATE_PENDING, STATE_SYNCING):
        return True

    last_checked = _parse_ts(state["last_checked"])
    if last_checked is None:
        return True

    if state["state"] == STATE_ERROR:
        return now - last_checked >= timedelta(seconds=ERROR_RETRY_SECONDS)

    # A fixed episode selection cannot change behind our back; only "latest"
    # needs polling, and only if the admin left the interval enabled.
    if (card.get("podcast") or {}).get("selection") != "latest":
        return False
    if not refresh_minutes:
        return False
    return now - last_checked >= timedelta(minutes=refresh_minutes)


# --------------------------------------------------------------------------
# One card
# --------------------------------------------------------------------------
SOURCE_FEED = "rss"   # resolved via the show's official podcast RSS feed
SOURCE_API = "api"    # resolved via ARD's internal catalogue API


def _latest_from_feed(show_id, count):
    """The newest episodes via the show's public podcast feed, or None.

    Preferred over the API wherever a feed exists, because that feed is
    ARD's own published download channel (concept §7.3). The feed URL is
    looked up server-side from the catalogue on every sync — never taken
    from the browser, which would turn a form field into a
    fetch-any-URL lever.

    Returns None (rather than raising) when there is no feed or it cannot be
    used, so the caller falls back to the API instead of failing the card.
    """
    try:
        show = ard_sounds.get_show(show_id)
    except ard_sounds.ArdSoundsError:
        return None
    feed_url = (show or {}).get("feed_url")
    if not feed_url:
        return None
    try:
        return podcast_feed.fetch_episodes(feed_url, limit=count)
    except podcast_feed.PodcastFeedError:
        return None


def _resolve_episodes(podcast):
    """The episodes a card's intent currently points at.

    Returns `(episodes, source)` with episodes in playback order (oldest
    first, matching the cache's date-sorted filenames).
    """
    show_id = podcast.get("show_id")
    if not show_id:
        raise ard_sounds.ArdSoundsError("This card has no ARD Sounds show configured.")

    if podcast.get("selection") == "latest":
        count = max(1, min(int(podcast.get("episode_count") or 1), MAX_EPISODES))

        episodes = _latest_from_feed(show_id, count)
        source = SOURCE_FEED
        if episodes is None:
            listing = ard_sounds.list_episodes(show_id, limit=count)
            episodes = [ep for ep in listing["episodes"] if ep.get("audio_url")]
            source = SOURCE_API
        if not episodes:
            raise ard_sounds.ArdSoundsError(
                "ARD Sounds currently lists no playable episode for this show."
            )
        # Both sources are newest-first; playback order is chronological.
        return (list(reversed(episodes)), source)

    wanted = (podcast.get("episodes") or [])[:MAX_EPISODES]
    if not wanted:
        raise ard_sounds.ArdSoundsError("No episode selected for this card.")
    # The API, always: a fixed selection names ARD episode ids, and a feed
    # carries no such id to match them against (and only a rolling window of
    # recent episodes anyway). Re-resolved every time rather than trusting
    # the stored audio URL — ARD's CDN paths rotate, so a URL saved days ago
    # may already be a 404.
    episodes = [ard_sounds.get_episode(entry["id"]) for entry in wanted]
    episodes.sort(key=lambda ep: (ep.get("publish_date") or "", ep["id"]))
    return (episodes, SOURCE_API)


def sync_card(store, data_dir, esp_id, card_id):
    """Resolves and downloads one podcast card. Returns its final state.

    Never raises: every failure ends up as STATE_ERROR with a message the
    admin sees in the card list.
    """
    card = store.get_card(esp_id, card_id)
    if card is None or not is_podcast(card):
        return None

    podcast = card.get("podcast") or {}
    store.update_podcast_sync(
        esp_id, card_id, state=STATE_SYNCING, message="", done=0, total=0
    )

    def fail(message):
        return store.update_podcast_sync(
            esp_id,
            card_id,
            state=STATE_ERROR,
            message=message,
            last_checked=store_lib.now_iso(),
        )

    try:
        episodes, source = _resolve_episodes(podcast)
    except ard_sounds.ArdSoundsError as exc:
        return fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        # A card must never be left stuck on "syncing" by something we didn't
        # anticipate: an error state is retried, a stuck one is not.
        return fail(f"Unexpected error while resolving episodes: {exc}")

    store.update_podcast_sync(esp_id, card_id, total=len(episodes), source=source)

    files, resolved = [], []
    for index, episode in enumerate(episodes):
        relpath = podcast_cache.episode_relpath(podcast["show_id"], episode)
        try:
            info = podcast_cache.ensure_episode(data_dir, relpath, episode.get("audio_url"))
        except (podcast_cache.PodcastDownloadError, OSError) as exc:
            return fail(f"{episode.get('title') or relpath}: {exc}")
        files.append(info)
        resolved.append(
            {
                "id": episode["id"],
                "title": episode.get("title") or "",
                "publish_date": episode.get("publish_date"),
                "duration": episode.get("duration") or 0,
                "path": info["path"],
                "size": info["size"],
            }
        )
        store.update_podcast_sync(esp_id, card_id, done=index + 1)

    result = store.save_podcast_result(esp_id, card_id, files, resolved)
    try:
        prune_cache(store, data_dir)
    except OSError:
        # Housekeeping only — a card that just synced successfully must not be
        # reported as failed because the cleanup pass tripped over the disk.
        pass
    return result


def prune_cache(store, data_dir):
    """Drops cached episodes no card references any more."""
    keep = {
        entry["path"]
        for card in store.list_cards().values()
        if is_podcast(card)
        for entry in card.get("files", [])
    }
    return podcast_cache.prune(data_dir, keep)


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------
class SyncWorker:
    """Single background thread doing the podcast work for the whole hub."""

    def __init__(self, store, data_dir):
        self.store = store
        self.data_dir = data_dir
        self._lock_path = os.path.join(data_dir, LOCK_FILENAME)
        self._lock_file = None
        self._wakeup = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="podcast-sync", daemon=True)
        self._thread.start()

    def nudge(self):
        """Ask the worker to look for work now instead of after the poll
        interval. Only effective in the process that holds the lock — in any
        other one the change is picked up within POLL_SECONDS anyway, which
        is why this is a nicety and not the mechanism."""
        self._wakeup.set()

    # -- internals ---------------------------------------------------------
    def _acquire_lock(self):
        """True if this process is the elected sync worker."""
        if self._lock_file is not None:
            return True
        try:
            handle = open(self._lock_path, "a+b")
        except OSError:
            return False
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._lock_file = handle
        # A download interrupted by a restart left its card on "syncing"
        # forever; as the (single) fresh worker we can safely reset those.
        self._reset_stuck()
        return True

    def _reset_stuck(self):
        for card in list(self.store.list_cards().values()):
            if is_podcast(card) and sync_state(card)["state"] == STATE_SYNCING:
                self.store.update_podcast_sync(
                    card["esp_id"], card["card_id"], state=STATE_PENDING, done=0
                )

    def _run(self):
        while True:
            try:
                if self._acquire_lock():
                    self._tick()
            except Exception:  # noqa: BLE001 — a worker that dies stops all syncing
                pass
            self._wakeup.wait(POLL_SECONDS)
            self._wakeup.clear()

    def _tick(self):
        refresh_minutes = self.store.get_settings().get("podcast_refresh_minutes")
        now = _now()
        for card in list(self.store.list_cards().values()):
            if is_due(card, refresh_minutes, now):
                sync_card(self.store, self.data_dir, card["esp_id"], card["card_id"])


def start(store, data_dir):
    """Starts the hub's sync worker and returns it."""
    worker = SyncWorker(store, data_dir)
    worker.start()
    return worker
