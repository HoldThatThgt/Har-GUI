import { randomUUID } from 'node:crypto';
import { appendFileSync, writeFileSync } from 'node:fs';
import type OpenAI from 'openai';
import type { CreateOpenAIClientFn } from '@midscene/shared/env/types';

/** @brief 服务商直接返回的计数；缺失或无效值明确保存为 null。 */
export interface ProviderUsage {
  /** 服务商报告的输入 token 总数。 */
  prompt_tokens: number | null;
  /** 服务商报告的输出 token 总数。 */
  completion_tokens: number | null;
  /** 服务商报告的输入与输出 token 总数。 */
  total_tokens: number | null;
  /** DeepSeek 报告的输入缓存命中 token 数，不从其他字段推算。 */
  prompt_cache_hit_tokens: number | null;
  /** DeepSeek 报告的输入缓存未命中 token 数，不从输入总数推算。 */
  prompt_cache_miss_tokens: number | null;
}

/** @brief 一次真实模型请求的审计记录，不包含请求正文、响应正文或凭据。 */
export interface ModelUsageRecord {
  /** 本地主机生成的单次调用标识。 */
  call_id: string;
  /** 请求中明确选用的模型名称。 */
  requested_model: string;
  /** 响应中的实际模型名称；没有响应或未提供时为 null。 */
  model: string | null;
  /** 调用开始的 UTC 时间。 */
  started_at: string;
  /** 调用结束的 UTC 时间；仍在执行时为 null。 */
  finished_at: string | null;
  /** 请求的执行状态；失败不代表未产生费用。 */
  status: 'running' | 'completed' | 'failed';
  /** 服务商用量白名单字段；未返回用量时为 null。 */
  usage: ProviderUsage | null;
}

/** @brief 聚合已观察调用；任一调用缺少某计数时该计数总量保持 null。 */
export interface UsageSummary {
  /** 已发起调用数量。 */
  calls: number;
  /** 收到响应的调用数量。 */
  completed_calls: number;
  /** 请求失败或取消的调用数量。 */
  failed_calls: number;
  /** 尚未完成的调用数量。 */
  pending_calls: number;
  /** 完全没有服务商 usage 的调用数量。 */
  missing_usage_calls: number;
  /** 返回 usage 但至少一个白名单计数缺失的调用数量。 */
  partial_usage_calls: number;
  /** 每项独立保持完整性；未知计数绝不当作零累加。 */
  totals: ProviderUsage;
}

/** @brief 调用方明确提供的美元价格快照，单位均为每百万 token。 */
export interface UsageRates {
  /** 明确指定的计价货币。 */
  currency: 'USD';
  /** 输入缓存命中单价。 */
  input_cache_hit_per_million: number;
  /** 输入缓存未命中单价。 */
  input_cache_miss_per_million: number;
  /** 输出单价。 */
  output_per_million: number;
}

/** @brief 根据显式价格得到的费用结果；未知调用不能被计为零费用。 */
export interface UsageCost {
  /** 明确标记为按指定价格计算的估算，不是服务商账单。 */
  is_estimate: true;
  /** 费用计价货币。 */
  currency: 'USD';
  /** 本次计算使用的原样价格快照。 */
  rates: UsageRates;
  /** 可以完整计算费用的调用数量。 */
  priced_calls: number;
  /** 因缺失计数无法计算费用的调用数量。 */
  unpriced_calls: number;
  /** 每次调用的费用；未知时为 null。 */
  calls: Array<{ call_id: string; cost_usd: number | null }>;
  /** 全部调用的费用；任一次未知则总费用也是 null。 */
  total_cost_usd: number | null;
}

const usageFields = ['prompt_tokens', 'completion_tokens', 'total_tokens',
  'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens'] as const;

/** @brief 只提取服务商数字计数，不保存任意附加文本或从其他协议字段推导。 */
function providerUsage(value: unknown): ProviderUsage | null {
  if (!value || typeof value !== 'object') return null;
  const raw = value as Record<string, unknown>;
  return Object.fromEntries(usageFields.map((name) => {
    const count = raw[name];
    return [name, typeof count === 'number' && Number.isSafeInteger(count) && count >= 0 ? count : null];
  })) as unknown as ProviderUsage;
}

/** @brief 汇总一个计数字段；缺失任意调用时不输出误导性的部分总量。 */
function totalTokens(records: readonly ModelUsageRecord[], field: keyof ProviderUsage): number | null {
  let total = 0;
  for (const record of records) {
    const count = record.usage?.[field];
    if (count === null || count === undefined) return null;
    total += count;
  }
  return total;
}

/** @brief 明确拒绝未实现的流式计数路径，避免调用绕过用量记录。 */
function rejectStreaming(): never {
  console.error('Usage recording requires non-stream chat completions');
  throw new Error('Usage recording requires non-stream chat completions');
}

/** @brief 通过 Midscene 公开 client factory 记录官方 OpenAI SDK 的真实非流式响应。 */
export class UsageRecorder {
  private calls: ModelUsageRecord[] = [];

