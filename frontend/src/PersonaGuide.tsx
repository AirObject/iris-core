export function PersonaGuide() {
  return (
    <details className="persona-guide panel">
      <summary>persona 从哪里来？与当前状态、目标有什么区别？</summary>
      <figure>
        <figcaption>
          经历成为自我认知，经过提炼与检查后形成 persona。
        </figcaption>
        <ol className="persona-flow">
          <li>初始背景 ＋ 持续经历</li>
          <li>
            自我认知<small>有来源的自我记忆</small>
          </li>
          <li>
            结合生成目标 ＋ 监管要求<small>由管理员设定，不从记忆中改写</small>
          </li>
          <li>
            梦境整理中提炼与检查<small>逐句依据与表达检查</small>
          </li>
          <li>
            persona<small>按发布方式发布或等待确认</small>
          </li>
        </ol>
      </figure>
      <dl className="persona-distinction">
        <div>
          <dt>当前状态</dt>
          <dd>现在在做什么、心情如何，由宿主报告，可能过时。</dd>
        </div>
        <div>
          <dt>目标与询问</dt>
          <dd>未来要做或要问的事，跨入口共享；提醒被取走不代表目标完成。</dd>
        </div>
        <div>
          <dt>persona</dt>
          <dd>
            相对稳定的自我表达，有版本与依据；不是当前心情，也不是待办清单。
          </dd>
        </div>
      </dl>
      <p>
        宿主结合当前状态、目标、persona、相关记忆和本入口近期消息，组织上下文并决定说什么、做什么。
      </p>
      <p className="muted">
        “待更新”表示当前版本有失效依据；“待确认”表示已有候选等待管理员决定。两者可以同时出现。
      </p>
    </details>
  );
}
