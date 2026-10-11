import type { GraphEdge, GraphNode } from "./PeopleGraph";

export type Point = { x: number; y: number };
export type GraphFrame = ReturnType<typeof graphFrame>;
export const edgeKey = (edge: GraphEdge) =>
  JSON.stringify([edge.source, edge.target]);
export const edgeLabel = (edge: GraphEdge) => {
  const kind = edge.links.some((link) => link.kind === "same_as")
    ? "可能同一人"
    : edge.links.some((link) => link.kind === "roleplay")
      ? "扮演"
      : edge.relationship_count
        ? "关系"
        : "共现";
  return `${kind} · ${edge.memory_count}`;
};

// Small graphs fit the available width, including a phone. Larger graphs get a
// scrollable surface instead of shrinking names or sacrificing node spacing.
export function graphFrame(count: number, availableWidth = 900) {
  const compact = availableWidth < 560;
  const marginX = compact ? 48 : 88;
  const gapX = compact ? 104 : 180;
  const gapY = compact ? 110 : 135;
  const top = compact ? 24 : 36;
  const bottom = compact ? 64 : 80;
  const desiredColumns = Math.max(
    1,
    Math.ceil(Math.sqrt(count * (compact ? 1 : 1.7))),
  );
  const width = Math.max(
    260,
    availableWidth,
    count > 12 ? marginX * 2 + (desiredColumns - 1) * gapX : 0,
  );
  const columns = Math.min(
    desiredColumns,
    Math.max(1, Math.floor((width - marginX * 2) / gapX) + 1),
  );
  const rows = Math.max(1, Math.ceil(count / columns));
  const height = Math.max(
    count <= 3 ? 220 : compact ? 330 : 400,
    top + bottom + (rows - 1) * gapY,
  );
  return { width, height, compact, marginX, top, bottom, rows };
}

// Fixed work, deterministic output. Use the force layout to keep connected
// people near each other, then assign spaced slots across the whole surface.
// This prevents attraction from collapsing a small graph into its centre.
export function graphLayout(
  nodes: GraphNode[],
  edges: GraphEdge[],
  availableWidth = 900,
): Record<string, Point> {
  if (!nodes.length) return {};
  const frame = graphFrame(nodes.length, availableWidth);
  const points = nodes.map((_, i) => ({
    x: 200 * Math.cos((i * 2 * Math.PI) / nodes.length),
    y: 200 * Math.sin((i * 2 * Math.PI) / nodes.length),
  }));
  const indices = new Map(nodes.map((node, i) => [node.id, i]));
  for (let step = 0; step < 90; step++) {
    const forces = points.map((p) => ({ x: -p.x * 0.009, y: -p.y * 0.009 }));
    for (let i = 0; i < points.length; i++) {
      for (let j = i + 1; j < points.length; j++) {
        const dx = points[i].x - points[j].x,
          dy = points[i].y - points[j].y;
        const distance = Math.max(1, Math.hypot(dx, dy));
        const force = Math.min(12, 6500 / (distance * distance));
        forces[i].x += (dx / distance) * force;
        forces[i].y += (dy / distance) * force;
        forces[j].x -= (dx / distance) * force;
        forces[j].y -= (dy / distance) * force;
      }
    }
    for (const edge of edges) {
      const a = indices.get(edge.source),
        b = indices.get(edge.target);
      if (a === undefined || b === undefined) continue;
      const dx = points[b].x - points[a].x,
        dy = points[b].y - points[a].y;
      const distance = Math.max(1, Math.hypot(dx, dy));
      const force = (distance - 165) * 0.025;
      forces[a].x += (dx / distance) * force;
      forces[a].y += (dy / distance) * force;
      forces[b].x -= (dx / distance) * force;
      forces[b].y -= (dy / distance) * force;
    }
    const speed = 5 * (1 - step / 100);
    points.forEach((point, i) => {
      point.x += Math.max(-speed, Math.min(speed, forces[i].x));
      point.y += Math.max(-speed, Math.min(speed, forces[i].y));
    });
  }
  const slots: Point[] = [];
  for (let row = 0; row < frame.rows; row++) {
    const count =
      Math.floor(nodes.length / frame.rows) +
      (row < nodes.length % frame.rows ? 1 : 0);
    for (let col = 0; col < count; col++) {
      slots.push({
        x:
          count === 1
            ? frame.width / 2
            : frame.marginX +
              (col * (frame.width - frame.marginX * 2)) / (count - 1),
        y:
          frame.rows === 1
            ? (frame.top + frame.height - frame.bottom) / 2
            : frame.top +
              (row * (frame.height - frame.top - frame.bottom)) /
                (frame.rows - 1),
      });
    }
  }
  const xs = points.map((p) => p.x),
    ys = points.map((p) => p.y);
  const minX = Math.min(...xs),
    minY = Math.min(...ys);
  const spanX = Math.max(...xs) - minX,
    spanY = Math.max(...ys) - minY;
  return Object.fromEntries(
    nodes.map((node, i) => {
      const x =
        spanX > 0.001
          ? frame.marginX +
            ((points[i].x - minX) / spanX) * (frame.width - frame.marginX * 2)
          : frame.width / 2;
      const y =
        spanY > 0.001
          ? frame.top +
            ((points[i].y - minY) / spanY) *
              (frame.height - frame.top - frame.bottom)
          : (frame.top + frame.height - frame.bottom) / 2;
      let nearest = 0;
      for (let j = 1; j < slots.length; j++) {
        if (
          Math.hypot(slots[j].x - x, slots[j].y - y) <
          Math.hypot(slots[nearest].x - x, slots[nearest].y - y)
        )
          nearest = j;
      }
      return [node.id, slots.splice(nearest, 1)[0]];
    }),
  );
}

