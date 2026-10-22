import { appendFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { z } from 'zod';
import type { LoadedConfiguration } from './config.js';

export const windowSchema = z.object({
  window_id: z.number().int(), bundle_name: z.string(), process_id: z.number().int(),
  display_id: z.number().int(), z_order: z.number(), window_mode: z.number(),
  bounding_box: z.object({ x: z.number(), y: z.number(), width: z.number(), height: z.number() }),
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

  /** @brief 建立运行预算和输出位置。 */
  constructor(readonly config: LoadedConfiguration, readonly directory: string) {
    this.signal = AbortSignal.any([this.controller.signal,
      AbortSignal.timeout(config.project.run.max_duration_seconds * 1000)]);
  }

  /** @brief 读取项目中正在执行的任务。 */
  get task() { return this.config.project.tasks[this.taskIndex]; }

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
      status, updated_at: new Date().toISOString(), actions: this.actions,
      accepted: this.accepted, ...extra,
    }, null, 2) + '\n');
  }
}

/** @brief 比较用于动作路由的窗口身份与状态，避免旧观察误操作。 */
export function sameWindow(left: WindowInfo, right: WindowInfo): boolean {
  return ['window_id', 'bundle_name', 'process_id', 'display_id', 'ui_node_id', 'window_mode', 'z_order', 'focused']
    .every((key) => left[key] === right[key])
    && JSON.stringify(left.bounding_box) === JSON.stringify(right.bounding_box);
}
