---
title: "合成接入：本地聊天与告警测试"
---

## 用途与限制

`synthetic-chat-v1` 与 `synthetic-alert-v1` 是开发和测试专用协议，并非 SeaTalk 或任何实际告警平台的协议。生产环境启动时拒绝这些绑定。真实平台需要另外核验签名、身份映射、challenge 和回调契约；不得把本适配器暴露给生产流量。

## 部署配置

在 `service.synthetic_ingress` 中显式配置连接器名称、`protocol`、`team_id`、`agent_id`、`secret_ref`；聊天还需 `actors`（外部身份到当前团队主体的映射），告警需固定 `actor_subject`。`secret_ref` 只接受 `env://变量名`，真实值由部署环境注入，不写入版本库。每个映射主体还需在 `service.connector_identities` 中有匹配 team 和 agent 的有效部署绑定；该主体也必须是当前有效团队成员且有 operator 权限。

HTTP 入口为 `POST /v1/ingress/{connector_id}`。测试签名为 HMAC-SHA256，输入是五个按顺序排列的长度前缀字段：`DOD-SYNTHETIC-MAC-V2`、协议名、URL 中的连接器名、十进制秒时间戳和未修改的原始请求体。每个字段以四字节无符号大端长度开头；旧的未绑定签名一律拒绝。请求头为 `x-dod-timestamp` 和十六进制 `x-dod-signature`。签名时间偏差不超过 5 分钟，请求体不超过 64 KiB；事件 `occurred_at` 为 Unix 秒，超过 7 天的事件拒绝并要求显式补录（补录入口尚未实现）。

聊天事件包含 `event_id`、`thread_id`、`actor`、`text` 和 `occurred_at`。不带 `resume` 的事件始终创建新的排队 Run，并使用来源线程映射；不会自动填充最近的等待点。需要恢复时必须显式传入 `resume`，包括 `run_id`、`interrupt_id`、`expected_revision` 和符合该中断 Schema 的 `response`。服务验证来源线程、当前身份、中断指定主体及检查点，事件收据和恢复请求在同一事务提交。重复事件返回同一受理结果，变更内容会冲突。事件与线程映射保留 90 天；实际来源平台的更长线程保留策略待设计。

## 尚未完成

真实 SeaTalk challenge、回调签名与事件 Schema、告警平台协议、不同来源正文和链接冲突归一、显式历史补录及实际环境验收均未实现。不要把合成测试结果记录为真实连接成功，也不要在回调中发送消息；外部通知属于后续 Outbox 交付。
