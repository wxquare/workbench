---
title: "DeepSeek：M2 模型能力验收"
---

## 真实服务验证

2026-11-13 UTC 在项目本地环境运行 `tests/contract/live/test_deepseek.py --run-live`，结果 **7 passed**。使用 `https://api.deepseek.com`、请求别名 `deepseek-flash`，服务返回模型元数据 `deepseek-flash`；依赖锁定 `langchain-deepseek 1.1.1`，SDK 自动重试关闭。别名和响应元数据并不证明底层权重可复现。

真实调用覆盖文本和 usage、一个仅在本地执行的加法工具及后续模型回复、流式增量、客户端提前关闭流、JSON，以及带 SQLite 账本的文本、JSON ModelInvoker 和版本化 ModelProbe。取消证据仅证明客户端关闭流；未验证服务端立即停止计算或计费。真实账本记录了 provider usage；未提供定价时费用保持 pending，而不是记为零。

该探针是小规模非业务写入。测试只断言结构和状态，不输出凭据、请求正文或模型响应正文。用户应通过自己的运行环境向 Worker 注入引用对应的凭据，不要将凭据写入版本控制、报告或 Job。

## 配置与运行边界

模型配置的创建、PATCH 和发布接口使用版本与 `If-Match`，发布生成不可变版本；探针接口仅提交持久化 `model_probe` 管理 Job。管理 Job 由 Worker 独立领取，结果包含固定版本/hash、配置状态、能力状态和响应模型元数据；`client_stream_close` 表示客户端流已关闭，`remote_cancel` 始终标记为 unknown。可以通过工作空间管理 Job 接口查询结果。

部署必须在 `service.model_credential_bindings` 中为每个团队和工作空间明确授权 `base_url` 与 `secret_ref` 的精确组合；Worker 在解析凭据前验证绑定，默认空列表拒绝调用。`config/local.yaml` 仅授权默认开发工作空间访问 DeepSeek 的本地引用。修改模型草稿或使用其他 HTTPS 地址不会自动获得凭据使用权。

API 的默认引用可用性检查只遍历环境变量名称，不读取值；Worker 才解析 `env://` 实际值。API 与 Worker 在不同主机或容器时，必须提供一致的引用库存；`create_app(..., secret_availability=...)` 接受实现 `available(reference)` 的检查器。默认实现不支持 `secret://`；使用外部秘密服务时需接入与 Worker 配套的引用检查器和 SecretProvider。名称存在不能保证值非空，也不能保证上游鉴权成功；运行时仍可能返回 `MODEL_CREDENTIAL_UNAVAILABLE`。

默认总调用上限为三次实际网络尝试，含 JSON 修正；每次尝试在发送前单独提交账本，未知 usage 不释放预留。最终汇总保留至少一次调用，预算不足时上层应生成确定性 partial。模型不自动切换。流关闭不等于远端停止；计量未知时须保留待对账状态。
