import { act, render, screen, within, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import PeopleGraph, { type GraphData } from "./PeopleGraph";
import {
  edgeKey,
  edgeLabel,
  graphFrame,
  graphLabels,
  graphLayout,
} from "./peopleGraphLayout";

const data: GraphData = {
  nodes: [
    {
      id: "a /林",
      name: "小林",
      kind: "person",
      aliases: ["阿林"],
      account_count: 2,
      memory_count: 4,
    },
    {
      id: "b",
      name: "小周",
      kind: "person",
      aliases: [],
      account_count: 1,
      memory_count: 3,
    },
    {
      id: "self",
      name: "Iris",
      kind: "self",
      aliases: [],
      account_count: 0,
      memory_count: 2,
    },
  ],
  edges: [
    {
      source: "a /林",
      target: "b",
      memory_count: 2,
      relationship_count: 1,
      relationship_summaries: [{ id: 10, summary: "小林与小周是同事。" }],
      links: [
        {
          id: 8,
          source: "a /林",
          target: "b",
          kind: "same_as",
          belief: 60,
          worlds: [null],
          evidence_message_count: 1,
        },
      ],
    },
  ],
  total_nodes: 3,
  total_edges: 1,
  truncated: false,
  node_limit: 60,
  edge_limit: 100,
};
let graph: GraphData;
let requests: { url: string; init?: RequestInit }[];
let failGraph: boolean;
let failEvidence: boolean;
const openMemory = vi.fn();
beforeEach(() => {
  graph = structuredClone(data);
  requests = [];
  failGraph = false;
  failEvidence = false;
  openMemory.mockReset();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      requests.push({ url, init });
      if (
        (failEvidence && url.includes("/people-graph/edge")) ||
        (failGraph && url.includes("/people-graph?"))
      )
        return new Response(
          JSON.stringify({ error: { message: "读取失败，请重试" } }),
          { status: 503 },
        );
      if (url.endsWith("/catalog"))
        return new Response(
          JSON.stringify({
            people: [
              ...data.nodes,
              { id: "scene", name: "场景", kind: "person" },
            ],
            entries: [{ id: "room&1", name: "朋友群", kind: "group" }],
          }),
        );
      if (url.includes("/people-graph/edge"))
        return new Response(
          JSON.stringify({
            items:
              new URL("http://localhost" + url).searchParams.get("offset") ===
              "30"
                ? [
                    {
                      id: 40,
                      content: "更早的同行经历",
                      kind: "事件",
                      revision: 1,
                      cooccurrence: true,
                      link_evidence: false,
                    },
                  ]
                : [
                    {
                      id: 10,
                      content: "小林与小周是同事。",
                      kind: "关系",
                      revision: 3,
                      cooccurrence: true,
                      link_evidence: true,
                    },
                  ],
            total: 31,
            limit: 30,
            offset: 0,
            links: graph.edges[0]?.links || [],
          }),
        );
      return new Response(JSON.stringify(graph));
    }),
  );
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const mount = (initialQuery = "") =>
  render(<PeopleGraph openMemory={openMemory} initialQuery={initialQuery} />);

test("shows graph, aliases, counts, identity distinction and encoded memory links", async () => {
  mount();
  expect(
    await screen.findByRole("group", { name: "人物关系图" }),
  ).toBeInTheDocument();
  expect(screen.getByText("别名：阿林")).toBeInTheDocument();
  expect(screen.getByText("2 个账号 · 4 条有效记忆")).toBeInTheDocument();
  expect(
    screen.getAllByRole("link", { name: "查看小林的有效记忆" })[0],
  ).toHaveAttribute(
    "href",
    "#/memories?person_id=a%20%2F%E6%9E%97&lifecycle=active",
  );
  expect(screen.getByText(/共现不等于关系事实/)).toBeInTheDocument();
  expect(screen.getByText(/scene 不作为人物/)).toBeInTheDocument();
  expect(
    screen.queryByRole("option", { name: /场景/ }),
  ).not.toBeInTheDocument();
  expect(screen.getAllByText(/小林与小周是同事/).length).toBeGreaterThan(0);
  expect(
    requests.every((r) => !r.init?.method || r.init.method === "GET"),
  ).toBe(true);
  expect(requests.every((r) => r.init?.credentials === "same-origin")).toBe(
    true,
  );
});

