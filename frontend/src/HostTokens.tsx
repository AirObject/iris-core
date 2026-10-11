import { useState } from "react";
import { api, errorText, json, useData } from "./api";
import { Badge, Dialog, Empty, Notice, RoleTime } from "./ui";
import type { Entry } from "./types";
import type { HostToken, TokenList, TokenScope } from "./access-types";

function Scope({ value }: { value: TokenScope }) {
  return value.kind === "all" ? (
    <span>全部入口</span>
  ) : value.kind === "prefix" ? (
    <span>
      入口前缀：<code>{value.prefix}</code>
    </span>
  ) : (
    <span>
      入口列表：
      {value.entries.map((id, i) => (
        <span key={id}>
          {i > 0 && "、"}
          <code>{id}</code>
        </span>
      ))}
    </span>
  );
}
export function HostTokens() {
  const list = useData<TokenList>("/tokens", 5000);
  const [create, setCreate] = useState(false);
  const [revoking, setRevoking] = useState<HostToken | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  return (
    <section className="panel access-panel" aria-label="宿主令牌">
      <div className="panel-heading">
        <h2>宿主令牌</h2>
        <button
          className="primary"
          onClick={() => {
            setMessage("");
            setCreate(true);
          }}
        >
          创建宿主令牌
        </button>
      </div>
      <p>
        每个宿主使用自己的令牌。入口范围控制该宿主可以操作哪些入口，不是记忆可见范围设置。
      </p>
      {list.data && (
        <p className="muted">
          每令牌限流：每秒 {list.data.rate_limits.rate_per_second} 次，突发上限{" "}
          {list.data.rate_limits.burst} 次。
        </p>
      )}
      {message && <Notice>{message}</Notice>}
      {list.error && (
        <Notice error>
          {list.error}
          <button onClick={list.refresh}>重试令牌列表</button>
        </Notice>
      )}
      {!list.data && !list.error && <p>正在读取令牌…</p>}
      {list.data?.items.length === 0 && (
        <Empty title="尚未创建宿主令牌">
          创建令牌后，将它配置到同一台电脑上的宿主程序。
        </Empty>
      )}
      <div className="access-records">
        {list.data?.items.map((token) => (
          <article
            className="access-record"
            key={token.id}
            aria-label={`${token.host} · ${token.id}`}
          >
            <div className="panel-heading">
              <h3>{token.host}</h3>
              <Badge tone={token.revoked_at ? "" : "green"}>
                {token.revoked_at ? "已撤销" : "有效"}
              </Badge>
            </div>
            <p>
              <Scope value={token.scope} />
            </p>
            <dl className="access-facts">
              <div>
                <dt>创建时间</dt>
                <dd>
                  <RoleTime value={token.created_at} />
                </dd>
              </div>
              <div>
                <dt>最近使用</dt>
                <dd>
                  {token.last_used_at ? (
                    <RoleTime value={token.last_used_at} />
                  ) : (
                    "尚未使用"
                  )}
                </dd>
              </div>
              {token.revoked_at && (
                <div>
                  <dt>撤销时间</dt>
                  <dd>
                    <RoleTime value={token.revoked_at} />
                  </dd>
                </div>
              )}
            </dl>
            <p className="muted">
              令牌 ID：<code>{token.id}</code>
            </p>
            {!token.revoked_at && (
              <button
                className="danger-button"
                onClick={() => {
                  setError("");
                  setRevoking(token);
                }}
              >
                撤销 {token.host} 的令牌
              </button>
            )}
          </article>
        ))}
      </div>
      <p>
        <a href="#/operations?object_type=host_token">查看令牌操作记录</a>
      </p>
      {create && (
        <CreateToken close={() => setCreate(false)} created={list.refresh} />
      )}
      {revoking && (
        <Dialog
          title="撤销宿主令牌"
          onClose={() => setRevoking(null)}
          closeDisabled={busy}
        >
          <div className="access-dialog">
            <p>
              即将撤销 <strong>{revoking.host}</strong> 的令牌（{revoking.id}
              ）。后续宿主请求立即失效，无法恢复此令牌；再次接入需要创建新令牌。
            </p>
            {error && <Notice error>{error}</Notice>}
            <div className="button-row">
              <button disabled={busy} onClick={() => setRevoking(null)}>
                取消
              </button>
              <button
                className="danger-button"
                disabled={busy}
                onClick={async () => {
                  if (busy) return;
                  setBusy(true);
                  setError("");
                  try {
                    await api(
                      `/tokens/${encodeURIComponent(revoking.id)}/revoke`,
                      json("POST", {}),
                    );
                    setRevoking(null);
                    setMessage("令牌已撤销");
                    list.refresh();
                  } catch (e) {
                    setError(errorText(e));
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                {busy ? "正在撤销…" : "确认撤销"}
              </button>
            </div>
          </div>
        </Dialog>
      )}
    </section>
  );
}
function CreateToken({
  close,
  created,
}: {
  close: () => void;
  created: () => void;
}) {
  const catalog = useData<{ entries: Entry[] }>("/catalog");
  const [host, setHost] = useState("");
  const [kind, setKind] = useState<TokenScope["kind"]>("entries");
  const [entries, setEntries] = useState("");
  const [prefix, setPrefix] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // The credential exists only in this dialog's state, never in a URL or browser storage.
  const [issued, setIssued] = useState<(HostToken & { token: string }) | null>(
    null,
  );
  const [copied, setCopied] = useState("");
  return (
    <Dialog
      title={issued ? "令牌已创建 · 仅显示这一次" : "创建宿主令牌"}
      onClose={close}
      closeDisabled={busy}
    >
      <div className="access-dialog">
        {issued ? (
          <>
            <Notice>
              请现在复制并交给 {issued.host}{" "}
              保存。关闭此窗口、离开页面或刷新后，无法再次查看令牌原文；丢失后只能撤销并重新创建。
            </Notice>
            <label>
              新令牌原文
              <textarea
                className="token-secret"
                readOnly
                rows={3}
                spellCheck={false}
                autoComplete="off"
                value={issued.token}
                onFocus={(e) => e.target.select()}
              />
            </label>
            <div className="button-row">
              <button
                className="primary"
                onClick={async () => {
                  try {
                    await navigator.clipboard.writeText(issued.token);
                    setCopied("已复制，请妥善保存");
                  } catch {
                    setCopied("无法自动复制，请选择令牌原文并手动复制。");
                  }
                }}
              >
                复制令牌
              </button>
              <button onClick={close}>关闭并清除原文</button>
            </div>
            {copied && <Notice>{copied}</Notice>}
          </>
        ) : (
          <form
            noValidate
            onSubmit={async (e) => {
              e.preventDefault();
              if (busy) return;
              const ids = entries.split(/\r?\n/).filter((id) => id !== "");
              if (!host.trim() || [...host.trim()].length > 100) {
                setError("宿主名称须为 1—100 字，不能只含空白");
                return;
              }
              if (
                kind === "entries" &&
                (!ids.length ||
                  ids.length > 1000 ||
                  new Set(ids).size !== ids.length ||
                  ids.some(
                    (id) =>
                      !id.trim() || [...id].length > 200 || id.includes("\0"),
                  ))
              ) {
                setError(
                  "请填写 1—1000 个不重复的入口 ID，每行一个，每个为 1—200 字，不能只含空白。",
                );
                return;
              }
              if (
                kind === "prefix" &&
                (!prefix.trim() ||
                  [...prefix].length > 200 ||
                  prefix.includes("\0"))
              ) {
                setError("入口前缀须为 1—200 字，不能只含空白");
                return;
              }
              const scope: TokenScope =
                kind === "entries"
                  ? { kind, entries: ids }
                  : kind === "prefix"
                    ? { kind, prefix }
                    : { kind };
              setBusy(true);
              setError("");
              try {
                const result = await api<HostToken & { token: string }>(
                  "/tokens",
                  json("POST", { host: host.trim(), scope }),
                );
                setIssued(result);
                created();
              } catch (e) {
                setError(
                  `${errorText(e)}。如请求已提交但结果不明确，请刷新列表核对；若令牌已创建却未取得原文，请撤销后重新创建。`,
                );
                created();
              } finally {
                setBusy(false);
              }
            }}
          >
            <fieldset disabled={busy}>
              <label>
                宿主名称
                <input
                  value={host}
                  maxLength={100}
                  onChange={(e) => setHost(e.target.value)}
                  placeholder="例如：本机聊天机器人"
                />
              </label>
              <label>
                入口范围
                <select
                  value={kind}
                  onChange={(e) =>
                    setKind(e.target.value as TokenScope["kind"])
                  }
                >
                  <option value="entries">入口列表</option>
                  <option value="prefix">入口前缀</option>
                  <option value="all">全部入口</option>
                </select>
              </label>
              {kind === "entries" && (
                <>
                  {!!catalog.data?.entries.length && (
                    <label>
                      添加已有入口
                      <select
                        value=""
                        onChange={(e) => {
                          if (
                            e.target.value &&
                            !entries.split(/\r?\n/).includes(e.target.value)
                          )
                            setEntries(
                              entries
                                ? `${entries}\n${e.target.value}`
                                : e.target.value,
                            );
                        }}
                      >
                        <option value="">选择入口加入列表</option>
                        {catalog.data.entries.map((entry) => (
                          <option key={entry.id} value={entry.id}>
                            {entry.name} · {entry.id}
                          </option>
                        ))}
                      </select>
                    </label>
                  )}
                  <label>
                    入口 ID 列表
                    <textarea
                      rows={4}
                      value={entries}
                      onChange={(e) => setEntries(e.target.value)}
                      placeholder={"group-a\nprivate-a"}
                    />
                  </label>
                  {catalog.error && (
                    <p className="muted">
                      已有入口暂时无法读取，仍可手动填写入口 ID。
                    </p>
                  )}
                </>
              )}
              {kind === "prefix" && (
                <label>
                  入口 ID 前缀
                  <input
                    value={prefix}
                    onChange={(e) => setPrefix(e.target.value)}
                    maxLength={200}
                    placeholder="例如：astrbot:"
                  />
                </label>
              )}
              <p>
                列表和前缀按入口 ID
                原字符匹配，区分大小写；前缀不是通配符。可以授权尚未创建的入口。范围创建后不可修改，需要调整时请新建令牌并撤销旧令牌。
              </p>
              {kind === "all" && (
                <Notice>此令牌可访问全部现有及今后创建的入口。</Notice>
              )}
              {error && <Notice error>{error}</Notice>}
              <button className="primary" disabled={busy}>
                {busy ? "正在创建…" : "创建并显示令牌"}
              </button>
            </fieldset>
          </form>
        )}
      </div>
    </Dialog>
  );
}
