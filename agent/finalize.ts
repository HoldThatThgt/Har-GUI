import { randomUUID } from 'node:crypto';
import { existsSync } from 'node:fs';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { Agent } from '@midscene/core';
import type { AbstractInterface } from '@midscene/core/device';
import { verifySavedScreenshot, verifyWindowChanges } from './acceptance.js';
import { localBridge, type DeviceOptions } from './bridge.js';
import { resolveModelConfig, type LoadedConfiguration } from './config.js';
import { RunState } from './state.js';
import { UsageRecorder, estimateUsageCost } from './usage.js';

/** @brief 已保存采集的独立验收参数，不包含设备动作或历史观察。 */
export interface FinalizeOptions {
  /** 原始采集证据目录。 */
  directory: string;
  /** 当前 project.toml 中的任务名称。 */
  taskName: string;
  /** 直接采集没有 candidate.json 时，由调用者明确指定的目标应用。 */
  targetBundleName?: string;
}

/** @brief Midscene 官方通用设备接口的静态图片实现，不连接真实设备且不提供任何动作。 */
export class SavedScreenshotDevice implements AbstractInterface {
  interfaceType = 'saved-screenshot';
  frozenScreenshot?: string;
  private readonly canvas: { width: number; height: number };

  /** @brief 固定项目画布，不向 HDC 查询当前设备尺寸。 */
  constructor(canvas: { width: number; height: number }) { this.canvas = { ...canvas }; }

  /** @brief 只返回正在验收的文件图片；未绑定图片时拒绝读取。 */
  async screenshotBase64(): Promise<string> {
    if (!this.frozenScreenshot) {
      console.error('No saved screenshot is bound for acceptance');
      throw new Error('No saved screenshot is bound for acceptance');
    }
    return this.frozenScreenshot;
  }

  /** @brief 返回已经由交付准备步骤验证的项目画布尺寸。 */
  async size() { return { ...this.canvas }; }

  /** @brief 静态验收没有输入、导航或窗口动作。 */
  actionSpace() { return []; }
}

/** @brief 核对任务与目标应用，保留原始窗口变更事实而不装载旧观察。 */
function validateCandidate(state: RunState, metadata: any, candidate: any, options: FinalizeOptions): void {
  if (metadata.status !== 'complete' || candidate.capture_id !== metadata.capture_id) {
    throw new Error('Finalization requires a complete capture with matching candidate identity');
  }
  if (candidate.task_name !== state.task.name || path.resolve(candidate.path) !== options.directory) {
    throw new Error('Candidate task or capture directory differs from the finalization request');
  }
  const bundle = candidate.target_bundle_name;
  if (typeof bundle !== 'string' || !bundle.trim()
    || (options.targetBundleName && options.targetBundleName !== bundle)) {
    throw new Error('Finalization target bundle is missing or differs from the captured candidate');
  }
  if (state.task.bundle_names && !state.task.bundle_names.includes(bundle)) {
    throw new Error('Finalization target bundle is outside task scope');
  }
  if (!metadata.windows.some((window: any) => window.is_system === false && window.bundle_name === bundle
    && metadata.coverage.covered_window_ids.includes(window.window_id))) {
    throw new Error('Finalization target bundle is not a captured visible application');
  }
  if (typeof candidate.page_name !== 'string' || !candidate.page_name.trim()
    || !Array.isArray(candidate.window_changes)) throw new Error('Candidate page or window history is invalid');
}

/** @brief 读取采集身份；直接采集必须明确提供应用，不猜测应用或导航状态。 */
async function loadCandidate(state: RunState, options: FinalizeOptions): Promise<any> {
  const metadata = JSON.parse(await readFile(path.join(options.directory, 'metadata.json'), 'utf8'));
  const candidateFile = path.join(options.directory, 'candidate.json');
  const candidate = existsSync(candidateFile) ? JSON.parse(await readFile(candidateFile, 'utf8')) : {
    capture_id: metadata.capture_id, path: options.directory, task_name: state.task.name,
    target_bundle_name: options.targetBundleName, page_name: metadata.capture_id,
    window_changes: [], origin: 'direct_capture',
  };
  validateCandidate(state, metadata, candidate, options);
  state.candidate = candidate;
  return metadata;
}

/** @brief 汇总本次独立验收的真实请求，失败及未报告用量保持可见。 */
function modelUsage(state: RunState, usage: UsageRecorder) {
  const records = usage.records;
  return { ...usage.summary(), call_ids: records.map((record) => record.call_id),
    configured_cost: state.config.profile.pricing ? estimateUsageCost(records, state.config.profile.pricing) : null };
}

/** @brief 使用同一配置身份恢复已发布样本，禁止重复计数或跨任务重用收据。 */
async function restorePublication(state: RunState, connection: Pick<DeviceOptions, 'python'>): Promise<any> {
  const status = await localBridge(connection.python, { operation: 'dataset_status', root: state.config.datasetPath,
    identity: state.datasetIdentity }, state.signal);
  state.accepted = status.samples;
  const existing = status.samples.find((sample: any) => sample.sample_id === state.candidate.capture_id);
  if (existing && (existing.task_name !== state.task.name
    || existing.target_bundle_name !== state.candidate.target_bundle_name
    || path.resolve(existing.evidence_directory) !== state.candidate.path)) {
    throw new Error('Published sample belongs to a different finalization request');
  }
  state.event('dataset_resumed', { counts: status.counts, remaining: status.remaining, failures: status.failures });
  return existing;
}

