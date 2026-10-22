import type { DeviceAction, ExecutorContext } from '@midscene/core';
import { z } from 'zod';
import type { RunState } from './state.js';
import { safeActionParameters, safeFailedActionParameters } from './privacy.js';

/** @brief 注册项在 SDK 动作之外声明结果校验和变更语义。 */
export interface RegisteredTool {
  /** 提供给规划器的标准 Midscene 动作。 */
  action: DeviceAction;
  /** 原始结果的运行时结构约束。 */
  resultSchema: z.ZodTypeAny;
  /** 成功后是否使旧观察失效。 */
  mutates: boolean;
}

/** @brief 显式注册工具；扩展时添加注册项，不改变 agent 循环。 */
export class ToolRegistry {
  private tools = new Map<string, RegisteredTool>();

  /** @brief 注册唯一名称，重复注册直接报错。 */
  register(tool: RegisteredTool): void {
    if (this.tools.has(tool.action.name)) {
      console.error(`Duplicate tool: ${tool.action.name}`);
      throw new Error(`Duplicate tool: ${tool.action.name}`);
    }
    this.tools.set(tool.action.name, tool);
  }

  /** @brief 返回可用于静态配置校验的完整名称。 */
  names(): string[] { return [...this.tools.keys()]; }

  /** @brief 仅向模型公开项目启用的动作，所有动作走同一执行边界。 */
  actions(state: RunState): DeviceAction[] {
    return state.config.project.tools.enabled.map((name) => {
      const tool = this.tools.get(name);
      if (!tool) { console.error(`Unknown tool: ${name}`); throw new Error(`Unknown tool: ${name}`); }
      return { ...tool.action, call: (parameters, context) => this.invoke(tool, parameters, state, context) };
    });
  }

  /** @brief 校验结果、反馈规划器并记录失败，不吞掉错误。 */
  private async invoke(tool: RegisteredTool, parameters: any, state: RunState, context?: ExecutorContext) {
    try {
      state.enter(tool.action.name);
      const result = tool.resultSchema.parse(await tool.action.call(parameters, context));
      if (tool.mutates) state.revision += 1;
      const feedback = JSON.stringify(result ?? { completed: true });
      if (context) context.task.planningFeedback = feedback;
      state.event('action_finished', { name: tool.action.name,
        parameters: safeActionParameters(tool.action.name, parameters), result });
      return result;
    } catch (error) {
      const message = String(error);
      console.error(`Tool ${tool.action.name} failed: ${message}`);
      const diagnostics = error instanceof Error && 'diagnostics' in error ? error.diagnostics : undefined;
      state.event('action_failed', { name: tool.action.name, error: message,
        parameters: safeFailedActionParameters(tool.action.name, parameters), diagnostics });
      state.controller.abort(new Error(message));
      throw error;
    } finally { state.leave(); }
  }
}
