# ESPuino MediaHub

Lightweight, **locally** run hub for centrally managing the RFID
assignments of multiple [ESPuinos](https://github.com/biologist79/ESPuino). Concept & details: [docs/mediahub-konzept.md](docs/mediahub-konzept.md).

## A picture first

![Overview](docs/Mediahub_overview.png "Mediahub")

## Quick start

The container runs (per default) as `www-data` (uid/gid `33:33`) rather than root, so
`./data` (the only thing it writes to) needs to be writable by that user
first. Point `./media` at wherever your existing audio library already
lives — it's mounted read-only and MediaHub never writes to it. If you want
to make any changes: don't edit docker-compose.yml directly - use .env instead.

```bash
cp env-example .env
mkdir -p data
chown -R 33:33 data
docker compose up -d --build
```

By default MediaHub looks for a local `./media` folder; set `MEDIAHUB_MEDIA` in
`.env` to point at your actual library instead, e.g. `MEDIAHUB_MEDIA=/mnt/audiobooks`.

**Directory listing and file reading are separate Unix permissions** — a
track can show up in the browser tree yet fail to save with "permission
denied" if the file itself isn't readable by uid `33`. If that happens,
either `chmod -R o+rX /path/to/your/library` or set `MEDIAHUB_UID`/`MEDIAHUB_GID`
in `.env` to the uid/gid that already owns your library (`id -u` / `id -g`).

Then open [http://localhost:8080](http://localhost:8080).
Hint: Adjust localhost and port according to your needs.

For local development without Docker:

```bash
pip install -r requirements.txt
python app.py        # http://localhost:8080
```

## Updating

```bash
git pull
docker compose up -d --build
```

`git pull` stays conflict-free because your settings live in `.env` (which git
ignores), not in the tracked files. `--build` is required — without it Compose
reuses the existing image and keeps running the previous version.

## Stack

- **Python 3.12 + Flask** (served by Gunicorn in the container), image based on `python:3.12-slim`.
- **Framework-free frontend** — hand-written CSS in the ESPuino look (blue top bar, logo). Works offline, no CDN dependencies.
- Two volumes: `./data` (devices/cards/assignments in a single `db.json`, plus the ARD Sounds episode cache, read-write) and `./media` (your own existing audio library, mounted read-only — MediaHub only browses and references it, it never copies or uploads files).
- Runs as non-root (`33:33` / `www-data`) by default; see [Quick start](#quick-start).
- **Multilingual** (DE/EN/FR) via Flask-Babel; language switcher top right, auto-detected via `Accept-Language`.
- **Optional password** for the web UI (Settings page) — the ESPuino-facing API stays open regardless, since devices can't log in.

## Feature overview

- **Devices** (`/devices`): ESPuinos that have contacted the hub (IP, last seen, last card).
- **Cards & Assignments** (`/cards`): overview, assign/edit — three content types per card: files/folders from the mounted library via an inline tree browser (`static/js/media-browser.js`, modeled after the ESPuino web UI's own SD explorer), a webradio stream URL, or an **ARD Sounds** show (see below) — force refresh (per card/all), delete.
- **ARD Sounds podcasts** (concept §7.3): search the ARD Sounds catalogue right in the assignment form, then pick either *always the newest episode(s)* or specific episodes. Where a show has an official podcast RSS feed, "newest" is resolved through that feed — ARD's own published download channel — rather than through its internal app API. The hub downloads the chosen episodes into its own cache (`<data>/podcasts/`) and hands the ESPuino an ordinary file manifest — **no firmware change**, and the card plays from the SD card offline like any other assignment. "Latest" cards are re-checked on a configurable interval (Settings) or on demand ("Check episodes"); a new episode is downloaded in the background and plays from the next tap onwards. Cached episodes are shared between cards and removed once nothing references them. Note that ARD offers no official public API — see the caveats in the concept.
- **New Cards**: filter at `/cards?pending=1` — cards registered on tap but not yet assigned (see concept §5.3).
- **Media** (`/media`): storage usage per card; `/media/browse?path=` is the JSON API backing the tree browser.
- **Settings** (`/settings`): delete behavior lazy vs. secure (concept §13.1) — secure calls `DELETE /rfid` on the ESPuino and only removes the hub entry after a confirmed 200 response. Also: subfolder recursion depth, how often ARD Sounds cards are checked for new episodes, and set/remove the optional hub password.
- **MediaHub API**: `GET /<espId>/card/<cardId>/manifest.json` (manifest, or `pending` registration, or `preparing` while a podcast card is still downloading), `GET /media/<path>` (library files, path relative to the library root), `GET /podcast-media/<path>` (cached ARD Sounds episodes).

## Maintaining translations

The source language for `_()`/`ngettext()` strings is English; German and French are catalog translations under `translations/<lang>/LC_MESSAGES/messages.po`. After text changes:

```bash
pybabel extract -F babel.cfg -o messages.pot .
pybabel update -i messages.pot -d translations   # update existing .po files
# fill in msgstr in translations/de/... and translations/fr/...
pybabel compile -d translations                  # only needed for local testing without Docker
```

The `.mo` files are compiled automatically at Docker build time (see Dockerfile) and are not committed.

## Status

Functional hub with device/card management, per-card manifests
(`version` = SHA-256, including the force-refresh lever), a library file/folder
browser for assignment (no uploads — files stay in place under `./media`),
ARD Sounds podcast cards (search, newest-episode subscriptions, background
download and cache cleanup), `pending` registration, lazy/secure delete, and an
optional web UI password.

The ESPuino side has shipped: `MEDIAHUB` (play mode 18) and
`src/MediaHub.cpp` — manifest fetch, SHA-256-verified download to SD,
stale/re-sync, LED download animation — are part of ESPuino firmware **3.0
(07.09.2026)**. MediaHub cards therefore need **firmware 3.0 or newer** on
the device; everything below that has no `mediahub://` support at all.

**Keep the hub on your local network.** The ESPuino-facing endpoints
(`manifest.json`, `/media/`, `/podcast-media/`) are deliberately
unauthenticated — devices can't log in (concept §2). That is fine inside a
household, but a hub reachable from the internet publishes whatever it
serves. For ARD Sounds cards that matters beyond privacy: ARD content is
licensed for private, non-commercial use, and making cached episodes
publicly reachable is not covered by private-copy rules. The optional web UI
password protects the admin interface only, never these endpoints.
