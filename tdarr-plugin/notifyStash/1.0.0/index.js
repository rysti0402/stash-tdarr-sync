/* eslint-disable no-console */
/**
 * Notify Stash - Tdarr local flow plugin.
 *
 * Hands the before-and-after paths of a transcode to the tdarrSync Stash plugin so
 * Stash can fold the new file back onto the original scene.
 *
 * Place this node at the VERY END of your flow, after the file has been moved to its
 * final destination. Earlier in the flow `args.inputFileObj._id` still points at the
 * working/cache copy, which is not what Stash needs to scan.
 */

const details = () => ({
  name: 'Notify Stash',
  description:
    'Tell Stash that a file was transcoded, so it can rescan the new file and merge it '
    + 'back onto the original scene (keeping tags, performers, markers, o-counter and '
    + 'play history). Place this at the end of the flow, after the final file move.',
  style: {
    borderColor: 'green',
  },
  tags: '',
  isStartPlugin: false,
  pType: '',
  requiresVersion: '2.11.01',
  sidebarPosition: -1,
  icon: 'faDatabase',
  inputs: [
    {
      label: 'Stash URL',
      name: 'stashUrl',
      type: 'string',
      defaultValue: 'http://localhost:9999',
      inputUI: { type: 'text' },
      tooltip: 'Base URL of your Stash server, no trailing slash. As seen from the Tdarr node.',
    },
    {
      label: 'Stash API Key',
      name: 'apiKey',
      type: 'string',
      defaultValue: '',
      inputUI: { type: 'text' },
      tooltip:
        'Stash > Settings > Security > API Key. Required if Stash has authentication '
        + 'enabled, which it should.',
    },
    {
      label: 'Plugin ID',
      name: 'pluginId',
      type: 'string',
      defaultValue: 'tdarrSync',
      inputUI: { type: 'text' },
      tooltip: 'The folder name of the Stash plugin. Leave as tdarrSync unless you renamed it.',
    },
    {
      label: 'Task Name',
      name: 'taskName',
      type: 'string',
      defaultValue: 'Sync Transcoded File',
      inputUI: { type: 'text' },
      tooltip: 'Must match the task name in tdarrSync.yml exactly.',
    },
    {
      label: 'Queue Directory',
      name: 'queueDir',
      type: 'string',
      defaultValue: '',
      inputUI: { type: 'text' },
      tooltip:
        'Optional spool directory, as seen from this Tdarr node. If the Stash call fails, '
        + 'the event is written here as JSON so the "Drain Queue" task can pick it up later. '
        + 'Stash needs to be able to read this directory too.',
    },
    {
      label: 'Fail Flow On Error',
      name: 'failOnError',
      type: 'boolean',
      defaultValue: 'false',
      inputUI: { type: 'switch' },
      tooltip:
        'Off by default. A bookkeeping failure should not undo a successful transcode; '
        + 'the event is spooled or logged instead.',
    },
  ],
  outputs: [
    {
      number: 1,
      tooltip: 'Continue to next plugin',
    },
  ],
});

const readInputs = (args) => {
  // loadDefaultValues lives at a path that differs between community and local plugin
  // trees, so fall back to reading the declared defaults ourselves.
  try {
    // eslint-disable-next-line global-require, import/no-dynamic-require
    const lib = require('../../../../../methods/lib')();
    return lib.loadDefaultValues(args.inputs, details);
  } catch (err) {
    const fallback = {};
    details().inputs.forEach((input) => {
      fallback[input.name] = args.inputs[input.name] !== undefined
        ? args.inputs[input.name]
        : input.defaultValue;
    });
    return fallback;
  }
};

const spool = async (args, queueDir, event) => {
  if (!queueDir) return false;
  try {
    const fs = args.deps.fsextra;
    await fs.ensureDir(queueDir);
    const safe = String(event.newPath).replace(/[^a-zA-Z0-9]+/g, '_').slice(-80);
    const target = `${queueDir.replace(/\/+$/, '')}/${Date.now()}_${safe}.json`;
    await fs.writeFile(target, JSON.stringify(event, null, 2), 'utf8');
    args.jobLog(`Spooled the Stash event to ${target}`);
    return true;
  } catch (err) {
    args.jobLog(`Could not spool the Stash event: ${err.message || String(err)}`);
    return false;
  }
};

const plugin = async (args) => {
  const inputs = readInputs(args);

  const stashUrl = String(inputs.stashUrl || '').replace(/\/+$/, '');
  const apiKey = String(inputs.apiKey || '');
  const pluginId = String(inputs.pluginId || 'tdarrSync');
  const taskName = String(inputs.taskName || 'Sync Transcoded File');
  const queueDir = String(inputs.queueDir || '');
  const failOnError = String(inputs.failOnError) === 'true' || inputs.failOnError === true;

  const originalPath = (args.originalLibraryFile && args.originalLibraryFile._id) || '';
  const newPath = (args.inputFileObj && args.inputFileObj._id) || '';

  const event = { originalPath, newPath, source: 'tdarr', at: new Date().toISOString() };

  if (!newPath) {
    args.jobLog('No current file path available; nothing to tell Stash about.');
    return { outputFileObj: args.inputFileObj, outputNumber: 1, variables: args.variables };
  }

  if (originalPath === newPath) {
    args.jobLog(`Stash: in-place transcode of ${newPath}`);
  } else {
    args.jobLog(`Stash: ${originalPath || '(unknown)'} -> ${newPath}`);
  }

  const headers = { 'Content-Type': 'application/json', Accept: 'application/json' };
  if (apiKey) headers.ApiKey = apiKey;

  const body = {
    query: `
      mutation($pluginId: ID!, $taskName: String!, $argsMap: Map, $description: String) {
        runPluginTask(
          plugin_id: $pluginId
          task_name: $taskName
          args_map: $argsMap
          description: $description
        )
      }
    `,
    variables: {
      pluginId,
      taskName,
      description: `Tdarr sync: ${newPath.split(/[\\/]/).pop()}`,
      argsMap: { mode: 'sync', originalPath, newPath },
    },
  };

  try {
    const response = await args.deps.axios({
      method: 'post',
      url: `${stashUrl}/graphql`,
      headers,
      data: body,
      timeout: 30000,
    });

    const errors = response.data && response.data.errors;
    if (errors && errors.length) {
      throw new Error(JSON.stringify(errors).slice(0, 400));
    }

    const jobId = response.data && response.data.data && response.data.data.runPluginTask;
    args.jobLog(`Stash queued the sync as job ${jobId}`);
  } catch (err) {
    const message = (err && err.message) || String(err);
    args.jobLog(`Stash notification failed: ${message}`);
    const spooled = await spool(args, queueDir, event);
    if (failOnError && !spooled) {
      throw new Error(`Stash notification failed and could not be spooled: ${message}`);
    }
    if (!spooled) {
      args.jobLog(
        'No queue directory configured, so this transcode will need the '
        + '"Reconcile Orphans" task in Stash to be picked up.',
      );
    }
  }

  return {
    outputFileObj: args.inputFileObj,
    outputNumber: 1,
    variables: args.variables,
  };
};

module.exports = { details, plugin };
module.exports.details = details;
module.exports.plugin = plugin;