type Rect = { left: number; right: number; top: number; bottom: number };
const overlaps = (a: Rect, b: Rect) =>
  a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;

// Keep short labels away from circles, names, counts and previously placed
// labels. Dense edges may omit a label; their tooltip and text list remain.
export function graphLabels(
  nodes: GraphNode[],
  edges: GraphEdge[],
  points: Record<string, Point>,
  frame: GraphFrame,
): Record<string, Point> {
  const occupied: Rect[] = nodes.map((node) => {
    const p = points[node.id];
    return {
      left: p.x - (frame.compact ? 48 : 84),
      right: p.x + (frame.compact ? 48 : 84),
      top: p.y - (frame.compact ? 22 : 33),
      bottom: p.y + (frame.compact ? 57 : 70),
    };
  });
  const placed: Record<string, Point> = {};
  for (const edge of edges) {
    const a = points[edge.source],
      b = points[edge.target];
    const dx = b.x - a.x,
      dy = b.y - a.y,
      length = Math.max(1, Math.hypot(dx, dy));
    const fontSize = frame.compact ? 11 : 12;
    const width = Array.from(edgeLabel(edge)).reduce(
      (sum, char) => sum + fontSize * (char.charCodeAt(0) > 255 ? 1 : 0.65),
      12,
    );
    const height = fontSize + 10;
    let found = false;
    for (const fraction of [0.5, 0.35, 0.65, 0.2, 0.8]) {
      for (const offset of [-16, 16, -32, 32, -48, 48]) {
        const p = {
          x: a.x + dx * fraction - (dy / length) * offset,
          y: a.y + dy * fraction + (dx / length) * offset,
        };
        const rect = {
          left: p.x - width / 2,
          right: p.x + width / 2,
          top: p.y - height / 2,
          bottom: p.y + height / 2,
        };
        if (
          rect.left < 4 ||
          rect.right > frame.width - 4 ||
          rect.top < 4 ||
          rect.bottom > frame.height - 4 ||
          occupied.some((other) => overlaps(rect, other))
        )
          continue;
        placed[edgeKey(edge)] = p;
        occupied.push(rect);
        found = true;
        break;
      }
      if (found) break;
    }
  }
  return placed;
}
