import { randomUUID } from 'node:crypto';
import { writeFile } from 'node:fs/promises';
import path from 'node:path';
import { HarmonyDevice } from '@midscene/harmony';
import { z } from 'zod';
import { bridge } from './bridge.js';
import type { CollectionDevice } from './device.js';
import { ToolRegistry } from './registry.js';
import { assertInputScope, windowAt } from './scope.js';
import { sameScreenBounds, windowSchema, type WindowInfo } from './state.js';
import { checkWindows } from './observation.js';
import { registerGeometry } from './geometry.js';
import { safeActionParameters } from './privacy.js';
import { harmonyKeyboardContract } from './keyboard.js';
import { registerTextInput } from './text-input.js';

/** @brief 只注册当前已验证的 SDK 输入动作，保持名称与 Midscene 一致。 */
function registerInput(registry: ToolRegistry, getDevice: () => CollectionDevice): void {
  const definitions = new HarmonyDevice('configuration-only', {}).actionSpace();
  const names = ['Tap', 'Scroll', 'DragAndDrop', 'KeyboardPress', 'DoubleClick'];
  for (const name of names) {
    const definition = definitions.find((action) => action.name === name);
    if (!definition) throw new Error(`Required SDK action is missing: ${name}`);
    const action = name === 'KeyboardPress' ? { ...definition, ...harmonyKeyboardContract } : definition;
    registry.register({ mutates: true, resultSchema: z.object({ completed: z.literal(true),
      resolved_parameters: z.unknown() }), action: {
      ...action,
      description: action.description + ' For bundle-restricted tasks, stay inside an allowed application window. '
        + 'Do not minimize or close it unless the task requests this. Do not click system Dock or launcher icons; '
        + 'use the enabled application query and launch tools when the target application is not visible.',
      call: async (parameters, context) => {
        if (name === 'KeyboardPress') parameters = harmonyKeyboardContract.paramSchema.parse(parameters);
        const device = getDevice();
        const observation = await checkWindows(device);
        assertInputScope(name, parameters, observation.windows, device.state.task.bundle_names);
        const native = device.nativeActions().find((action) => action.name === name)!;
        await native.call(parameters, context);
        return { completed: true, resolved_parameters: safeActionParameters(name, parameters) };
      },
    } });
  }
}

/** @brief 用有界分页返回应用信息，查询结果明确进入下一次规划。 */
function registerApplications(registry: ToolRegistry, getDevice: () => CollectionDevice): void {
  registry.register({ mutates: false, resultSchema: z.object({
    applications: z.array(z.object({ bundle_name: z.string(), user_id: z.number() })),
    total: z.number(), offset: z.number(), limit: z.number(), next_offset: z.number().nullable(),
  }), action: {
    name: 'ListApplications', planningFeedbackMaxLength: 16000,
    description: 'List installed applications. Use query to filter bundle names and offset to paginate.',
    paramSchema: z.object({ query: z.string().optional(), offset: z.number().int().min(0).optional(),
      limit: z.number().int().min(1).max(50).optional() }),
    call: (parameters) => bridge(getDevice().connection,
      { operation: 'list_applications', ...parameters }, getDevice().state.signal),
  } });
  registry.register({ mutates: false, resultSchema: z.object({ bundle_name: z.string(),
    abilities: z.array(z.object({ ability_name: z.string(), module_name: z.string(), launcher: z.boolean() })),
  }), action: {
    name: 'ReadApplication', planningFeedbackMaxLength: 16000,
    description: 'Read declared UI abilities before launching or restoring a non-visible application. '
      + 'Choose the appropriate ability_name explicitly when multiple desktop entries exist.',
    paramSchema: z.object({ bundle_name: z.string() }),
    call: (parameters) => bridge(getDevice().connection,
      { operation: 'read_application', ...parameters }, getDevice().state.signal),
  } });
  registry.register({ mutates: true, resultSchema: z.object({ bundle_name: z.string(),
    module_name: z.string(), ability_name: z.string(), launched: z.literal(true) }), action: {
    name: 'LaunchApplication', description: 'Request an installed bundle through its real declared UI entry, '
      + 'including when its window is no longer visible. ReadApplication, when enabled, provides real entries. '
      + 'Never use system Dock clicks to restore it. '
      + 'After launch, inspect a fresh screenshot and verify the window with enabled query tools; '
      + 'stop if verification is unavailable. '
      + 'A confirmed launch does not prove that a visible window or the requested page is ready.',
    paramSchema: z.object({ bundle_name: z.string(), ability_name: z.string().optional() }),
    call: (parameters) => {
      const device = getDevice();
      const allowed = device.state.task.bundle_names;
      if (allowed && !allowed.includes(parameters.bundle_name)) throw new Error('Bundle is outside task scope');
      return bridge(device.connection, { operation: 'launch_application', ...parameters }, device.state.signal);
    },
  } });
}

