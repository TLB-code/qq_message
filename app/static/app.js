const state = {
  groups: [],
  selectedGroupId: null,
  selectedGroup: null,
  isAutoRefreshing: false,
  isMutating: false,
  history: {
    groupId: null,
    messages: [],
    nextCursor: null,
    hasMore: false,
    isLoading: false,
    initialized: false,
    date: "",
  },
};

const AUTO_REFRESH_INTERVAL_MS = 5000;
const HISTORY_PAGE_SIZE = 50;

const elements = {
  groupList: document.querySelector("#groupList"),
  refreshGroups: document.querySelector("#refreshGroups"),
  groupTitle: document.querySelector("#groupTitle"),
  groupMeta: document.querySelector("#groupMeta"),
  summarizeBtn: document.querySelector("#summarizeBtn"),
  markReadBtn: document.querySelector("#markReadBtn"),
  status: document.querySelector("#status"),
  unreadCount: document.querySelector("#unreadCount"),
  unreadMessages: document.querySelector("#unreadMessages"),
  summaryList: document.querySelector("#summaryList"),
  historyCount: document.querySelector("#historyCount"),
  historyMessages: document.querySelector("#historyMessages"),
  historyDate: document.querySelector("#historyDate"),
  loadHistoryDateBtn: document.querySelector("#loadHistoryDateBtn"),
  clearHistoryDateBtn: document.querySelector("#clearHistoryDateBtn"),
  deleteHistoryDayBtn: document.querySelector("#deleteHistoryDayBtn"),
};

