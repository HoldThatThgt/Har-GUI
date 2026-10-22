import { createHash, randomUUID } from 'node:crypto';
import { mkdtemp, readFile, unlink, rmdir } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { HarmonyAgent, HarmonyDevice, type HarmonyAgentOpt } from '@midscene/harmony';
import type { AgentProgressEvent, AiActProgressData, DeviceAction, ServiceAction } from '@midscene/core';
import { bridge, type DeviceOptions } from './bridge.js';
import { windowSchema, type Observation, type RunState } from './state.js';

/** @brief 独立清理设备文件、主机文件和目录，收集错误而不跳过后续清理。 */
async function cleanupScreenshot(
  hdc: Awaited<ReturnType<HarmonyDevice['getHdc']>>, remote: string, local: string, directory: string,
): Promise<unknown[]> {
  const errors: unknown[] = [];
  const operations = [
    () => hdc.shell(`rm -f ${remote}`),
    () => unlink(local).catch((error: NodeJS.ErrnoException) => {
      if (error.code !== 'ENOENT') throw error;
    }),
    () => rmdir(directory),
  ];
  for (const operation of operations) {
    try { await operation(); }
    catch (error) { console.error('Observation cleanup failed:', String(error)); errors.push(error); }
  }
  return errors;
}

/** @brief 复用官方 Harmony 输入，并控制观察截图的生命周期。 */
export class CollectionDevice extends HarmonyDevice {
  actions: DeviceAction[] = [];
  latestObservation?: Observation;
  /** 当前规划轮真正发送给模型的截图对应观察，后续执行截图不能覆盖它。 */
  planningObservation?: Observation;
  frozenScreenshot?: string;

  /** @brief 绑定明确的设备和运行状态。 */
  constructor(readonly connection: DeviceOptions, readonly state: RunState) {
    super(connection.serial, { hdcPath: connection.hdc });
  }

  /** @brief 读取 SDK 原生动作供注册器选择，禁止直接公开全部动作。 */
  nativeActions(): DeviceAction[] { return super.actionSpace(); }

  /** @brief 仅公开注册器批准的动作。 */
  override actionSpace(): DeviceAction[] { return this.actions; }

  /** @brief SDK 内置等待同样消耗预算，未知隐式动作不能绕过注册器。 */
  async beforeInvokeAction(name: string): Promise<void> {
    if (this.actions.some((action) => action.name === name)) return;
    try {
      if (name !== 'Sleep') throw new Error(`Unregistered SDK action: ${name}`);
      this.state.enter(name);
    } catch (error) {
      console.error('SDK action rejected:', String(error));
      this.state.controller.abort(error);
      throw error;
    }
  }

  /** @brief 记录 SDK 内置等待成功并释放设备占用。 */
  async afterInvokeAction(name: string): Promise<void> {
    if (name === 'Sleep') {
      this.state.event('action_finished', { name });
      this.state.leave();
    }
  }

  /** @brief 读取官方窗口事实并校验结果。 */
  async windows() {
    const result = await bridge(this.connection, { operation: 'read_windows' }, this.state.signal);
    return windowSchema.array().parse(result.windows);
  }

  /** @brief 临时观察不进入数据集；无论成功失败都清理设备和主机临时文件。 */
  override async screenshotBase64(): Promise<string> {
    this.state.signal.throwIfAborted();
    if (this.frozenScreenshot) return this.frozenScreenshot;
    const observation = { id: randomUUID(), revision: this.state.revision, windows: await this.windows() };
    const hdc = await this.getHdc();
    this.state.signal.throwIfAborted();
    const directory = await mkdtemp(path.join(os.tmpdir(), 'har-gui-'));
    const remote = `/data/local/tmp/har-gui-${observation.id}.png`;
    const local = path.join(directory, 'observe.png');
    let result = '';
    let failure: Error | undefined;
    try {
      const output = await hdc.shell(`uitest screenCap -d 0 -p ${remote}`);
      this.state.signal.throwIfAborted();
      if (!output.includes(`ScreenCap saved to ${remote}`)) throw new Error('Observation screenshot failed');
      await hdc.fileRecv(remote, local);
      this.state.signal.throwIfAborted();
      const bytes = await readFile(local);
      this.state.signal.throwIfAborted();
      this.latestObservation = observation;
      this.state.event('observation', { ...observation, sha256: createHash('sha256').update(bytes).digest('hex') });
      result = `data:image/png;base64,${bytes.toString('base64')}`;
    } catch (error) {
      console.error('Observation failed:', String(error));
      failure = error instanceof Error ? error : new Error(String(error));
    }
    const cleanupErrors = await cleanupScreenshot(hdc, remote, local, directory);
    if (cleanupErrors.length) {
      throw new AggregateError(failure ? [failure, ...cleanupErrors] : cleanupErrors, 'Observation cleanup failed');
    }
    if (failure) throw failure;
    return result;
  }
}

/** @brief 绑定模型实际使用的观察与执行上下文，不按执行时最新图像猜测。 */
export class CollectionAgent extends HarmonyAgent {
  /** @brief 在 SDK 的真实规划开始事件上固定观察，不使用执行时重拍截图代替。 */
  constructor(device: CollectionDevice, options?: HarmonyAgentOpt) {
    super(device, options);
    this.addProgressListener((event) => this.bindPlanningObservation(event));
  }

  /** @brief 只读取规划事件中的截图标识；缺失绑定立即中止，不能被 SDK 吞错后继续。 */
  private bindPlanningObservation(event: AgentProgressEvent): void {
    if (event.scope !== 'aiAct') return;
    const device = this.page as CollectionDevice;
    if (event.phase === 'start') device.planningObservation = undefined;
    if (event.phase !== 'plan_thinking') return;
    device.planningObservation = undefined;
    try {
      const data = event.data as AiActProgressData;
      const id = data.screenshot?.id;
      const observation = id ? device.state.observations.get(id) : undefined;
      if (!observation) throw new Error('Planning screenshot has no bound observation');
      device.planningObservation = observation;
      device.state.event('planning_observation', { screenshot_id: id, observation_id: observation.id,
        revision: observation.revision, plan_index: data.planIndex });
    } catch (error) {
      console.error('Planning observation binding failed:', String(error));
      device.state.controller.abort(error);
    }
  }

  /** @brief 记录每张规划截图对应的窗口事实。 */
  override async getUIContext(action?: ServiceAction) {
    const context = await super.getUIContext(action);
    const device = this.page as CollectionDevice;
    if (device.latestObservation && !device.frozenScreenshot) {
      device.state.observations.set(context.screenshot.id, device.latestObservation);
    }
    return context;
  }
}
