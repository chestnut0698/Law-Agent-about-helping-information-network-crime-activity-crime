---
name: agent-role-model
description: 实现信证智析五类模型工作角色、统一模型网关、角色输入范围、结构化输出和运行日志。用于 Plan、安全门控、提取映射、受控辅助或独立校核模型调用。
disable-model-invocation: true
---

# 五角色模型调用规范

## 统一要求

- 外部模型只能经服务端网关调用。
- 默认模型为项目配置的 DeepSeek 版本，不在业务代码硬编码模型名。
- 每个角色使用独立 Prompt、输入范围、工具权限和 Pydantic Schema。
- 超时、限流和 Schema 失败最多自动重试一次。
- 生产案卷不得进入训练、Prompt 调试或未授权评测。
- 不保存未脱敏 Prompt 正文；保存脱敏输入哈希和结构化结果。

每次调用写 `agent_runs`：

- `case_id`
- `plan_item_id`
- `role`
- `model_name`
- `model_version`
- `prompt_template_version`
- `redacted_input_hash`
- `structured_output`
- `token_usage`
- `latency_ms`
- `cost`
- `retry_count`
- `status`
- `error_code`

## 角色一：plan_supervisor

用于 LA-005、LA-011 和 LA-015 的 REPLAN 提案。

输入：

- 用户意图；
- 案件状态摘要；
- 材料清单；
- 成果有效性；
- 白名单动作；
- 预算和人工确认条件。

输出：

- 结构化 Plan DAG；
- 保留和失效成果；
- 追问；
- 完成条件。

禁止：

- 白名单外动作；
- 直接写业务表；
- 省略强制步骤；
- 输出事实、定罪或量刑结论。

## 角色二：safety_gate

用于 LA-004。

输入仅限：

- 用户请求；
- 任务类型；
- 最少案件元数据。

不得输入案卷正文。

输出：

```json
{
  "classification": "ALLOW|CLARIFY|DENY",
  "reason_codes": [],
  "clarifying_questions": [],
  "alternatives": [],
  "rule_version": ""
}
```

## 角色三：extraction_mapper

用于 LA-006、LA-007，以及 LA-009 的缺口自然语言说明。

输入：

- 脱敏片段；
- 事实 Schema；
- 要素模板及版本；
- 允许枚举。

输出：

- 事实、评价和未确认信息；
- 实体及关系；
- 原文引用；
- 要素映射。

禁止：

- 无片段引用字段；
- 推测补全；
- 自由长篇法律结论；
- 计算金额或规则区间。

## 角色四：controlled_assistant

用于 LA-012 规则条件解释、LA-013 类案差异、LA-015 依据问答。

输入：

- 已确认事实；
- 已发布法律规则或授权类案；
- 允许的输出模板。

输出的每个主张必须有引用。

禁止：

- 引用检索结果之外的法条；
- 直接计算量刑；
- 用类案均值推导当前案件；
- 在问答中直接写库。

## 角色五：independent_verifier

用于 LA-014，也可作为提取步骤后置检查。

输入：

- 原文片段；
- 待校核结构化输出；
- 引用；
- 允许和禁止表述规则。

不得输入前序模型隐性推理。

输出：

```json
{
  "passed": false,
  "issues": [
    {
      "code": "",
      "severity": "LOW|MEDIUM|HIGH|BLOCKING",
      "target_field": "",
      "suggested_action": ""
    }
  ]
}
```

## Prompt 管理

- Prompt 模板存入版本化文件，不在函数中拼接大段常量。
- Prompt 版本与 `agent_runs` 绑定。
- 要素模板、法律规则或 Prompt 变化时触发回归评测。
- Prompt 只要求可审计结构化结果，不索取或保存隐性推理。

## 网关要求

网关负责：

- 脱敏确认检查；
- 角色允许输入检查；
- 模型参数；
- 超时、重试和限流；
- Pydantic 解析；
- 调用审计和费用；
- 敏感日志过滤；
- 测试适配器注入。

业务服务不得绕过网关直接调用 HTTP API。

## 验收清单

- [ ] 五角色有独立 Schema
- [ ] 安全门控不含案卷正文
- [ ] 提取结果强制原文引用
- [ ] 辅助角色只使用已发布知识
- [ ] 校核角色与前序隐性上下文隔离
- [ ] 每次调用有完整运行记录
- [ ] 客户端无法直连模型
- [ ] Prompt 变更可触发回归
- [ ] Schema 失败不直接入库