test("applies entry, focus, hops, memory threshold and caps only after submit", async () => {
  const user = userEvent.setup();
  mount();
  await screen.findByRole("group", { name: "人物关系图" });
  await user.selectOptions(screen.getByLabelText("来源入口"), "room&1");
  await user.selectOptions(screen.getByLabelText("聚焦人物"), "a /林");
  await user.selectOptions(screen.getByLabelText("范围"), "2");
  await user.clear(screen.getByLabelText("共现最少记忆数"));
  await user.type(screen.getByLabelText("共现最少记忆数"), "3");
  await user.clear(screen.getByLabelText("节点上限"));
  await user.type(screen.getByLabelText("节点上限"), "20");
  await user.clear(screen.getByLabelText("连线上限"));
  await user.type(screen.getByLabelText("连线上限"), "40");
  expect(requests.filter((r) => r.url.includes("/people-graph?")).length).toBe(
    1,
  );
  await user.click(screen.getByRole("button", { name: "生成关系图" }));
  await waitFor(() =>
    expect(
      requests.filter((r) => r.url.includes("/people-graph?")).length,
    ).toBe(2),
  );
  const params = new URL("http://localhost" + requests.at(-1)!.url)
    .searchParams;
  expect(Object.fromEntries(params)).toEqual({
    entry_id: "room&1",
    person_id: "a /林",
    hops: "2",
    min_memories: "3",
    node_limit: "20",
    edge_limit: "40",
  });
});

test("opens an edge's paged current evidence and then memory detail", async () => {
  const user = userEvent.setup();
  mount("entry_id=room%261");
  await screen.findByRole("group", { name: "人物关系图" });
  await user.click(
    screen.getAllByRole("button", { name: "查看小林与小周的连线依据" })[0],
  );
  const dialog = await screen.findByRole("dialog");
  expect(await within(dialog).findByText("同来源联系证据")).toBeInTheDocument();
  expect(within(dialog).getByText(/相信程度 60/)).toBeInTheDocument();
  expect(
    within(dialog).getByText(/同来源记忆不等于确认身份/),
  ).toBeInTheDocument();
  await user.click(within(dialog).getByRole("button", { name: /下一页/ }));
  expect(await within(dialog).findByText("更早的同行经历")).toBeInTheDocument();
  const url = requests
    .filter((r) => r.url.includes("/people-graph/edge"))
    .at(-1)!.url;
  expect(new URL("http://localhost" + url).searchParams.get("entry_id")).toBe(
    "room&1",
  );
  expect(new URL("http://localhost" + url).searchParams.get("offset")).toBe(
    "30",
  );
  await user.click(
    within(dialog).getByRole("button", { name: "查看记忆 #40" }),
  );
  expect(openMemory).toHaveBeenCalledWith(40);
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

test("keyboard edge activation and link-only roleplay keep direction and zero memory count", async () => {
  graph.edges[0] = {
    ...graph.edges[0],
    memory_count: 0,
    relationship_count: 0,
    relationship_summaries: [],
    links: [
      {
        id: 9,
        kind: "roleplay",
        source: "b",
        target: "a /林",
        belief: 80,
        worlds: ["海岛", null],
        evidence_message_count: 1,
      },
    ],
  };
  const user = userEvent.setup();
  mount();
  const buttons = await screen.findAllByRole("button", {
    name: "查看小林与小周的连线依据",
  });
  buttons[0].focus();
  await user.keyboard("{Enter}");
  const dialog = await screen.findByRole("dialog");
  expect(await within(dialog).findByText(/小周 → 小林/)).toBeInTheDocument();
  expect(within(dialog).getByText(/海岛、场景未记录/)).toBeInTheDocument();
  expect(screen.getAllByText(/0 条共同涉及记忆/).length).toBeGreaterThan(0);
});

test("truncation explains totals and focus can be changed from a node", async () => {
  graph.truncated = true;
  graph.total_nodes = 120;
  graph.total_edges = 340;
  const user = userEvent.setup();
  mount();
  expect(await screen.findByText(/120 个节点、340 条连线/)).toBeInTheDocument();
  expect(screen.getByText(/按共同涉及记忆数从高到低保留/)).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "聚焦小周" }));
  await waitFor(() => expect(requests.at(-1)!.url).toContain("person_id=b"));
  expect(screen.getByLabelText("聚焦人物")).toHaveValue("b");
});

