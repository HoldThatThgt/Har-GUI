import { getTaskSearchArea, getTaskServiceDump, type DeviceAction, type ExecutionTask } from '@midscene/core';
import { findAllMidsceneLocatorField, type ModelRuntime } from '@midscene/core/ai-model';
import { defineActionSleep } from '@midscene/core/device';

/** @brief SDK 公开 TaskExecutor.convertPlanToExecutable 接受的无参数终止动作。 */
const FINISHED_ACTION = 'Finished';

/** @brief 只读取对象自身已知字段，不递归复制不透明数据。 @param value 原值。 @return 可读取字段。 */
function object(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : {};
}

/** @brief 从获准保留的文本中移除调用方明确提供的凭据。 @param value 原值。 @param secrets 实际凭据。
 * @return 脱敏文本；非文本为空。
 */
function text(value: unknown, secrets: readonly string[]): string | null {
  if (typeof value !== 'string') return null;
  return secrets.reduce((result, secret) => secret ? result.split(secret).join('[redacted]') : result, value);
}

/** @brief 多模态定位只保留描述文本。 @param value 定位描述。 @param secrets 实际凭据。 @return 脱敏描述。 */
function prompt(value: unknown, secrets: readonly string[]): string | null {
  return text(typeof value === 'string' ? value : object(value).prompt, secrets);
}

/** @brief 保留固定长度的有限数值向量。 @param value 原值。 @param length 长度。 @return 数值副本或空。 */
function vector(value: unknown, length: number): number[] | null {
  return Array.isArray(value) && value.length === length
    && value.every((item) => typeof item === 'number' && Number.isFinite(item)) ? [...value] : null;
}

/** @brief 只保留定位矩形的四个有限数值字段。 @param value 原矩形。 @return 安全矩形或空。 */
function rectangle(value: unknown): Record<string, number> | null {
  const source = object(value);
  const fields = ['left', 'top', 'width', 'height'];
  if (!fields.every((key) => typeof source[key] === 'number' && Number.isFinite(source[key]))) return null;
  return Object.fromEntries(fields.map((key) => [key, source[key] as number]));
}

/** @brief 定位仅保留描述及明确数值几何，不保留正文、路径和图像。 @param value 定位值。
 * @param secrets 实际凭据。 @param preserveDescriptions 是否保留已授权来源的描述。 @return 安全定位数据。
 */
function location(
  value: unknown, secrets: readonly string[], preserveDescriptions: boolean,
): Record<string, unknown> {
  const source = object(value), result: Record<string, unknown> = {};
  const description = preserveDescriptions ? text(source.description, secrets) : null;
  const target = preserveDescriptions ? prompt(source.prompt, secrets) : null;
  if (description !== null) result.description = description;
  if (target !== null) result.prompt = target;
  for (const [key, length] of [['center', 2], ['point', 2], ['bbox', 4]] as const) {
    const coordinates = vector(source[key], length);
    if (coordinates) result[key] = coordinates;
  }
  const rect = rectangle(source.rect);
  if (rect) result.rect = rect;
  return result;
}

/** @brief 声明动作诊断中允许保留的数值字段。 @param name 动作名。 @return 字段白名单。 */
function numericFields(name: string): readonly string[] {
  switch (name) {
    case 'ListApplications': return ['offset', 'limit'];
    case 'ToggleWindowSize': return ['window_id'];
    case 'MoveWindow': return ['window_id', 'x', 'y'];
    case 'ResizeWindow': return ['window_id', 'width', 'height'];
    case 'OccludeWindow': return ['window_id', 'target_window_id', 'x', 'y'];
    case 'Capture': return ['target_window_id'];
    case 'Scroll': return ['distance'];
    case 'Sleep': return ['timeMs'];
    default: return [];
  }
}

/** @brief 只保留固定操作枚举，不把键盘打印字符或任意文本记为参数。 @param name 动作名。
 * @param source 原参数。 @param result 安全结果。 @param secrets 实际凭据。
 */
function enums(
  name: string, source: Record<string, unknown>, result: Record<string, unknown>, secrets: readonly string[],
): void {
  const fields = name === 'Input' ? { mode: ['replace', 'append', 'clear'] }
    : name === 'Scroll' ? { direction: ['up', 'down', 'left', 'right'],
      scrollType: ['singleAction', 'scrollToTop', 'scrollToBottom', 'scrollToLeft', 'scrollToRight'] }
      : name === 'KeyboardPress' ? { keyName: ['Enter', 'Backspace', 'Tab', 'Escape', 'Home', 'ArrowUp',
        'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Space', 'Delete', 'Alt', 'Shift', 'Control', 'Meta', 'Back', 'Power'] }
        : {};
  for (const [key, allowed] of Object.entries(fields)) {
    if (typeof source[key] === 'string' && allowed.includes(source[key])) result[key] = text(source[key], secrets);
  }
}

