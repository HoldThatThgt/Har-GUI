/** @brief 从动作日志和反馈中去掉输入正文及定位描述，保留已解析坐标。 */
export function safeActionParameters(name: string, parameters: any): any {
  if (Array.isArray(parameters)) return parameters.map((item) => safeActionParameters(name, item));
  if (!parameters || typeof parameters !== 'object') return parameters;
  const result: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(parameters)) {
    result[key] = key === 'prompt' || (name === 'Input' && /value|text|content/i.test(key))
      ? '[redacted]' : safeActionParameters(name, value);
  }
  return result;
}
