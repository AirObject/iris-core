import { useEffect, useRef, useState } from "react";
import type { PersonaSettings } from "./persona-types";
import { PersonaGuide } from "./PersonaGuide";
import { Notice } from "./ui";
export function PersonaSettingsEditor({
  value,
  busy,
  save,
}: {
  value: PersonaSettings;
  busy: boolean;
  save: (value: PersonaSettings) => Promise<PersonaSettings | null>;
}) {
  const [draft, setDraft] = useState(value),
    [error, setError] = useState("");
  const dirty = useRef(false);
  useEffect(() => {
    if (!dirty.current) setDraft(value);
  }, [value]);
  return (
    <section className="panel settings-panel persona-settings">
      <h2>persona 的生成与发布</h2>
      <p>
        生成目标和监管要求只能由管理员修改，学习和整理不能修改它们。保存只影响之后接受的任务；已接受的任务、已有候选和当前
        persona 保持原设置。
      </p>
      <form
        noValidate
        onSubmit={async (e) => {
          e.preventDefault();
          if (busy) return;
          const goal = draft.goal.trim(),
            rules = draft.rules.trim();
          if (!goal || [...goal].length > 4000) {
            setError("生成目标须为 1—4000 字，不能只含空白");
            return;
          }
          if (!rules || [...rules].length > 16000) {
            setError("监管要求须为 1—16000 字，不能只含空白");
            return;
          }
          setError("");
          const saved = await save({
            goal,
            rules,
            publish_mode: draft.publish_mode,
          });
          if (saved) {
            dirty.current = false;
            setDraft(saved);
          }
        }}
      >
        <label>
          persona 发布方式
          <select
            disabled={busy}
            value={draft.publish_mode}
            onChange={(e) => {
              dirty.current = true;
              setDraft({
                ...draft,
                publish_mode: e.target.value as PersonaSettings["publish_mode"],
              });
            }}
          >
            <option value="small_medium_auto">小或中自动发布</option>
            <option value="all_auto">全部自动</option>
            <option value="all_manual">全部人工确认（默认）</option>
          </select>
        </label>
        <p className="lifecycle-help">
          M3 的 persona
          门槛尚未达到，默认采用“全部人工确认”：检查通过的候选也先由管理员确认，确认后才生效。可以改回“小或中自动发布”或“全部自动”。
        </p>
        <p className="lifecycle-help">
          所有候选都要经过检查。“小或中自动发布”会自动发布检查通过的小、中变化，大变化等待确认。
        </p>
        <Notice>
          自动候选删去或改动手写内容时按“大变化”处理。选择“全部自动”后，检查通过的大变化也会直接发布，包括删改手写内容；不会等待确认。
        </Notice>
        <label>
          生成目标
          <textarea
            rows={4}
            disabled={busy}
            value={draft.goal}
            onChange={(e) => {
              dirty.current = true;
              setDraft({ ...draft, goal: e.target.value });
            }}
          />
        </label>
        <label>
          监管要求
          <textarea
            rows={6}
            disabled={busy}
            value={draft.rules}
            onChange={(e) => {
              dirty.current = true;
              setDraft({ ...draft, rules: e.target.value });
            }}
          />
        </label>
        {error && <Notice error>{error}</Notice>}
        <button className="primary" disabled={busy}>
          保存 persona 设置
        </button>
      </form>
      <p>
        <a href="#/persona">查看 persona 的依据、候选和版本历史</a>
      </p>
      <PersonaGuide />
    </section>
  );
}
