<template>
  <main class="app-shell">
    <GroupSidebar
      :groups="groups"
      :selected-group-id="selectedGroupId"
      :loading="isRefreshing"
      @refresh="refreshCurrentView({ force: true })"
      @select="selectGroup"
    />

    <section class="workspace">
      <header class="topbar">
        <div>
          <p class="eyebrow">本地 QQ 群摘要</p>
          <h2>{{ selectedGroup?.group?.group_name || "选择一个群聊" }}</h2>
          <p class="topbar-meta">
            <template v-if="selectedGroup?.group">
              群号 {{ selectedGroup.group.group_id }} · 最近更新 {{ formatFullTime(selectedGroup.group.updated_at) }}
            </template>
            <template v-else>连接 NapCat 后，收到消息的群会自动出现在左侧</template>
          </p>
        </div>
        <div class="topbar-actions">
          <button class="primary-button" type="button" :disabled="!canSummarize" @click="summarizeSelectedGroup">
            <Sparkles :size="17" />
            <span>总结未读</span>
          </button>
          <button type="button" :disabled="!canMutateUnread" @click="markSelectedGroupRead">
            <CheckCheck :size="17" />
            <span>标记已读</span>
          </button>
        </div>
      </header>

      <div v-if="status.message" class="status-bar" :class="status.type">
        <Info v-if="status.type !== 'error'" :size="17" />
        <CircleAlert v-else :size="17" />
        <span>{{ status.message }}</span>
      </div>

      <section class="overview-strip">
        <div>
          <span>未读</span>
          <strong>{{ unreadMessages.length }}</strong>
        </div>
        <div>
          <span>本地总消息</span>
          <strong>{{ selectedGroupStats.messageCount }}</strong>
        </div>
        <div>
          <span>已加载总结</span>
          <strong>{{ summaries.length }}</strong>
        </div>
        <div>
          <span>自动刷新</span>
          <strong>5 秒</strong>
        </div>
      </section>

      <div class="content-grid">
        <UnreadPanel :unread="unreadMessages" />
        <SummaryPanel
          :summaries="summaries"
          :has-more="summaryHistory.hasMore"
          :loading="summaryHistory.isLoading"
          @load-more="loadMoreSummaries"
        />
        <HistoryPanel
          ref="historyPanel"
          v-model:date="history.date"
          :messages="history.messages"
          :has-more="history.hasMore"
          :loading="history.isLoading"
          @load-more="loadMoreHistory"
          @load-date="loadSelectedHistoryDate"
          @clear-date="clearHistoryDate"
          @delete-date="deleteSelectedHistoryDay"
        />
      </div>
    </section>
  </main>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref } from "vue";
import { CheckCheck, CircleAlert, Info, Sparkles } from "@lucide/vue";
import GroupSidebar from "./components/GroupSidebar.vue";
import HistoryPanel from "./components/HistoryPanel.vue";
import SummaryPanel from "./components/SummaryPanel.vue";
import UnreadPanel from "./components/UnreadPanel.vue";
import {
  deleteHistoryDay,
  getGroupDetail,
  getHistory,
  getSummaries,
  listGroups,
  markGroupRead,
  summarizeGroup,
} from "./services/api";
import { formatFullTime } from "./utils/format";

const AUTO_REFRESH_INTERVAL_MS = 5000;
const HISTORY_PAGE_SIZE = 50;
const SUMMARY_PAGE_SIZE = 5;

const groups = ref([]);
const selectedGroupId = ref(null);
const selectedGroup = ref(null);
const isRefreshing = ref(false);
const isMutating = ref(false);
const historyPanel = ref(null);
const status = reactive({ message: "", type: "" });
const history = reactive({
  groupId: null,
  messages: [],
  nextCursor: null,
  hasMore: false,
  isLoading: false,
  initialized: false,
  date: "",
});
const summaryHistory = reactive({
  groupId: null,
  summaries: [],
  nextCursor: null,
  hasMore: false,
  isLoading: false,
  initialized: false,
});

let refreshTimer = null;

