import type { ImageUsage as Usage } from "./types";
import { seconds } from "./ui";

export function ImageUsage({
  value,
}: {
  value?: { today: Usage; week: Usage };
}) {
  if (!value) return <p>暂时无法读取图片用量。</p>;
  const rows: [string, (usage: Usage) => string | number][] = [
    ["调用次数", (usage) => usage.calls],
    ["token（输入＋输出）", (usage) => usage.tokens.toLocaleString()],
    ["累计耗时", (usage) => seconds(usage.duration_ms)],
    ["失败调用", (usage) => usage.failures],
    ["其中被拒绝", (usage) => usage.refusals],
    ["超时调用", (usage) => usage.timeouts],
    ["未返回 token 用量的调用", (usage) => usage.calls_without_usage],
  ];
  return (
    <>
      <table className="image-usage">
        <caption>图片理解用量</caption>
        <thead>
          <tr>
            <th scope="col">指标</th>
            <th scope="col">今日</th>
            <th scope="col">本周</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([label, read]) => (
            <tr key={label}>
              <th scope="row">{label}</th>
              <td>{read(value.today)}</td>
              <td>{read(value.week)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="fine-print">
        按角色时区统计，本周自周一开始。含重试、连接测试和恢复探测；未返回的
        token 用量不作推算。
      </p>
    </>
  );
}
