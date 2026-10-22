import { z } from 'zod';
import { bridge } from './bridge.js';
import type { CollectionDevice } from './device.js';
import { checkWindows } from './observation.js';
import type { ToolRegistry } from './registry.js';
import { sameScreenBounds, windowSchema, type WindowInfo } from './state.js';

/** @brief 屏幕物理像素坐标。 */
interface Point {
  /** 横坐标。 */
  x: number;
  /** 纵坐标。 */
  y: number;
}
/** @brief 已验证的物理屏幕边界，保留设备观察中的浮点精度。 */
type Bounds = WindowInfo['screen_bounds'];
const position = z.object({ window_id: z.number().int(), x: z.number().int().nonnegative(),
  y: z.number().int().nonnegative() });
const resultSchema = z.object({ window_id: z.number(), before: windowSchema, after: windowSchema,
  bounds_changed: z.literal(true), occlusion: z.object({ target_window_id: z.number(),
    occluder_window_id: z.number(), bounding_box_overlap_fraction: z.number() }).optional() });

/** @brief 查找任务允许的普通应用窗口。 */
function targetWindow(device: CollectionDevice, windows: WindowInfo[], id: number): WindowInfo {
  const window = windows.find((item) => item.window_id === id);
  if (!window || window.is_system) throw new Error('Target must be a visible application window');
  const allowed = device.state.task.bundle_names;
  if (allowed && !allowed.includes(window.bundle_name)) throw new Error('Window is outside task scope');
  return window;
}

/** @brief 要求浮点目标矩形位于屏幕内；用户参数的整数约束由工具参数校验。 */
export function validateBounds(box: Bounds, screen: { width: number; height: number }): void {
  if (![box.x, box.y, box.width, box.height].every(Number.isFinite)
    || box.x < 0 || box.y < 0 || box.width <= 0 || box.height <= 0
    || box.x + box.width > screen.width || box.y + box.height > screen.height) {
    throw new Error('Requested window bounds must fit within the physical screen');
  }
}

/** @brief 确认鼠标起点在目标窗口内且没有被另一普通应用覆盖。 */
export function validateDragStart(window: WindowInfo, point: Point, windows: WindowInfo[]): void {
  const contains = (box: Bounds) => point.x >= box.x && point.x < box.x + box.width
    && point.y >= box.y && point.y < box.y + box.height;
  if (!contains(window.screen_bounds) || windows.some((other) => !other.is_system
    && other.window_id !== window.window_id && other.z_order > window.z_order && contains(other.screen_bounds))) {
    throw new Error('Window drag start is outside the target or covered by another application');
  }
}

/** @brief 计算目标矩形被前景矩形覆盖的面积比例，不推断透明像素。 */
export function overlapFraction(target: Bounds, foreground: Bounds): number {
  const width = Math.max(0, Math.min(target.x + target.width, foreground.x + foreground.width)
    - Math.max(target.x, foreground.x));
  const height = Math.max(0, Math.min(target.y + target.height, foreground.y + foreground.height)
    - Math.max(target.y, foreground.y));
  return width * height / (target.width * target.height);
}

/** @brief 只为窗口几何目标使用 SDK 整数像素网格，不用于屏幕输入命中。 */
function pixelBounds(bounds: Bounds): Bounds {
  return { x: Math.round(bounds.x), y: Math.round(bounds.y),
    width: Math.round(bounds.width), height: Math.round(bounds.height) };
}

/** @brief 最多读回三次，连续两次匹配目标像素网格才成功，保留精确实际边界。 */
async function verifyGeometry(device: CollectionDevice, before: WindowInfo, expected: Bounds): Promise<WindowInfo> {
  let matches = 0;
  let actual: Bounds | undefined;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    device.state.signal.throwIfAborted();
    const after = (await device.windows()).find((window) => window.window_id === before.window_id);
    if (!after || ['process_id', 'bundle_name', 'ui_node_id', 'display_id', 'is_system']
      .some((key) => after[key] !== before[key])) {
      throw new Error('Target window disappeared or changed identity');
    }
    actual = after.screen_bounds;
    matches = sameScreenBounds(pixelBounds(actual), pixelBounds(expected)) ? matches + 1 : 0;
    if (matches >= 2) return after;
    if (attempt < 2) await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error('Requested screen bounds were not reached on the rounded integer pixel grid: '
    + `expected=${JSON.stringify(pixelBounds(expected))}, actual=${JSON.stringify(actual)}`);
}

/** @brief 用官方 uinput 原生鼠标拖拽并验证实际窗口，不公开任意 shell 命令。 */
async function dragWindow(device: CollectionDevice, before: WindowInfo, goal: Bounds, points: Point[]) {
  if (before.window_mode !== 102) throw new Error('Window must be floating; restore it before moving or resizing');
  const screen = await device.size();
  validateBounds(goal, screen);
  if (sameScreenBounds(pixelBounds(goal), pixelBounds(before.screen_bounds))) {
    throw new Error('Requested screen bounds are unchanged on the rounded integer pixel grid');
  }
  const observation = await checkWindows(device);
  validateDragStart(before, points[0], observation.windows);
  const coordinates = points.flatMap((point) => [point.x, point.y]);
  if (!coordinates.every(Number.isSafeInteger) || points.some((point) => point.x < 0 || point.y < 0
    || point.x >= screen.width || point.y >= screen.height)) {
    throw new Error('Mouse coordinates must be safe integers within the physical screen');
  }
  const hdc = await device.getHdc();
  device.state.signal.throwIfAborted();
  device.state.event('window_drag', { window_id: before.window_id, from: points[0], to: points[1],
    expected: pixelBounds(goal), comparison: 'Math.round of each exact screen-bound field' });
  await hdc.shell(`uinput -M -g ${coordinates.join(' ')} 800`);
  const after = await verifyGeometry(device, before, goal);
  return { window_id: before.window_id, before, after, bounds_changed: true as const };
}

