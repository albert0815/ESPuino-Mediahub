"""Persistent storage for devices, cards and settings.

A single JSON file (``db.json``) under ``DATA_DIR`` is plenty for the
expected scale (a handful of devices, a few dozen cards) — a database
server would be pure overhead here. Writes are serialized by a
process-local lock *and* an ``flock`` on a sibling file, then applied
atomically (tmp file + rename), mirroring the download pattern used on the
ESPuino itself (concept §13). The cross-process lock matters because the
container runs several gunicorn workers and one of them additionally writes
from the background podcast sync (see podcast_sync) — without it, a
read-modify-write in one process could silently drop a change another
process made in the meantime.

Cards are keyed by (esp_id, card_id), not card_id alone — the same
physical card can be enrolled on several ESPuinos (that's the point of
the feature: kids swap cards between devices), and each device gets its
own independent assignment. This also makes secure delete unambiguous:
an assignment already knows exactly which one ESPuino to call, no more
guessing from whichever device last happened to tap the card.
"""

import fcntl
import json
import os
import threading
from datetime import datetime, timezone

_lock = threading.Lock()

DEFAULT_RECURSION_DEPTH = 3
DEFAULT_PODCAST_REFRESH_MINUTES = 360

_DEFAULT_DB = {
    "settings": {
        "delete_mode": "lazy",  # "lazy" | "secure" — see concept §13.1
        "password_hash": None,  # None = hub web UI has no login requirement
        "recursion_depth": DEFAULT_RECURSION_DEPTH,  # subfolder levels "use folder" descends into for recursive play modes
        # How often the hub re-checks an ARD Sounds card set to "latest
        # episode" for a newer one (0 = only when asked to, §7.3).
        "podcast_refresh_minutes": DEFAULT_PODCAST_REFRESH_MINUTES,
    },
    "devices": {},
    "cards": {},
}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _card_key(esp_id, card_id):
    return f"{esp_id}/{card_id}"


