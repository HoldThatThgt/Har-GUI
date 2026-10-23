import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

export const PAGE_PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAQAAAADCAIAAAA7ljmRAAAAE0lEQVR4nGP8+u07AwwwwVnoHABrRALop6WreQAAAABJRU5ErkJggg==',
  'base64',
);

/** @brief 为合成测试保存可核验的目标首尾观察；末帧及两张裁剪只保存在返回对象中。 */
export function writePageObservation(directory: string, metadata: any, windowId: number,
  images: { before: Buffer; after: Buffer; afterScreen: Buffer }) {
  const window = metadata.windows.find((item: any) => item.window_id === windowId);
  assert.ok(window);
  const before = readFileSync(join(directory, metadata.files.screenshot.file));
  const target = { window_id: window.window_id, bundle_name: window.bundle_name,
    process_id: window.process_id, display_id: window.display_id };
  const bounds = window.screen_bounds;
  const crop = { x: Math.floor(bounds.x), y: Math.floor(bounds.y),
    width: Math.ceil(bounds.x + bounds.width) - Math.floor(bounds.x),
    height: Math.ceil(bounds.y + bounds.height) - Math.floor(bounds.y) };
  const digest = (bytes: Buffer) => createHash('sha256').update(bytes).digest('hex');
  const frame = (screen: Buffer, cropImage: Buffer, suffix: string) => ({
    screenshot_sha256: digest(screen), crop_sha256: digest(cropImage),
    screen_bounds: { ...bounds }, crop_bounds: { ...crop }, windows_sha256: metadata.files[suffix + 'windows'].sha256,
    layout_sha256: metadata.files[suffix + 'layout'].sha256 });
  const receipt = { rule_version: 1 as const, target, before: frame(before, images.before, ''),
    after: frame(images.afterScreen, images.after, 'closing_') };
  const bytes = Buffer.from(JSON.stringify(receipt));
  writeFileSync(join(directory, 'page-observation.json'), bytes);
  metadata.files.page_observation = { file: 'page-observation.json', bytes: bytes.length, sha256: digest(bytes) };
  return { before_image: `data:image/png;base64,${images.before.toString('base64')}`,
    after_image: `data:image/png;base64,${images.after.toString('base64')}`, receipt };
}

/** @brief 构造明确标注为模拟判断的离线语义收据，不调用模型。 */
export function writePageDecision(directory: string, metadata: any, captureId: string, decision = 'same_page') {
  const observation = JSON.parse(readFileSync(join(directory, 'page-observation.json'), 'utf8'));
  const receipt = { rule_version: 1, capture_id: captureId, target: observation.target,
    observation_sha256: metadata.files.page_observation.sha256, decision, evidence: 'Mocked semantic fixture decision' };
  writeFileSync(join(directory, 'page-stability.json'), JSON.stringify(receipt));
  return receipt;
}
