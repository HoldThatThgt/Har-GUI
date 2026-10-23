import { parseArgs } from 'node:util';
import { mkdtemp, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadConfiguration, type LoadedConfiguration } from './config.js';

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

/** @brief 解析仅用于 run 的本轮新增数量上限，省略时不限制。 */
function parseSampleLimit(command: string, value?: string): number | undefined {
  if (value === undefined) return undefined;
  if (command !== 'run') throw new Error('--max-new-samples is only available for run');
  const limit = Number(value);
  if (!/^\d+$/.test(value) || !Number.isSafeInteger(limit)) {
    throw new Error('--max-new-samples must be a non-negative safe integer');
  }
  return limit;
}

/** @brief 仅为执行和独立验收接受明确的历史项目来源，并统一为绝对路径。 */
function parseProjectSources(command: string, values: string[] = []): string[] {
  if (values.length && !['run', 'finalize', 'export'].includes(command)) {
    throw new Error('--project-source is only available for run, finalize and export');
  }
  return values.map((value) => {
    if (!value.trim()) throw new Error('--project-source requires a nonempty file or directory path');
    return path.resolve(value);
  });
}

/** @brief 分派本地导出、独立验收或设备采集，导出分支不读取凭据文件。 */
async function executeCommand(command: string, config: LoadedConfiguration, values: Record<string, any>,
  options: { projectSources: string[]; maxNewSamples?: number; sdkDirectory: string }): Promise<void> {
  const { projectSources, maxNewSamples } = options;
  if (command === 'export') {
    if (!values['sample-id']) throw new Error('Export requires --sample-id');
    const { exportSample } = await import('./export.js');
    console.log(await exportSample(config, values.python, { sampleId: values['sample-id'], projectSources }));
    return;
  }
  if (values['env-file']) process.loadEnvFile(path.resolve(values['env-file']));
  isolateSdk(options.sdkDirectory);
  if (command === 'finalize') {
    if (!values.capture || !values.task) throw new Error('Finalize requires --capture and --task');
    const { finalize } = await import('./finalize.js');
    console.log(await finalize(config, { python: values.python }, { directory: path.resolve(values.capture),
      taskName: values.task, targetBundleName: values['target-bundle'], projectSources }));
    return;
  }
  if (!values.device || !/^[A-Za-z0-9_.:-]+$/.test(values.device)) throw new Error('A valid --device is required');
  const hdc = values.hdc ?? fileURLToPath(new URL('../.tools/openharmony/toolchains/hdc.exe', import.meta.url));
  const { run } = await import('./runner.js');
  console.log(await run(config, { python: values.python, hdc: path.resolve(hdc), serial: values.device },
    { maxNewSamples, projectSources }));
}

/** @brief 提供静态 validate 和独立 run 命令。 */
async function main(): Promise<void> {
  const { values, positionals } = parseArgs({ allowPositionals: true, options: {
    project: { type: 'string', default: 'project.toml' }, config: { type: 'string', default: 'config.toml' },
    device: { type: 'string' }, hdc: { type: 'string' }, python: { type: 'string', default: 'python' },
    'env-file': { type: 'string' }, 'max-new-samples': { type: 'string' },
    'project-source': { type: 'string', multiple: true },
    'sample-id': { type: 'string' },
    capture: { type: 'string' }, task: { type: 'string' }, 'target-bundle': { type: 'string' },
  } });
  const command = positionals[0];
  if (positionals.length !== 1 || !['run', 'validate', 'tools', 'finalize', 'export'].includes(command)) {
    throw new Error('Usage: npm run agent -- <validate|run|tools|finalize|export> --project PATH --config PATH');
  }
  if (values['sample-id'] !== undefined && command !== 'export') {
    throw new Error('--sample-id is only available for export');
  }
  const maxNewSamples = parseSampleLimit(command, values['max-new-samples']);
  const projectSources = parseProjectSources(command, values['project-source']);
  const temporary = await mkdtemp(path.join(os.tmpdir(), 'har-gui-sdk-'));
  isolateSdk(temporary);
  try {
    const { createRegistry } = await import('./tools.js');
    const registry = createRegistry(() => { throw new Error('Device unavailable during static validation'); });
    if (command === 'tools') { console.log(JSON.stringify(registry.names(), null, 2)); return; }
    const config = loadConfiguration(values.project, values.config, registry.names());
    if (command === 'validate') { console.log(JSON.stringify({ valid: true, ...config }, null, 2)); return; }
    await executeCommand(command, config, values, { projectSources, maxNewSamples, sdkDirectory: temporary });
  } finally {
    const resolved = path.resolve(temporary);
    if (path.dirname(resolved) !== path.resolve(os.tmpdir()) || !path.basename(resolved).startsWith('har-gui-sdk-')) {
      throw new Error('Refusing to clean an unexpected temporary directory');
    }
    await rm(resolved, { recursive: true });
  }
}

main().catch((error) => { console.error('Agent command failed:', String(error)); process.exitCode = 1; });