  /** @brief 创建本次运行的独立 JSONL 文件，拒绝覆盖先前记录。 @param filePath 完整输出路径。 */
  constructor(readonly filePath: string) {
    try { writeFileSync(filePath, '', { flag: 'wx' }); }
    catch (error) { console.error('Cannot create model usage log:', String(error)); throw error; }
  }

  /** @brief 获取审计记录副本，避免调用方修改已记录数据。 */
  get records(): readonly ModelUsageRecord[] { return structuredClone(this.calls); }

  /** @brief 将此公开 factory 直接传给 AgentOpt.createOpenAIClient；只支持配置明确的非流式 chat。 */
  createOpenAIClient: CreateOpenAIClientFn = async (client) => {
    const completion = client.chat.completions;
    const original = completion.create.bind(completion);
    completion.create = (body: OpenAI.ChatCompletionCreateParams, options?: OpenAI.RequestOptions) => {
      if (body.stream === true) rejectStreaming();
      return this.record(body.model, () => original(body, options));
    };
    completion.stream = rejectStreaming;
    return client;
  };

  /** @brief 汇总当前运行用量，分别保留失败、缺失和不完整调用计数。 */
  summary(startIndex = 0): UsageSummary {
    const calls = this.calls.slice(startIndex);
    const totals = Object.fromEntries(usageFields.map((field) => [field, totalTokens(calls, field)]));
    return {
      calls: calls.length,
      completed_calls: calls.filter((record) => record.status === 'completed').length,
      failed_calls: calls.filter((record) => record.status === 'failed').length,
      pending_calls: calls.filter((record) => record.status === 'running').length,
      missing_usage_calls: calls.filter((record) => record.usage === null).length,
      partial_usage_calls: calls.filter((record) => record.usage
        && Object.values(record.usage).some((value) => value === null)).length,
      totals: totals as unknown as ProviderUsage,
    };
  }

  /** @brief 同步追加调用状态；写入失败直接中止，禁止产生没有开始记录的模型请求。 */
  private persist(record: ModelUsageRecord): void {
    try { appendFileSync(this.filePath, JSON.stringify(record) + '\n'); }
    catch (error) { console.error('Cannot persist model usage lifecycle:', String(error)); throw error; }
  }

  /** @brief 先持久记录开始再调用模型，结束追加相同 call_id 的状态，内存只维护当前记录。 */
  private async record(model: string, invoke: () => Promise<OpenAI.ChatCompletion>): Promise<OpenAI.ChatCompletion> {
    const record: ModelUsageRecord = { call_id: randomUUID(), requested_model: model, model: null,
      started_at: new Date().toISOString(), finished_at: null, status: 'running', usage: null };
    this.persist(record);
    this.calls.push(record);
    try {
      const response = await invoke();
      record.model = typeof response.model === 'string' ? response.model : null;
      record.usage = providerUsage(response.usage);
      record.status = 'completed';
      return response;
    } catch (error) {
      record.status = 'failed';
      console.error('Model request failed; provider usage is unavailable');
      throw error;
    } finally {
      record.finished_at = new Date().toISOString();
      this.persist(record);
    }
  }
}

/** @brief 使用真实缓存命中、未命中和输出计数计算一项费用，不推断缺失数据。 */
function callCost(record: ModelUsageRecord, rates: UsageRates): number | null {
  const usage = record.usage;
  if (!usage || usage.prompt_tokens === null || usage.completion_tokens === null
    || usage.prompt_cache_hit_tokens === null || usage.prompt_cache_miss_tokens === null) return null;
  return (usage.prompt_cache_hit_tokens * rates.input_cache_hit_per_million
    + usage.prompt_cache_miss_tokens * rates.input_cache_miss_per_million
    + usage.completion_tokens * rates.output_per_million) / 1_000_000;
}

/** @brief 用调用方显式提供的价格快照估算费用；不内置价格、时段或模型映射。
 * @param records 实际调用记录。 @param rates 本次计算采用的每百万 token 美元单价。
 * @return 保留逐调用 null 和完整性计数的费用结果。
 */
export function estimateUsageCost(records: readonly ModelUsageRecord[], rates: UsageRates): UsageCost {
  const prices = [rates.input_cache_hit_per_million, rates.input_cache_miss_per_million, rates.output_per_million];
  if (rates.currency !== 'USD' || prices.some((price) => !Number.isFinite(price) || price < 0)) {
    console.error('Invalid model usage prices');
    throw new Error('Model usage prices must be finite nonnegative numbers');
  }
  const calls = records.map((record) => ({ call_id: record.call_id, cost_usd: callCost(record, rates) }));
  const unpriced = calls.filter((call) => call.cost_usd === null).length;
  return { is_estimate: true, currency: 'USD', rates: { ...rates }, calls,
    priced_calls: calls.length - unpriced, unpriced_calls: unpriced,
    total_cost_usd: unpriced ? null : calls.reduce((total, call) => total + call.cost_usd!, 0) };
}