const unreadMessages = computed(() => selectedGroup.value?.unread || []);
const summaries = computed(() => summaryHistory.summaries);
const selectedGroupStats = computed(() => {
  const found = groups.value.find((group) => group.group_id === selectedGroupId.value);
  return {
    messageCount: found?.message_count || 0,
  };
});
const canMutateUnread = computed(() => Boolean(selectedGroupId.value && unreadMessages.value.length && !isMutating.value));
const canSummarize = computed(() => canMutateUnread.value);

function setStatus(message, type = "") {
  status.message = message;
  status.type = type;
}

function resetHistoryState(groupId, date = "") {
  history.groupId = groupId;
  history.messages = [];
  history.nextCursor = null;
  history.hasMore = false;
  history.isLoading = false;
  history.initialized = false;
  history.date = date;
}

function resetSummaryState(groupId) {
  summaryHistory.groupId = groupId;
  summaryHistory.summaries = [];
  summaryHistory.nextCursor = null;
  summaryHistory.hasMore = false;
  summaryHistory.isLoading = false;
  summaryHistory.initialized = false;
}

async function loadGroups() {
  const data = await listGroups();
  groups.value = data.groups || [];
}

async function loadSelectedGroupDetail(groupId = selectedGroupId.value) {
  if (!groupId) return;
  const detail = await getGroupDetail(groupId, 500);
  if (selectedGroupId.value === groupId) {
    selectedGroup.value = detail;
  }
}

async function loadHistoryMessages({ reset = false, date = history.date, preserve = false } = {}) {
  const groupId = selectedGroupId.value;
  if (!groupId || history.isLoading) return;
  if (!reset && history.initialized && !history.hasMore) return;

  if (reset || history.groupId !== groupId || history.date !== date) {
    resetHistoryState(groupId, date);
  }

  const snapshot = preserve ? historyPanel.value?.getScrollSnapshot() : null;
  history.isLoading = true;

  try {
    const data = await getHistory(groupId, {
      cursor: reset ? null : history.nextCursor,
      date: history.date,
      limit: HISTORY_PAGE_SIZE,
    });
    if (selectedGroupId.value !== groupId || history.groupId !== groupId) return;

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
  } finally {
    history.isLoading = false;
    await nextTick();
    if (reset) {
      historyPanel.value?.scrollToBottom();
    } else if (snapshot) {
      historyPanel.value?.preservePosition(snapshot.height, snapshot.top);
    }
  }
}

async function loadSummaryHistory({ reset = false } = {}) {
  const groupId = selectedGroupId.value;
  if (!groupId || summaryHistory.isLoading) return;
  if (!reset && summaryHistory.initialized && !summaryHistory.hasMore) return;

  if (reset || summaryHistory.groupId !== groupId) {
    resetSummaryState(groupId);
  }

  summaryHistory.isLoading = true;

  try {
    const data = await getSummaries(groupId, {
      cursor: reset ? null : summaryHistory.nextCursor,
      limit: SUMMARY_PAGE_SIZE,
    });
    if (selectedGroupId.value !== groupId || summaryHistory.groupId !== groupId) return;

    const records = data.summaries || [];
    if (reset) {
      summaryHistory.summaries = records;
    } else {
      const known = new Set(summaryHistory.summaries.map((summary) => summary.id));
      summaryHistory.summaries = [
        ...summaryHistory.summaries,
        ...records.filter((summary) => !known.has(summary.id)),
      ];
    }
    summaryHistory.nextCursor = data.next_cursor || null;
    summaryHistory.hasMore = Boolean(data.has_more);
    summaryHistory.initialized = true;
  } finally {
    summaryHistory.isLoading = false;
  }
}

async function appendNewHistoryMessagesIfAtBottom() {
  const groupId = selectedGroupId.value;
  if (!groupId || history.groupId !== groupId || !history.initialized || history.isLoading || history.date) return;
  if (!historyPanel.value?.isNearBottom()) return;

  const data = await getHistory(groupId, { limit: HISTORY_PAGE_SIZE });
  if (selectedGroupId.value !== groupId || history.groupId !== groupId) return;

  const known = new Set(history.messages.map((message) => message.message_id));
  const newer = (data.messages || []).filter((message) => !known.has(message.message_id));
  if (!newer.length) return;

  history.messages = [...history.messages, ...newer];
  await nextTick();
  historyPanel.value?.scrollToBottom();
}

