import type { CollectionDevice } from './device.js';
import { sameWindow, type Observation } from './state.js';

/** @brief 检查模型实际规划截图对应的窗口仍保持原身份和状态。 */
export async function checkWindows(device: CollectionDevice): Promise<Observation> {
  const observation = device.planningObservation;
  if (!observation) throw new Error('Action has no bound planning observation');
  if (observation.revision !== device.state.revision) throw new Error('Planning observation is stale');
  const current = await device.windows();
  if (current.length !== observation.windows.length || observation.windows.some((window) => {
    const actual = current.find((item) => item.window_id === window.window_id);
    return !actual || !sameWindow(window, actual);
  })) throw new Error('Window state changed since planning; action rejected');
  return observation;
}
