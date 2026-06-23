export function formatTime(value, fallback = "未知时间") {
  if (!value) return fallback;
  return new Date(value * 1000).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatFullTime(value, fallback = "未知时间") {
  if (!value) return fallback;
  return new Date(value * 1000).toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function clampCount(value) {
  const count = Number(value || 0);
  if (count > 999) return "999+";
  return String(count);
}
