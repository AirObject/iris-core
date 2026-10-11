import { useState, type FormEvent } from "react";
import { useData } from "./api";
import { Empty, Notice, Pagination, fullTime } from "./ui";
import type { Operation, Page } from "./types";

export const operationNames: Record<string, string> = {
  token_create: "创建宿主令牌",
  token_revoke: "撤销宿主令牌",
  token_limits_saved: "更新宿主令牌限流",
  backup_export: "导出备份",
  backup_import: "从备份导入",
  consolidation_settings_saved: "更新梦境整理设置",
  goal_dedup_judge_saved: "更新目标去重判断设置",
  consolidation_merge: "整理合并记忆",
  consolidation_conflict: "整理提出矛盾建议",
  consolidation_dependency: "整理提出依赖复核建议",
  consolidation_annotation_confirm: "确认整理建议",
  consolidation_annotation_clear: "清除整理建议",
  goal_basis_review: "目标依据复核",
  goal_basis_clear: "清除目标依据标注",
  persona_settings_saved: "更新 persona 设置",
  persona_edit: "编辑发布 persona",
  persona_confirm: "确认 persona 候选",
  persona_reject: "拒绝 persona 候选",
  persona_rollback: "回滚 persona",
  persona_current: "发布 persona",
  persona_pending: "persona 候选待确认",
  persona_rejected: "persona 候选被拒绝",
  persona_generation_requested: "请求生成 persona",
  persona_generation_current: "persona 生成并发布",
  persona_generation_pending: "persona 生成后待确认",
  persona_generation_rejected: "persona 生成检查未通过",
  persona_generation_skipped: "跳过 persona 生成",
  persona_generation_conflict: "persona 生成遇到版本或依据冲突",
  persona_generation_failed: "persona 生成失败",
  persona_generation_interrupted: "persona 生成中断",
  goal_dedup_judged: "目标去重判断结果",
  goal_dedup_queued: "目标待去重复核",
  goal_create: "创建目标",
  goal_update: "更新目标",
  goal_merge: "合并重复目标",
  goal_duplicate_dismiss: "驳回目标重复",
  settings_goals: "更新目标与提醒设置",
  memory_adjust: "调整记忆",
  memory_forget: "手动遗忘",
  memory_bulk_forget: "批量遗忘记忆",
  memory_bulk_delete: "批量删除记忆",
  memory_bulk_purge: "批量彻底清除记忆",
  memory_restore: "恢复记忆",
  memory_purge: "彻底清除记忆",
  memory_recreate: "按旧内容新建",
  memory_revision: "人工修订记忆",
  lifecycle_saved: "更新生命周期设置",
  state_settings_saved: "更新当前状态设置",
  maintenance_requested: "请求维护",
  maintenance_completed: "维护完成",
  batch_relearn: "重新学习批次",
  batch_result: "批次结果",
  learn_requested: "请求立即学习",
  feedback: "使用反馈",
  feedback_rejected: "使用反馈被拒绝",
  learn_rejected: "立即学习请求被拒绝",
  trial_media_uploaded: "上传试用图片",
  trial_entry_created: "创建试用入口",
  trial_speaker_created: "创建发言人",
  trial_message_received: "试用消息接收",
  trial_prepare: "试用回复准备",
  trial_reply: "试用角色回复",
  setup_password: "设置管理员密码",
  setup_completed: "完成首次设置",
  models_saved: "更新模型配置",
  role_saved: "更新角色设置",
  limits_saved: "更新用量与并发",
  model_retry: "请求模型重试",
  model_test: "测试模型连接",
  recall_judge_saved: "更新召回判断设置",
  entry_visibility: "更新入口记忆可见范围",
  entry_settings_updated: "更新入口节奏与过滤",
  subject_alias_added: "添加人物别名",
  subject_alias_removed: "删除人物别名",
  subject_link_denied: "否认人物联系",
  subjects_merged: "确认并合并人物",
  models_imported: "导入模型配置",
  login: "管理员登录",
  logout: "退出登录",
};
export const actorNames: Record<string, string> = {
  local_cli: "本机命令行",
  persona: "persona 更新",
  consolidation: "梦境整理",
  goal_dedup: "目标去重判断",
  learning: "学习",
  admin: "管理员",
  host: "宿主",
  system: "系统",
  scheduler: "后台调度",
  maintenance: "维护",
  local_import: "本机导入",
};
const objectNames: Record<string, string> = {
  host_token: "宿主令牌",
  backup: "备份",
  persona: "persona 版本",
  persona_attempt: "persona 生成任务",
  goal: "目标",
  memory: "记忆",
  memory_bulk: "批量记忆操作",
  batch: "批次",
  entry: "入口",
  subject: "人物",
  subject_link: "人物联系",
  model: "模型",
  settings: "设置",
  maintenance: "维护",
  recall: "召回",
  request: "请求",
};

