const state = {
  groups: [],
  selectedGroupId: null,
  selectedGroup: null,
};

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
  elements.summarizeBtn.disabled = unread.length === 0;
  elements.markReadBtn.disabled = unread.length === 0;

  if (!unread.length) {
    elements.unreadMessages.className = "message-list empty";
    elements.unreadMessages.textContent = "暂无未读消息。";
  } else {
    elements.unreadMessages.className = "message-list";
    elements.unreadMessages.innerHTML = "";
    for (const message of unread) {
      const item = document.createElement("article");
      item.className = "message";
      item.innerHTML = `
        <div class="message-meta"></div>
        <div class="message-content"></div>
      `;
      item.querySelector(".message-meta").textContent = `${formatTime(message.timestamp)} · ${message.sender_name}`;
      item.querySelector(".message-content").textContent = message.content;
      elements.unreadMessages.appendChild(item);
    }
  }

  if (!summaries.length) {
    elements.summaryList.className = "summary-list empty";
    elements.summaryList.textContent = "暂无总结。";
  } else {
    elements.summaryList.className = "summary-list";
    elements.summaryList.innerHTML = "";
    for (const summary of summaries) {
      const item = document.createElement("article");
      item.className = "summary";
      item.innerHTML = `
        <div class="summary-meta"></div>
        <div class="summary-body"></div>
      `;
      item.querySelector(".summary-meta").textContent =
        `${formatTime(summary.created_at)} · ${summary.message_count} 条消息 · ${summary.model}`;
      item.querySelector(".summary-body").textContent = summary.summary;
      elements.summaryList.appendChild(item);
    }
  }
}

async function loadGroups() {
  const data = await requestJson("/api/groups");
  state.groups = data.groups || [];
  renderGroups();
}

async function selectGroup(groupId) {
  state.selectedGroupId = groupId;
  renderGroups();
  setStatus("正在加载群消息...");
  try {
    const detail = await requestJson(`/api/groups/${encodeURIComponent(groupId)}?limit=500`);
    renderGroupDetail(detail);
    setStatus("");
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function summarizeSelectedGroup() {
  if (!state.selectedGroupId) return;
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
    elements.summarizeBtn.disabled = false;
  }
}

async function markSelectedGroupRead() {
  if (!state.selectedGroupId) return;
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
    elements.markReadBtn.disabled = false;
  }
}

elements.refreshGroups.addEventListener("click", () => {
  loadGroups().catch((error) => setStatus(error.message, "error"));
});
elements.summarizeBtn.addEventListener("click", summarizeSelectedGroup);
elements.markReadBtn.addEventListener("click", markSelectedGroupRead);

loadGroups().catch((error) => setStatus(error.message, "error"));