class Store:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.db_path = os.path.join(data_dir, "db.json")
        self.lock_path = self.db_path + ".lock"
        os.makedirs(data_dir, exist_ok=True)
        if not os.path.exists(self.db_path):
            self._write(_DEFAULT_DB)
        else:
            self._migrate_legacy_cards()

    # -- low-level ---------------------------------------------------
    def _read(self):
        with open(self.db_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write(self, data):
        tmp_path = self.db_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
        os.replace(tmp_path, self.db_path)

    def _mutate(self, fn):
        """Read, let fn mutate the data in place, write back atomically.

        The whole read-modify-write runs under both locks, so it is atomic
        against the other threads of this process *and* against the other
        gunicorn workers.
        """
        with _lock, open(self.lock_path, "a+b") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                data = self._read()
                result = fn(data)
                self._write(data)
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
            return result

    def _migrate_legacy_cards(self):
        """One-time upgrade from the pre-esp_id schema, where "cards" was
        keyed by card_id alone and carried a "last_seen_esp_id" field.
        Cards that never saw a device (no last_seen_esp_id) can't be
        migrated — there's nothing to attach them to — and are dropped.
        """

        def mutate(data):
            legacy = {
                key: card
                for key, card in data["cards"].items()
                if "esp_id" not in card
            }
            if not legacy:
                return 0
            for key, card in legacy.items():
                del data["cards"][key]
                esp_id = card.pop("last_seen_esp_id", None)
                if not esp_id:
                    continue
                card["esp_id"] = esp_id
                card["card_id"] = key
                data["cards"][_card_key(esp_id, key)] = card
            return len(legacy)

        self._mutate(mutate)

    # -- devices -------------------------------------------------------
    def list_devices(self):
        return self._read()["devices"]

    def touch_device(self, esp_id, ip, card_id, ts):
        def mutate(data):
            dev = data["devices"].setdefault(
                esp_id,
                {"first_seen": ts, "last_seen": ts, "ip": ip, "last_card_id": card_id, "alias": None},
            )
            dev["last_seen"] = ts
            dev["ip"] = ip
            dev["last_card_id"] = card_id
            return dev

        return self._mutate(mutate)

    def set_device_alias(self, esp_id, alias):
        def mutate(data):
            dev = data["devices"].get(esp_id)
            if dev is not None:
                dev["alias"] = alias or None
            return dev

        return self._mutate(mutate)

    def count_cards_for_device(self, esp_id):
        return sum(1 for c in self._read()["cards"].values() if c["esp_id"] == esp_id)

    def delete_device(self, esp_id):
        """Removes the device bookkeeping entry AND all of its card
        assignments (cascade) — the admin already confirmed the count
        client-side before this is called. Returns the number of card
        assignments removed."""

        def mutate(data):
            keys = [key for key, c in data["cards"].items() if c["esp_id"] == esp_id]
            for key in keys:
                del data["cards"][key]
            data["devices"].pop(esp_id, None)
            return len(keys)

        return self._mutate(mutate)

    # -- cards -----------------------------------------------------------
    def list_cards(self):
        return self._read()["cards"]

    def get_card(self, esp_id, card_id):
        return self._read()["cards"].get(_card_key(esp_id, card_id))

    def register_pending(self, esp_id, card_id, ts):
        """Registers a not-yet-known (esp_id, card_id) pair as 'pending'
        (concept §5.3)."""

        def mutate(data):
            key = _card_key(esp_id, card_id)
            if key in data["cards"]:
                card = data["cards"][key]
                card["last_seen"] = ts
                return card
            card = {
                "esp_id": esp_id,
                "card_id": card_id,
                "status": "pending",
                "name": "",
                "kind": "files",
                "play_mode": None,
                "stream_url": None,
                "files": [],
                "force_epoch": 0,
                "first_seen": ts,
                "last_seen": ts,
                "created_at": ts,
                "updated_at": ts,
            }
            data["cards"][key] = card
            return card

        return self._mutate(mutate)

    def touch_card_seen(self, esp_id, card_id, ts):
        def mutate(data):
            card = data["cards"].get(_card_key(esp_id, card_id))
            if card is not None:
                card["last_seen"] = ts
            return card

        return self._mutate(mutate)

    def save_assignment(self, esp_id, card_id, name, kind, play_mode, stream_url, files):
        def mutate(data):
            ts = now_iso()
            key = _card_key(esp_id, card_id)
            card = data["cards"].setdefault(
                key,
                {
                    "esp_id": esp_id,
                    "card_id": card_id,
                    "status": "pending",
                    "force_epoch": 0,
                    "first_seen": ts,
                    "last_seen": ts,
                },
            )
            card["status"] = "assigned"
            card["name"] = name
            card["kind"] = kind
            card["play_mode"] = play_mode
            card["stream_url"] = stream_url
            card["files"] = files
            card["updated_at"] = ts
            # Switching a card away from "podcast" must not leave its ARD
            # Sounds intent behind — the background worker would keep syncing
            # a card whose content type says otherwise.
            card.pop("podcast", None)
            card.pop("podcast_sync", None)
            return card

        return self._mutate(mutate)

    # -- cards: ARD Sounds (podcast) -------------------------------------
    def save_podcast_assignment(self, esp_id, card_id, name, play_mode, podcast):
        """Saves the *intent* of an ARD Sounds card (show + which episodes).

        The concrete file list is not known here — resolving the selection and
        downloading the episodes is the background worker's job (see
        podcast_sync). Any previously synced files are deliberately kept so
        the card stays playable until the new selection finished downloading.
        """

        def mutate(data):
            ts = now_iso()
            key = _card_key(esp_id, card_id)
            card = data["cards"].setdefault(
                key,
                {
                    "esp_id": esp_id,
                    "card_id": card_id,
                    "status": "pending",
                    "force_epoch": 0,
                    "first_seen": ts,
                    "last_seen": ts,
                },
            )
            card["status"] = "assigned"
            card["name"] = name
            card["kind"] = "podcast"
            card["play_mode"] = play_mode
            card["stream_url"] = None
            card.setdefault("files", [])
            card["podcast"] = podcast
            card["podcast_sync"] = {
                "state": "pending",
                "message": "",
                "done": 0,
                "total": 0,
                "last_checked": None,
                "last_synced": (card.get("podcast_sync") or {}).get("last_synced"),
                "episodes": (card.get("podcast_sync") or {}).get("episodes", []),
            }
            card["updated_at"] = ts
            return card

        return self._mutate(mutate)

    def update_podcast_sync(self, esp_id, card_id, **fields):
        """Merges bookkeeping fields (state, message, progress, timestamps)
        into a podcast card's sync record. Deliberately does not touch
        `updated_at`/`files`: progress reporting must not change the manifest
        (and thus its `version`).

        Ignores a card that is no longer an ARD Sounds one — the admin may
        have switched its content type while the background worker was in the
        middle of a download, and a late progress write must not resurrect
        podcast state on a plain file card.
        """

        def mutate(data):
            card = data["cards"].get(_card_key(esp_id, card_id))
            if card is None or card.get("kind") != "podcast":
                return None
            sync = card.setdefault("podcast_sync", {})
            sync.update(fields)
            return card

        return self._mutate(mutate)

    def save_podcast_result(self, esp_id, card_id, files, episodes):
        """Stores a finished sync: the manifest file list plus the episode
        metadata behind it."""

        def mutate(data):
            card = data["cards"].get(_card_key(esp_id, card_id))
            if card is None or card.get("kind") != "podcast":
                return None
            ts = now_iso()
            changed = card.get("files") != files
            card["files"] = files
            if changed:
                # Only a real content change is an assignment change; a
                # re-check that confirmed the same episode is not.
                card["updated_at"] = ts
            card["podcast_sync"] = {
                **card.get("podcast_sync", {}),
                "state": "ready",
                "message": "",
                "done": len(files),
                "total": len(files),
                "last_checked": ts,
                "last_synced": ts,
                "episodes": episodes,
            }
            return card

        return self._mutate(mutate)

    def mark_podcast_pending(self, esp_id, card_id):
        """"Check for new episodes now" — makes the card due for the worker."""

        def mutate(data):
            card = data["cards"].get(_card_key(esp_id, card_id))
            if card is None or card.get("kind") != "podcast":
                return None
            sync = card.setdefault("podcast_sync", {})
            sync.update({"state": "pending", "message": "", "done": 0, "total": 0})
            return card

        return self._mutate(mutate)

    def bump_force_epoch(self, esp_id, card_id):
        def mutate(data):
            card = data["cards"].get(_card_key(esp_id, card_id))
            if card is not None:
                card["force_epoch"] = card.get("force_epoch", 0) + 1
                card["updated_at"] = now_iso()
            return card

        return self._mutate(mutate)

    def bump_force_epoch_all(self):
        def mutate(data):
            ts = now_iso()
            for card in data["cards"].values():
                card["force_epoch"] = card.get("force_epoch", 0) + 1
                card["updated_at"] = ts
            return len(data["cards"])

        return self._mutate(mutate)

    def delete_card(self, esp_id, card_id):
        def mutate(data):
            return data["cards"].pop(_card_key(esp_id, card_id), None)

        return self._mutate(mutate)

    # -- settings ----------------------------------------------------
    def get_settings(self):
        # Merge over defaults so a db.json predating a newly-added setting
        # (e.g. recursion_depth) still returns a usable value instead of
        # requiring an explicit migration step for every new setting.
        return {**_DEFAULT_DB["settings"], **self._read()["settings"]}

    def set_delete_mode(self, mode):
        def mutate(data):
            data["settings"]["delete_mode"] = mode
            return mode

        return self._mutate(mutate)

    def set_recursion_depth(self, depth):
        def mutate(data):
            data["settings"]["recursion_depth"] = depth
            return depth

        return self._mutate(mutate)

    def set_podcast_refresh_minutes(self, minutes):
        def mutate(data):
            data["settings"]["podcast_refresh_minutes"] = minutes
            return minutes

        return self._mutate(mutate)

    def set_password_hash(self, password_hash):
        """password_hash is None to disable the login requirement entirely."""

        def mutate(data):
            data["settings"]["password_hash"] = password_hash
            return password_hash

        return self._mutate(mutate)
