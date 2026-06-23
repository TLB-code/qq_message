async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await response.text();
  const data = text ? JSON.parse(text) : {};

  if (!response.ok) {
    throw new Error(data.error || `请求失败：${response.status}`);
  }

  return data;
}

export function listGroups() {
  return requestJson("/api/groups");
}

export function getGroupDetail(groupId, limit = 500) {
  return requestJson(`/api/groups/${encodeURIComponent(groupId)}?limit=${limit}`);
}

export function summarizeGroup(groupId, limit = 500) {
  return requestJson(`/api/groups/${encodeURIComponent(groupId)}/summarize`, {
    method: "POST",
    body: JSON.stringify({ limit, mark_read: true }),
  });
}

export function markGroupRead(groupId) {
  return requestJson(`/api/groups/${encodeURIComponent(groupId)}/mark-read`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export function getHistory(groupId, { cursor = null, date = "", limit = 50 } = {}) {
  const params = new URLSearchParams({ limit: String(limit) });
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
