import { parseArgs } from 'node:util';
import { mkdtemp, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadConfiguration } from './config.js';

/** @brief 配置 SDK 日志的临时目录，避免额外截图和模型上下文留在项目中。 */
function isolateSdk(directory: string): void {
  process.env.MIDSCENE_RUN_DIR = directory;
  delete process.env.MIDSCENE_DEBUG_MODE;
  process.env.MIDSCENE_RECORD_MODEL_CALL = 'false';
  process.env.MIDSCENE_LANGSMITH_DEBUG = 'false';
  process.env.MIDSCENE_LANGFUSE_DEBUG = 'false';
  process.env.MIDSCENE_DANGEROUSLY_PRINT_ALL_CONFIG = 'false';
  delete process.env.MIDSCENE_DEBUG_MODEL_RESPONSE;
  delete process.env.MIDSCENE_DEBUG_MODEL_PROFILE;
  process.env.DEBUG = '';
}

/** @brief 提供静态 validate 和独立 run 命令。 */
async function main(): Promise<void> {
  const { values, positionals } = parseArgs({ allowPositionals: true, options: {
    project: { type: 'string', default: 'project.toml' }, config: { type: 'string', default: 'config.toml' },
    device: { type: 'string' }, hdc: { type: 'string' }, python: { type: 'string', default: 'python' },
    'env-file': { type: 'string' },
    capture: { type: 'string' }, task: { type: 'string' }, 'target-bundle': { type: 'string' },
  } });
  const command = positionals[0];
  if (positionals.length !== 1 || !['run', 'validate', 'tools', 'finalize'].includes(command)) {
    throw new Error('Usage: npm run agent -- <validate|run|tools|finalize> --project PATH --config PATH');
  }
  const temporary = await mkdtemp(path.join(os.tmpdir(), 'har-gui-sdk-'));
  isolateSdk(temporary);
  try {
    const { createRegistry } = await import('./tools.js');
    const registry = createRegistry(() => { throw new Error('Device unavailable during static validation'); });
    if (command === 'tools') { console.log(JSON.stringify(registry.names(), null, 2)); return; }
    const config = loadConfiguration(values.project, values.config, registry.names());
    if (command === 'validate') { console.log(JSON.stringify({ valid: true, ...config }, null, 2)); return; }
    if (values['env-file']) process.loadEnvFile(path.resolve(values['env-file']));
    isolateSdk(temporary);
    if (command === 'finalize') {
      if (!values.capture || !values.task) throw new Error('Finalize requires --capture and --task');
      const { finalize } = await import('./finalize.js');
      console.log(await finalize(config, { python: values.python }, { directory: path.resolve(values.capture),
        taskName: values.task, targetBundleName: values['target-bundle'] }));
      return;
    }
    if (!values.device || !/^[A-Za-z0-9_.:-]+$/.test(values.device)) throw new Error('A valid --device is required');
    const hdc = values.hdc ?? fileURLToPath(new URL('../.tools/openharmony/toolchains/hdc.exe', import.meta.url));
    const { run } = await import('./runner.js');
    console.log(await run(config, { python: values.python, hdc: path.resolve(hdc), serial: values.device }));
  } finally {
    const resolved = path.resolve(temporary);
    if (path.dirname(resolved) !== path.resolve(os.tmpdir()) || !path.basename(resolved).startsWith('har-gui-sdk-')) {
      throw new Error('Refusing to clean an unexpected temporary directory');
    }
    await rm(resolved, { recursive: true });
  }
}

main().catch((error) => { console.error('Agent command failed:', String(error)); process.exitCode = 1; });
