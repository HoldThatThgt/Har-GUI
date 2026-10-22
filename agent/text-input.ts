import { getMidsceneLocationSchema } from '@midscene/core/device';
import { z } from 'zod';
import { bridge } from './bridge.js';
import type { CollectionDevice } from './device.js';
import { checkWindows } from './observation.js';
import { safeActionParameters } from './privacy.js';
import type { ToolRegistry } from './registry.js';
import { assertInputScope, windowAt } from './scope.js';

const inputFields = {
  value: z.string().describe('Required text: final content for replace, appended text for append, empty for clear.'),
  mode: z.enum(['replace', 'append', 'clear']).default('replace')
    .describe('Replace the field, append at the end of the field, or clear it with an explicitly empty value.'),
  autoDismissKeyboard: z.boolean().default(false)
    .describe('Dismiss the keyboard after verified input only when explicitly requested. Default: false.'),
};
const modelSchema = z.object({ ...inputFields,
  locate: getMidsceneLocationSchema().describe('The editable field whose full text will be verified after input.'),
}).strict();
const resolvedSchema = z.object({ ...inputFields,
  locate: z.object({ center: z.tuple([z.number().finite(), z.number().finite()]) }).passthrough(),
}).strict();
const receiptSchema = z.object({
  window_id: z.number().int(), mode: z.enum(['replace', 'append', 'clear']),
  verified: z.literal(true).describe('Only component text is verified; not IME composition or page submission.'),
  character_count: z.number().int().nonnegative(), text_sha256: z.string().regex(/^[a-f0-9]{64}$/),
}).strict();

/** @brief 在设备访问前校验输入，并将正文仅经标准输入交给原生文本输入桥接。 */
async function inputText(parameters: unknown, getDevice: () => CollectionDevice) {
  const parsed = resolvedSchema.parse(parameters);
  if (parsed.mode === 'clear' && parsed.value !== '') throw new Error('Clear mode requires an empty value');
  const device = getDevice();
  const observation = await checkWindows(device);
  assertInputScope('Input', parsed, observation.windows, device.state.task.bundle_names);
  const window = windowAt(parsed.locate, observation.windows);
  if (!window) throw new Error('Input target is outside known application window rectangles');
  const [x, y] = parsed.locate.center.map(Math.round);
  const receipt = receiptSchema.parse(await bridge(device.connection, {
    operation: 'input_text', x, y, value: parsed.value, mode: parsed.mode,
    window_id: window.window_id, bundle_name: window.bundle_name, display_id: window.display_id,
  }, device.state.signal));
  if (receipt.window_id !== window.window_id || receipt.mode !== parsed.mode) {
    throw new Error('Input verification does not match the requested window and mode');
  }
  if (parsed.autoDismissKeyboard) await device.hideKeyboard();
  return { ...receipt, resolved_parameters: safeActionParameters('Input', parsed) };
}

/** @brief 注册定位必填且读回验证的文本输入，不继承 SDK 的旧输入模式或输入策略。 */
export function registerTextInput(registry: ToolRegistry, getDevice: () => CollectionDevice): void {
  registry.register({ mutates: true,
    resultSchema: receiptSchema.extend({ resolved_parameters: z.unknown() }).strict(), action: {
      name: 'Input', interfaceAlias: 'aiInput', paramSchema: modelSchema,
      description: 'Enter text into a located editable field and verify its full content. '
        + 'Append always adds at the end. Input does not navigate or submit the page; '
        + 'field readback does not prove page submission. If IME preedit is visible, '
        + 'cancel it with the visible IME control or KeyboardPress Escape when appropriate; '
        + 'observe again before Input.',
      sample: { value: 'test@example.com', locate: { prompt: 'the email input field' } },
      call: (parameters) => inputText(parameters, getDevice),
    } });
}
