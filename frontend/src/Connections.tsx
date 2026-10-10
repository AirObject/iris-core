import { HostTokens } from "./HostTokens";
import { Backups } from "./Backups";

export default function Connections() {
  const address = `http://127.0.0.1${location.port ? `:${location.port}` : ""}/api/v1`;
  return (
    <>
      <div className="page-heading">
        <div>
          <p className="eyebrow">接入与备份</p>
          <h1>接入与数据</h1>
          <p>连接同一台电脑上的宿主，管理令牌与实例备份。</p>
        </div>
      </div>
      <section className="panel access-panel" aria-label="接入说明">
        <h2>把宿主连到 Iris</h2>
        <p>
          宿主程序与 Iris
          在同一台电脑上运行。先创建令牌，在宿主配置中保存，再访问{" "}
          <code>{address}</code>。端口与当前 Iris 服务一致。
        </p>
        <p>
          请求使用 <code>Authorization: Bearer &lt;令牌&gt;</code>
          。管理员登录会话不能代替宿主令牌；Iris
          的宿主接口负责接收消息与准备回复，不生成对外回复。
        </p>
        <div className="button-row">
          <a href="/docs" target="_blank" rel="noreferrer">
            打开接口文档
          </a>
          <a href="/openapi.json" target="_blank" rel="noreferrer">
            查看 OpenAPI 契约
          </a>
        </div>
      </section>
      <HostTokens />
      <div className="access-data-grid">
        <Backups />
      </div>
    </>
  );
}
