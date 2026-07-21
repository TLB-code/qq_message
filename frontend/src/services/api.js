async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...options,
  });
  const text = await response.text();
  let data = {};
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      const responseType = response.headers.get("Content-Type") || "未知类型";
      const statusText = response.statusText ? ` ${response.statusText}` : "";
      const error = new Error(
        response.ok
          ? `服务器返回了无法识别的响应（${responseType}）`
          : `请求失败：${response.status}${statusText}，服务器返回了非 JSON 响应`,
      );
      error.status = response.status;
      error.responseText = text.slice(0, 500);
      throw error;
    }
  }

  if (!response.ok) {
    const error = new Error(data.error || `请求失败：${response.status}`);
    error.status = response.status;
    throw error;
  }

  return data;
}

export function getAuthStatus() {
  return requestJson("/api/auth/status");
}

export function login(password) {
  return requestJson("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({ password }),
  });
}

export function logout() {
  return requestJson("/api/auth/logout", {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export function listGroups() {
  return requestJson("/api/groups");
}

export function getGroupDetail(groupId, limit = 500) {
  return requestJson(`/api/groups/${encodeURIComponent(groupId)}?limit=${limit}`);
}

export function getUnreadMessages(groupId, { cursor = null, limit = 100 } = {}) {
  const params = new URLSearchParams({ limit: String(limit) });
  if (cursor) {
    params.set("before_timestamp", String(cursor.before_timestamp));
    params.set("before_message_id", String(cursor.before_message_id));
  }

  return requestJson(`/api/groups/${encodeURIComponent(groupId)}/unread?${params.toString()}`);
}

export function getSummaries(groupId, { cursor = null, limit = 5, includeTotal = true } = {}) {
  const params = new URLSearchParams({
    limit: String(limit),
    include_total: String(includeTotal),
  });
  if (cursor) {
    params.set("before_id", String(cursor.before_id));
  }

  return requestJson(`/api/groups/${encodeURIComponent(groupId)}/summaries?${params.toString()}`);
}

export function markSummaryRead(groupId, summaryId) {
  return requestJson(
    `/api/groups/${encodeURIComponent(groupId)}/summaries/${encodeURIComponent(summaryId)}/read`,
    {
      method: "POST",
      body: JSON.stringify({}),
    },
  );
}

export function summarizeGroup(groupId, limit = 5000) {
  return requestJson(`/api/groups/${encodeURIComponent(groupId)}/summarize`, {
    method: "POST",
    body: JSON.stringify({ limit, mark_read: true }),
  });
}

export function getSummaryTask(groupId, taskId) {
  return requestJson(
    `/api/groups/${encodeURIComponent(groupId)}/summary-tasks/${encodeURIComponent(taskId)}`,
  );
}

export function getActiveSummaryTask(groupId) {
  return requestJson(`/api/groups/${encodeURIComponent(groupId)}/summary-tasks/active`);
}

export function markGroupRead(groupId) {
  return requestJson(`/api/groups/${encodeURIComponent(groupId)}/mark-read`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export function setGroupAutoSummary(groupId, enabled) {
  return requestJson(`/api/groups/${encodeURIComponent(groupId)}/auto-summary`, {
    method: "POST",
    body: JSON.stringify({ enabled }),
  });
}

export function getHistory(groupId, { cursor = null, date = "", limit = 50, includeTotal = true } = {}) {
  const params = new URLSearchParams({
    limit: String(limit),
    include_total: String(includeTotal),
  });
  if (date) params.set("date", date);
  if (cursor) {
    params.set("before_timestamp", String(cursor.before_timestamp));
    params.set("before_message_id", String(cursor.before_message_id));
  }

  return requestJson(`/api/groups/${encodeURIComponent(groupId)}/history?${params.toString()}`);
}

export function deleteHistoryDay(groupId, date) {
  return requestJson(
    `/api/groups/${encodeURIComponent(groupId)}/history?date=${encodeURIComponent(date)}`,
    { method: "DELETE" },
  );
}
