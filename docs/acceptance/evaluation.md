# 人工质量评估

`dod evaluate --dataset <manifest.json> --output <report.json>` 离线统计人工审核结果，不调用模型或上传数据。未达到发布门槛时报告仍会写出，命令返回非零。

数据集固定使用 `cases` 列表，每项需要唯一 `id`。仅 `review_status` 为 `reviewed` 且 `reviewer` 和案例来源 `source` 非空的标签计为已审核；人工确认的根因填写非空 `confirmed_root_cause` 和非空证据引用列表 `root_cause_evidence_refs`。每个案例的 `result_review` 包含审核人 `reviewer`、输出引用 `output_ref`、布尔值 `output_present`、`evidence_reference_correct`、`answer_usable`、`unavailable_reported_as_normal`、`unauthorized_access_missed`，确认根因的案例还需要布尔值 `root_cause_match`。这些字段由人工根据实际输出和证据填写，不由模型自评充当真值。

所有清单案例均进入证据引用与可用回答比例的分母，所有已确认根因案例进入根因匹配比例的分母。缺少人工标签、输出或任何必需评审字段均计入 `missing_reviews` 并阻止发布；重复 ID 被拒绝。统计只反映输入的有限审核样本，未审核的候选不能算通过。安全漏检计数仅包含已审核结果，必须结合 `missing_reviews` 判断覆盖。

报告的 `status` 为 `passed`、`failed` 或 `unavailable`：有已审核安全漏检时为失败；数据为空或存在缺失审核时为未验收；其余按质量门槛判定。
