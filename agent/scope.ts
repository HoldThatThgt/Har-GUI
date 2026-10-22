import type { WindowInfo } from './state.js';

/** @brief SDK 已完成定位的屏幕坐标。 */
interface ResolvedLocation {
  /** 输入原语将按 Math.round 取整的中心点。 */
  center: readonly [number, number];
}

/** @brief 当前已注册原生输入动作使用的定位字段。 */
interface InputLocations {
  /** 点击、输入或滚动的目标中心。 */
  locate?: ResolvedLocation;
  /** 拖拽开始位置。 */
  from?: ResolvedLocation;
  /** 拖拽结束位置。 */
  to?: ResolvedLocation;
}

/** @brief 对范围拒绝记录英文错误，并阻止后续原生输入。 */
function rejectScope(message: string): never {
  console.error('Input scope rejected:', message);
  throw new Error(message);
}

/** @brief 按 SDK 实际取整坐标查找矩形内最高 z_order 的普通应用；不推断系统窗口透明像素。 */
function windowAt(location: ResolvedLocation, windows: readonly WindowInfo[]): WindowInfo | undefined {
  const [x, y] = location.center.map(Math.round);
  return windows.filter((window) => {
    const box = window.bounding_box;
    return !window.is_system && x >= box.x && x < box.x + box.width
      && y >= box.y && y < box.y + box.height;
  }).sort((left, right) => right.z_order - left.z_order)[0];
}

/** @brief 验证已知应用矩形和焦点的允许范围；此检查不是像素级遮挡证明。
 * @param name 已注册 SDK 动作名称。 @param parameters SDK 已解析的动作参数。
 * @param windows 已完成身份和状态比对的窗口。 @param bundleNames 项目允许的应用；缺省不限制。
 */
export function assertInputScope(
  name: string, parameters: InputLocations, windows: readonly WindowInfo[], bundleNames?: readonly string[],
): void {
  if (!bundleNames) return;
  let targets: WindowInfo[];
  if (name === 'KeyboardPress') {
    targets = windows.filter((window) => !window.is_system && window.focused);
    if (targets.length !== 1) rejectScope('KeyboardPress requires exactly one focused application window');
  } else {
    const locations = name === 'DragAndDrop' ? [parameters.from, parameters.to] : [parameters.locate];
    if (locations.some((location) => !location)) rejectScope(`${name} requires located targets within task scope`);
    targets = locations.map((location) => {
      const window = windowAt(location!, windows);
      if (!window) rejectScope('Input target is outside known application window rectangles');
      return window;
    });
  }
  for (const window of targets) {
    if (!bundleNames.includes(window.bundle_name)) {
      rejectScope(`Input target is outside task scope: ${window.bundle_name}`);
    }
  }
}
