"""Generate an importable Tdarr flow JSON.

The Notify Stash step is a Community "Custom JS Function" node with the code inlined,
so the flow imports and runs without installing anything on the Tdarr side. Generating
rather than hand-writing the JSON keeps the embedded JavaScript correctly escaped.
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "Stash-HEVC-Flow.json")

NOTIFY_STASH_JS = r"""module.exports = async (args) => {
  // ===================== CONFIGURE THESE =====================
  // Reachable from the WINDOWS Tdarr node, not from your browser.
  const STASH_URL = 'http://STASH-HOST:9999';
  // Stash > Settings > Security > API Key
  const STASH_API_KEY = '';
  // Optional spool directory as this Windows node sees it, e.g. 'Z:\\tdarr-queue'.
  // Leave '' to disable. If set, Stash must be able to read the same folder.
  const QUEUE_DIR = '';
  // ===========================================================

  const PLUGIN_ID = 'tdarrSync';
  const TASK_NAME = 'Sync Transcoded File';

  const pathOf = (obj) => (obj && (obj._id || obj.file)) || '';
  const originalPath = pathOf(args.originalLibraryFile);
  const newPath = pathOf(args.inputFileObj);

  const done = () => ({
    outputFileObj: args.inputFileObj,
    outputNumber: 1,
    variables: args.variables,
  });

  if (!newPath) {
    args.jobLog('Stash: no current file path, nothing to report');
    return done();
  }

  if (originalPath === newPath) {
    args.jobLog('Stash: in-place transcode of ' + newPath);
  } else {
    args.jobLog('Stash: ' + (originalPath || '(unknown)') + ' -> ' + newPath);
  }

  const headers = { 'Content-Type': 'application/json', Accept: 'application/json' };
  if (STASH_API_KEY) { headers.ApiKey = STASH_API_KEY; }

  const query = 'mutation($pluginId: ID!, $taskName: String!, $argsMap: Map, $description: String) {'
    + ' runPluginTask('
    + ' plugin_id: $pluginId'
    + ' task_name: $taskName'
    + ' args_map: $argsMap'
    + ' description: $description'
    + ' ) }';

  try {
    const res = await args.deps.axios({
      method: 'post',
      url: STASH_URL.replace(/\/+$/, '') + '/graphql',
      headers: headers,
      timeout: 30000,
      data: {
        query: query,
        variables: {
          pluginId: PLUGIN_ID,
          taskName: TASK_NAME,
          description: 'Tdarr sync: ' + newPath.split(/[\\/]/).pop(),
          argsMap: { mode: 'sync', originalPath: originalPath, newPath: newPath },
        },
      },
    });

    const errors = res.data && res.data.errors;
    if (errors && errors.length) {
      throw new Error(JSON.stringify(errors).slice(0, 400));
    }
    const jobId = res.data && res.data.data && res.data.data.runPluginTask;
    args.jobLog('Stash queued the sync as job ' + jobId);
  } catch (err) {
    const message = (err && err.message) || String(err);
    args.jobLog('Stash notification FAILED: ' + message);

    if (QUEUE_DIR) {
      try {
        const fs = args.deps.fsextra;
        await fs.ensureDir(QUEUE_DIR);
        const safe = String(newPath).replace(/[^a-zA-Z0-9]+/g, '_').slice(-80);
        const target = QUEUE_DIR.replace(/[\\/]+$/, '') + '/' + Date.now() + '_' + safe + '.json';
        await fs.writeFile(target, JSON.stringify({
          originalPath: originalPath,
          newPath: newPath,
          source: 'tdarr',
          at: new Date().toISOString(),
        }, null, 2), 'utf8');
        args.jobLog('Stash: spooled to ' + target + ' for the Drain Queue task');
      } catch (spoolErr) {
        args.jobLog('Stash: could not spool either: ' + ((spoolErr && spoolErr.message) || spoolErr));
      }
    } else {
      args.jobLog('Stash: no queue dir set, so run "Reconcile Orphans" in Stash to catch this one');
    }
  }

  // Never fail the flow over bookkeeping - the transcode itself succeeded.
  return done();
};
"""


def node(node_id, name, plugin_name, x, y, inputs=None, version="1.0.0"):
    entry = {
        "name": name,
        "sourceRepo": "Community",
        "pluginName": plugin_name,
        "version": version,
        "fpEnabled": True,
        "id": node_id,
        "position": {"x": x, "y": y},
    }
    if inputs:
        entry["inputsDB"] = inputs
    return entry


def comment(node_id, text, x, y):
    return node(node_id, text, "comment", x, y)


COL = 420.0

flow_plugins = [
    node("stInput01", "Input File", "inputFile", COL, 0.0),
    node("stMedium1", "Check File Medium", "checkFileMedium", COL, 110.0),
    node("stCodec01", "Already HEVC?", "checkVideoCodec", COL, 220.0,
         {"codec": "hevc"}),
    node("stBegin01", "Begin Command", "ffmpegCommandStart", COL, 350.0),
    node("stEncode1", "Set Video Encoder (HEVC)", "ffmpegCommandSetVideoEncoder", COL, 460.0,
         {
             "outputCodec": "hevc",
             "ffmpegPresetEnabled": "true",
             "ffmpegPreset": "slow",
             "ffmpegQualityEnabled": "true",
             "ffmpegQuality": "24",
             "hardwareEncoding": "true",
             "hardwareType": "auto",
             "hardwareDecoding": "true",
             "forceEncoding": "false",
         }),
    node("stCntnr01", "Set Container (mkv)", "ffmpegCommandSetContainer", COL, 570.0,
         {"container": "mkv", "forceConform": "false"}),
    node("stNoData1", "Remove Data Streams", "ffmpegCommandRemoveDataStreams", COL, 680.0),
    node("stExec001", "Execute", "ffmpegCommandExecute", COL, 790.0),
    node("stRatio01", "Size Sanity Check (20-95%)", "compareFileSizeRatio", COL, 900.0,
         {"greaterThan": "20", "lessThan": "95"}, version="2.0.0"),
    node("stReplc01", "Replace Original File", "replaceOriginalFile", COL, 1030.0),
    node("stNotify1", "Notify Stash  <-- EDIT THE URL + API KEY IN HERE",
         "customFunction", COL, 1140.0, {"code": NOTIFY_STASH_JS}),
    node("stFail001", "Fail Flow (output suspiciously small)", "failFlow", COL + 380.0, 1030.0),

    comment("stCmt0001",
            "SETUP: open the 'Notify Stash' node at the bottom and set STASH_URL and "
            "STASH_API_KEY. Then set the path map in Stash > Settings > Plugins > Tdarr Sync.",
            -80.0, -20.0),
    comment("stCmt0002",
            "Output 1 means the file is already HEVC, so the flow ends and Stash is never "
            "touched. This is what stops Tdarr re-encoding its own output forever.",
            -80.0, 220.0),
    comment("stCmt0003",
            "Band is 20-95%, not the stock 40-110%. 95 because an output LARGER than the "
            "input is a pure loss for a space-saving flow. 20 because HEVC off old DivX "
            "legitimately lands under 40% and the stock floor would throw those away.",
            -80.0, 880.0),
    comment("stCmt0004",
            "Notify Stash MUST be last. Before 'Replace Original File' runs, "
            "args.inputFileObj._id still points at the cache copy, not the file Stash "
            "needs to scan. Branches that do not replace the original never get here, "
            "which is correct - nothing changed, so there is nothing to sync.",
            -80.0, 1140.0),
]

_edge_seq = [0]


def edge(source, target, handle="1"):
    _edge_seq[0] += 1
    return {
        "source": source,
        "sourceHandle": handle,
        "target": target,
        "targetHandle": None,
        "id": "edge{:05d}".format(_edge_seq[0]),
    }


flow_edges = [
    edge("stInput01", "stMedium1"),
    # Check File Medium: 1 = Video. Audio (2) and Other (3) are left unconnected,
    # which simply ends the flow.
    edge("stMedium1", "stCodec01", "1"),
    # Check Video Codec: 1 = matches hevc (end here), 2 = does not match.
    edge("stCodec01", "stBegin01", "2"),
    edge("stBegin01", "stEncode1"),
    edge("stEncode1", "stCntnr01"),
    edge("stCntnr01", "stNoData1"),
    edge("stNoData1", "stExec001"),
    edge("stExec001", "stRatio01"),
    # Compare File Size Ratio v2: 1 = in range, 2 = below floor, 3 = above ceiling.
    edge("stRatio01", "stReplc01", "1"),
    edge("stRatio01", "stFail001", "2"),
    edge("stReplc01", "stNotify1"),
    # Comment chain, purely cosmetic, mirrors how the stock Tdarr example flows do it.
    edge("stCmt0001", "stCmt0002"),
    edge("stCmt0002", "stCmt0003"),
    edge("stCmt0003", "stCmt0004"),
]

flow = {
    "_id": "stashTdarrSyncHevc",
    "name": "HEVC + Stash Sync",
    "description": (
        "Re-encode to HEVC in an mkv, replace the original, then tell Stash so it can "
        "merge the new file onto the original scene and keep tags, markers, o-counter "
        "and play history. Edit the Notify Stash node before first use."
    ),
    "tags": "",
    "flowPlugins": flow_plugins,
    "flowEdges": flow_edges,
}

with open(OUT, "w", encoding="utf-8") as handle:
    json.dump(flow, handle, indent=2)
    handle.write("\n")

print("wrote {} ({} nodes, {} edges)".format(OUT, len(flow_plugins), len(flow_edges)))