function formatTime(value) {
  if (!value) return "未知时间";
  return new Date(value * 1000).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function setStatus(message, type = "") {
  if (!message) {
    elements.status.hidden = true;
    elements.status.className = "status";
    elements.status.textContent = "";
    return;
  }
  elements.status.hidden = false;
  elements.status.className = `status ${type}`.trim();
  elements.status.textContent = message;
}

function appendInlineMarkdown(parent, text) {
  const tokens = String(text).split(/(\*\*[^*]+\*\*|`[^`]+`)/g);
  for (const token of tokens) {
    if (!token) continue;
    if (token.startsWith("**") && token.endsWith("**")) {
      const strong = document.createElement("strong");
      strong.textContent = token.slice(2, -2);
      parent.appendChild(strong);
    } else if (token.startsWith("`") && token.endsWith("`")) {
      const code = document.createElement("code");
      code.textContent = token.slice(1, -1);
      parent.appendChild(code);
    } else {
      parent.appendChild(document.createTextNode(token));
    }
  }
}

function renderSummaryMarkdown(markdown) {
  const body = document.createElement("div");
  body.className = "summary-body";
  const lines = String(markdown || "").replace(/\r\n/g, "\n").split("\n");
  let list = null;

  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line) {
      list = null;
      continue;
    }

    const heading = line.match(/^(#{2,4})\s+(.+)$/);
    if (heading) {
      list = null;
      const headingLevel = heading[1].length === 2 ? "h3" : "h4";
      const headingElement = document.createElement(headingLevel);
      appendInlineMarkdown(headingElement, heading[2]);
      body.appendChild(headingElement);
      continue;
    }

    const bullet = line.match(/^[-*]\s+(.+)$/) || line.match(/^\d+[.)]\s+(.+)$/);
    if (bullet) {
      if (!list) {
        list = document.createElement("ul");
        body.appendChild(list);
      }
      const item = document.createElement("li");
      appendInlineMarkdown(item, bullet[1]);
      list.appendChild(item);
      continue;
    }

    list = null;
    const paragraph = document.createElement("p");
    appendInlineMarkdown(paragraph, line);
    body.appendChild(paragraph);
  }

  if (!body.childElementCount) {
    const paragraph = document.createElement("p");
    paragraph.textContent = "暂无总结内容";
    body.appendChild(paragraph);
  }
  return body;
}

function appendMediaMeta(parent, part) {
  const label = document.createElement("span");
  label.className = "message-media-label";
  label.textContent = part.label || "媒体";
  parent.appendChild(label);

  if (part.name) {
    const name = document.createElement("span");
    name.className = "message-media-name";
    name.textContent = part.name;
    parent.appendChild(name);
  }
}

function renderImagePart(part) {
  const wrapper = part.url ? document.createElement("a") : document.createElement("div");
  wrapper.className = "message-image";
  if (part.url) {
    wrapper.href = part.url;
    wrapper.target = "_blank";
    wrapper.rel = "noreferrer";

    const image = document.createElement("img");
    image.src = part.url;
    image.alt = part.name || part.label || "图片";
    image.loading = "lazy";
    image.decoding = "async";
    image.addEventListener("error", () => {
      wrapper.classList.add("is-broken");
      image.remove();
    });
    wrapper.appendChild(image);
  }

  const meta = document.createElement("span");
  meta.className = "message-image-meta";
  appendMediaMeta(meta, part);
  wrapper.appendChild(meta);
  return wrapper;
}

function renderChipPart(part) {
  const chip = document.createElement("span");
  chip.className = `message-chip ${part.type ? `message-chip-${part.type}` : ""}`.trim();
  appendMediaMeta(chip, part);
  return chip;
}

function renderMessageContent(container, message) {
  container.innerHTML = "";
  const parts = Array.isArray(message.display_parts) ? message.display_parts : [];
  if (!parts.length) {
    container.textContent = message.content || "";
    return;
  }

  container.classList.add("message-content-parts");
  for (const part of parts) {
    if (part.type === "text") {
      const span = document.createElement("span");
      span.className = "message-text";
      span.textContent = part.text || "";
      container.appendChild(span);
    } else if (part.type === "at") {
      const span = document.createElement("span");
      span.className = "message-at";
      span.textContent = part.text || "";
      container.appendChild(span);
    } else if (part.type === "reply") {
      const reply = document.createElement("div");
      reply.className = "message-reply";
      reply.textContent = part.text || "回复消息";
      container.appendChild(reply);
    } else if (part.type === "image" || (part.type === "sticker" && part.url)) {
      container.appendChild(renderImagePart(part));
    } else if (["face", "sticker", "attachment"].includes(part.type)) {
      container.appendChild(renderChipPart(part));
    } else {
      container.appendChild(renderChipPart({ label: part.label || part.type || "消息", name: part.name || "" }));
    }
  }
}

function createMessageElement(message, extraClass = "") {
  const item = document.createElement("article");
  item.className = `message ${extraClass}`.trim();
  item.innerHTML = `
    <div class="message-meta"></div>
    <div class="message-content"></div>
  `;
  item.querySelector(".message-meta").textContent = `${formatTime(message.timestamp)} · ${message.sender_name}`;
  renderMessageContent(item.querySelector(".message-content"), message);
  return item;
}

function resetHistoryState(groupId, date = "") {
  state.history = {
    groupId,
    messages: [],
    nextCursor: null,
    hasMore: false,
    isLoading: false,
    initialized: false,
    date,
  };
  renderHistoryMessages();
}

function renderHistoryMessages() {
  const history = state.history;
  elements.historyCount.textContent = history.messages.length;
  elements.historyDate.value = history.date || "";
  elements.deleteHistoryDayBtn.disabled = !state.selectedGroupId || !history.date || history.isLoading;

  if (!state.selectedGroupId) {
    elements.historyMessages.className = "message-list history-list empty";
    elements.historyMessages.textContent = "还没有选择群。";
    return;
  }

  if (!history.initialized && history.isLoading) {
    elements.historyMessages.className = "message-list history-list empty";
    elements.historyMessages.textContent = "正在加载历史消息...";
    return;
  }

  if (!history.messages.length) {
    elements.historyMessages.className = "message-list history-list empty";
    elements.historyMessages.textContent = history.initialized ? "暂无历史消息。" : "正在加载历史消息...";
    return;
  }

  elements.historyMessages.className = "message-list history-list";
  elements.historyMessages.innerHTML = "";

  const marker = document.createElement("div");
  marker.className = "history-marker";
  marker.textContent = history.isLoading ? "正在加载更早消息..." : (history.hasMore ? "" : "已到最早消息");
  if (marker.textContent) {
    elements.historyMessages.appendChild(marker);
  }

  for (const message of history.messages) {
    elements.historyMessages.appendChild(createMessageElement(message, "history-message"));
  }
}

function historyUrl(groupId, cursor = null, date = state.history.date) {
  const params = new URLSearchParams({ limit: String(HISTORY_PAGE_SIZE) });
  if (date) {
    params.set("date", date);
  }
  if (cursor) {
    params.set("before_timestamp", String(cursor.before_timestamp));
    params.set("before_message_id", String(cursor.before_message_id));
  }
  return `/api/groups/${encodeURIComponent(groupId)}/history?${params.toString()}`;
}

async function loadHistoryMessages({ reset = false, date = state.history.date } = {}) {
  const groupId = state.selectedGroupId;
  if (!groupId || state.history.isLoading) return;
  if (!reset && state.history.initialized && !state.history.hasMore) return;

  if (reset || state.history.groupId !== groupId || state.history.date !== date) {
    resetHistoryState(groupId, date);
  }

  const history = state.history;
  history.isLoading = true;
  renderHistoryMessages();

  const container = elements.historyMessages;
  const previousHeight = container.scrollHeight;
  const previousTop = container.scrollTop;
  const cursor = reset ? null : history.nextCursor;

  try {
    const data = await requestJson(historyUrl(groupId, cursor, history.date));
    if (state.selectedGroupId !== groupId || state.history.groupId !== groupId) return;

    const messages = data.messages || [];
    if (reset) {
      history.messages = messages;
    } else {
      const known = new Set(history.messages.map((message) => message.message_id));
      history.messages = [
        ...messages.filter((message) => !known.has(message.message_id)),
        ...history.messages,
      ];
    }
    history.nextCursor = data.next_cursor || null;
    history.hasMore = Boolean(data.has_more);
    history.initialized = true;
    history.isLoading = false;
    renderHistoryMessages();

    if (reset) {
      container.scrollTop = container.scrollHeight;
    } else {
      container.scrollTop = container.scrollHeight - previousHeight + previousTop;
    }
  } catch (error) {
    history.isLoading = false;
    renderHistoryMessages();
    throw error;
  }
}

async function appendNewHistoryMessagesIfAtBottom() {
  const groupId = state.selectedGroupId;
  const history = state.history;
  if (!groupId || history.groupId !== groupId || !history.initialized || history.isLoading) return;
  if (history.date) return;

  const container = elements.historyMessages;
  const distanceFromBottom = container.scrollHeight - container.scrollTop - container.clientHeight;
  if (distanceFromBottom > 80) return;

  const data = await requestJson(historyUrl(groupId));
  if (state.selectedGroupId !== groupId || state.history.groupId !== groupId) return;

  const known = new Set(history.messages.map((message) => message.message_id));
  const newer = (data.messages || []).filter((message) => !known.has(message.message_id));
  if (!newer.length) return;

  history.messages = [...history.messages, ...newer];
  renderHistoryMessages();
  container.scrollTop = container.scrollHeight;
}

function handleHistoryScroll() {
  if (elements.historyMessages.classList.contains("empty")) return;
  if (elements.historyMessages.scrollTop > 24) return;
  loadHistoryMessages({ reset: false }).catch((error) => setStatus(error.message, "error"));
}

async function loadSelectedHistoryDate() {
  const date = elements.historyDate.value;
  if (!state.selectedGroupId || !date) return;
  try {
    await loadHistoryMessages({ reset: true, date });
    setStatus("");
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function clearHistoryDate() {
  if (!state.selectedGroupId) return;
  try {
    await loadHistoryMessages({ reset: true, date: "" });
    setStatus("");
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function deleteSelectedHistoryDay() {
  const groupId = state.selectedGroupId;
  const date = state.history.date || elements.historyDate.value;
  if (!groupId || !date) return;

  const ok = window.confirm(`确定删除 ${date} 当天的本地历史消息吗？此操作只删除本项目数据库里的消息记录，不能撤销。`);
  if (!ok) return;

  state.history.isLoading = true;
  renderHistoryMessages();
  try {
    const result = await requestJson(`/api/groups/${encodeURIComponent(groupId)}/history?date=${encodeURIComponent(date)}`, {
      method: "DELETE",
    });
    await loadGroups();
    await loadSelectedGroupDetail(groupId);
    await loadHistoryMessages({ reset: true, date });
    setStatus(`已删除 ${date} 的 ${result.deleted_count || 0} 条本地历史消息。`, "success");
  } catch (error) {
    state.history.isLoading = false;
    renderHistoryMessages();
    setStatus(error.message, "error");
  }
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || `请求失败：${response.status}`);
  }
  return data;
}

function renderGroups() {
  if (!state.groups.length) {
    elements.groupList.className = "group-list empty";
    elements.groupList.textContent = "暂无群消息";
    return;
  }

  elements.groupList.className = "group-list";
  elements.groupList.innerHTML = "";
  for (const group of state.groups) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `group-item ${group.group_id === state.selectedGroupId ? "active" : ""}`.trim();
    button.innerHTML = `
      <span class="group-name"></span>
      <span class="group-stats"></span>
    `;
    button.querySelector(".group-name").textContent = group.group_name;
    button.querySelector(".group-stats").textContent = `${group.unread_count || 0} 条未读 / ${group.message_count || 0} 条总消息`;
    button.addEventListener("click", () => selectGroup(group.group_id));
    elements.groupList.appendChild(button);
  }
}

function renderGroupDetail(detail) {
  state.selectedGroup = detail;
  const group = detail.group;
  const unread = detail.unread || [];
  const summaries = detail.summaries || [];

  elements.groupTitle.textContent = group.group_name;
  elements.groupMeta.textContent = `群号 ${group.group_id} · 最近更新 ${formatTime(group.updated_at)}`;
  elements.unreadCount.textContent = unread.length;
  elements.summarizeBtn.disabled = state.isMutating || unread.length === 0;
  elements.markReadBtn.disabled = state.isMutating || unread.length === 0;

  if (!unread.length) {
    elements.unreadMessages.className = "message-list empty";
    elements.unreadMessages.textContent = "暂无未读消息。";
  } else {
    elements.unreadMessages.className = "message-list";
    elements.unreadMessages.innerHTML = "";
    for (const message of unread) {
      elements.unreadMessages.appendChild(createMessageElement(message));
    }
  }

  if (!summaries.length) {
    elements.summaryList.className = "summary-list empty";
    elements.summaryList.textContent = "暂无总结。";
  } else {
    elements.summaryList.className = "summary-list";
    elements.summaryList.innerHTML = "";
    for (const [index, summary] of summaries.entries()) {
      const item = document.createElement("article");
      item.className = "summary";
      item.innerHTML = `
        <div class="summary-head">
          <div>
            <div class="summary-title"></div>
            <div class="summary-meta"></div>
          </div>
          <span class="summary-badge"></span>
        </div>
        <div class="summary-body"></div>
      `;
      item.querySelector(".summary-title").textContent = index === 0 ? "最新总结" : `历史总结 #${summary.id}`;
      item.querySelector(".summary-badge").textContent = `${summary.message_count || 0} 条`;
      item.querySelector(".summary-meta").textContent =
        `${formatTime(summary.created_at)} · ${summary.model}`;
      item.querySelector(".summary-body").replaceWith(renderSummaryMarkdown(summary.summary));
      elements.summaryList.appendChild(item);
    }
  }
}