/** @brief 按动作白名单生成诊断参数，输入正文和未知字段完全省略。 @param name 动作名。
 * @param parameters 原参数。 @param secrets 实际凭据。
 * @param preserveDescriptions 是否保留已授权来源的描述。 @return 安全参数副本。
 */
export function safeDiagnosticParameters(
  name: string, parameters: unknown, secrets: readonly string[], preserveDescriptions: boolean,
): Record<string, unknown> {
  const source = object(parameters), result: Record<string, unknown> = {};
  const fields = name === 'DragAndDrop' ? ['from', 'to']
    : ['Tap', 'DoubleClick', 'Scroll', 'Input'].includes(name) ? ['locate'] : [];
  for (const field of fields) {
    const target = location(source[field], secrets, preserveDescriptions);
    if (Object.keys(target).length) result[field] = target;
  }
  for (const field of numericFields(name)) {
    if (typeof source[field] === 'number' && Number.isFinite(source[field])) result[field] = source[field];
  }
  enums(name, source, result, secrets);
  if (name === 'Scroll' && source.distance === null) result.distance = null;
  if (name === 'Input' && typeof source.autoDismissKeyboard === 'boolean') {
    result.autoDismissKeyboard = source.autoDismissKeyboard;
  }
  return result;
}

/** @brief 保留标准 SDK 公开坐标声明，不根据像素结果反推坐标制。 @param value 公开声明。
 * @param secrets 实际凭据。 @return 声明的已知字段。
 */
function specification(value: unknown, secrets: readonly string[]): Record<string, unknown> | null {
  if (value === undefined || value === null) return null;
  const source = object(value), result: Record<string, unknown> = {};
  for (const key of ['resultKey', 'resultValueSchema', 'resultValueDescription', 'resultNoun', 'resultNounPlural']) {
    const field = text(source[key], secrets);
    if (field !== null) result[key] = field;
  }
  if (Array.isArray(source.resultKeyAliases)) {
    result.resultKeyAliases = source.resultKeyAliases.map((item) => text(item, secrets))
      .filter((item) => item !== null);
  }
  if (Array.isArray(source.exampleValues)) {
    result.exampleValues = source.exampleValues.map((item) => vector(item, 2) ?? vector(item, 4))
      .filter((item) => item !== null);
  }
  return result;
}

/** @brief 已出现的定位几何必须具有明确的有限数值形状。 @param value SDK 定位值。 */
function validateGeometry(value: unknown): void {
  const source = object(value);
  for (const [key, length] of [['center', 2], ['point', 2], ['bbox', 4]] as const) {
    if (source[key] !== undefined && vector(source[key], length) === null) {
      throw new Error('Invalid diagnostic geometry');
    }
  }
  if (source.rect !== undefined && rectangle(source.rect) === null) throw new Error('Invalid diagnostic rectangle');
}

/** @brief 只保留自定义规划器实际公开的坐标声明。 @param value SDK 坐标声明。
 * @param secrets 实际凭据。 @return 声明字段或空。
 */
function coordinateSystem(value: unknown, secrets: readonly string[]): Record<string, unknown> | null {
  if (value === undefined) return null;
  const source = object(value), result: Record<string, unknown> = {};
  for (const key of ['shape', 'order', 'rounding']) {
    const declared = text(source[key], secrets);
    if (declared !== null) result[key] = declared;
  }
  if (source.normalizedBy !== undefined) {
    if (typeof source.normalizedBy !== 'number' || !Number.isFinite(source.normalizedBy)
      || source.normalizedBy <= 0) throw new Error('Invalid diagnostic coordinate system');
    result.normalizedBy = source.normalizedBy;
  }
  return result;
}

/** @brief 已规范化动作只保留 SDK 公开的截图像素定位，不把它记作模型原始坐标。
 * @param definition 已注册动作。 @param value SDK 参数。 @param secrets 实际凭据。 @return 安全参数。
 */
function normalizedParameters(
  definition: DeviceAction, value: unknown, secrets: readonly string[],
): Record<string, unknown> {
  const source = object(value), result = safeDiagnosticParameters(definition.name, value, secrets, false);
  const fields = definition.paramSchema ? findAllMidsceneLocatorField(definition.paramSchema) : [];
  for (const field of fields) {
    if (source[field] === undefined) continue;
    const locator = object(source[field]);
    validateGeometry(locator);
    const target = location({ center: locator.center, rect: locator.rect }, secrets, false);
    if (locator.locatedPixelResult !== undefined) {
      const pixel = object(locator.locatedPixelResult);
      if (vector(pixel.center, 2) === null) throw new Error('Invalid normalized diagnostic center');
      validateGeometry(pixel);
      target.locatedPixelResult = location({ center: pixel.center, rect: pixel.rect }, secrets, false);
    }
    if (Object.keys(target).length) result[field] = target;
    else delete result[field];
  }
  return result;
}

