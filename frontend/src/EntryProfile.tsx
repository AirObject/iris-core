import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api, errorText, json, useData } from "./api";
import { Badge, Dialog, Notice, fullTime } from "./ui";
import { EntryProfileHistory } from "./EntryProfileHistory";
import { ProfileMemoryCard, ProfileMemoryCreate } from "./EntryProfileMemory";
import { ProfileProgressView, ProfileWorkList } from "./EntryProfileProgress";
import { incrementalReason, status } from "./entry-profile-labels";
import type {
  ProfileAttempt,
  ProfileProgress,
  ProfileSnapshot,
} from "./entry-profile-types";

export type ProfileWrite = (
  path: string,
  method: string,
  body: unknown,
) => Promise<unknown>;

function ProfileTask({
  base,
  initial,
  settled,
  progress,
}: {
  base: string;
  initial: ProfileAttempt;
  progress?: ProfileProgress | null;
  settled: (value: ProfileAttempt) => void;
}) {
  const [polling, setPolling] = useState(initial.state === "running");
  const task = useData<ProfileAttempt>(
    `${base}/attempts/${initial.id}`,
    polling ? 1500 : 0,
  );
  const completed = useRef(false);
  useEffect(() => {
    if (task.data && task.data.state !== "running" && !completed.current) {
      completed.current = true;
      setPolling(false);
      settled(task.data);
    }
  }, [task.data, settled]);
  const value = polling ? task.data || initial : initial;
  return (
    <section className="entry-profile-task" aria-label="画像更新任务">
      <h3>更新任务 #{value.id}</h3>
      {task.error && (
        <Notice error>
          {task.error}
          <button onClick={task.refresh}>重试任务状态</button>
        </Notice>
      )}
      <p role="status">
        {value.state === "running"
          ? value.stage === "checking"
            ? "正在检查画像条目…"
            : "正在观察消息分块…"
          : value.state === "published"
            ? "本轮已提交处理结果，仍以逐条准入状态为准。"
            : status(value.state)}
      </p>
      {value.reason && <p>{incrementalReason(value.reason)}</p>}
      <p className="fine-print">
        开始：{fullTime(value.started_at)}
        {value.finished_at && ` · 结束：${fullTime(value.finished_at)}`}
      </p>
      <ProfileProgressView progress={value.progress || progress} />
      <ProfileWorkList work={value.work} />
      {value.version != null && (
        <p>任务完成时的宿主投影版本：v{value.version}</p>
      )}
    </section>
  );
}