export default function Operations({
  initialQuery = "",
}: {
  initialQuery?: string;
}) {
  const initial = new URLSearchParams(initialQuery);
  const [filters, setFilters] = useState({
    time_from: "",
    time_to: "",
    action: "",
    actor: "",
    object_type: initial.get("object_type") || "",
    object_id: initial.get("object_id") || "",
  });
  const encode = (values: typeof filters) =>
    new URLSearchParams(
      Object.entries(values).filter(([, v]) => !!v),
    ).toString();
  const [query, setQuery] = useState(() => encode(filters)),
    [offset, setOffset] = useState(0);
  const [error, setError] = useState("");
  const list = useData<Page<Operation>>(
    `/operations?${query}&limit=30&offset=${offset}`,
  );
  const change = (key: keyof typeof filters, value: string) =>
    setFilters((f) => ({ ...f, [key]: value }));
  function filter(event: FormEvent) {
    event.preventDefault();
    if (
      filters.time_from &&
      filters.time_to &&
      filters.time_from > filters.time_to
    ) {
      setError("开始日期不能晚于结束日期");
      return;
    }
    setError("");
    setOffset(0);
    setQuery(encode(filters));
    list.refresh();
  }
  return (
    <>
      <div className="page-heading">
        <div>
          <p className="eyebrow">每次变更，均有记录</p>
          <h1>操作记录</h1>
          <p>查看管理员、宿主与系统保存的操作摘要。</p>
        </div>
      </div>
      <section className="panel">
        <form onSubmit={filter} className="memory-filters">
          <div className="filter-grid operation-filters">
            <label>
              开始日期
              <input
                type="date"
                value={filters.time_from}
                onChange={(e) => change("time_from", e.target.value)}
              />
            </label>
            <label>
              结束日期
              <input
                type="date"
                value={filters.time_to}
                onChange={(e) => change("time_to", e.target.value)}
              />
            </label>
            <label>
              操作类型
              <select
                value={filters.action}
                onChange={(e) => change("action", e.target.value)}
              >
                <option value="">全部类型</option>
                {Object.entries(operationNames).map(([value, label]) => (
                  <option value={value} key={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              操作者
              <select
                value={filters.actor}
                onChange={(e) => change("actor", e.target.value)}
              >
                <option value="">全部操作者</option>
                {Object.entries(actorNames).map(([value, label]) => (
                  <option value={value} key={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              对象类型
              <select
                value={filters.object_type}
                onChange={(e) => change("object_type", e.target.value)}
              >
                <option value="">全部对象</option>
                {Object.entries(objectNames).map(([value, label]) => (
                  <option value={value} key={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              对象标识
              <input
                value={filters.object_id}
                maxLength={200}
                placeholder="如记忆编号 8"
                onChange={(e) => change("object_id", e.target.value)}
              />
            </label>
          </div>
          <p className="fine-print">
            日期筛选按角色时区解释，结束日期包含当天；记录时间按本机时区显示。
          </p>
          <div className="actions">
            <button className="primary">筛选记录</button>
            <button type="button" className="secondary" onClick={list.refresh}>
              刷新记录
            </button>
          </div>
        </form>
      </section>
      {(error || list.error) && <Notice error>{error || list.error}</Notice>}
      <div className="list-heading">
        <span>{list.data ? `共 ${list.data.total} 条` : "正在读取…"}</span>
      </div>
      {list.data?.items.length === 0 && (
        <Empty title="没有符合条件的操作记录" />
      )}
      <div className="learning-list">
        {list.data?.items.map((o) => (
          <article key={o.id} className="panel operation-card">
            <div className="panel-heading">
              <h2>{operationNames[o.action] || o.action}</h2>
              <span className="muted">记录 #{o.id}</span>
            </div>
            <p>
              {fullTime(o.created_at)} · {actorNames[o.actor] || o.actor}
            </p>
            {o.object_type && (
              <p>
                对象：{objectNames[o.object_type] || o.object_type}
                {o.object_id !== null ? ` · ${o.object_id}` : ""}
              </p>
            )}
            <details>
              <summary>查看记录详情</summary>
              <pre className="record-json">
                {JSON.stringify(o.details, null, 2)}
              </pre>
            </details>
          </article>
        ))}
      </div>
      {list.data && (
        <Pagination
          total={list.data.total}
          offset={offset}
          change={setOffset}
        />
      )}
    </>
  );
}