/** @brief 自定义规划器逐项保存 SDK 动作，不调用其他原文解析器。 @param task SDK 规划任务。
 * @param planning 自定义规划器。 @param actionSpace 已注册动作。 @param secrets 实际凭据。
 * @return 诊断能力声明与已规范化动作。
 */
function customPlan(
  task: ExecutionTask, planning: Extract<ModelRuntime['adapter']['planning'], { kind: 'custom' }>,
  actionSpace: DeviceAction[], secrets: readonly string[],
): Record<string, unknown> {
  const actions = object(task.output).actions;
  if (actions !== undefined && !Array.isArray(actions)) throw new Error('Invalid normalized diagnostic actions');
  const normalized = actions === undefined ? null : (actions as unknown[]).map((value) => {
    const action = object(value);
    if (action.type === FINISHED_ACTION) return { name: FINISHED_ACTION, parameters: {} };
    const definition = actionSpace.find((item) => item.name === action.type);
    if (!definition) throw new Error('Planning action is not registered');
    return { name: text(definition.name, secrets),
      parameters: normalizedParameters(definition, action.param, secrets) };
  });
  return { task_id: text(task.taskId, secrets), adapter_kind: 'custom',
    raw_response_available: typeof object(task.log).rawResponse === 'string', raw_coordinates_available: false,
    raw_unavailable_reason: 'public_custom_adapter_has_no_raw_parser',
    prompt_unavailable_reason: 'custom_planner_prompt_may_contain_model_thought',
    normalized_actions: normalized, normalized_coordinate_space: 'screenshot_pixels',
    coordinate_system: coordinateSystem(planning.coordinateSystem, secrets) };
}

/** @brief 复用公开 SDK 协议解析规划原文，只输出动作及原始数值定位。 @param task 规划执行记录。
 * @param runtime 实际模型适配器。 @param actionSpace 已公开动作。 @param secrets 实际凭据。
 * @return 安全规划数据；原始模型全文不会保留。
 */
export function projectPlan(
  task: ExecutionTask, runtime: ModelRuntime, actionSpace: DeviceAction[], secrets: readonly string[],
): Record<string, unknown> {
  try {
    const planning = runtime.adapter.planning;
    const registered = [...actionSpace, defineActionSleep()];
    if (planning.kind === 'custom') return customPlan(task, planning, registered, secrets);
    const raw = object(task.log).rawResponse;
    const result: Record<string, unknown> = { task_id: text(task.taskId, secrets), adapter_kind: 'standard',
      raw_response_available: typeof raw === 'string', action: null, raw_locations: {},
      prompt_spec: specification(planning.locateResultCodec?.promptSpec, secrets) };
    if (typeof raw !== 'string') return result;
    const protocol = planning.protocol.actionOutputProtocol;
    const action = protocol.parseActionOutput(raw, registered);
    if (!action) return result;
    if (action.type === FINISHED_ACTION) {
      result.action = { name: FINISHED_ACTION, parameters: {} };
      return result;
    }
    const definition = registered.find((item) => item.name === action.type);
    if (!definition) throw new Error('Planning action is not registered');
    result.action = { name: text(action.type, secrets),
      parameters: safeDiagnosticParameters(action.type, action.param, secrets, true) };
    const fields = definition.paramSchema ? findAllMidsceneLocatorField(definition.paramSchema) : [];
    const rawLocations: Record<string, unknown> = {};
    for (const field of fields) {
      const value = object(action.param)[field];
      if (value !== undefined) {
        const parsed = protocol.parseRawLocateParameter(value);
        const target = location({ prompt: parsed.prompt }, secrets, true);
        for (const [key, length] of [['point', 2], ['bbox', 4]] as const) {
          const rawCoordinates = coordinates(parsed[key]);
          if (rawCoordinates !== null) {
            if (rawCoordinates.length !== length) throw new Error('Invalid raw diagnostic coordinates');
            target[key] = rawCoordinates;
          }
        }
        rawLocations[field] = target;
      }
    }
    result.raw_locations = rawLocations;
    return result;
  } catch {
    console.error('Planning diagnostic projection failed');
    throw new Error('Planning diagnostic projection failed');
  }
}

/** @brief 命中来源只保留名称和计划定位的数值，不复制缓存与路径。 @param value 命中信息。
 * @param secrets 实际凭据。 @return 安全命中信息或空。
 */