/** @brief 使用当前系统标题栏的可拖动点移动窗口。 */
async function moveWindow(device: CollectionDevice, before: WindowInfo, destination: Point) {
  const title = await bridge(device.connection, { operation: 'window_title_bar',
    window_id: before.window_id }, device.state.signal);
  if (title.window_id !== before.window_id) throw new Error('Title bar belongs to another window');
  const start = title.drag_point as Point;
  const box = before.screen_bounds;
  const end = { x: Math.round(start.x + destination.x - box.x),
    y: Math.round(start.y + destination.y - box.y) };
  return dragWindow(device, before, { ...box, ...destination }, [start, end]);
}

/** @brief 从右下边框调整尺寸，保持左上角不变。 */
async function resizeWindow(
  device: CollectionDevice, parameters: { window_id: number; width: number; height: number },
) {
  const observation = await checkWindows(device);
  const before = targetWindow(device, observation.windows, parameters.window_id);
  const box = before.screen_bounds;
  const goal = { ...box, width: parameters.width, height: parameters.height };
  const start = { x: Math.ceil(box.x + box.width) - 1, y: Math.ceil(box.y + box.height) - 1 };
  return dragWindow(device, before, goal, [
    start,
    { x: start.x + Math.round(goal.width - box.width), y: start.y + Math.round(goal.height - box.height) },
  ]);
}

/** @brief 把指定窗口移到目标前方，验证不同窗口、前后顺序与部分矩形交集。 */
async function occludeWindow(
  device: CollectionDevice, parameters: z.infer<typeof position> & { target_window_id: number },
) {
  const observation = await checkWindows(device);
  const target = targetWindow(device, observation.windows, parameters.target_window_id);
  const foreground = targetWindow(device, observation.windows, parameters.window_id);
  if (target.window_id === foreground.window_id) throw new Error('Occlusion requires two different windows');
  const goal = { ...foreground.screen_bounds, x: parameters.x, y: parameters.y };
  const fraction = overlapFraction(target.screen_bounds, goal);
  if (!(fraction > 0 && fraction < 1)) throw new Error('Requested placement must partially overlap the target');
  const result = await moveWindow(device, foreground, { x: parameters.x, y: parameters.y });
  const windows = await device.windows();
  const currentTarget = targetWindow(device, windows, target.window_id);
  const currentForeground = targetWindow(device, windows, foreground.window_id);
  const actual = overlapFraction(currentTarget.screen_bounds, currentForeground.screen_bounds);
  if (currentForeground.z_order <= currentTarget.z_order || !(actual > 0 && actual < 1)) {
    throw new Error('Foreground window did not partially occlude the target in verified screen geometry');
  }
  return { ...result, after: currentForeground, occlusion: { target_window_id: target.window_id,
    occluder_window_id: foreground.window_id, bounding_box_overlap_fraction: actual } };
}

/** @brief 注册可组合的窗口操作，统一保留每次验证后的变更历史。 */
export function registerGeometry(registry: ToolRegistry, getDevice: () => CollectionDevice): void {
  const definitions = [
    { name: 'MoveWindow', paramSchema: position,
      description: 'Move a floating window to physical pixel x,y using its actual system title bar. '
        + 'ReadWindows first. Bring the app to the foreground if its title bar is covered. '
        + 'Requested bounds must fit the screen; the drag point must be exposed. '
        + 'The goal is verified by Math.round of each exact screen-bound field; before/after retain exact bounds.',
      execute: async (device: CollectionDevice, parameters: any) => {
        const observation = await checkWindows(device);
        return moveWindow(device, targetWindow(device, observation.windows, parameters.window_id),
          { x: parameters.x, y: parameters.y });
      } },
    { name: 'ResizeWindow', paramSchema: z.object({ window_id: z.number().int(),
      width: z.number().int().positive(), height: z.number().int().positive() }),
      description: 'Resize a floating window to physical pixel width,height, keeping its top-left corner. '
        + 'ReadWindows first. Bring the app to the foreground using LaunchApplication if another app covers it. '
        + 'The bottom-right corner must be exposed. App minimum sizes still apply. '
        + 'The goal is verified by Math.round of each exact screen-bound field; before/after retain exact bounds.',
      execute: resizeWindow },
    { name: 'OccludeWindow', paramSchema: position.extend({ target_window_id: z.number().int() }),
      description: 'Move foreground window_id to x,y so it partially covers target_window_id. '
        + 'ReadWindows first. Verifies z order and exact screen-bounds overlap. Use two different app windows. '
        + 'Movement goals use Math.round of each exact screen-bound field; before/after retain exact bounds.',
      execute: occludeWindow },
  ];
  for (const definition of definitions) {
    registry.register({ mutates: true, resultSchema, action: {
      name: definition.name, description: definition.description, paramSchema: definition.paramSchema,
      planningFeedbackMaxLength: 5000, call: async (parameters) => {
        const device = getDevice();
        const result = await definition.execute(device, parameters as any);
        device.state.changes.push(result);
        return result;
      },
    } });
  }
}