test("graph errors are retryable and empty data is explicit", async () => {
  failGraph = true;
  const user = userEvent.setup();
  mount();
  expect(await screen.findByText("读取失败，请重试")).toBeInTheDocument();
  failGraph = false;
  graph = { ...graph, nodes: [], edges: [], total_nodes: 0, total_edges: 0 };
  await user.click(screen.getByRole("button", { name: "重新读取关系图" }));
  expect(
    await screen.findByText("当前范围没有人物关系数据"),
  ).toBeInTheDocument();
});

test("evidence failure does not substitute old excerpts and can retry", async () => {
  failEvidence = true;
  const user = userEvent.setup();
  mount();
  await screen.findByRole("group", { name: "人物关系图" });
  await user.click(
    screen.getAllByRole("button", { name: "查看小林与小周的连线依据" })[0],
  );
  const dialog = await screen.findByRole("dialog");
  expect(
    await within(dialog).findByText("读取失败，请重试"),
  ).toBeInTheDocument();
  expect(
    within(dialog).queryByText("小林与小周是同事。"),
  ).not.toBeInTheDocument();
  failEvidence = false;
  await user.click(
    within(dialog).getByRole("button", { name: "重新读取依据" }),
  );
  expect(
    await within(dialog).findByText("小林与小周是同事。"),
  ).toBeInTheDocument();
});

test("plain text summaries and names are never interpreted as HTML", async () => {
  graph.nodes[0].name = "<img src=x onerror=alert(1)>";
  graph.edges[0].relationship_summaries[0].summary = "<script>danger</script>";
  const { container } = mount();
  await screen.findByRole("group", { name: "人物关系图" });
  expect(container.querySelector("script, img")).toBeNull();
  expect(screen.getAllByText(/<script>danger/).length).toBeGreaterThan(0);
});

const sevenNodes = [
  ...data.nodes,
  { ...data.nodes[1], id: "yue", name: "小月" },
  { ...data.nodes[1], id: "captain", name: "船长" },
  { ...data.nodes[1], id: "old_lin", name: "林同学" },
  { ...data.nodes[1], id: "qing", name: "小青" },
];

test("summaries appear on hover or keyboard focus, never as permanent SVG text", async () => {
  const user = userEvent.setup();
  mount();
  const svg = await screen.findByRole("group", { name: "人物关系图" });
  const edge = within(svg).getByRole("button", {
    name: "查看小林与小周的连线依据",
  });
  expect(within(svg).getByText("可能同一人 · 2")).toBeInTheDocument();
  expect(within(svg).queryByText("小林与小周是同事。")).not.toBeInTheDocument();
  expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  await user.hover(edge);
  expect(
    within(screen.getByRole("tooltip")).getByText("小林与小周是同事。"),
  ).toBeInTheDocument();
  expect(edge).toHaveAttribute(
    "aria-describedby",
    screen.getByRole("tooltip").id,
  );
  expect(within(svg).queryByText("小林与小周是同事。")).not.toBeInTheDocument();
  await user.unhover(edge);
  expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  act(() => edge.focus());
  expect(
    within(screen.getByRole("tooltip")).getByText("小林与小周是同事。"),
  ).toBeInTheDocument();
  await user.tab();
  expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
});

