import { z } from 'zod';

/** @brief 只声明固定 Harmony SDK 支持的单键；键码转换仍由原生 SDK 完成。 */
export const harmonyKeyboardContract = {
  description: 'Press one supported HarmonyOS key on the currently focused element. '
    + 'Key combinations are unsupported, and consecutive modifier/key presses cannot form a shortcut. '
    + 'Use Tap first to change focus and Input to enter text. Letter case does not add Shift.',
  paramSchema: z.object({
    keyName: z.union([
      z.enum(['Enter', 'Backspace', 'Tab', 'Escape', 'Home', 'ArrowUp', 'ArrowDown', 'ArrowLeft',
        'ArrowRight', 'Space', 'Delete', 'Alt', 'Shift', 'Control', 'Meta', 'Back', 'Power']),
      z.string().regex(/^[A-Za-z0-9]$/),
    ]).describe('One listed key name or one ASCII letter/digit. No combinations or text strings.'),
  }).strict(),
};
