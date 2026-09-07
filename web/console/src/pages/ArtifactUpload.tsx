import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { Action, Fields, Resource, Result } from "../api/design";
import { allowed, commandFields, Dialog, encodeFields, ErrorNotice, FieldsForm, Reason, useEnvironment } from "../components/core";

const MAX_BYTES = 8 * 1024 * 1024;

export function ArtifactUpload({ action, onClose, onSuccess }: {
  action: Action;
  onClose: () => void;
  onSuccess: (result: Result<Resource>) => void;
}) {
  const { bootstrap } = useEnvironment();
  const [draft, setDraft] = useState<Fields>(() => Object.fromEntries(action.fields
    .filter((field) => field.default !== undefined).map((field) => [field.key, field.default!])));
  const [file, setFile] = useState<File>();
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState<unknown>();
  const key = useRef(crypto.randomUUID());
  const lock = useRef(false);
  const controller = useRef(new AbortController());
  useEffect(() => {
    const signal = new AbortController();
    controller.current = signal;
    return () => signal.abort();
  }, []);
  const submit = async () => {
    if (lock.current || !allowed(bootstrap, action)) return;
    if (!file || file.size < 1 || file.size > MAX_BYTES || !reason) {
      setError(new Error("请选择 1 字节至 8 MiB 的文件，并填写操作原因。"));
      return;
    }
    lock.current = true;
    setBusy(true);
    setError(undefined);
    setProgress(0);
    try {
      const fields = encodeFields(action.fields, draft);
      const metadata = JSON.stringify({ ...commandFields(action, fields), reason_code: reason });
      if (new TextEncoder().encode(metadata).byteLength > 8192)
        throw new Error("上传元数据超过 8 KiB。请缩短来源和隐私标签。 ");
      const result = await api.action<Resource>(`/memory/artifacts:upload?metadata=${encodeURIComponent(metadata)}`, {
        method: "POST", raw: file, contentType: "application/octet-stream", key: key.current,
        signal: controller.current.signal, userActivity: true,
        progress: (loaded, total) => setProgress(total > 0 ? Math.min(100, Math.floor(100 * loaded / total)) : 0),
      });
      onSuccess(result);
      onClose();
    } catch (failure) {
      if (!(failure instanceof DOMException && failure.name === "AbortError")) setError(failure);
    } finally {
      lock.current = false;
      setBusy(false);
    }
  };
  return <Dialog title={action.label} onClose={() => { controller.current.abort(); onClose(); }}>
    <form onSubmit={(event) => { event.preventDefault(); void submit(); }}>
      <p>{action.description}</p>
      <FieldsForm fields={action.fields} value={draft} disabled={busy} onChange={(value) => {
        setDraft(value); key.current = crypto.randomUUID();
      }} />
      <label>原始附件文件
        <input type="file" required disabled={busy} onChange={(event) => {
          setFile(event.target.files?.[0]); key.current = crypto.randomUUID();
        }} />
      </label>
      <Reason codes={action.reason_codes} value={reason} onChange={(value) => { setReason(value); key.current = crypto.randomUUID(); }} />
      {busy && <p role="status">上传进度 {progress}%。等待服务器确认保存。</p>}
      <ErrorNotice error={error} />
      <button className="primary" disabled={busy || !allowed(bootstrap, action)}>{busy ? "上传中…" : "确认上传"}</button>
    </form>
  </Dialog>;
}
