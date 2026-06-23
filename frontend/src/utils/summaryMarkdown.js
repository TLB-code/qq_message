export function parseInline(text) {
  return String(text || "")
    .split(/(\*\*[^*]+\*\*|`[^`]+`)/g)
    .filter(Boolean)
    .map((token) => {
      if (token.startsWith("**") && token.endsWith("**")) {
        return { type: "strong", text: token.slice(2, -2) };
      }
      if (token.startsWith("`") && token.endsWith("`")) {
        return { type: "code", text: token.slice(1, -1) };
      }
      return { type: "text", text: token };
    });
}

export function parseSummaryMarkdown(markdown) {
  const blocks = [];
  let currentList = null;
  const lines = String(markdown || "").replace(/\r\n/g, "\n").split("\n");

  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line) {
      currentList = null;
      continue;
    }

    const heading = line.match(/^(#{2,4})\s+(.+)$/);
    if (heading) {
      currentList = null;
      blocks.push({
        type: "heading",
        level: Math.min(heading[1].length, 4),
        content: parseInline(heading[2]),
      });
      continue;
    }

    const bullet = line.match(/^[-*]\s+(.+)$/) || line.match(/^\d+[.)]\s+(.+)$/);
    if (bullet) {
      if (!currentList) {
        currentList = { type: "list", items: [] };
        blocks.push(currentList);
      }
      currentList.items.push(parseInline(bullet[1]));
      continue;
    }

    currentList = null;
    blocks.push({ type: "paragraph", content: parseInline(line) });
  }

  if (!blocks.length) {
    blocks.push({ type: "paragraph", content: parseInline("暂无总结内容") });
  }

  return blocks;
}