async function loadGroups() {
  const data = await requestJson("/api/groups");
  state.groups = data.groups || [];
  renderGroups();
}

async function loadSelectedGroupDetail(groupId = state.selectedGroupId) {
  if (!groupId) return;
  const detail = await requestJson(`/api/groups/${encodeURIComponent(groupId)}?limit=500`);
  if (state.selectedGroupId === groupId) {
    renderGroupDetail(detail);
  }
}

async function selectGroup(groupId) {
  state.selectedGroupId = groupId;
  renderGroups();
  resetHistoryState(groupId);
  setStatus("正在加载群消息...");
  try {
    await loadSelectedGroupDetail(groupId);
    await loadHistoryMessages({ reset: true });
    setStatus("");
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function refreshCurrentView({ silent = false, force = false } = {}) {
  if (state.isAutoRefreshing || state.isMutating || (!force && document.hidden)) return;
  state.isAutoRefreshing = true;
  try {
    await loadGroups();
    if (state.selectedGroupId) {
      await loadSelectedGroupDetail(state.selectedGroupId);
      await appendNewHistoryMessagesIfAtBottom();
    }
  } catch (error) {
    if (!silent) {
      setStatus(error.message, "error");
    }
  } finally {
    state.isAutoRefreshing = false;
  }
}

async function summarizeSelectedGroup() {
  if (!state.selectedGroupId) return;
  state.isMutating = true;
  elements.summarizeBtn.disabled = true;
  setStatus("正在调用 DeepSeek 总结，请稍等...");
  try {
    await requestJson(`/api/groups/${encodeURIComponent(state.selectedGroupId)}/summarize`, {
      method: "POST",
      body: JSON.stringify({ limit: 500, mark_read: true }),
    });
    await loadGroups();
    await selectGroup(state.selectedGroupId);
    setStatus("总结完成。", "success");
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    state.isMutating = false;
    if (state.selectedGroup) {
      renderGroupDetail(state.selectedGroup);
    }
  }
}

async function markSelectedGroupRead() {
  if (!state.selectedGroupId) return;
  state.isMutating = true;
  elements.markReadBtn.disabled = true;
  setStatus("正在标记已读...");
  try {
    await requestJson(`/api/groups/${encodeURIComponent(state.selectedGroupId)}/mark-read`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    await loadGroups();
    await selectGroup(state.selectedGroupId);
    setStatus("已标记为已读。", "success");
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    state.isMutating = false;
    if (state.selectedGroup) {
      renderGroupDetail(state.selectedGroup);
    }
  }
}

elements.refreshGroups.addEventListener("click", () => {
  refreshCurrentView().catch((error) => setStatus(error.message, "error"));
});
elements.summarizeBtn.addEventListener("click", summarizeSelectedGroup);
elements.markReadBtn.addEventListener("click", markSelectedGroupRead);
elements.historyMessages.addEventListener("scroll", handleHistoryScroll);
elements.loadHistoryDateBtn.addEventListener("click", loadSelectedHistoryDate);
elements.clearHistoryDateBtn.addEventListener("click", clearHistoryDate);
elements.deleteHistoryDayBtn.addEventListener("click", deleteSelectedHistoryDay);
elements.historyDate.addEventListener("change", loadSelectedHistoryDate);

refreshCurrentView({ force: true }).catch((error) => setStatus(error.message, "error"));
setInterval(() => {
  refreshCurrentView({ silent: true });
}, AUTO_REFRESH_INTERVAL_MS);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    refreshCurrentView({ silent: true, force: true });
  }
});
