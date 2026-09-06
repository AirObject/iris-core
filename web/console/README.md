# Iris Memory Core Console

独立 React + strict TypeScript + Vite 前端；页面 `/console/`，同源 API `/console/v1`。当前后端已有认证/密钥与未验收读面；业务写入、统计、导入导出、Provider、Settings 和运维仍只有前端设计适配。**2026-09-06 核查发现生成类型漂移，Memory 页尚未适配正式 descriptor，当前工程不能宣称已完成真实业务联调。**

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

Node 22.12+；依赖和锁文件独立，当前锁文件使用公开 npm 镜像、固定版本与完整性校验。生成类型后仍须适配页面，不能以重新生成代替真实联调。根目录 `make ci` 当前不含本工程检查；发布时必须执行独立前端门禁。

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
