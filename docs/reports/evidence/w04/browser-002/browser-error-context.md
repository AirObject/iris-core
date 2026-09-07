# Page snapshot

```yaml
- main [ref=e3]:
  - generic [ref=e4]: I
  - paragraph [ref=e5]: IRIS MEMORY CORE
  - heading "管理控制台" [level=1] [ref=e6]
  - paragraph [ref=e7]: 使用离线签发的运营密钥建立同源会话。
  - generic [ref=e8]:
    - generic [ref=e9]:
      - text: 运营密钥
      - textbox "运营密钥" [ref=e10]
    - alert [ref=e11]:
      - text: 请求受限；请在 33 秒后重试。
      - generic [ref=e12]: "access_denied / rate_limited · 请求 01a07d52-f1cf-7acb-bc41-40fff82b5197 · {\"kind\":\"rate_limited\"}"
    - button "登录控制台" [disabled] [ref=e13]
  - paragraph [ref=e14]: 密钥只用于本次认证，登录后由 HttpOnly Cookie 承载会话。未发布的业务模块不会出现在真实环境导航中。
```