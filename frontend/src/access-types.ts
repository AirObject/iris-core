export type TokenScope =
  | { kind: "all" }
  | { kind: "entries"; entries: string[] }
  | { kind: "prefix"; prefix: string };
export type HostToken = {
  id: string;
  host: string;
  scope: TokenScope;
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
};
export type TokenList = {
  items: HostToken[];
  rate_limits: { rate_per_second: number; burst: number };
};
export type BackupRecord = {
  backup_id: string;
  created_at: string;
  format_version: number;
  migration_version: string;
  includes_model_secrets: boolean;
  media_included: boolean;
  file_count: number;
  size: number;
  purpose: string;
  actor: string;
};
export type Backups = {
  items: BackupRecord[];
  import_mode: string;
  import_instructions: string;
};
