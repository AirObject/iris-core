import type { Backups, TokenList } from "./access-types";
export const tokensFixture: TokenList = {
  items: [
    {
      id: "token-list",
      host: "聊天机器人",
      scope: { kind: "entries", entries: ["group-a", "private-a"] },
      created_at: "2026-10-10T08:00:00Z",
      last_used_at: "2026-10-10T09:00:00Z",
      revoked_at: null,
    },
    {
      id: "token-prefix",
      host: "测试宿主",
      scope: { kind: "prefix", prefix: "demo:%_" },
      created_at: "2026-10-10T08:01:00Z",
      last_used_at: null,
      revoked_at: null,
    },
    {
      id: "token-revoked",
      host: "旧宿主",
      scope: { kind: "all" },
      created_at: "2026-10-09T08:00:00Z",
      last_used_at: null,
      revoked_at: "2026-10-10T08:00:00Z",
    },
  ],
  rate_limits: { rate_per_second: 20, burst: 60 },
};
export const backupsFixture: Backups = {
  items: [
    {
      backup_id: "0123456789abcdef0123456789abcdef",
      created_at: "2026-10-10T09:00:00Z",
      format_version: 1,
      migration_version: "022",
      includes_model_secrets: false,
      media_included: true,
      file_count: 3,
      size: 2048,
      purpose: "manual",
      actor: "admin",
    },
    {
      backup_id: "abcdef0123456789abcdef0123456789",
      created_at: "2026-10-09T09:00:00Z",
      format_version: 1,
      migration_version: "021",
      includes_model_secrets: true,
      media_included: false,
      file_count: 2,
      size: 4096,
      purpose: "pre_import",
      actor: "local_cli",
    },
  ],
  import_mode: "offline",
  import_instructions:
    "停止服务后执行 iris --data-dir <目录> backup import <备份>；覆盖须加 --confirm-overwrite。",
};
