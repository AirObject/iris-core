# Iris Memory Core Console

独立 React + strict TypeScript + Vite 前端；页面 `/console/`，同源 API `/console/v1`。当前后端已有认证/密钥、已修复验证的读面及 State 创建/更正/过期、Note 创建/编辑/状态转换、Focus 创建/编辑/激活/状态转换、Task 创建/编辑/状态转换/步骤及依赖创建和解除；其他业务写入、统计、导入导出、Provider、Settings 和运维仍只有前端设计适配。2026-09-06 已修复类型漂移并适配 Memory 正式 descriptor，真实浏览器覆盖注册表、Note 列表/详情/历史与刷新；固定筛选与批量 Forget Operation 已按 Schema 20 接入正式契约、实际 Worker 与进度/取消页面，Event 取消投递也已接入严格契约和真实领域 CAS，保留投递历史及 Task 状态；上述切片的当前完整组合验收已通过，剩余业务模块尚未真实联调。

Persona 发布与回滚已按 [ADR-0044](../../docs/adr/0044-console-persona-publication.md) 整合，使用独立 Current 页面、正式命令元数据和 Persona/Policy 双 CAS。只读及发布/重新认证/回滚/刷新已通过主目录完整 CI、真实浏览器和独立安装验收；PersonaState 管理已按 [ADR-0045](../../docs/adr/0045-console-persona-state.md) 通过完整组合验收。Proposal 已按 [ADR-0046](../../docs/adr/0046-console-persona-proposals.md) 通过主目录完整 CI、创建/审批/拒绝真实浏览器和独立安装验收；Policy 管理已按 [ADR-0047](../../docs/adr/0047-console-persona-policy.md) 通过完整组合验收；Draft 管理仍未完成。 Persona 各类表单的并发差异提示已补齐并整合，专项验证通过、完整组合待运行，见 Phase14 验证报告。

## 启动与检查

```sh
cd web/console
npm ci
npm run dev          # 真实 API；默认代理到 127.0.0.1:8766
npm run dev:mock     # 显式模拟环境，持续显示模拟标识
npm run types:generate
npm run check       # 生成一致性、lint、strict typecheck、测试、生产构建
npm run test:browser # 先 build；一次性测试库和真实认证，业务 Fixture 另计
```

Node 22.12+；依赖和锁文件独立，当前锁文件使用公开 npm 镜像、固定版本与完整性校验。生成类型后仍须适配页面，不能以重新生成代替真实联调。根目录 `make ci` 已包含本工程检查、生产 build 与浏览器门禁。

`CONSOLE_BACKEND` 指定开发代理目标，代理不改写 Origin/Host。例如浏览器地址 `http://127.0.0.1:5173`，后端应显式配置 `--console-origin http://127.0.0.1:5173 --console-allowed-hosts 127.0.0.1 --console-dev-http`，仅用于回环明文开发，不开 CORS。启用 Console 安装 Python `console` extra；更多密钥、会话、监听和安全边界见 [统一设计](../../docs/design/console-backend.md#21-路由与启动)。

生产执行 `npm run build`，将 `web/console/dist` 的绝对路径交给 `serve --enable-console --console-assets ABSOLUTE_DIST`。声明真实外部 Origin、Host 与可信代理，不使用 Vite 开发服务器托管。CSP 要求同源外部脚本/样式，无内联脚本、eval、CDN、CSS-in-JS；前置代理不得把 `/console/v1/*` 404 转为 SPA 页面。生产 TLS/反代验收尚未完成。

浏览器测试可指定本机 Chrome，否则安装 Playwright Chromium：

```sh
CONSOLE_BROWSER_EXECUTABLE='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' npm run test:browser
```

## 工程边界

| 位置 | 责任 |
| --- | --- |
| `src/api/generated.d.ts`、`protocol.json` | 仅从 Console OpenAPI 生成，禁止手工编辑；types:check 校验漂移 |
| `src/api/client.ts` | 会话/CSRF/reauth、single-flight、幂等重放、错误、上传/下载 |
| `src/api/design.ts` | 未发布或尚未适配的业务模型，不能当作正式契约 |
| `src/components/`、`src/pages/` | 对话框、字段表单、状态与业务工作区 |
| `src/mock/` | 显式开发/测试状态模拟；生产构建 tree-shake/扫描，生产 mock 模式拒绝，请求失败不切 mock |
| `tests/`、`tests/browser/` | 客户端/组件测试，以及生产 CSP/真实认证与显式业务 Fixture 浏览器用例 |
| `tests/backend/serve_fixture.py` | 应用服务初始化临时库、离线签发测试密钥，监听回环；无认证依赖替换、不接生产库、不输出密钥，临时凭据文件 0600 |

协议、交互和安全约定集中在 [前端接入约定](../../docs/design/console-backend.md#13-前端接入约定)，功能状态见 [对接矩阵](./INTEGRATION_MATRIX.md)，历史通过及当前失败见 [验证记录](../../docs/reports/phase-13-verification.md)。模拟测试仅证明前端状态/请求编排，不能证明后端授权、领域事务、备份、Provider 出站或数据往返。

## Package version

The private frontend package version (`0.1.0`) is independent of Core and the Console HTTP contract. Runtime versions come from bootstrap metadata and generated protocol declarations; changing the frontend package label does not certify a Core release.
