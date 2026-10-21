#!/usr/bin/env python3
"""tdarrSync - repair the Stash database after Tdarr transcodes a file.

Stash identifies files by path and by oshash. Transcoding changes the bytes (so the
oshash changes) and usually the container (so the path changes too). From Stash's point
of view the original scene's file has vanished and an unrelated new file has appeared,
which means a fresh scene with none of your tags, performers, markers, o-counter or
play history.

This plugin closes that gap. Given the before and after paths it rescans the new file
and folds it onto the original scene, keeping the original scene id.

Standard library only - nothing to pip install.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

PLUGIN_ID = "tdarrSync"

# ---------------------------------------------------------------------------
# Logging
#
# Stash reads plugin logs from stderr. Each line is prefixed with SOH, a single
# level character (t/d/i/w/e/p for trace/debug/info/warning/error/progress) and STX.
# ---------------------------------------------------------------------------

_SOH = "\x01"
_STX = "\x02"


def _emit(level, message):
    for line in str(message).split("\n"):
        sys.stderr.write("{}{}{}{}\n".format(_SOH, level, _STX, line))
    sys.stderr.flush()


def debug(m):
    _emit("d", m)


def info(m):
    _emit("i", m)


def warn(m):
    _emit("w", m)


def error(m):
    _emit("e", m)


def progress(fraction):
    _emit("p", str(round(min(max(float(fraction), 0.0), 1.0), 4)))


# ---------------------------------------------------------------------------
# GraphQL client
# ---------------------------------------------------------------------------


class StashError(RuntimeError):
    pass


class StashClient:
    def __init__(self, connection):
        scheme = connection.get("Scheme") or "http"
        host = connection.get("Host") or "localhost"
        # Stash reports its listen address, which is often a wildcard we cannot dial.
        if host in ("0.0.0.0", "::", "[::]", ""):
            host = "localhost"
        port = connection.get("Port") or 9999
        self.url = "{}://{}:{}/graphql".format(scheme, host, port)

        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        cookie = connection.get("SessionCookie") or {}
        if cookie.get("Name") and cookie.get("Value"):
            self.headers["Cookie"] = "{}={}".format(cookie["Name"], cookie["Value"])
        api_key = os.environ.get("STASH_API_KEY")
        if api_key:
            self.headers["ApiKey"] = api_key

    def call(self, query, variables=None, timeout=60):
        payload = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
        request = urllib.request.Request(
            self.url, data=payload, headers=self.headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise StashError("GraphQL HTTP {}: {}".format(exc.code, detail)) from exc
        except urllib.error.URLError as exc:
            raise StashError(
                "Cannot reach Stash at {} ({}).".format(self.url, exc.reason)
            ) from exc

        if body.get("errors"):
            raise StashError("GraphQL error: " + json.dumps(body["errors"])[:800])
        return body.get("data") or {}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

DEFAULTS = {
    "pathMap": "",
    "queueDir": "",
    "disableRegenerate": False,
    "keepStaleFileRecord": False,
    "scanTimeout": 900,
    "settleTimeout": 30,
    "phashDistance": 4,
    "dryRun": False,
}


def load_settings(client):
    settings = dict(DEFAULTS)
    try:
        plugins = client.call("query { configuration { plugins } }")
        stored = (plugins.get("configuration") or {}).get("plugins") or {}
        for key, value in (stored.get(PLUGIN_ID) or {}).items():
            if value not in (None, ""):
                settings[key] = value
    except StashError as exc:
        warn("Could not read plugin settings, falling back to defaults: {}".format(exc))

    for numeric in ("scanTimeout", "settleTimeout", "phashDistance"):
        try:
            settings[numeric] = int(settings[numeric])
        except (TypeError, ValueError):
            settings[numeric] = DEFAULTS[numeric]
    return settings


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def parse_path_map(raw):
    """Parse "a=>b|c=>d" into prefix pairs, longest source first."""
    pairs = []
    for chunk in str(raw or "").split("|"):
        chunk = chunk.strip()
        if not chunk or "=>" not in chunk:
            continue
        source, destination = chunk.split("=>", 1)
        source, destination = source.strip(), destination.strip()
        if source and destination:
            pairs.append((source, destination))
    pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
    return pairs


def _is_windows_style(path):
    """True for drive-letter or UNC paths, which are case-insensitive."""
    return bool(re.match(r"^[A-Za-z]:[\\/]", path or "")) or (path or "").startswith("\\\\")


def map_path(path, pairs):
    """Rewrite a Tdarr-visible path into the equivalent Stash-visible path."""
    if not path:
        return path
    candidate = path.replace("\\", "/")
    for source, destination in pairs:
        prefix = source.replace("\\", "/").rstrip("/")
        # A Windows node may report z:\media while the rule says Z:\Media. Windows does
        # not care about the difference, so neither should we - but keep Linux-to-Linux
        # rules case-sensitive, where /Media and /media really can be different folders.
        fold = _is_windows_style(source) or _is_windows_style(path)
        left = candidate.casefold() if fold else candidate
        right = prefix.casefold() if fold else prefix
        if left == right or left.startswith(right + "/"):
            return destination.rstrip("/") + candidate[len(prefix):]
    return path


def wait_for_file(path, timeout):
    """Wait for a file to exist and stop changing size.

    Tdarr writing across an SMB share and Stash reading the same storage through a bind
    mount do not always agree about when a write finished. Scanning too early gets us a
    truncated file with the wrong size and fingerprint.
    """
    if timeout <= 0:
        return os.path.exists(path)

    deadline = time.time() + timeout
    last_size, stable_for = None, 0.0
    while time.time() < deadline:
        try:
            size = os.path.getsize(path)
        except OSError:
            last_size, stable_for = None, 0.0
            time.sleep(1.0)
            continue
        if size == last_size:
            stable_for += 1.0
            if stable_for >= 2.0:
                return True
        else:
            if last_size is not None:
                debug("{} still growing ({} -> {} bytes)".format(path, last_size, size))
            last_size, stable_for = size, 0.0
        time.sleep(1.0)
    return os.path.exists(path)


def norm(path):
    """Normalise a path for comparison only - never for writing back to Stash."""
    if not path:
        return ""
    flattened = os.path.normpath(path.replace("\\", "/")).replace("\\", "/").rstrip("/")
    return flattened.casefold() if os.name == "nt" else flattened


# ---------------------------------------------------------------------------
# Stash queries
# ---------------------------------------------------------------------------

SCENE_FIELDS = """
  id
  title
  files {
    id
    path
    basename
    size
    duration
    video_codec
    fingerprints { type value }
  }