async function selectGroup(groupId) {
  selectedGroupId.value = groupId;
  selectedGroup.value = null;
  resetHistoryState(groupId);
  resetSummaryState(groupId);
  setStatus("正在加载群消息...");

  try {
    await loadSelectedGroupDetail(groupId);
    await loadSummaryHistory({ reset: true });
    await loadHistoryMessages({ reset: true });
    setStatus("");
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function refreshCurrentView({ silent = false, force = false } = {}) {
  if (isRefreshing.value || isMutating.value || (!force && document.hidden)) return;
  isRefreshing.value = true;

  try {
    await loadGroups();
    if (!selectedGroupId.value && groups.value.length) {
      selectedGroupId.value = groups.value[0].group_id;
      resetHistoryState(selectedGroupId.value);
      resetSummaryState(selectedGroupId.value);
      await loadSelectedGroupDetail(selectedGroupId.value);
      await loadSummaryHistory({ reset: true });
      await loadHistoryMessages({ reset: true });
    } else if (selectedGroupId.value) {
      await loadSelectedGroupDetail(selectedGroupId.value);
      if (!summaryHistory.initialized || summaryHistory.groupId !== selectedGroupId.value) {
        await loadSummaryHistory({ reset: true });
      }
      await appendNewHistoryMessagesIfAtBottom();
    }
  } catch (error) {
    if (!silent) setStatus(error.message, "error");
  } finally {
    isRefreshing.value = false;
  }
}

async function summarizeSelectedGroup() {
  if (!selectedGroupId.value) return;
  isMutating.value = true;
  setStatus("正在调用 DeepSeek 总结，当前批次最多 500 条...");

  try {
    await summarizeGroup(selectedGroupId.value, 500);
    await loadGroups();
    await loadSelectedGroupDetail(selectedGroupId.value);
    await loadSummaryHistory({ reset: true });
    await loadHistoryMessages({ reset: true, date: history.date });
    setStatus("总结完成。", "success");
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    isMutating.value = false;
  }
}

async function markSelectedGroupRead() {
  if (!selectedGroupId.value) return;
  isMutating.value = true;
  setStatus("正在标记已读...");

  try {
    await markGroupRead(selectedGroupId.value);
    await loadGroups();
    await loadSelectedGroupDetail(selectedGroupId.value);
    setStatus("已标记为已读。", "success");
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    isMutating.value = false;
  }
}

async function loadMoreHistory() {
  try {
    await loadHistoryMessages({ preserve: true });
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function loadMoreSummaries() {
  try {
    await loadSummaryHistory();
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function loadSelectedHistoryDate() {
  if (!selectedGroupId.value || !history.date) return;
  try {
    await loadHistoryMessages({ reset: true, date: history.date });
    setStatus("");
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function clearHistoryDate() {
  if (!selectedGroupId.value) return;
  try {
    await loadHistoryMessages({ reset: true, date: "" });
    setStatus("");
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function deleteSelectedHistoryDay() {
  if (!selectedGroupId.value || !history.date) return;
  const ok = window.confirm(`确定删除 ${history.date} 当天的本地历史消息吗？此操作只删除本项目数据库中的记录。`);
  if (!ok) return;

  isMutating.value = true;
  try {
    const result = await deleteHistoryDay(selectedGroupId.value, history.date);
    await loadGroups();
    await loadSelectedGroupDetail(selectedGroupId.value);
    await loadHistoryMessages({ reset: true, date: history.date });
    setStatus(`已删除 ${history.date} 的 ${result.deleted_count || 0} 条本地历史消息。`, "success");
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    isMutating.value = false;
  }
}

onMounted(() => {
  refreshCurrentView({ force: true });
  refreshTimer = window.setInterval(() => refreshCurrentView({ silent: true }), AUTO_REFRESH_INTERVAL_MS);
  document.addEventListener("visibilitychange", handleVisibilityChange);
});

onUnmounted(() => {
  if (refreshTimer) window.clearInterval(refreshTimer);
  document.removeEventListener("visibilitychange", handleVisibilityChange);
});

function handleVisibilityChange() {
  if (!document.hidden) {
    refreshCurrentView({ silent: true, force: true });
  }
}
</script>
