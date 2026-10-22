import { randomUUID } from 'node:crypto';
import { HarmonyDevice } from '@midscene/harmony';
import type { ExecutorContext } from '@midscene/core';
import { z } from 'zod';
import { bridge } from './bridge.js';
import type { CollectionDevice } from './device.js';
import { ToolRegistry } from './registry.js';
import { assertInputScope } from './scope.js';
import { windowSchema } from './state.js';
import { checkWindows } from './observation.js';
import { registerGeometry } from './geometry.js';

/** @brief 只注册当前已验证的 SDK 输入动作，保持名称与 Midscene 一致。 */
function registerInput(registry: ToolRegistry, getDevice: () => CollectionDevice): void {
  const definitions = new HarmonyDevice('configuration-only', {}).actionSpace();
  const names = ['Tap', 'Input', 'Scroll', 'DragAndDrop', 'KeyboardPress', 'DoubleClick'];
  for (const name of names) {
    const definition = definitions.find((action) => action.name === name);
    if (!definition) throw new Error(`Required SDK action is missing: ${name}`);
    registry.register({ mutates: true, resultSchema: z.object({ completed: z.literal(true),
      resolved_parameters: z.unknown() }), action: {
      ...definition,
      call: async (parameters, context) => {
        const device = getDevice();
        const observation = await checkWindows(device);
        assertInputScope(name, parameters, observation.windows, device.state.task.bundle_names);
        const native = device.nativeActions().find((action) => action.name === name)!;
        await native.call(parameters, context);
        return { completed: true, resolved_parameters: parameters };
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
  registry.register({ mutates: true, resultSchema: z.object({ bundle_name: z.string(),
    module_name: z.string(), ability_name: z.string(), launched: z.literal(true) }), action: {
    name: 'LaunchApplication', description: 'Launch an installed bundle through its declared entry ability.',
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
    resultSchema: z.object({ observation_id: z.string(), windows: windowSchema.array() }), action: {
      name: 'ReadWindows', description: 'Read current app windows: IDs, bundle names, bounding boxes and z order.',
      paramSchema: z.object({}), planningFeedbackMaxLength: 20000,
      call: async (_parameters, context) => {
        const device = getDevice();
        const observation = await checkWindows(device);
        return { observation_id: observation.id, windows: observation.windows };
      },
    } });
  registry.register({ mutates: true, resultSchema: z.object({
    window_id: z.number(), before: windowSchema, after: windowSchema, bounds_changed: z.literal(true),
  }), action: {
    name: 'ToggleWindowSize', planningFeedbackMaxLength: 4000,
    description: 'Toggle maximize/restore using the actual system accessibility button, then verify WMS bounds. '
      + 'ReadWindows in the previous planning round to obtain window_id. No guessed coordinates are needed.',
    paramSchema: z.object({ window_id: z.number().int() }),
    call: (parameters, context) => toggleWindow(getDevice(), parameters, context),
  } });
}

/** @brief 定位标题栏按钮执行窗口大小切换，并读回实际边界；不模拟任意尺寸 API。 */
async function toggleWindow(device: CollectionDevice, parameters: any, context?: ExecutorContext) {
  const observation = await checkWindows(device);
  const before = observation.windows.find((window) => window.window_id === parameters.window_id);
  if (!before || before.is_system) throw new Error('Target must be a visible application window');
  const allowed = device.state.task.bundle_names;
  if (allowed && !allowed.includes(before.bundle_name)) throw new Error('Window is outside task scope');
  const control = await bridge(device.connection, { operation: 'window_size_control',
    window_id: before.window_id }, device.state.signal);
  await checkWindows(device);
  const { x, y } = control.center;
  const box = before.bounding_box;
  if (x < box.x || x >= box.x + box.width || y < box.y || y >= box.y + box.height) {
    throw new Error('Window control is outside the target window');
  }
  const hdc = await device.getHdc();
  device.state.event('window_control', { window_id: before.window_id, control });
  await hdc.click(x, y);
  const deadline = Date.now() + 5000;
  while (Date.now() < deadline) {
    device.state.signal.throwIfAborted();
    const after = (await device.windows()).find((window) => window.window_id === before.window_id);
    if (!after || after.process_id !== before.process_id || after.ui_node_id !== before.ui_node_id) {
      throw new Error('Target window disappeared or changed identity');
    }
    if (JSON.stringify(after.bounding_box) !== JSON.stringify(before.bounding_box)) {
      const result = { window_id: before.window_id, before, after, bounds_changed: true as const };
      device.state.changes.push(result);
      return result;
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error('Window bounds did not change after activating the requested control');
}

/** @brief 调用原有采集器保存单张截图和双树，计数由后续验收决定。 */
function registerCapture(registry: ToolRegistry, getDevice: () => CollectionDevice): void {
  registry.register({ mutates: false, resultSchema: z.object({
    capture_id: z.string(), path: z.string(), pending_acceptance: z.literal(true),
  }), action: {
    name: 'Capture', description: 'Capture the completed page once. Saves one screenshot, WMS, ArkUI JSON '
      + 'and accessibility trees. Call only after the task and its variations are complete. Then finish.',
    paramSchema: z.object({ page_name: z.string().min(1), variation: z.string().min(1) }),
    call: async (parameters, context) => {
      const device = getDevice();
      await checkWindows(device);
      const result = await bridge(device.connection, { operation: 'capture',
        output_dir: `${device.state.directory}/captures` }, device.state.signal);
      device.state.candidate = { ...result, ...parameters, task_name: device.state.task.name,
        sample_id: randomUUID(), window_changes: [...device.state.changes] };
      return { capture_id: result.capture_id, path: result.path, pending_acceptance: true };
    },
  } });
}

/** @brief 集中声明本次实现的工具；新增工具只需增加模块与注册调用。 */
export function createRegistry(getDevice: () => CollectionDevice): ToolRegistry {
  const registry = new ToolRegistry();
  registerInput(registry, getDevice);
  registerApplications(registry, getDevice);
  registerWindows(registry, getDevice);
  registerGeometry(registry, getDevice);
  registerCapture(registry, getDevice);
  return registry;
}