function hit(value: unknown, secrets: readonly string[]): Record<string, unknown> | null {
  const source = object(value), from = text(source.from, secrets);
  if (from === null) return null;
  const result: Record<string, unknown> = { from };
  const valueFromPlan = object(source.context).locatedPixelResult;
  const planned = object(valueFromPlan);
  if (valueFromPlan !== undefined && vector(planned.center, 2) === null) {
    throw new Error('Invalid normalized diagnostic center');
  }
  validateGeometry(planned);
  const center = vector(planned.center, 2), rect = rectangle(planned.rect);
  if (center || rect) result.locatedPixelResult = { ...(center ? { center } : {}), ...(rect ? { rect } : {}) };
  return result;
}

/** @brief 读取 SDK 协议提取的数值向量及单个向量外层数组，不重写模型标签或像素换算。 @param value 原始坐标值。
 * @return 两个或四个有限数值；其他值为空。
 */
function coordinates(value: unknown): number[] | null {
  if (value === undefined) return null;
  const parsed = typeof value === 'string' ? JSON.parse(value) as unknown : value;
  const values = Array.isArray(parsed) && parsed.length === 1 ? parsed[0] : parsed;
  const result = vector(values, 2) ?? vector(values, 4);
  if (result === null) throw new Error('Invalid raw diagnostic coordinates');
  return result;
}

/** @brief 只保留 SDK 已公开的原截图搜索区域，不重建裁图或换算执行位置。 @param task 定位任务。
 * @return 截图像素矩形；未发生区域定位时为空。
 */
function searchArea(task: ExecutionTask): Record<string, number> | null {
  const value = getTaskSearchArea(task);
  if (value === undefined) return null;
  const result = rectangle(value);
  if (!result || result.left < 0 || result.top < 0 || result.width <= 0 || result.height <= 0) {
    throw new Error('Invalid diagnostic search area');
  }
  return result;
}

/** @brief 投影真实定位任务，计划命中不冒充额外模型定位。 @param task 定位执行记录。
 * @param runtime 实际定位适配器。 @param secrets 实际凭据。
 * @param preserveDescriptions 实际规划器是否允许保留描述。 @return 安全定位数据及原始坐标声明。
 */
export function projectLocate(
  task: ExecutionTask, runtime: ModelRuntime, secrets: readonly string[], preserveDescriptions: boolean,
): Record<string, unknown> {
  let stage = 'element_geometry';
  try {
    const value = object(object(task.output).element);
    validateGeometry(value);
    const preserveText = preserveDescriptions && runtime.adapter.locate.kind === 'standard';
    const element = location({ center: value.center, rect: value.rect,
      description: value.description }, secrets, preserveText);
    const raw = getTaskServiceDump(task)?.taskInfo.rawResponse;
    const from = object(task.hitBy).from;
    const source = from === 'Plan' ? 'plan' : from === 'Cache' ? 'cache'
      : from === 'User expected path' ? 'user_expected_path' : typeof raw === 'string' ? 'ai' : null;
    const hitBy = hit(task.hitBy, secrets);
    stage = 'search_area_geometry';
    const area = searchArea(task);
    const kind = runtime.adapter.locate.kind;
    const result: Record<string, unknown> = { task_id: text(task.taskId, secrets), adapter_kind: kind,
      prompt: preserveText ? prompt(object(task.param).prompt, secrets) : null, hit_by: hitBy,
      element: Object.keys(element).length ? element : null, source,
      search_area: area, search_area_coordinate_space: area ? 'screenshot_pixels' : null,
      raw_reference: source === 'ai' ? area ? 'enlarged_search_area' : 'screenshot' : null,
      element_coordinate_space: 'screenshot_pixels', raw_response_available: typeof raw === 'string',
      raw_coordinates: null, prompt_spec: null };
    if (!preserveText) result.prompt_unavailable_reason = preserveDescriptions
      ? 'custom_locator_description_origin_not_public' : 'custom_planner_prompt_may_contain_model_thought';
    if (kind === 'custom') {
      result.raw_unavailable_reason = 'public_custom_adapter_has_no_raw_parser';
      return result;
    }
    if (source !== 'ai') return result;
    if (runtime.adapter.locate.kind !== 'standard') throw new Error('Invalid locate adapter kind');
    const operation = runtime.adapter.locate.element, spec = operation.resultCodec.promptSpec;
    result.prompt_spec = specification(spec, secrets);
    stage = 'raw_protocol';
    const parsed = operation.protocol.parseRawResponse(raw as string, spec);
    if (parsed.kind === 'located') {
      stage = 'raw_coordinates';
      const target = coordinates(parsed.target);
      if (target) result.raw_coordinates = { field: text(spec.resultKey, secrets), value: target };
    }
    return result;
  } catch {
    console.error('Locate diagnostic projection failed:', stage);
    throw new Error('Locate diagnostic projection failed', { cause: stage });
  }
}
