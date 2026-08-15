---
name: fact-evidence-contract
description: 约束信证智析事实、评价、实体、原文片段、证据链接和待证要素的数据契约。用于 LA-002、LA-006 至 LA-011、Schema、原文绑定或事实证据映射工作。
disable-model-invocation: true
---

# 可追溯事实—证据数据契约

## 三类信息必须分离

- `OBJECTIVE_FACT`：材料中可定位的主体、行为、时间、金额等具体记载。
- `MATERIAL_OPINION`：材料中某个主体的判断性表达。
- `UNCONFIRMED_INFORMATION`：信息不足、模糊或无法可靠归类。

“存在异常交易”属于材料评价；具体转账时间和金额属于客观事实。评价必须保存评价主体和 `SOURCE_OF_OPINION` 原文链接。

## 原文片段契约

每个可引用片段至少包含：

- `document_version_id`
- `page_no`
- `block_no`
- `char_start`
- `char_end`
- `raw_text_hash`
- `redacted_text`
- 可用时保存 `bbox`

事实引用必须包含 `chunk_id`、`quote`、`quote_hash` 和定位信息。

## 模型输出入库流水线

```text
脱敏片段
→ 模型 JSON
→ Pydantic Schema
→ quote 反向匹配
→ 金额/日期/主体存在性校验
→ 越界表述扫描
→ PENDING_REVIEW
→ 人工复核
→ CONFIRMED
```

任一校验失败：

- 不得写入已确认事实；
- 保存错误码和原始结构化输出；
- 按 Plan 规则重试或转人工复核。

## 证据链接语义

- `SUPPORTS`：材料记载与事实命题方向一致，仅表示“有对应记载”。
- `CONTRADICTS`：材料存在反向记载。
- `MENTIONS`：中性提及。
- `SOURCE_OF_OPINION`：材料评价来源。

`SUPPORTS` 不表示证据能力、真实性、证明力或证明标准。

## 待证要素

四个维度：

- `OBJECTIVE_ASSISTANCE`
- `SUBJECTIVE_KNOWLEDGE_RELATED`
- `SERIOUS_CIRCUMSTANCE_RELATED`
- `PROCEDURE_AND_SENTENCING`

允许状态：

- `NOT_REVIEWED`
- `MATERIALS_FOUND`
- `NO_MATERIAL_FOUND`
- `CONFLICTING_MATERIALS`
- `INSUFFICIENT_METADATA`
- `PENDING_HUMAN_REVIEW`

用户界面和报告禁止用“已证明、不成立、证据不足、构成犯罪”表示系统实体判断。

## 数值与时间

- 金额使用 Decimal，保存币种和单位，禁止 float。
- 时间保存开始、结束和精度。
- 时间精度至少支持准确日期、月份、区间和未知。
- 日期冲突并列显示，不自动选正确值。
- 情节严重阈值必须来自已发布法律规则，不得由模型生成。

## 实体

至少支持：

`PERSON`、`ORGANIZATION`、`BANK_ACCOUNT`、`PAYMENT_ACCOUNT`、`BANK_CARD`、`PHONE_NUMBER`、`DEVICE`、`SIM_CARD`、`IP_ADDRESS`、`LOCATION`。

实体合并和拆分必须：

- 保留原实体版本；
- 重新指向下游引用；
- 生成影响预览；
- 记录人工复核和审计。

## 未找到信息

统一表示为空值和“未发现”。禁止：

- 用常识补齐；
- 从其他案件迁移；
- 用默认金额、时间或主体；
- 将低置信度推断当作事实。

置信度只用于复核排序，不代表事实真伪。

## 数据不变量

1. 模型事实至少绑定一个可反向定位片段。
2. 原文定位包含文档版本、页码、块、字符区间和哈希。
3. 未找到不得推测。
4. 修正不得覆盖历史。
5. 文档替换后旧脱敏确认失效。
6. 上游片段变化后相关事实至少 `PENDING_REVIEW` 或 `STALE`。
7. `STALE/INVALID` 不进入正式报告或量刑。
8. 量刑只使用用户选择且达到确认级别的事实。
9. 地区、时间、规则有效期不匹配时拒绝计算。
10. Plan 依赖未完成不得执行下游。
11. 自动重试受预算限制。
12. 问答写意图转 REPLAN。
13. 删除/替换材料先影响分析。
14. 模型输出不能直写确认表。
15. 正式导出绑定案件、Plan、规则和报告版本。

## 验收清单

- [ ] 事实/评价/未知分离
- [ ] 每项事实可回链
- [ ] 虚构数值被拒绝
- [ ] 支持与反向材料分组
- [ ] 删除映射不删除原始事实
- [ ] 无来源事实不能有效映射
- [ ] 低置信度进入复核队列
- [ ] 时间冲突不自动消歧
- [ ] 无来源关系不能确认
- [ ] 15 条不变量有自动化覆盖
