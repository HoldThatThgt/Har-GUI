import { createHash, randomUUID } from 'node:crypto';
import { appendFileSync, mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import type { HarmonyAgent } from '@midscene/harmony';
import { getTaskServiceDump, type AgentProgressEvent, type AiActProgressData, type DeviceAction, type ExecutionDump,
  type ExecutionTask, type ExecutorContext, type ScreenshotItem, type UIContext } from '@midscene/core';
import { getModelRuntime, type ModelRuntime } from '@midscene/core/ai-model';
import { ReportImageStore, parseBase64ImageDataUrl } from '@midscene/core/dump';
import { imageInfoOfBase64 } from '@midscene/shared/img';
import type { CreateOpenAIClientFn } from '@midscene/shared/env/types';
import type { Observation, RunState } from './state.js';
import { projectLocate, projectPlan, safeDiagnosticParameters } from './planning-data.js';

/** @brief 来自 SDK 正在执行任务的直接关联，不通过时间或相同坐标推测。 */
interface TaskBinding {
  /** SDK 执行标识。 */
  execution_id: string;
  /** SDK 原子任务标识。 */
  task_id: string;
  /** SDK 任务类别。 */
  sdk_task_type: string;
  /** SDK 任务名称。 */
  sub_type: string | null;
}

/** @brief 内部图片文件及其真实字节信息。 */
interface EvidenceImage {
  /** 相对运行证据目录的图片路径。 */
  file: string;
  /** 原始图片字节摘要。 */
  sha256: string;
  /** 图片原始字节数。 */
  bytes: number;
  /** 文件真实宽度。 */
  width: number;
  /** 文件真实高度。 */
  height: number;
  /** 图片类型，不改变原始编码。 */
  mime_type: string;
}

/** @brief 同步摘取 SDK 图片，不保留可能随后变化的执行对象。 */
function screenshotSnapshot(screenshot: ScreenshotItem) {
  return { screenshot_id: screenshot.id, captured_at: screenshot.capturedAt,
    data: screenshot.base64, item: screenshot };
}

/** @brief 从真实请求中只读取图片内容，绝不复制消息正文、地址或请求选项。 */
function requestImages(body: any): Array<{ message_index: number; content_index: number; data: string }> {
  const images: Array<{ message_index: number; content_index: number; data: string }> = [];
  for (const [messageIndex, message] of (body.messages ?? []).entries()) {
    if (!Array.isArray(message.content)) continue;
    for (const [contentIndex, item] of message.content.entries()) {
      if (item.type === 'image_url' && typeof item.image_url?.url === 'string') {
        if (!item.image_url.url.startsWith('data:image/')) {
          console.error('Planning evidence requires inline model images');
          throw new Error('Planning evidence requires inline model images');
        }
        images.push({ message_index: messageIndex, content_index: contentIndex, data: item.image_url.url });
      }
    }
  }
  return images;
}

/** @brief 使用 SDK 公开快照与图片存储，保存导航诊断，不参与页面验收或公开导出。 */
export class PlanningEvidence {
  private readonly directory: string;
  private readonly file: string;
  private readonly imageStore: ReportImageStore;
  private readonly secrets: string[];
  private queue = Promise.resolve();
  private failure?: Error;
  private sequence = 0;
  private active = false;
  private invocation?: string;
  private planIndex?: number;
  private currentTask?: TaskBinding;
  private planningRuntime?: ModelRuntime;
  private locateRuntime?: ModelRuntime;
  private actionSpace: DeviceAction[] = [];
  private readonly executions = new Set<string>();
  private readonly snapshots = new Set<string>();
  private readonly contexts = new Map<string, { observation_id: string; image: EvidenceImage;
    shot_size: { width: number; height: number }; shrunk_shot_to_logical_ratio: number }>();
  private readonly dispatches = new Set<string>();

  /** @brief 独占创建本轮内部证据目录，凭据只用于过滤字符串且不会写出。 */
  constructor(private readonly state: RunState) {
    this.directory = path.join(state.directory, 'planning-evidence');
    this.file = path.join(this.directory, 'trace.jsonl');
    this.secrets = [process.env[state.config.profile.api_key_env] ?? ''].filter(Boolean);
    try {
      mkdirSync(this.directory, { recursive: true });
      writeFileSync(this.file, '', { flag: 'wx' });
    } catch (error) { this.reject(error); throw this.failure; }
    this.imageStore = new ReportImageStore({ mode: 'directory', reportPath: this.file,
      screenshotsDir: path.join(this.directory, 'screenshots') });
  }

  /** @brief 订阅现有 SDK 的公开诊断接口；保持完整报告与原始执行转储关闭。 */
  attach(agent: HarmonyAgent, actionSpace: DeviceAction[]): void {
    this.planningRuntime = getModelRuntime(agent.modelConfigManager.getModelConfig('planning'));
    this.locateRuntime = getModelRuntime(agent.modelConfigManager.getModelConfig('locate'));
    this.actionSpace = actionSpace;
    agent.addProgressListener((event) => this.progress(event));
    agent.addDumpUpdateListener((_serialized, dump) => {
      try { if (dump) this.snapshot(dump); }
      catch (error) { this.reject(error); }
    });
  }

  /** @brief 持久化失败须中止运行，不让 SDK 吞掉监听器失败后继续采集。 */
  private reject(error: unknown): void {
    if (this.failure) return;
    console.error('Planning evidence persistence failed:', error instanceof Error ? error.name : 'UnknownError');
    this.failure ??= new Error('Planning evidence persistence failed');
    this.state.controller.abort(this.failure);
  }

  /** @brief 串行保存已摘取的证据；后台失败立即中止，收尾仍会等待全部写入。 */
  private enqueue(operation: () => Promise<void>): Promise<void> {
    this.queue = this.queue.then(async () => { if (!this.failure) await operation(); })
      .catch((error) => this.reject(error));
    return this.queue;
  }

  /** @brief 等待所有证据写入；取消运行不取消本地证据收尾。 */
  async flush(): Promise<void> {
    await this.queue;
    if (this.failure) throw this.failure;
  }

  /** @brief 给内部记录分配顺序和当时的关联信息，避免异步写入时使用后来的规划状态。 */
  private record(type: string, data: object, binding = this.currentTask) {
    return { sequence: ++this.sequence, time: new Date().toISOString(), type,
      invocation_id: this.invocation ?? null, plan_index: this.planIndex ?? null,
      task_name: this.state.task.name, ...binding, ...data };
  }

  /** @brief 只写经过白名单摘取的数据，再过滤已知凭据字面值。 */
  private append(record: object): void {
    try {
      const text = JSON.stringify(record, (_key, value) => typeof value === 'string'
        ? this.secrets.reduce((result, secret) => result.replaceAll(secret, '[redacted]'), value) : value);
      appendFileSync(this.file, text + '\n');
    } catch (error) { this.reject(error); throw this.failure; }
  }

  /** @brief 复用 SDK 图片存储按内容保存原始编码，文件只位于内部运行目录。 */
  private async image(data: string, screenshot?: ScreenshotItem): Promise<EvidenceImage> {
    const parsed = parseBase64ImageDataUrl(data);
    const bytes = Buffer.from(parsed.rawBase64, 'base64');
    const dimensions = await imageInfoOfBase64(data);
    const reference = screenshot ? await this.imageStore.persist(screenshot)
      : await this.imageStore.persistReferenceImage(data);
    if (!reference.path) throw new Error('SDK image storage did not return a file reference');
    return { file: path.posix.join('planning-evidence', reference.path),
      sha256: createHash('sha256').update(bytes).digest('hex'), bytes: bytes.length,
      width: dimensions.width, height: dimensions.height, mime_type: parsed.mimeType };
  }

  /** @brief 保留本轮物理观察原图，不改变临时设备文件清理或公开样本截图。 */
  async observation(observation: Observation, data: string): Promise<void> {
    if (!this.active) return;
    const record = this.record('observation', { observation_id: observation.id, revision: observation.revision,
      windows: observation.windows, host_received_at: new Date().toISOString() });
    await this.enqueue(async () => this.append({ ...record, image: await this.image(data) }));
    await this.flush();
  }

  /** @brief 保存 SDK 上下文图与比例，独立于之后的模型图片预处理。 */
  async context(context: UIContext, observation: Observation): Promise<void> {
    if (!this.active) return;
    const screenshot = screenshotSnapshot(context.screenshot);
    const fields = { observation_id: observation.id, screenshot_id: screenshot.screenshot_id,
      captured_at: screenshot.captured_at, shot_size: { ...context.shotSize },
      shrunk_shot_to_logical_ratio: context.shrunkShotToLogicalRatio };
    const record = this.record('context', fields);
    await this.enqueue(async () => {
      const image = await this.image(screenshot.data, screenshot.item);
      this.contexts.set(screenshot.screenshot_id, { ...fields, image });
      this.append({ ...record, image });
    });
    await this.flush();
  }

  /** @brief 规划事件只绑定其明确提供的截图，不把执行时截图替换为规划图。 */
  private progress(event: AgentProgressEvent): void {
    if (event.scope !== 'aiAct') return;
    try {
      const data = event.data as AiActProgressData;
      if (event.phase === 'start') {
        this.active = true; this.invocation = randomUUID(); this.planIndex = undefined; this.currentTask = undefined;
      }
      if (event.phase === 'plan_thinking') this.planIndex = data.planIndex;
      const screenshotId = data.screenshot?.id;
      const context = screenshotId ? this.contexts.get(screenshotId) : undefined;
      const record = this.record('planning', { phase: event.phase, sdk_sequence: event.sequence,
        screenshot_id: screenshotId ?? null, context: context ?? null });
      this.enqueue(async () => this.append(record));
      if (event.phase === 'complete' || event.phase === 'failed') this.active = false;
    } catch (error) { this.reject(error); }
  }

  /** @brief 先同步复制首次任务状态，SDK 后续修改计划参数不会改变已经保存的证据。 */
  private snapshot(dump: ExecutionDump): void {
    if (!dump.id) throw new Error('SDK execution dump has no execution identity');
    if (!this.active && !this.executions.has(dump.id)) return;
    this.executions.add(dump.id);
    const running = dump.tasks.filter((task) => task.status === 'running');
    if (running.length > 1) throw new Error('SDK execution has multiple running tasks');
    const current = running.length ? this.binding(dump.id, running[0]) : undefined;
    // SDK 先采图再宣布规划序号，新计划不能沿用上一轮序号。
    if (current?.sdk_task_type === 'Planning' && current.sub_type === 'Plan'
      && current.task_id !== this.currentTask?.task_id) this.planIndex = undefined;
    this.currentTask = current;
    for (const task of dump.tasks) {
      const key = `${dump.id}:${task.taskId}:${task.status}`;
      if (this.snapshots.has(key)) continue;
      this.snapshots.add(key);
      this.taskSnapshot(task, this.binding(dump.id, task));
    }
  }

  /** @brief 仅复制 SDK 原始身份字段。 */
  private binding(execution: string, task: ExecutionTask): TaskBinding {
    return { execution_id: execution, task_id: task.taskId, sdk_task_type: task.type,
      sub_type: task.subType ?? null };
  }

  /** @brief 分别记录计划、定位和动作，只有动作任务的后继图片表示动作后的观察。 */
  private taskSnapshot(task: ExecutionTask, binding: TaskBinding): void {
    const terminal = ['finished', 'failed', 'cancelled'].includes(task.status);
    const plan = terminal && task.type === 'Planning' && task.subType === 'Plan'
      ? projectPlan(task, this.planningRuntime!, this.actionSpace, this.secrets) : null;
    let locate = null;
    if (terminal && task.subType === 'Locate') {
      try {
        locate = projectLocate(task, this.locateRuntime!, this.secrets,
          this.planningRuntime?.adapter.planning.kind === 'standard');
      } catch (error) {
        const stage = error instanceof Error ? error.cause : undefined;
        if (typeof stage === 'string' && ['element_geometry', 'raw_protocol', 'raw_coordinates'].includes(stage)) {
          this.append(this.record('diagnostic_failure', { projection: 'locate', projection_stage: stage,
            status: task.status, screenshot_id: task.uiContext?.screenshot.id ?? null,
            raw_response_available: typeof getTaskServiceDump(task)?.taskInfo.rawResponse === 'string',
            sdk_element: safeDiagnosticParameters('Tap', { locate: (task.output as any)?.element },
              this.secrets, false).locate ?? null }, binding));
        }
        throw error;
      }
    }
    const record = this.record('sdk_task', { status: task.status,
      screenshot_id: task.uiContext?.screenshot.id ?? null, plan, locate }, binding);
    if (task.type === 'Planning' && task.subType === 'Plan' && task.status === 'pending') record.plan_index = null;
    this.enqueue(async () => this.append(record));
    if (terminal && task.type === 'Action Space') this.afterAction(task, binding);
  }

  /** @brief 保留 SDK 对应动作的后继图片；缺图明确未知，不推断界面变化或成功。 */
  private afterAction(task: ExecutionTask, binding: TaskBinding): void {
    const screenshots = (task.recorder ?? []).filter((item) => item.timing === 'after-calling' && item.screenshot)
      .map((item) => screenshotSnapshot(item.screenshot!));
    const record = this.record('action_observation', { action_status: task.status,
      native_invocation_recorded: this.dispatches.has(task.taskId),
      observation_status: screenshots.length ? 'recorded' : 'unavailable', ui_effect: 'not_evaluated' }, binding);
    this.enqueue(async () => {
      const images = [];
      for (const screenshot of screenshots) images.push({ screenshot_id: screenshot.screenshot_id,
        captured_at: screenshot.captured_at, image: await this.image(screenshot.data, screenshot.item) });
      this.append({ ...record, after: images });
    });
  }

  /** @brief 注册器记录经过 SDK 参数转换后的工具边界，查询或变更结果不被当作界面效果判定。 */
  toolEvent(status: 'invoked' | 'returned' | 'failed', name: string,
    parameters: unknown, context?: ExecutorContext): void {
    if (!this.active) return;
    const record = this.record('tool_call', { status, name, sdk_task_id: context?.task.taskId ?? null,
      revision: this.state.revision, parameters: safeDiagnosticParameters(name, parameters,
        this.secrets, this.planningRuntime?.adapter.planning.kind === 'standard'),
      coordinate_system: 'device_logical_pixels', ui_effect: 'not_evaluated' });
    this.enqueue(async () => this.append(record));
  }

  /** @brief 在原生动作真正被调用前后保存参数；返回仅表示原生调用返回，不是操作系统或界面效果回执。 */
  async nativeCall<T>(name: string, parameters: unknown, context: ExecutorContext | undefined,
    invoke: () => Promise<T>): Promise<T> {
    if (!this.active) return invoke();
    await this.flush();
    if (!context || context.task.taskId !== this.currentTask?.task_id) {
      console.error('Native dispatch is missing its SDK task binding');
      throw new Error('Native dispatch is missing its SDK task binding');
    }
    const id = context.task.taskId;
    const fields = { name, parameters: safeDiagnosticParameters(name, parameters,
      this.secrets, this.planningRuntime?.adapter.planning.kind === 'standard'),
      coordinate_system: 'device_logical_pixels', os_delivery_confirmed: false, ui_effect: 'not_evaluated' };
    this.dispatches.add(id);
    this.append(this.record('action_dispatch', { ...fields, status: 'invoked' }));
    try {
      const result = await invoke();
      this.append(this.record('action_dispatch', { ...fields, status: 'native_returned' }));
      return result;
    } catch (error) {
      this.append(this.record('action_dispatch', { ...fields, status: 'failed_or_interrupted',
        error_kind: error instanceof Error ? error.name : 'UnknownError' }));
      throw error;
    }
  }

  /** @brief 复用公开客户端工厂，只观察实际发出的模型图片，不读取凭据或认证头。 */
  clientFactory(next: CreateOpenAIClientFn): CreateOpenAIClientFn {
    return async (client, options) => {
      const wrapped = await next(client, options);
      const completion = wrapped.chat.completions;
      const create = completion.create.bind(completion);
      completion.create = (body: any, requestOptions?: unknown) => this.active
        ? this.modelRequest(body, () => create(body, requestOptions)) : create(body, requestOptions);
      return wrapped;
    };
  }

  /** @brief 请求图片按实际请求的排列存储，任务关联来自公开 running 快照而非时间匹配。 */
  private async modelRequest<T>(body: any, invoke: () => Promise<T>): Promise<T> {
    if (!this.currentTask) {
      this.reject(new Error('Model request has no running SDK task'));
      throw this.failure;
    }
    const binding = { ...this.currentTask };
    const id = randomUUID();
    let images: ReturnType<typeof requestImages>;
    try { images = requestImages(body); }
    catch (error) { this.reject(error); throw this.failure; }
    const record = this.record('model_request', { request_id: id,
      model: typeof body.model === 'string' ? body.model : null, status: 'started' }, binding);
    await this.enqueue(async () => {
      const prepared = [];
      for (const image of images) prepared.push({ message_index: image.message_index,
        content_index: image.content_index, image: await this.image(image.data) });
      this.append({ ...record, images: prepared });
    });
    await this.flush();
    try {
      const result = await invoke();
      this.append(this.record('model_request', { request_id: id, status: 'returned' }, binding));
      return result;
    } catch (error) {
      this.append(this.record('model_request', { request_id: id, status: 'failed',
        error_kind: error instanceof Error ? error.name : 'UnknownError' }, binding));
      throw error;
    }
  }
}