export function EntryProfileDialog({
  entry,
  onClose,
  openMemory,
}: {
  entry: { id: string; name: string };
  onClose: () => void;
  openMemory: (id: number) => void;
}) {
  const base = `/entries/${encodeURIComponent(entry.id)}/profile`;
  const snapshot = useData<ProfileSnapshot>(base, 5000);
  const catalog = useData<{ people: { id: string; name: string }[] }>(
    "/catalog",
  );
  const speakerNames = Object.fromEntries(
    (catalog.data?.people || []).map((person) => [person.id, person.name]),
  );
  const [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  const [creating, setCreating] = useState(false);
  const [settings, setSettings] = useState<{
    enabled: boolean;
    expected_version: number;
  } | null>(null);
  const [accepted, setAccepted] = useState<ProfileAttempt | null>(null);
  const submitting = useRef(false),
    alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const write: ProfileWrite = async (path, method, body) => {
    if (submitting.current)
      throw new ApiError("busy", "正在保存，请稍候。", 409);
    submitting.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      return await api(path, json(method, body));
    } finally {
      submitting.current = false;
      if (alive.current) {
        setBusy(false);
        snapshot.refresh();
      }
    }
  };
  const settled = useCallback(
    (value: ProfileAttempt) => {
      setAccepted(value);
      snapshot.refresh();
    },
    [snapshot.refresh],
  );
  const value = snapshot.data;
  const task =
    value?.latest_attempt &&
    (!accepted ||
      value.latest_attempt.id > accepted.id ||
      (value.latest_attempt.id === accepted.id &&
        (accepted.state === "running" ||
          value.latest_attempt.state !== "running")))
      ? value.latest_attempt
      : accepted || value?.latest_attempt;
  const blocked = busy || snapshot.loading || !!snapshot.error;
  const working = task?.state === "running";
  const navigateMemory = (id: number) => {
    if (!busy) {
      onClose();
      openMemory(id);
    }
  };
  async function saveSettings() {
    if (!settings || blocked) return;
    try {
      await write(`${base}/settings`, "PATCH", settings);
      setSettings(null);
      setNotice("画像设置已保存。");
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 409
          ? "画像已变化，设置草稿已保留；请核对最新投影后重新确认。"
          : errorText(e),
      );
    }
  }
  async function regenerate() {
    if (!value || blocked || working) return;
    try {
      const result = (await write(`${base}/regenerate`, "POST", {
        expected_version: value.settings.current_version,
      })) as { attempt: ProfileAttempt };
      setAccepted(result.attempt);
      setNotice("更新请求已提交，可关闭面板后再查看任务结果。");
    } catch (e) {
      setError(errorText(e));
    }
  }
  return (
    <Dialog
      title={`入口画像 · ${entry.name}`}
      onClose={onClose}
      closeDisabled={busy}
    >
      <div className="entry-profile">
        <p className="muted">
          每条画像都是本入口的记忆，自动更新。手写内容按人工记忆保护。
        </p>
        {(snapshot.error || error) && (
          <Notice error>
            {error || snapshot.error}
            <button
              className="text-button"
              disabled={busy}
              onClick={snapshot.refresh}
            >
              刷新画像
            </button>
          </Notice>
        )}
        {notice && <Notice>{notice}</Notice>}
        {!value ? (
          <p role="status">正在读取入口画像…</p>
        ) : !value.supported ? (
          <Notice>私聊入口不生成入口画像。</Notice>
        ) : value.mode !== "memory" ? (
          <Notice>
            入口画像尚未切换为记忆形式，请更新服务后刷新。旧版历史仍可只读查看。
          </Notice>
        ) : (
          <>
            <section className="entry-profile-section" aria-label="画像设置">
              <div className="button-row">
                <Badge>{value.settings.enabled ? "已开启" : "已关闭"}</Badge>
                <span>自动更新，无需确认候选</span>
              </div>
              {!value.global_enabled && (
                <Notice>
                  全局画像开关已关闭，当前不会生成或加入回复准备。
                </Notice>
              )}
              {settings ? (
                <form
                  onSubmit={(e) => {
                    e.preventDefault();
                    void saveSettings();
                  }}
                >
                  <label className="check-label">
                    <input
                      type="checkbox"
                      checked={settings.enabled}
                      disabled={busy}
                      onChange={(e) =>
                        setSettings({ ...settings, enabled: e.target.checked })
                      }
                    />
                    启用本入口画像
                  </label>
                  {settings.expected_version !==
                    value.settings.current_version && (
                    <Notice>
                      投影已更新为 v{value.settings.current_version}
                      ，请先核对下方预览。
                      <button
                        type="button"
                        disabled={blocked}
                        onClick={() =>
                          setSettings({
                            ...settings,
                            expected_version: value.settings.current_version,
                          })
                        }
                      >
                        已核对，保留设置草稿
                      </button>
                    </Notice>
                  )}
                  <div className="button-row">
                    <button
                      disabled={
                        blocked ||
                        settings.expected_version !==
                          value.settings.current_version
                      }
                    >
                      保存画像设置
                    </button>
                    <button
                      type="button"
                      className="secondary"
                      disabled={busy}
                      onClick={() => setSettings(null)}
                    >
                      取消设置
                    </button>
                  </div>
                </form>
              ) : (
                <button
                  className="secondary"
                  disabled={blocked}
                  onClick={() =>
                    setSettings({
                      enabled: value.settings.enabled,
                      expected_version: value.settings.current_version,
                    })
                  }
                >
                  修改画像设置
                </button>
              )}
            </section>
            <section
              className="entry-profile-section"
              aria-label="宿主投影预览"
            >
              <h3>宿主投影预览 · v{value.settings.current_version}</h3>
              <p className="muted">
                以下为回复准备提供的原文；宿主传入已知记忆时会排除相应条目。投影版本独立于每条记忆的修订号。
              </p>
              {value.projection ? (
                <>
                  <p className="entry-profile-content profile-projection">
                    {value.projection.text}
                  </p>
                  <p className="fine-print">
                    version：{value.projection.version} · 更新于{" "}
                    {fullTime(value.projection.generated_at)}
                  </p>
                  <div className="button-row">
                    {value.projection.memory_ids.map((id) => (
                      <button
                        className="text-button"
                        key={id}
                        onClick={() =>
                          document
                            .getElementById(`profile-memory-${id}`)
                            ?.scrollIntoView?.({
                              behavior: "smooth",
                              block: "start",
                            })
                        }
                      >
                        画像记忆 #{id}
                      </button>
                    ))}
                  </div>
                  {value.projection.possibly_stale && (
                    <Notice>
                      投影可能过时：还有未处理消息、观察缺口、待检查或待重建的内容。
                    </Notice>
                  )}
                </>
              ) : (
                <p>当前不向宿主提供画像（entry_profile 为 null）。</p>
              )}
              {!!value.diagnostics.omitted_memory_ids?.length && (
                <Notice>
                  受展示预算限制，以下记忆未加入投影：
                  {value.diagnostics.omitted_memory_ids.map((id) => (
                    <button
                      className="text-button"
                      key={id}
                      onClick={() =>
                        document
                          .getElementById(`profile-memory-${id}`)
                          ?.scrollIntoView?.({
                            behavior: "smooth",
                            block: "start",
                          })
                      }
                    >
                      #{id}
                    </button>
                  ))}
                  <p>这些记忆仍然保留；人工内容不会因展示预算被隐藏。</p>
                </Notice>
              )}
              {value.rebuild_pending && (
                <Notice>画像正在等待新任务重建或继续处理消息。</Notice>
              )}
            </section>
            <section
              className="entry-profile-section"
              aria-label="每日增量进度"
            >
              <h3>每日增量进度</h3>
              <p className="muted">
                自动任务按角色时区处理已结束日期；立即运行也处理当日尾块。准入由系统自动检查，无需审批。
              </p>
              <button
                disabled={
                  blocked ||
                  working ||
                  !value.settings.enabled ||
                  !value.global_enabled
                }
                onClick={() => void regenerate()}
              >
                立即运行
              </button>
              <p className="fine-print">
                继续处理新消息和排队的检查；没有新依据时不会重复确认。
              </p>
              {!task && <ProfileProgressView progress={value.progress} />}
              {task && (
                <ProfileTask
                  key={task.id}
                  base={base}
                  initial={task}
                  settled={settled}
                  progress={value.progress}
                />
              )}
            </section>
            <section className="entry-profile-section" aria-label="画像记忆">
              <div className="panel-heading">
                <h3>画像记忆 · {value.memories.length} 条</h3>
                <button
                  disabled={blocked || creating}
                  onClick={() => setCreating(true)}
                >
                  人工新建
                </button>
              </div>
              {creating && (
                <ProfileMemoryCreate
                  entry={entry}
                  blocked={blocked}
                  busy={busy}
                  write={write}
                  cancel={() => setCreating(false)}
                  created={() => {
                    setCreating(false);
                    setNotice("已创建本入口的画像记忆。");
                  }}
                />
              )}
              {!value.memories.length && (
                <p>暂无画像记忆。可手写补充，也可等待新消息形成画像。</p>
              )}
              <div className="profile-memory-list">
                {value.memories.map((memory) => (
                  <ProfileMemoryCard
                    key={memory.memory_id}
                    memory={memory}
                    speakerNames={speakerNames}
                    displayed={
                      !!value.projection?.memory_ids.includes(memory.memory_id)
                    }
                    blocked={blocked}
                    busy={busy}
                    write={write}
                    refresh={snapshot.refresh}
                    openMemory={navigateMemory}
                  />
                ))}
              </div>
            </section>
          </>
        )}
        {value?.supported && (
          <>
            {!!value.migration?.length && (
              <details className="history">
                <summary>旧画像迁移记录</summary>
                <ul>
                  {value.migration.map((row) => (
                    <li key={`${row.legacy_version}:${row.sentence_index}`}>
                      旧版 v{row.legacy_version} · 原条目序号{" "}
                      {row.sentence_index}：
                      {{
                        manual_imported: "已转为人工画像记忆",
                        manual_unclassified: "已转为管理员说明",
                        model_rebuild_pending: "原模型条目待按依据重建",
                        cleared_history: "原内容已清空",
                      }[row.disposition] || row.disposition}
                      {row.memory_id != null && (
                        <button
                          className="text-button"
                          disabled={busy}
                          onClick={() => navigateMemory(row.memory_id!)}
                        >
                          查看记忆 #{row.memory_id}
                        </button>
                      )}
                    </li>
                  ))}
                </ul>
              </details>
            )}
            <EntryProfileHistory base={base} openMemory={navigateMemory} />
          </>
        )}
      </div>
    </Dialog>
  );
}
