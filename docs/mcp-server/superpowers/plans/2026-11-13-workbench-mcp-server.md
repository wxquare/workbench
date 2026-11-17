---
title: "Workbench MCP Server：实施与验证计划"
date: "2026-11-13"
---

## 目标与执行方式

按已确认 spec 实现独立 Python 3.12 MCP 服务。采用内联执行，各行为遵循 Red/Green/Refactor，领域无外围依赖，应用经端口使用连接器；仅交付 workbench-mcp-server，不修改 dod-agent。

## 约束

四个只读工具；stdio 和 Streamable HTTP；完整资源元组授权；不默认 live；env:// 凭据；20 秒默认总 deadline；最多 4 个全局和 2 个主体并发；无连接器自动重试；256 KiB MCP 输出与 2 MiB 下游响应上限。离线验证和真实业务验收分别记录。

## 工作清单

- [x] 领域与拒绝：tests/unit/test_domain.py 先定义跨元组拒绝、非法窗口和证据范围的失败测试；实现 domain/scope.py、identity.py、policy.py、evidence.py、errors.py。ScopePolicy.authorize(principal, tool, scope, source_scope) 拒绝时抛出领域错误，不访问网络。
- [x] 配置与身份：tests/integration/test_config.py 先覆盖未知字段、缺少 secret、重复 source、非法地址；实现 infrastructure/config、auth，配置转为领域不可变对象。check 不发网络请求。
- [x] 查询用例：tests/unit/test_application.py 覆盖拒绝零查询、empty/unavailable/timeout 区分、证据来源和审计；实现 application/queries.py、results.py、ports.py、use_cases。execute(principal, request) 返回 QueryOutcome。
- [x] 连接器：tests/contract 与 integration/test_http_connectors.py 覆盖实际请求载荷、认证、非 JSON、429、流式大小、知识 V2 字段映射；实现 normalized_http.py、knowledge_v2.py。使用受控本地 HTTP server，不连接公司系统。
- [x] MCP：tests/integration/test_mcp_stdio.py、test_mcp_http.py 先验证四工具发现、知识查询、非法 token；实现 interfaces/mcp、bootstrap、CLI。通过官方 ClientSession 验证序列化与 transport。
- [x] 完整性：增加游标范围/过期、防注入模板、Trace 跨服务、并发、取消、脱敏和输出截断测试，修复失败后运行完整测试；AST 校验依赖方向。
- [x] 交付：README、example.yaml、acceptance.md；构建 wheel，源码目录外安装执行，锁定依赖；复制最终文件到用户指定目录，保留已有 spec，记录真实业务尚未验收。

## 每项验证

先运行新测试确认缺失行为导致失败，再实现并运行对应测试；最后执行 `.venv/bin/python -m pytest -q`、ruff、构建及安装验证。关键 Red/Green 与最终结果写入 docs/acceptance.md；不记录凭据或原始业务数据。