test("edge labels contain only the short kind and shared memory count", () => {
  expect(edgeLabel({ ...data.edges[0], links: [] })).toBe("关系 · 2");
  expect(
    edgeLabel({ ...data.edges[0], links: [], relationship_count: 0 }),
  ).toBe("共现 · 2");
  expect(
    edgeLabel({
      ...data.edges[0],
      memory_count: 0,
      links: [{ ...data.edges[0].links[0], kind: "roleplay" }],
    }),
  ).toBe("扮演 · 0");
});

test("seven nodes fit the initial 315px canvas inside a 375px phone without horizontal scrolling", async () => {
  const original = HTMLElement.prototype.getBoundingClientRect;
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
    function (this: HTMLElement) {
      return this.classList.contains("graph-scroll")
        ? new DOMRect(0, 0, 315, 330)
        : original.call(this);
    },
  );
  graph.nodes = sevenNodes;
  graph.total_nodes = 7;
  mount();
  const svg = await screen.findByRole("group", { name: "人物关系图" });
  await waitFor(() => expect(svg).toHaveAttribute("viewBox", "0 0 315 330"));
  expect(svg).toHaveStyle({ minWidth: "315px" });
  const circles = [...svg.querySelectorAll("a circle")];
  expect(circles).toHaveLength(7);
  for (const circle of circles) {
    expect(Number(circle.getAttribute("cx"))).toBeGreaterThanOrEqual(48);
    expect(Number(circle.getAttribute("cx"))).toBeLessThanOrEqual(267);
    expect(Number(circle.getAttribute("cy"))).toBeGreaterThanOrEqual(24);
    expect(Number(circle.getAttribute("cy"))).toBeLessThanOrEqual(266);
  }
});

test("layout fills the available surface, keeps minimum spacing and is deterministic", () => {
  expect(graphLayout([], [])).toEqual({});
  for (const width of [315, 900]) {
    const points = graphLayout(sevenNodes, data.edges, width);
    expect(points).toEqual(graphLayout(sevenNodes, data.edges, width));
    const frame = graphFrame(7, width);
    const values = Object.values(points);
    const xs = values.map((p) => p.x),
      ys = values.map((p) => p.y);
    expect(Math.max(...xs) - Math.min(...xs)).toBeGreaterThan(
      frame.width * 0.65,
    );
    expect(Math.max(...ys) - Math.min(...ys)).toBeGreaterThan(
      frame.height * 0.65,
    );
    for (let i = 0; i < values.length; i++) {
      expect(values[i].x).toBeGreaterThanOrEqual(frame.marginX);
      expect(values[i].x).toBeLessThanOrEqual(frame.width - frame.marginX);
      expect(values[i].y).toBeGreaterThanOrEqual(frame.top);
      expect(values[i].y).toBeLessThanOrEqual(frame.height - frame.bottom);
      for (let j = 0; j < i; j++) {
        expect(
          Math.hypot(values[i].x - values[j].x, values[i].y - values[j].y),
        ).toBeGreaterThanOrEqual(width < 560 ? 104 : 135);
      }
    }
  }
  expect(graphLayout([data.nodes[0]], [])[data.nodes[0].id]).toEqual({
    x: 450,
    y: 88,
  });
});

// A third person's name sits under the midpoint of this connection. The label
// must move rather than cover that person (the regression from the screenshots).
test("short edge labels avoid an intervening node and its name", () => {
  const points = {
    "a /林": { x: 88, y: 150 },
    b: { x: 812, y: 150 },
    self: { x: 450, y: 134 },
  };
  const label = graphLabels(data.nodes, data.edges, points, graphFrame(7))[
    edgeKey(data.edges[0])
  ];
  expect(label).toBeDefined();
  // The visible '可能同一人 · 2' label needs roughly 50px either side.
  expect(
    Math.abs(label.x - points.self.x) > 134 || label.y < 85 || label.y > 220,
  ).toBe(true);
});