/** @brief 独立打码及五件套准备；重试由 Python 核对规则凭证和全部文件摘要。 */
async function prepare(state: RunState, connection: Pick<DeviceOptions, 'python'>) {
  const candidate = state.candidate;
  return localBridge(connection.python, { operation: 'prepare_delivery', directory: candidate.path,
    options: { app: state.task.app, page_id: candidate.page_name, scene: state.task.scene,
      state_id: candidate.capture_id, canvas: state.config.project.dataset.canvas,
      stability_regions: state.config.project.dataset.stability_regions }, privacy: state.task.privacy,
  }, state.signal);
}

/** @brief 仅将最终保存的图片交给视觉模型，并保证销毁 SDK 执行资源。 */
async function assertSavedImage(state: RunState, usage: UsageRecorder, manifest: any) {
  const device = new SavedScreenshotDevice(state.config.project.dataset.canvas);
  const agent = new Agent(device, { modelConfig: resolveModelConfig(state.config.profile),
    createOpenAIClient: usage.createOpenAIClient, generateReport: false, persistExecutionDump: false,
    cache: false, autoPrintReportMsg: false });
  try { return await verifySavedScreenshot(state, device, agent, manifest); }
  finally { await agent.destroy(); }
}

/** @brief 将本次验收摘要持久化，然后通过同一登记入口原子发布并增加配额。 */
async function publish(state: RunState, connection: Pick<DeviceOptions, 'python'>, facts: any) {
  const candidate = state.candidate;
  const acceptance = { task_name: state.task.name, target_bundle_name: candidate.target_bundle_name,
    pass: true, export: facts.manifest, accepted_screenshot_sha256: facts.accepted.accepted_screenshot_sha256,
    model_usage: facts.usage };
  const sample = { ...candidate, finalization_directory: state.directory, acceptance: facts.accepted.verdict,
    window_occlusions: facts.occlusions, accepted_screenshot_sha256: acceptance.accepted_screenshot_sha256,
    model_usage: facts.usage };
  const sampleFile = path.join(candidate.path, 'sample.json');
  await writeFile(sampleFile, JSON.stringify(sample, null, 2) + '\n');
  const published = await localBridge(connection.python, { operation: 'publish_sample', root: state.config.datasetPath,
    identity: state.datasetIdentity, directory: facts.manifest.path, evidence_directory: candidate.path, acceptance,
  }, state.signal);
  state.accepted.push(published);
  state.event('sample_published', { sample_id: published.sample_id, path: published.path });
  await writeFile(sampleFile, JSON.stringify({ ...sample, publication: published }, null, 2) + '\n');
  return published;
}

/** @brief 按采集证据完成后处理，不执行任何设备观察或操作。 */
async function complete(
  state: RunState, connection: Pick<DeviceOptions, 'python'>, options: FinalizeOptions, usage: UsageRecorder,
) {
  const metadata = await loadCandidate(state, options);
  const occlusions = verifyWindowChanges(metadata, state.candidate.window_changes);
  const existing = await restorePublication(state, connection);
  const prepared = await prepare(state, connection);
  if (existing) {
    state.event('publication_verified', { sample_id: existing.sample_id, path: existing.path });
    return existing;
  }
  if (state.acceptedCount(state.task.name) >= state.task.target_count) {
    throw new Error('Task quota is already complete');
  }
  const accepted = await assertSavedImage(state, usage, prepared.manifest);
  state.signal.throwIfAborted();
  return publish(state, connection, { manifest: prepared.manifest, accepted, occlusions,
    usage: modelUsage(state, usage) });
}

/** @brief 为已保存的采集创建独立最终验收运行，成功和失败都保存实际模型用量。
 * @param config 当前任务及模型配置。 @param connection 仅供本地 Python 操作使用的进程参数。
 * @param options 证据目录、任务及明确目标应用。 @return 独立最终验收运行目录。
 */
export async function finalize(
  config: LoadedConfiguration, connection: Pick<DeviceOptions, 'python'>, options: FinalizeOptions,
): Promise<string> {
  const name = new Date().toISOString().replace(/[:.]/g, '-') + '-finalize-' + randomUUID();
  const directory = path.join(config.outputPath, name);
  await mkdir(directory, { recursive: true });
  const state = new RunState(config, directory);
  const usage = new UsageRecorder(path.join(directory, 'model-usage.jsonl'));
  const interrupt = () => state.controller.abort(new Error('Finalization interrupted'));
  process.once('SIGINT', interrupt);
  process.once('SIGTERM', interrupt);
  try {
    state.taskIndex = config.project.tasks.findIndex((task) => task.name === options.taskName);
    if (state.taskIndex < 0) throw new Error(`Unknown finalization task: ${options.taskName}`);
    await writeFile(path.join(directory, 'configuration.json'), JSON.stringify(config, null, 2) + '\n');
    state.save('running', { operation: 'finalize', capture_directory: path.resolve(options.directory) });
    const sample = await complete(state, connection, { ...options, directory: path.resolve(options.directory) }, usage);
    state.save('complete', { operation: 'finalize', sample, model_usage: modelUsage(state, usage) });
    return directory;
  } catch (error) {
    console.error('Saved capture finalization failed:', String(error));
    state.save('failed', { operation: 'finalize', error: String(error), candidate: state.candidate,
      model_usage: modelUsage(state, usage) });
    throw error;
  } finally {
    process.off('SIGINT', interrupt);
    process.off('SIGTERM', interrupt);
    await writeFile(path.join(directory, 'model-usage-summary.json'), JSON.stringify(modelUsage(state, usage), null, 2)
      + '\n');
  }
}
