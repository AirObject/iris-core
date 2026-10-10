import { useEffect, useRef, useState } from "react";
import { apiDownload, errorText, json, useData } from "./api";
import { Badge, Empty, Notice, RoleTime } from "./ui";
import type { Backups as BackupList } from "./access-types";

function fileSize(bytes: number) {
  if (!Number.isFinite(bytes)) return "未记录";
  return bytes < 1024
    ? `${bytes} B`
    : bytes < 1024 ** 2
      ? `${(bytes / 1024).toFixed(1)} KiB`
      : `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
}
export function Backups() {
  const history = useData<BackupList>("/backups?limit=30", 5000);
  const [includeSecrets, setIncludeSecrets] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [download, setDownload] = useState<{
    url: string;
    filename: string;
    includesSecrets: boolean;
  } | null>(null);
  const active = useRef<AbortController | null>(null);
  useEffect(() => () => active.current?.abort(), []);
  useEffect(
    () => () => {
      if (download) URL.revokeObjectURL(download.url);
    },
    [download],
  );
  return (
    <>
      <section className="panel access-panel" aria-label="导出备份">
        <h2>导出备份</h2>
        <p>
          服务运行时即可导出数据库与媒体的一致快照。备份包含原始消息和记忆，请妥善保存。
        </p>
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            if (busy) return;
            const controller = new AbortController();
            active.current = controller;
            setBusy(true);
            setError("");
            setDownload(null);
            try {
              const { blob, headers } = await apiDownload("/backups/export", {
                ...json("POST", { include_secrets: includeSecrets }),
                signal: controller.signal,
              });
              if (controller.signal.aborted) return;
              const includesSecrets =
                headers.get("X-Iris-Backup-Includes-Secrets") === "true" ||
                includeSecrets;
              const id = headers.get("X-Iris-Backup-Id");
              const filename = `iris-backup${id && /^[a-f0-9]{32}$/.test(id) ? `-${id}` : ""}${includesSecrets ? "-with-secrets" : ""}.zip`;
              const url = URL.createObjectURL(blob);
              setDownload({ url, filename, includesSecrets });
              const link = document.createElement("a");
              link.href = url;
              link.download = filename;
              document.body.append(link);
              link.click();
              link.remove();
            } catch (e) {
              if (!controller.signal.aborted) setError(errorText(e));
            } finally {
              if (!controller.signal.aborted) {
                setBusy(false);
                history.refresh();
              }
            }
          }}
        >
          <label className="check-label">
            <input
              type="checkbox"
              checked={includeSecrets}
              disabled={busy}
              onChange={(e) => setIncludeSecrets(e.target.checked)}
              aria-describedby="backup-key-help"
            />
            包含模型密钥
          </label>
          <p id="backup-key-help" className="muted">
            默认不包含模型密钥。选择包含时只导出本实例已保存的密钥，不包含外部模型配置文件或环境变量中的密钥。
          </p>
          {includeSecrets && (
            <Notice error>
              <strong>包含模型密钥的 ZIP 不加密。</strong>
              请只保存到可信位置，不要分享或提交到代码仓库。
            </Notice>
          )}
          <button className="primary" disabled={busy}>
            {busy
              ? "正在导出，请稍候…"
              : includeSecrets
                ? "导出并下载（含模型密钥）"
                : "导出并下载备份"}
          </button>
        </form>
        {error && (
          <Notice error>{error}。如未取得备份文件，请重新导出。</Notice>
        )}
        {download && (
          <Notice>
            已生成备份并请求浏览器下载。若下载未开始，可
            <a href={download.url} download={download.filename}>
              保存本次备份
            </a>
            。
            {download.includesSecrets && (
              <p>
                <strong>本次备份含未加密的模型密钥，请勿分享。</strong>
              </p>
            )}
          </Notice>
        )}
        <h3>最近的导出记录</h3>
        <p className="muted">
          最多显示最近 30 次。记录不保留可再次下载的文件，需要时请重新导出。
        </p>
        {history.error && (
          <Notice error>
            {history.error}
            <button onClick={history.refresh}>重试导出记录</button>
          </Notice>
        )}
        {!history.data && !history.error && <p>正在读取导出记录…</p>}
        {history.data?.items.length === 0 && <Empty title="还没有导出记录" />}
        <div className="access-records">
          {history.data?.items.map((record) => (
            <article className="access-record" key={record.backup_id}>
              <div className="panel-heading">
                <h4>
                  <RoleTime value={record.created_at} />
                </h4>
                <Badge
                  tone={record.includes_model_secrets ? "danger" : "green"}
                >
                  {record.includes_model_secrets
                    ? "含模型密钥"
                    : "不含模型密钥"}
                </Badge>
              </div>
              <p>
                {fileSize(record.size)} · {record.file_count ?? "未记录"} 个文件
                · {record.media_included ? "含媒体" : "无媒体"}
              </p>
              <p>
                格式版本 {record.format_version ?? "未记录"} · 迁移版本{" "}
                {record.migration_version ?? "未记录"}
              </p>
              <p>
                {record.purpose === "pre_import"
                  ? "导入前自动备份"
                  : "手动导出"}{" "}
                ·{" "}
                {record.actor === "admin"
                  ? "管理员"
                  : record.actor === "local_cli"
                    ? "本机命令行"
                    : record.actor}
              </p>
              <p className="muted">
                备份 ID：<code>{record.backup_id}</code>
              </p>
            </article>
          ))}
        </div>
        <p>
          <a href="#/operations?object_type=backup">查看导出与导入操作记录</a>
        </p>
      </section>
      <section className="panel access-panel" aria-label="离线导入">
        <h2>从备份导入</h2>
        <p>
          导入需要停止 Iris
          服务，在本机命令行操作。先确认备份与目标数据目录，再按以下步骤恢复。
        </p>
        <ol className="import-steps">
          <li>停止 Iris 服务和其他配置命令。</li>
          <li>
            将示例路径替换为自己的路径。导入到新目录：
            <pre>
              <code>
                uv run iris --data-dir /path/to/new-data backup import
                /path/to/backup.zip
              </code>
            </pre>
          </li>
          <li>
            覆盖已有实例时，确认目标后添加覆盖参数：
            <pre>
              <code>
                uv run iris --data-dir /path/to/iris-data backup import
                /path/to/backup.zip --confirm-overwrite
              </code>
            </pre>
            覆盖前会在数据目录旁自动备份现有实例；备份失败不会覆盖。
          </li>
          <li>导入完成后按原方式启动服务，检查记忆、媒体与模型设置。</li>
        </ol>
        <Notice>
          不含模型密钥的备份导入后，需要重新配置密钥，或继续使用原有外部配置；不会沿用目标目录原来的模型密钥。
        </Notice>
        <p>
          覆盖前的自动备份默认也不含模型密钥。如需保留现有密钥，在导入命令后增加{" "}
          <code>--backup-include-secrets</code>，并按含密钥备份妥善保存。
        </p>
        {history.data?.import_instructions && (
          <p className="muted">
            服务提供的导入说明：{history.data.import_instructions}
          </p>
        )}
      </section>
    </>
  );
}