/** @brief 返回明确的窗口标识与屏幕坐标，而不把整个组件树发送给模型。 */
function registerWindows(registry: ToolRegistry, getDevice: () => CollectionDevice): void {
  registry.register({ mutates: false,
    resultSchema: z.object({ observation_id: z.string(), task_scope: z.object({
      bundle_names: z.array(z.string()).nullable(), visible_window_ids: z.array(z.number().int()),
      absent_bundle_names: z.array(z.string()),
    }), windows: windowSchema.array() }), action: {
      name: 'ReadWindows', description: 'Read current app windows: IDs, bundles, exact physical screen_bounds '
        + 'and z order. task_scope identifies visible ordinary windows within allowed bundles and absent bundles; '
        + 'it does not judge page readiness. Inspect the page and Capture when it meets the task. '
        + 'For a non-visible target, use enabled application query and launch tools, never system Dock clicks; '
        + 'stop if the required navigation tools are unavailable. '
        + 'bounding_box is original WMS logical geometry; use screen_bounds for screen points.',
      paramSchema: z.object({}), planningFeedbackMaxLength: 20000,
      call: async (_parameters, context) => {
        const device = getDevice();
        const observation = await checkWindows(device);
        const allowed = device.state.task.bundle_names;
        const visible = observation.windows.filter((window) => !window.is_system
          && (!allowed || allowed.includes(window.bundle_name)));
        return { observation_id: observation.id, task_scope: { bundle_names: allowed ?? null,
          visible_window_ids: visible.map((window) => window.window_id),
          absent_bundle_names: allowed?.filter((bundle) => !visible.some((window) =>
            window.bundle_name === bundle)) ?? [],
        }, windows: observation.windows };
      },
    } });
  registry.register({ mutates: true, resultSchema: z.object({
    window_id: z.number(), before: windowSchema, after: windowSchema, bounds_changed: z.literal(true),
  }), action: {
    name: 'ToggleWindowSize', planningFeedbackMaxLength: 4000,
    description: 'Toggle maximize/restore using the actual system accessibility button, '
      + 'then verify that exact physical screen bounds change and stabilize. '
      + 'ReadWindows in the previous planning round to obtain window_id. No guessed coordinates are needed.',
    paramSchema: z.object({ window_id: z.number().int() }),
    call: (parameters) => toggleWindow(getDevice(), parameters),
  } });
}

/** @brief 最多读回三次，要求连续两次得到相同且已变化的精确屏幕矩形。 */
async function verifyWindowToggle(device: CollectionDevice, before: WindowInfo): Promise<WindowInfo> {
  let previous: WindowInfo | undefined;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    device.state.signal.throwIfAborted();
    const after = (await device.windows()).find((window) => window.window_id === before.window_id);
    if (!after || ['process_id', 'bundle_name', 'ui_node_id', 'display_id', 'is_system']
      .some((key) => after[key] !== before[key])) {
      throw new Error('Target window disappeared or changed identity');
    }
    if (!sameScreenBounds(after.screen_bounds, before.screen_bounds)) {
      if (previous && sameScreenBounds(after.screen_bounds, previous.screen_bounds)) return after;
      previous = after;
    } else previous = undefined;
    if (attempt < 2) await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error('Window screen bounds did not change and stabilize after activating the requested control');
}

