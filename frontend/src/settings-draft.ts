import { useState } from "react";

export function changedSettings<T extends object>(
  current: T,
  next: T,
): Partial<T> {
  return Object.fromEntries(
    (Object.keys(next) as (keyof T)[])
      .filter((key) => !Object.is(current[key], next[key]))
      .map((key) => [key, next[key]]),
  ) as Partial<T>;
}

// Polling refreshes untouched fields; only explicit edits stay in the draft.
export function useSettingsDraft<T extends object>(value: T) {
  const [edits, setEdits] = useState<Partial<T>>({});
  function change<K extends keyof T>(key: K, next: T[K]) {
    setEdits((previous) => {
      const updated = { ...previous };
      if (Object.is(value[key], next)) delete updated[key];
      else updated[key] = next;
      return updated;
    });
  }
  return { draft: { ...value, ...edits }, change, reset: () => setEdits({}) };
}
