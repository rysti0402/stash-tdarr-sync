# stash-tdarr-sync

Keeps [Stash](https://github.com/stashapp/stash) correct after [Tdarr](https://github.com/HaveAGitGat/Tdarr) transcodes a file.

## The problem

Stash identifies a file by its **path** and its **oshash**. Transcoding changes the bytes,
so the oshash changes, and it usually changes the container, so the path changes too.

From Stash's point of view the scene's file vanished and an unrelated new file appeared.
After your next scan you get a brand new scene with no tags, no performers, no studio,
no markers, no o-counter, no play history and no stash-ids — and the original scene
sitting there pointing at a file that no longer exists.

## The fix

Two pieces:

| Piece | Where it runs | What it does |
| --- | --- | --- |
| `notifyStash` | Tdarr, as the last node in your flow | Sends the before/after paths to Stash |
| `tdarrSync` | Stash, as a plugin | Rescans the new file and merges it onto the original scene |

The merge uses Stash's `sceneMerge` mutation with the **original scene as the destination**,
so the original scene id survives. Everything hanging off that id — markers, play history,
o-counter, stash-ids — comes along. `play_history` and `o_history` are unioned rather than
replaced.

**[FLOW.md](FLOW.md)** has a complete worked flow, node by node, for a Windows Tdarr node
talking to a Stash container on a separate Linux box — including the path map, which is
where this usually goes wrong.

## Install — Stash side

1. Copy `stash-plugin/tdarrSync/` into your Stash plugins directory:

   - Bare metal: `~/.stash/plugins/tdarrSync/`
   - Docker: `/root/.stash/plugins/tdarrSync/` inside the container (wherever your
     config volume is mounted)

   You should end up with `.../plugins/tdarrSync/tdarrSync.yml` and `.../tdarrSync.py`.

2. **Settings → Plugins → Reload Plugins**. "Tdarr Sync" appears in the list.

3. No `pip install` needed — the plugin is standard library only. If your Stash host's
   `python` is Python 2 or missing, change the `exec:` line in `tdarrSync.yml` to
   `python3` or an absolute interpreter path.

4. Run **Settings → Tasks → Plugin Tasks → Tdarr Sync → Test Connection** and read the log.
   It prints your library paths and the settings in effect. Fix anything it complains about
   before going further.

5. Turn on **Dry run** in the plugin settings for your first few real transcodes.

## Install — Tdarr side

**Easiest:** import `tdarr-flow/Stash-HEVC-Flow.json` (Tdarr → Flows → Import) and set
`STASH_URL` and `STASH_API_KEY` at the top of its **Notify Stash** node. Nothing to install —
the notification is a Community *Custom JS Function* node with the code inlined. See
[FLOW.md](FLOW.md).

**Alternative:** if you would rather add the step to an existing flow as a proper node,
create a local flow plugin from `tdarr-plugin/notifyStash/1.0.0/index.js` via the Tdarr UI
(**Plugins → Local Flow Plugins → new plugin**, paste the file contents). You do not need
both.

If you go the local-plugin route, in your flow:

1. Add the **Notify Stash** node **at the very end**, after the final file move.
   This matters: earlier in the flow `args.inputFileObj._id` still points at the working
   copy in the cache directory, not the file Stash needs to scan.
2. Set **Stash URL** as seen *from the Tdarr node* (not from your browser).
3. Set **Stash API Key** from Stash → Settings → Security → API Key.

### If you are not using flows

Any post-processing hook that can make an HTTP call will do. The plugin is just a
GraphQL mutation:

```bash
curl -s "$STASH_URL/graphql" \
  -H "Content-Type: application/json" \
  -H "ApiKey: $STASH_API_KEY" \
  -d @- <<JSON
{
  "query": "mutation(\$a: Map) { runPluginTask(plugin_id: \"tdarrSync\", task_name: \"Sync Transcoded File\", args_map: \$a) }",
  "variables": { "a": { "mode": "sync", "originalPath": "$ORIGINAL", "newPath": "$NEW" } }
}
JSON
```

Or skip HTTP entirely: write `{"originalPath": "...", "newPath": "..."}` to a `.json` file
in the queue directory and let the **Drain Queue** task pick it up.

## Path mapping

The single most common reason this doesn't work: Tdarr and Stash are in different
containers and see the same file at different paths.

Set **Path map** in the plugin settings. Pipe-separated `from=>to` pairs, longest prefix wins:

```
/media=>/data/media|/mnt/user/tv=>/tv
```

`from` is what Tdarr reports, `to` is what Stash sees. **Test Connection** accepts
`originalPath` / `newPath` args and will show you exactly how a path resolves and whether
the result exists and falls inside a library path.

## Tasks

| Task | Use |
| --- | --- |
| **Test Connection** | Run first. Verifies GraphQL, prints library paths, resolves your path map. |
| **Sync Transcoded File** | Invoked by Tdarr. Needs `originalPath` + `newPath`, so running it from the UI does nothing. |
| **Drain Queue** | Processes spooled `.json` events. Worth putting on a schedule if you use the queue. |
| **Reconcile Orphans** | Safety net — see below. |

### Reconcile Orphans

For transcodes that happened before you installed this, or whose notification got lost.

It asks Stash for groups of scenes with similar perceptual hashes. A **phash is computed
from the picture, not the bytes**, so it survives re-encoding — which is exactly why it can
recognise a transcoded file as the same content with nobody to tell it so.

In each group it looks for exactly one scene whose files are all gone from disk and exactly
one whose files are all present, and merges the present one into the missing one. Groups
that are ambiguous are skipped and left for you.

Run it with **Dry run** on first. Raise **Phash distance** above the default 4 only if you
are missing matches, and re-check the dry-run output when you do.

## Settings

| Setting | Default | Notes |
| --- | --- | --- |
| Path map | empty | `from=>to` pairs separated by `\|` |
| Queue directory | empty | Spool dir, must be readable by Stash |
| Skip regenerating | off | Leave off. Covers, sprites, previews and phash all describe the old file after a transcode. |
| Keep the original file's database row | off | When off, the stale row is deleted **only if the file is genuinely gone from disk**. Nothing on disk is ever touched. |
| File settle timeout | 30 | Waits for the new file to appear and stop growing before scanning. Matters when Tdarr writes over SMB and Stash reads a bind mount. 0 disables. |
| Phash distance | 4 | Reconcile only |
| Dry run | off | Logs decisions, writes nothing |

## What it handles

- **In-place re-encode** (same path) — targeted rescan with `rescan: true`, because a
  transcode can preserve mtime and Stash would otherwise skip the file.
- **Container change** (`.avi` → `.mkv`) — scan, then merge the new scene into the old one
  and repoint `primary_file_id`.
- **New file Stash already attached itself** — just repoints the primary file.
- **New file outside a library path** — fails loudly with that as the suggested cause,
  rather than silently doing nothing.

## What it does not handle

- **Deletion.** The plugin never deletes a file on disk. The most it removes is a database
  row for a file that is already gone.
- **Duration changes.** If Tdarr trims or pads the file, scene markers will be off by that
  much. The plugin logs a warning when duration drifts more than a second but cannot fix it.
- **Many-to-one or one-to-many transcodes.** One input, one output.
- **Ambiguous phash groups** in Reconcile — deliberately skipped rather than guessed.

## Troubleshooting

**"is not in Stash after a scan"** — the new file is outside your configured library paths,
or the path map is wrong. Run Test Connection with the `newPath` arg.

**"Cannot reach Stash at http://0.0.0.0:9999/graphql"** — shouldn't happen, the plugin
rewrites wildcard hosts to `localhost`, but if you see it, check that Stash's configured
host is dialable from itself.

**Nothing happens when Tdarr finishes** — check the Tdarr job log for the `Stash:` lines
the node writes. If the mutation failed you'll see why there. Then check
Stash → Settings → Logs, filtered to Plugin.

**Merged the wrong scenes** — `sceneMerge` is not reversible. This is what Dry run is for.
Back up your database (Settings → Tasks → Backup) before the first non-dry run.

## Verified against

The GraphQL in this plugin was written against the live Stash schema on `develop`:
`sceneMerge`, `sceneUpdate(primary_file_id)`, `deleteFiles`, `metadataScan(paths, rescan)`,
`metadataGenerate(sceneIDs, overwrite)`, `findJob`, `findDuplicateScenes(distance, duration_diff)`
and `PhashDistanceCriterionInput`. The Tdarr node uses the documented flow-plugin interface
(`args.originalLibraryFile._id`, `args.inputFileObj._id`, `args.deps.axios`, `args.jobLog`).
