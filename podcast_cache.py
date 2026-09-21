"""On-hub cache for ARD Sounds episodes (concept §7.3).

Why the hub downloads at all instead of handing the ESPuino the ARD URL:

- The manifest promises `size` and `sha256` per file, and the whole
  local-first design (concept §3) rests on the ESPuino holding the bytes on
  its SD card. An episode cached here is therefore an ordinary `files`
  manifest — **no firmware change, no new manifest field**, and the card
  plays offline in the car like any other assignment.
- ARD delivers over HTTPS from a CDN with rotating paths. Doing that
  download in Python once, instead of on an ESP32 every time, sidesteps
  both the TLS/heap fragility on the device and the "stable URL" problem the
  redirect-server approach exists to work around.

The cache lives under `DATA_DIR/podcasts/` — `MEDIA_DIR` is the admin's own
library and mounted read-only (concept §5.4), so nothing may ever be written
there. Files are shared by content, not by card: two cards subscribing to
the same show reference the same cached episode.

Download robustness mirrors §13 on the ESPuino side: stream to `.tmp`, hash
incrementally, and only rename into place once the download completed. A
failed download never leaves a plausible-looking truncated file behind.
"""

import hashlib
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request

import media_library

CACHE_SUBDIR = "podcasts"

# ARD serves mp3 throughout, but the mime type is what decides — restricted
# to the extensions the ESPuino firmware itself recognizes as playable
# (media_library.AUDIO_EXTENSIONS), so a cached episode can never end up
# with a name the device would refuse to play.
_MIME_EXTENSIONS = {
    "audio/mp3": ".mp3",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/m4a": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/flac": ".flac",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
}
DEFAULT_EXTENSION = ".mp3"

DOWNLOAD_TIMEOUT = 60  # seconds per socket operation, not for the whole transfer
CHUNK_SIZE = 1024 * 1024

_UNSAFE_CHARS = re.compile(r"[^0-9A-Za-z._-]")


class PodcastDownloadError(Exception):
    """A download that did not produce a usable cached file."""


def cache_root(data_dir):
    return os.path.join(data_dir, CACHE_SUBDIR)


def _extension(mime_type, url):
    ext = _MIME_EXTENSIONS.get((mime_type or "").split(";")[0].strip().lower())
    if ext:
        return ext
    # Fall back to the URL's own suffix, but only if the firmware would
    # accept it — anything else becomes .mp3 rather than an unplayable name.
    url_ext = os.path.splitext(urllib.parse.urlsplit(url or "").path)[1].lower()
    return url_ext if url_ext in media_library.AUDIO_EXTENSIONS else DEFAULT_EXTENSION


def _safe(part):
    return _UNSAFE_CHARS.sub("_", str(part or ""))[:64] or "x"


def episode_relpath(show_id, episode, mime_type=None):
    """Cache-relative path of one episode: `<showId>/<date>_<episodeId><ext>`.

    The publish date leads so that a plain filename sort equals chronological
    order — that's what the ESPuino's `ALL_TRACKS_OF_DIR_SORTED` modes do on
    the device, so a multi-episode card plays oldest-first without the hub
    having to influence playback order.
    """
    date = (episode.get("publish_date") or "")[:10] or "0000-00-00"
    ext = _extension(mime_type or episode.get("mime_type"), episode.get("audio_url"))
    return f"{_safe(show_id)}/{_safe(date)}_{_safe(episode['id'])}{ext}"


def cached_file_info(data_dir, relpath):
    """`{"path", "size", "sha256"}` for an already-cached episode, or None.

    Reuses the library's own stat-and-hash so a cached episode is described
    exactly like a library file — the manifest builder can't tell them apart.
    """
    return media_library.stat_and_hash(cache_root(data_dir), relpath)


def download_episode(data_dir, relpath, audio_url):
    """Downloads one episode into the cache and returns its file info.

    Streams into `<relpath>.tmp`, hashes while writing and only then renames
    into place. Raises PodcastDownloadError on any failure, leaving no
    partial file behind.
    """
    if not audio_url:
        raise PodcastDownloadError("ARD Sounds returned no audio URL for this episode.")

    root = cache_root(data_dir)
    target = media_library.resolve(root, relpath)
    if target is None:
        raise PodcastDownloadError(f"Refusing to write outside the podcast cache: {relpath}")

    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp_path = target + ".tmp"
    request = urllib.request.Request(
        audio_url,
        headers={"User-Agent": "ESPuino-MediaHub", "Accept": "*/*"},
    )

    hasher = hashlib.sha256()
    size = 0
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response, open(
            tmp_path, "wb"
        ) as out:
            while True:
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                out.write(chunk)
                hasher.update(chunk)
                size += len(chunk)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _remove(tmp_path)
        raise PodcastDownloadError(f"Download failed: {exc}") from exc

    if size == 0:
        _remove(tmp_path)
        raise PodcastDownloadError("Download produced an empty file.")

    try:
        os.replace(tmp_path, target)
    except OSError as exc:
        _remove(tmp_path)
        raise PodcastDownloadError(f"Could not store the downloaded episode: {exc}") from exc

    return {"path": relpath, "size": size, "sha256": hasher.hexdigest()}


def ensure_episode(data_dir, relpath, audio_url):
    """File info for an episode, downloading it only if not cached yet."""
    info = cached_file_info(data_dir, relpath)
    if info is not None:
        return info
    return download_episode(data_dir, relpath, audio_url)


def total_bytes(data_dir):
    """Bytes the cache occupies. In-flight downloads (`.tmp`) don't count —
    they are not content yet."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(cache_root(data_dir)):
        for name in filenames:
            if name.endswith(".tmp"):
                continue
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                continue
    return total


# A `.tmp` file this old cannot belong to a running download any more (the
# socket timeout alone would have given up long before), so it is a leftover
# from a crashed or restarted worker and safe to delete. Younger ones are
# left alone: pruning while another worker is mid-download would make its
# rename fail for no reason.
TMP_GRACE_SECONDS = 3600


def prune(data_dir, keep_relpaths):
    """Deletes cached episodes no longer referenced by any card assignment.

    Called after a card is synced, saved or deleted: the cache is hub-owned
    data (in contrast to the admin's library, which MediaHub never touches —
    §5.4), so it is ours to clean up. Returns (files removed, bytes freed).
    """
    root = cache_root(data_dir)
    if not os.path.isdir(root):
        return (0, 0)

    keep = set(keep_relpaths or ())
    now = time.time()
    removed, freed = 0, 0
    for dirpath, _dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            abs_path = os.path.join(dirpath, name)
            relpath = os.path.relpath(abs_path, root).replace(os.sep, "/")
            if relpath in keep:
                continue
            try:
                stat = os.stat(abs_path)
                if name.endswith(".tmp") and now - stat.st_mtime < TMP_GRACE_SECONDS:
                    continue
                os.remove(abs_path)
            except OSError:
                continue
            removed += 1
            freed += stat.st_size
        if dirpath != root:
            try:
                os.rmdir(dirpath)  # only succeeds once the folder is empty
            except OSError:
                pass
    return (removed, freed)


def delete_all(data_dir):
    shutil.rmtree(cache_root(data_dir), ignore_errors=True)


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass
