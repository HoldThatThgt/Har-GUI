/** @brief 递归去掉已知正文和定位描述字段，保留已解析坐标。 */
function redactActionValue(name: string, parameters: any): any {
  if (Array.isArray(parameters)) return parameters.map((item) => redactActionValue(name, item));
  if (!parameters || typeof parameters !== 'object') return parameters;
  const result: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(parameters)) {
    result[key] = key === 'prompt' || key === 'description' || (name === 'Input' && /value|text|content/i.test(key))
      ? '[redacted]' : redactActionValue(name, value);
  }
  return result;
}

/** @brief 只保留定位中的有限数值几何，丢弃不透明标识和未知字段。 */
function safeInputLocation(location: any): any {
  if (!location || typeof location !== 'object' || Array.isArray(location)) return '[redacted]';
  const result: Record<string, unknown> = {};
  if ('center' in location) {
    const center = location.center;
    result.center = Array.isArray(center) && center.length === 2 && center.every(Number.isFinite)
      ? [...center] : '[redacted]';
  }
  if ('rect' in location) {
    const rect = location.rect;
    const fields = ['left', 'top', 'width', 'height'];
    result.rect = rect && typeof rect === 'object' && fields.every((key) => Number.isFinite(rect[key]))
      ? Object.fromEntries(fields.map((key) => [key, rect[key]])) : '[redacted]';
  }
  for (const key of ['prompt', 'description']) if (key in location) result[key] = '[redacted]';
  return result;
}

/** @brief 输入失败也只记录当前接口允许的字段，非法枚举和未知字段不能携带正文。 */
export function safeActionParameters(name: string, parameters: any): any {
  if (name !== 'Input') return redactActionValue(name, parameters);
  if (!parameters || typeof parameters !== 'object' || Array.isArray(parameters)) return '[redacted]';
  const result: Record<string, unknown> = {};
  if ('value' in parameters) result.value = '[redacted]';
  if ('mode' in parameters) {
    result.mode = ['replace', 'append', 'clear'].includes(parameters.mode) ? parameters.mode : '[redacted]';
  }
  if ('autoDismissKeyboard' in parameters) {
    result.autoDismissKeyboard = typeof parameters.autoDismissKeyboard === 'boolean'
      ? parameters.autoDismissKeyboard : '[redacted]';
  }
  if ('locate' in parameters) {
    result.locate = safeInputLocation(parameters.locate);
  }
  return result;
}

/** @brief 失败事件只新增定位证据，未知或未通过接口校验的参数不能复制到日志。 */
export function safeFailedActionParameters(name: string, parameters: any): any {
  if (name === 'Input') return safeActionParameters(name, parameters);
  if (!parameters || typeof parameters !== 'object' || Array.isArray(parameters)) return {};
  const result: Record<string, unknown> = {};
  for (const key of ['locate', 'from', 'to']) {
    if (key in parameters) result[key] = safeInputLocation(parameters[key]);
  }
  return result;
}
