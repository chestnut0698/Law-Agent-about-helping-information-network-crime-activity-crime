---
name: plan-orchestration
description: 实现信证智析受控 Plan 状态机、白名单 DAG、检查点、REPLAN、成果失效和局部重算。用于 LA-005、LA-008、LA-011、LA-015 或 Plan 执行器相关工作。
disable-model-invocation: true
---

# Plan 状态机与增量重规划

## 定位

Plan 是任务控制器，不直接修改业务表，不作事实、定罪或量刑判断。所有动作由 Python 执行器在权限、Schema、版本、依赖和预算校验后提交。

## 四种模式

- `INITIAL_PLAN`：首次材料审查，用户确认后执行。
- `REPLAN`：材料、事实或实体变化后的影响分析和局部重算。
- `RESUME_PLAN`：从检查点恢复，跳过仍有效的已完成步骤。
- `POST_WORKFLOW_QA`：完成后的只读问答；写意图退出问答并生成 REPLAN。

## 白名单

只允许 PRD 第 9 节定义的动作。白名单必须同时存在于 Python 枚举和配置文件，应用启动时检查一致性。

至少禁止：

- 模型自造动作；
- 跳过本地脱敏；
- 跳过 `BIND_EVIDENCE`；
- 跳过 `VERIFY_OUTPUT`；
- Plan Agent 直接写数据库。

## DAG 校验

生成后、持久化前依次检查：

1. 动作均在白名单；
2. `item_id` 唯一；
3. 所有依赖存在；
4. 无环；
5. 强制步骤完整；
6. 可选步骤被关闭后无悬空依赖；
7. 每项输入引用存在或由上游产出；
8. 步骤数、模型调用和预算不超过上限。

任何一项失败都不得进入执行队列。

## 执行监督

### 每步执行前

- 依赖已完成；
- 输入材料和成果存在且有效；
- 当前用户/系统身份有权限；
- 相关文档已脱敏并批准外呼；
- 人工确认条件满足；
- 预算充足；
- 同一案件没有另一个修改型 Plan。

### 每步执行后

- 输出符合 Pydantic Schema；
- 引用可反向定位；
- 必填字段完整；
- 无越界表述；
- 新冲突已记录；
- 满足本步骤验收标准；
- 写入成果版本、输入快照和运行记录。

### 失败处理

按顺序选择：

1. 自动重试一次；
2. 确定性修复；
3. 创建 REPLAN；
4. 请求人工复核；
5. `WAITING_USER` 或 `BLOCKED`；
6. 达到上限后 `FAILED`。

不得无限循环。

## 状态

Plan 项：

`PENDING`、`RUNNING`、`COMPLETED`、`NEEDS_REVIEW`、`WAITING_USER`、`STALE`、`BLOCKED`、`FAILED`、`SKIPPED`、`CANCELLED`。

成果：

`VALID`、`PENDING_REVIEW`、`STALE`、`INVALID`。

## REPLAN 流程

1. 安全完成当前原子步骤；
2. 保存检查点；
3. 解析和脱敏新材料；
4. 计算片段新增、修改、删除；
5. 反向遍历成果依赖图；
6. 生成 `stale_artifacts` 和 `preserved_artifacts`；
7. 向用户展示影响预览；
8. 用户确认后再标记过期；
9. 创建递增 Plan 版本；
10. 从最早受影响动作执行；
11. 展示成果差异；
12. 比较增量和同版本全量重算结果。

用户未确认影响预览时不得标记成果或开始重算。

## 依赖传播基线

```text
document_version
  → chunk
  → fact/entity/evidence_link
  → element_assessment
  → gap/conflict
  → timeline/relation
  → sentencing/retrieval
  → report
```

仅依赖变化节点的下游成果过期。身份事实没有依赖新增银行流水时必须继续保持有效。

## 完成条件

只有同时满足以下条件才允许 `completed`：

- 必做项完成或批准；
- 阻塞级冲突已处理；
- 关键人工确认完成；
- 正式成果不含 `STALE/INVALID`；
- 输出校核通过；
- 未超预算；
- 无未处理高风险安全事件。

## 验收清单

- [ ] 非法动作硬拒绝
- [ ] DAG 无环且依赖完整
- [ ] 强制步骤不可关闭
- [ ] 下游不能越过依赖
- [ ] 重试不超过一次
- [ ] 暂停只在原子步骤结束后生效
- [ ] 恢复不重复有效步骤
- [ ] 影响预览未确认不重算
- [ ] 过期成果不能进报告和量刑
- [ ] 增量与全量结果一致
- [ ] QA 写意图只生成 REPLAN
