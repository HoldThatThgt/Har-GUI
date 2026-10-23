import { appendFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { z } from 'zod';
import type { LoadedConfiguration } from './config.js';

export const windowSchema = z.object({
  window_id: z.number().int(), bundle_name: z.string(), process_id: z.number().int(),
  display_id: z.number().int(), z_order: z.number(), window_mode: z.number(),
  bounding_box: z.object({ x: z.number(), y: z.number(), width: z.number(), height: z.number() }),
  screen_bounds: z.object({ x: z.number().finite(), y: z.number().finite(),
    width: z.number().finite(), height: z.number().finite() }),
  ui_node_id: z.number().int(), is_system: z.boolean(), focused: z.boolean(),
}).passthrough();
/** @brief 已经结构校验的窗口管理服务记录。 */
export type WindowInfo = z.infer<typeof windowSchema>;

/** @brief 一次模型观察所对应的设备窗口事实。 */
export interface Observation {
  /** 主机生成的观察标识。 */
  id: string;
  /** 观察时已经执行的变更次数。 */
  revision: number;
  /** 截图前读到的窗口事实。 */
  windows: WindowInfo[];
}

/** @brief 维护预算、审计记录和当前样本，不包含应用特例。 */
export class RunState {
  revision = 0;
  actions = 0;
  taskIndex = 0;
  candidate?: any;
  changes: object[] = [];
  accepted: object[] = [];
  observations = new Map<string, Observation>();
  signal: AbortSignal;
  controller = new AbortController();
  private busy = false;
  /** 调用者明确提供的原始项目文件或目录，统一使用绝对路径。 */
  readonly projectSources: string[];

  /** @brief 绑定稳定内部证据目录、本轮实际交付目录及明确的项目来源。 */
  constructor(readonly config: LoadedConfiguration, readonly directory: string,
    readonly deliveryDirectory: string, projectSources: string[] = []) {
    this.projectSources = projectSources.map((source) => path.resolve(source));
    this.signal = AbortSignal.any([this.controller.signal,
      AbortSignal.timeout(config.project.run.max_duration_seconds * 1000)]);
  }

  /** @brief 读取项目中正在执行的任务。 */
  get task() { return this.config.project.tasks[this.taskIndex]; }

  /** @brief 保存本次配置来源，并由持久化层核对稳定采集契约；配额与提示词属于运行快照。 */
  get datasetIdentity() {
    return { project_sha256: this.config.digests.project, config_sha256: this.config.digests.config,
      tasks: this.config.project.tasks.map((task) => ({ name: task.name, target_count: task.target_count })),
      configuration: this.config, project_sources: [...this.projectSources] };
  }

  /** @brief 配额只来自已发布且恢复时重新校验的样本。 */
  acceptedCount(taskName: string): number {
    return this.accepted.filter((sample: any) => sample.task_name === taskName).length;
  }

  /** @brief 追加不含截图和密钥的本地执行证据。 */
  event(type: string, data: object): void {
    appendFileSync(path.join(this.directory, 'events.jsonl'),
      JSON.stringify({ time: new Date().toISOString(), type, ...data }) + '\n');
  }

  /** @brief 在工具边界统一检查动作数量、期限和互斥。 */
  enter(name: string): void {
    this.signal.throwIfAborted();
    if (this.busy) throw new Error('A device operation is already running');
    if (this.actions >= this.config.project.run.max_actions) throw new Error('Action budget exhausted');
    if (this.candidate) throw new Error('Sample already captured; finish this task before further actions');
    this.actions += 1;
    this.busy = true;
    this.event('action_started', { name, action_count: this.actions });
    console.error(`Action ${this.actions}: ${name}`);
  }

  /** @brief 释放当前工具占用。 */
  leave(): void { this.busy = false; }

  /** @brief 写入可独立检查的运行结果。 */
  save(status: string, extra: object = {}): void {
    writeFileSync(path.join(this.directory, 'run.json'), JSON.stringify({
      status, run_index: path.basename(this.deliveryDirectory).split('-')[0], artifacts_directory: this.directory,
      updated_at: new Date().toISOString(), actions: this.actions,
      accepted: this.accepted, ...extra,
    }, null, 2) + '\n');
  }
}

/** @brief 比较用于动作路由的窗口身份与状态，避免旧观察误操作。 */
export function sameWindow(left: WindowInfo, right: WindowInfo): boolean {
  return ['window_id', 'bundle_name', 'process_id', 'display_id', 'ui_node_id', 'window_mode',
    'z_order', 'focused', 'is_system']
    .every((key) => left[key] === right[key])
    && JSON.stringify(left.bounding_box) === JSON.stringify(right.bounding_box)
    && JSON.stringify(left.transform) === JSON.stringify(right.transform)
    && JSON.stringify(left.session_visibility) === JSON.stringify(right.session_visibility)
    && sameScreenBounds(left.screen_bounds, right.screen_bounds);
}

/** @brief 按字段比较精确屏幕矩形，不把来源时间和摘要变化当作窗口变化。 */
export function sameScreenBounds(left: WindowInfo['screen_bounds'], right: WindowInfo['screen_bounds']): boolean {
  return (['x', 'y', 'width', 'height'] as const).every((key) => left[key] === right[key]);
}