/** @brief 定位标题栏按钮执行窗口大小切换，并读回实际屏幕边界。 */
async function toggleWindow(device: CollectionDevice, parameters: any) {
  const observation = await checkWindows(device);
  const before = observation.windows.find((window) => window.window_id === parameters.window_id);
  if (!before || before.is_system) throw new Error('Target must be a visible application window');
  const allowed = device.state.task.bundle_names;
  if (allowed && !allowed.includes(before.bundle_name)) throw new Error('Window is outside task scope');
  const control = await bridge(device.connection, { operation: 'window_size_control',
    window_id: before.window_id }, device.state.signal);
  const current = await checkWindows(device);
  const { x, y } = control.center;
  const box = before.screen_bounds;
  if (x < box.x || x >= box.x + box.width || y < box.y || y >= box.y + box.height) {
    throw new Error('Window control is outside the target window');
  }
  if (windowAt({ center: [x, y] }, current.windows)?.window_id !== before.window_id) {
    throw new Error('Window control is covered by another application window');
  }
  const hdc = await device.getHdc();
  device.state.event('window_control', { window_id: before.window_id, control });
  await hdc.click(x, y);
  const after = await verifyWindowToggle(device, before);
  const result = { window_id: before.window_id, before, after, bounds_changed: true as const };
  device.state.changes.push(result);
  return result;
}

const captureParameters = z.object({
  page_name: z.string().min(1).describe('A short page category, without path separators'),
  variation: z.string().min(1).describe('The actual page/window state collected'),
  target_bundle_name: z.string().min(1).describe('The main task application bundle returned by ReadWindows'),
  target_window_id: z.number().int().safe().positive().describe('The exact target window ID from ReadWindows'),
}).strict();

/** @brief 将明确的目标窗口传入采集器，瞬时裁图只留在设备内存中。 */
async function captureCandidate(device: CollectionDevice, input: unknown) {
  try {
    device.pageObservation = undefined;
    const parameters = captureParameters.parse(input);
    const observation = await checkWindows(device);
    const target = observation.windows.find((window) => !window.is_system
      && window.window_id === parameters.target_window_id && window.bundle_name === parameters.target_bundle_name);
    if (!target) throw new Error('Capture target is not the specified visible application window');
    const allowed = device.state.task.bundle_names;
    if (allowed && !allowed.includes(parameters.target_bundle_name)) throw new Error('Bundle is outside task scope');
    const { page_observation: paired, ...result } = await bridge(device.connection, { operation: 'capture',
      output_dir: `${device.state.directory}/captures`, display_id: target.display_id,
      target_bundle_name: target.bundle_name, target_window_id: target.window_id,
      stability_regions: device.state.config.project.dataset.stability_regions }, device.state.signal);
    if (!paired) throw new Error('Capture response has no paired target-page observation');
    device.pageObservation = paired;
    device.state.candidate = { ...result, ...parameters, task_name: device.state.task.name,
      sample_id: randomUUID(), window_changes: [...device.state.changes] };
    await writeFile(path.join(result.path, 'candidate.json'), JSON.stringify(device.state.candidate, null, 2) + '\n');
    return { capture_id: result.capture_id, path: result.path, pending_acceptance: true as const };
  } catch (error) {
    device.pageObservation = undefined;
    throw error;
  }
}

/** @brief 调用原有采集器保存单张截图和双树，计数由后续验收决定。 */
function registerCapture(registry: ToolRegistry, getDevice: () => CollectionDevice): void {
  registry.register({ mutates: false, resultSchema: z.object({
    capture_id: z.string(), path: z.string(), pending_acceptance: z.literal(true),
  }), action: {
    name: 'Capture', description: 'Persist the current screen as a dataset candidate with WMS and both UI trees. '
      + 'Specify target_window_id and its target_bundle_name from ReadWindows; never choose an implicit window. '
      + 'This tool is the ONLY operation that saves a sample. Observing screenshots does NOT save samples. '
      + 'You MUST execute Capture before reporting task completion; wait for pending_acceptance=true feedback.',
    paramSchema: captureParameters, call: (parameters) => captureCandidate(getDevice(), parameters),
  } });
}

/** @brief 集中声明本次实现的工具；新增工具只需增加模块与注册调用。 */
export function createRegistry(getDevice: () => CollectionDevice): ToolRegistry {
  const registry = new ToolRegistry();
  registerInput(registry, getDevice);
  registerTextInput(registry, getDevice);
  registerApplications(registry, getDevice);
  registerWindows(registry, getDevice);
  registerGeometry(registry, getDevice);
  registerCapture(registry, getDevice);
  return registry;
}
