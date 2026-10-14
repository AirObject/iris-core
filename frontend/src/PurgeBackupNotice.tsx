import type { BackupNotice } from "./types";
import { Notice } from "./ui";

export function PurgeBackupNotice({ notice }: { notice?: BackupNotice }) {
  return notice ? <Notice>{notice.message}</Notice> : null;
}