"""

FIND_BY_PATH = """
query($value: String!, $modifier: CriterionModifier!) {
  findScenes(
    scene_filter: { path: { value: $value, modifier: $modifier } }
    filter: { per_page: 50 }
  ) {
    scenes { %s }
  }
}
""" % SCENE_FIELDS


def _match_exact(scenes, path):
    """Return (scene, file) whose file path matches, else None."""
    target = norm(path)
    for scene in scenes:
        for scene_file in scene.get("files") or []:
            if norm(scene_file.get("path")) == target:
                return scene, scene_file
    return None


def find_by_path(client, path):
    """Locate the scene and file owning an exact path.

    The EQUALS path filter is the fast route; if it misses because of separator or
    prefix differences we retry on the basename and confirm client-side.
    """
    data = client.call(FIND_BY_PATH, {"value": path, "modifier": "EQUALS"})
    hit = _match_exact(data["findScenes"]["scenes"], path)
    if hit:
        return hit

    basename = os.path.basename(path.replace("\\", "/"))
    if not basename:
        return None
    data = client.call(FIND_BY_PATH, {"value": basename, "modifier": "INCLUDES"})
    return _match_exact(data["findScenes"]["scenes"], path)


def wait_for_job(client, job_id, timeout, label):
    query = "query($id: ID!) { findJob(input: {id: $id}) { id status progress error } }"
    deadline = time.time() + timeout
    last_status = None

    while time.time() < deadline:
        job = client.call(query, {"id": job_id}).get("findJob")
        if job is None:
            # The job has already been evicted from the queue, so it is done.
            debug("{} job {} no longer in the queue, treating as finished".format(label, job_id))
            return True

        status = job["status"]
        if status != last_status:
            debug("{} job {}: {}".format(label, job_id, status))
            last_status = status
        if status == "FINISHED":
            return True
        if status in ("CANCELLED", "FAILED"):
            error(
                "{} job {} ended as {}: {}".format(
                    label, job_id, status, job.get("error") or "no detail given"
                )
            )
            return False
        if job.get("progress") is not None:
            progress(job["progress"])
        time.sleep(1.0)

    error("Timed out after {}s waiting for the {} job ({})".format(timeout, label, job_id))
    return False


def scan_paths(client, paths, timeout):
    job_id = client.call(
        "mutation($input: ScanMetadataInput!) { metadataScan(input: $input) }",
        {
            "input": {
                "paths": paths,
                # The transcode may preserve mtime, so never trust it.
                "rescan": True,
                "scanGeneratePhashes": True,
                "scanGenerateCovers": True,
            }
        },
    )["metadataScan"]
    info("Scanning {} (job {})".format(", ".join(paths), job_id))
    return wait_for_job(client, job_id, timeout, "scan")


def regenerate(client, scene_ids):
    if not scene_ids:
        return
    job_id = client.call(
        "mutation($input: GenerateMetadataInput!) { metadataGenerate(input: $input) }",
        {
            "input": {
                "sceneIDs": sorted(set(scene_ids)),
                "overwrite": True,
                "covers": True,
                "sprites": True,
                "previews": True,
                "markers": True,
                "phashes": True,
            }
        },
    )["metadataGenerate"]
    info(
        "Queued regeneration of {} scene(s) (job {}); it will run in the background".format(
            len(set(scene_ids)), job_id
        )
    )


def merge_scenes(client, destination_id, source_ids):
    client.call(
        "mutation($input: SceneMergeInput!) { sceneMerge(input: $input) { id } }",
        {
            "input": {
                "destination": destination_id,
                "source": source_ids,
                "play_history": True,
                "o_history": True,
            }
        },
    )


def set_primary_file(client, scene_id, file_id):
    client.call(
        "mutation($input: SceneUpdateInput!) { sceneUpdate(input: $input) { id } }",
        {"input": {"id": scene_id, "primary_file_id": file_id}},
    )


def delete_file_records(client, file_ids):
    client.call("mutation($ids: [ID!]!) { deleteFiles(ids: $ids) }", {"ids": file_ids})


# ---------------------------------------------------------------------------
# Core sync
# ---------------------------------------------------------------------------


class Context:
    def __init__(self, client, settings):
        self.client = client
        self.settings = settings
        self.path_pairs = parse_path_map(settings["pathMap"])
        self.dry_run = bool(settings["dryRun"])
        self.timeout = settings["scanTimeout"]
        self.touched_scene_ids = []

    def mapped(self, path):
        translated = map_path(path, self.path_pairs)
        if translated != path:
            debug("Path map: {} -> {}".format(path, translated))
        return translated


def sync_one(ctx, original_path, new_path):
    """Reconcile one transcode. Returns True on success."""
    client = ctx.client

    if not new_path:
        error("No newPath supplied; nothing to sync")
        return False

    original_path = ctx.mapped(original_path or "")
    new_path = ctx.mapped(new_path)
    info("Syncing: {} -> {}".format(original_path or "(unknown original)", new_path))

    if not wait_for_file(new_path, ctx.settings["settleTimeout"]):
        warn(
            "Stash still cannot see {} after waiting {}s. If Stash runs on a different "
            "host or inside a container, the path map is the usual culprit.".format(
                new_path, ctx.settings["settleTimeout"]
            )
        )

    in_place = bool(original_path) and norm(original_path) == norm(new_path)

    old_scene = old_file = None
    if original_path and not in_place:
        found = find_by_path(client, original_path)
        if found:
            old_scene, old_file = found
            info(
                'Original is scene {} "{}"'.format(
                    old_scene["id"], old_scene.get("title") or "(untitled)"
                )
            )
        else:
            info("No existing scene owns {}; this will just be a new file".format(original_path))

    if ctx.dry_run:
        if old_scene:
            info("DRY RUN: would scan {} and merge it onto scene {}".format(new_path, old_scene["id"]))
        else:
            info("DRY RUN: would scan {}; no original scene to merge onto".format(new_path))
        return True

    # Scan the new file. Fall back to its folder if the direct path yields nothing,
    # which happens when the scanner needs a directory root.
    if not scan_paths(client, [new_path], ctx.timeout):
        return False

    found = find_by_path(client, new_path)
    if not found:
        parent = os.path.dirname(new_path.replace("\\", "/"))
        info("Not found after scanning the file directly; retrying with {}".format(parent))
        if parent and scan_paths(client, [parent], ctx.timeout):
            found = find_by_path(client, new_path)

    if not found:
        error(
            "{} is not in Stash after a scan. It is almost certainly outside your "
            "configured library paths, or the path map is wrong.".format(new_path)
        )
        return False

    new_scene, new_file = found

    if in_place:
        info("In-place transcode; scene {} refreshed from disk".format(new_scene["id"]))
        ctx.touched_scene_ids.append(new_scene["id"])
        return True

    if old_scene is None:
        info("Scanned {} as new scene {}".format(new_path, new_scene["id"]))
        ctx.touched_scene_ids.append(new_scene["id"])
        return True

    if old_scene["id"] == new_scene["id"]:
        info("Stash already attached the new file to scene {}".format(old_scene["id"]))
    else:
        info(
            "Merging new scene {} into original scene {}".format(
                new_scene["id"], old_scene["id"]
            )
        )
        merge_scenes(client, old_scene["id"], [new_scene["id"]])

    set_primary_file(client, old_scene["id"], new_file["id"])
    info("Scene {} now points at {}".format(old_scene["id"], new_file["path"]))

    _warn_on_duration_drift(old_file, new_file)

    if old_file and not ctx.settings["keepStaleFileRecord"]:
        if os.path.exists(old_file["path"]):
            info(
                "Keeping the old file row; {} still exists on disk".format(old_file["path"])
            )
        else:
            delete_file_records(client, [old_file["id"]])
            info("Removed the stale database row for {}".format(old_file["path"]))

    ctx.touched_scene_ids.append(old_scene["id"])
    return True


def _warn_on_duration_drift(old_file, new_file):
    if not old_file:
        return
    old_duration = old_file.get("duration")
    new_duration = new_file.get("duration")
    if not old_duration or not new_duration:
        return
    drift = abs(float(old_duration) - float(new_duration))
    if drift > 1.0:
        warn(
            "Duration changed by {:.1f}s ({:.1f} -> {:.1f}). Scene markers on this scene "
            "will be off by roughly that much.".format(drift, old_duration, new_duration)
        )


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


def mode_sync(ctx, args):
    original_path = args.get("originalPath") or args.get("original_path") or ""
    new_path = args.get("newPath") or args.get("new_path") or ""
    if not new_path:
        error(
            'The "Sync Transcoded File" task needs originalPath and newPath arguments. '
            "It is meant to be invoked by Tdarr, not run from the Tasks screen."
        )
        return False
    ok = sync_one(ctx, original_path, new_path)
    _finish_regeneration(ctx)
    return ok


def mode_drain(ctx, args):
    queue_dir = args.get("queueDir") or ctx.settings["queueDir"]
    if not queue_dir:
        error("No queue directory configured; set it in the plugin settings")
        return False
    if not os.path.isdir(queue_dir):
        error("Queue directory does not exist or is not readable: {}".format(queue_dir))
        return False

    events = sorted(
        (entry for entry in os.listdir(queue_dir) if entry.lower().endswith(".json")),
        key=lambda name: os.path.getmtime(os.path.join(queue_dir, name)),
    )
    if not events:
        info("Queue is empty")
        return True

    info("Draining {} queued event(s) from {}".format(len(events), queue_dir))
    failures = 0
    for index, name in enumerate(events):
        progress(index / float(len(events)))
        full_path = os.path.join(queue_dir, name)
        try:
            with open(full_path, "r", encoding="utf-8") as handle:
                event = json.load(handle)
        except (OSError, ValueError) as exc:
            error("Skipping unreadable event {}: {}".format(name, exc))
            _mark_failed(full_path, str(exc))
            failures += 1
            continue

        try:
            ok = sync_one(ctx, event.get("originalPath", ""), event.get("newPath", ""))
        except StashError as exc:
            error("Event {} failed: {}".format(name, exc))
            ok = False

        if ok and not ctx.dry_run:
            try:
                os.remove(full_path)
            except OSError as exc:
                warn("Processed {} but could not remove it: {}".format(name, exc))
        elif not ok:
            _mark_failed(full_path, "sync returned failure")
            failures += 1

    _finish_regeneration(ctx)
    progress(1.0)
    info("Drain complete: {} ok, {} failed".format(len(events) - failures, failures))
    return failures == 0


def _mark_failed(full_path, reason):
    try:
        with open(full_path + ".failed", "w", encoding="utf-8") as handle:
            handle.write(reason)
    except OSError:
        pass


def mode_reconcile(ctx, args):
    """Find scenes whose file vanished and re-link them by perceptual hash.

    A phash is computed from the picture, not the bytes, so it survives re-encoding.
    That makes it the one reliable way to recognise a transcoded file as the same
    content when nobody told us the before and after paths.
    """
    client = ctx.client
    distance = ctx.settings["phashDistance"]
    info("Looking for transcode orphans within phash distance {}".format(distance))

    groups = client.call(
        """
        query($distance: Int, $durationDiff: Float) {
          findDuplicateScenes(distance: $distance, duration_diff: $durationDiff) { %s }
        }
        """ % SCENE_FIELDS,
        {"distance": distance, "durationDiff": 1.0},
        timeout=300,
    )["findDuplicateScenes"]

    info("Stash returned {} similar-scene group(s)".format(len(groups)))
    repaired = 0

    for group in groups:
        missing, present = [], []
        for scene in group:
            files = scene.get("files") or []
            if not files:
                continue
            if all(not os.path.exists(f["path"]) for f in files):
                missing.append(scene)
            elif all(os.path.exists(f["path"]) for f in files):
                present.append(scene)

        if len(missing) != 1 or len(present) != 1:
            continue

        old_scene, new_scene = missing[0], present[0]
        info(
            'Orphan: scene {} "{}" lost its file; scene {} looks like its transcode'.format(
                old_scene["id"], old_scene.get("title") or "(untitled)", new_scene["id"]
            )
        )
        if ctx.dry_run:
            info("DRY RUN: would merge {} into {}".format(new_scene["id"], old_scene["id"]))
            continue

        new_file = (new_scene.get("files") or [None])[0]
        merge_scenes(client, old_scene["id"], [new_scene["id"]])
        if new_file:
            set_primary_file(client, old_scene["id"], new_file["id"])
        if not ctx.settings["keepStaleFileRecord"]:
            stale = [f["id"] for f in (old_scene.get("files") or []) if not os.path.exists(f["path"])]
            if stale:
                delete_file_records(client, stale)
        ctx.touched_scene_ids.append(old_scene["id"])
        repaired += 1

    _finish_regeneration(ctx)
    info("Reconcile complete: {} scene(s) repaired".format(repaired))
    return True


def mode_test(ctx, args):
    client = ctx.client
    info("GraphQL endpoint: {}".format(client.url))

    version = client.call("query { version { version build_time } }")["version"]
    info("Stash version: {} (built {})".format(version.get("version"), version.get("build_time")))

    try:
        general = client.call(
            "query { configuration { general { stashes { path } } } }"
        )["configuration"]["general"]
        library_paths = [entry["path"] for entry in general.get("stashes") or []]
        info("Library paths: {}".format(", ".join(library_paths) or "(none configured)"))
    except StashError as exc:
        warn("Could not read library paths: {}".format(exc))
        library_paths = []

    info("Settings in effect:")
    for key in sorted(ctx.settings):
        info("  {} = {!r}".format(key, ctx.settings[key]))

    if ctx.path_pairs:
        info("Path map rules (longest prefix wins):")
        for source, destination in ctx.path_pairs:
            info("  {} => {}".format(source, destination))
    else:
        info("No path map configured; Tdarr paths are used verbatim.")

    for label, key in (("originalPath", "originalPath"), ("newPath", "newPath")):
        raw = args.get(key)
        if not raw:
            continue
        resolved = ctx.mapped(raw)
        exists = os.path.exists(resolved)
        info(
            "  {}: {} -> {} ({})".format(
                label, raw, resolved, "exists" if exists else "NOT FOUND on the Stash host"
            )
        )
        if exists and library_paths:
            inside = any(norm(resolved).startswith(norm(p) + "/") for p in library_paths)
            if not inside:
                warn("    ...but it is not inside any library path, so a scan will ignore it")

    return True


def _finish_regeneration(ctx):
    if ctx.dry_run:
        return
    if ctx.settings["disableRegenerate"]:
        if ctx.touched_scene_ids:
            info("Regeneration is disabled in settings; covers and sprites are now stale")
        return
    regenerate(ctx.client, ctx.touched_scene_ids)


MODES = {
    "sync": mode_sync,
    "drain": mode_drain,
    "reconcile": mode_reconcile,
    "test": mode_test,
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError as exc:
        error("Could not parse the plugin input from stdin: {}".format(exc))
        json.dump({"error": "bad plugin input"}, sys.stdout)
        return 1

    args = payload.get("args") or {}
    connection = payload.get("server_connection") or {}

    mode = args.get("mode")
    if not mode:
        # Tdarr may send only the paths if the task defaults did not apply.
        mode = "sync" if args.get("newPath") else "drain"

    if mode not in MODES:
        error("Unknown mode {!r}; expected one of {}".format(mode, ", ".join(sorted(MODES))))
        json.dump({"error": "unknown mode"}, sys.stdout)
        return 1

    try:
        client = StashClient(connection)
        settings = load_settings(client)
        ctx = Context(client, settings)
        if ctx.dry_run:
            warn("DRY RUN is enabled - no changes will be written")
        ok = MODES[mode](ctx, args)
    except StashError as exc:
        error(str(exc))
        json.dump({"error": str(exc)}, sys.stdout)
        return 1
    except Exception as exc:  # noqa: BLE001 - surface anything else into the Stash log
        error("Unexpected failure in mode {}: {!r}".format(mode, exc))
        json.dump({"error": repr(exc)}, sys.stdout)
        return 1

    json.dump({"output": "ok" if ok else "completed with errors"}, sys.stdout)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
