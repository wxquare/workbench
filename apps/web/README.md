# Workbench 管理界面

工作台支持通过 Agent API 提交知识问答并查看已保存的任务状态和结果。输入由服务端验证的 Bearer Token，选择可访问的 Workspace，填写问题及查询范围。令牌仅保存在当前浏览器标签页的 sessionStorage 中；清除或关闭标签页后需要重新输入。查询时间按设备本地时区输入，并转换为 UTC。

任务记录和其他管理页面仍为示例或占位，不代表实际系统状态。真实任务详情使用 `/app/runs/{id}`，可刷新或直接打开；示例详情使用 `/app/demo/runs/{id}`。

知识问答需要后端支持从 `business_context.query_scope` 建立持久化查询范围，以及已发布的 QA workflow、模型、知识工具和相应授权。前端不直接调用模型或知识源。
实际接入和发布 `dod.v16` 的步骤见 [知识问答配置说明](../agent/KNOWLEDGE_QA_SETUP.md)。默认 Workspace 不会自动切换；配置或代码变更后需重启相关 Agent 进程。

## 本地启动

需要 Node.js LTS 和 npm，Agent API 默认运行在 `127.0.0.1:8000`。Vite 开发服务器将 `/v1` 代理到该地址。

```bash
cd apps/web
npm ci
npm run dev
```

打开终端打印的 `/app/` 地址。也可以构建后由 Agent API 在 `/app/` 提供页面；设置 `WORKBENCH_FRONTEND_DIST_ROOT` 指向 `apps/web/dist`。

```bash
npm test
npm run typecheck
npm run build
```
