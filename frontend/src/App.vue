<template>
  <main v-if="auth.checked && auth.required && !auth.authenticated" class="login-shell">
    <form class="login-panel" @submit.prevent="loginToApp">
      <div class="brand-mark login-mark">
        <ShieldCheck :size="24" stroke-width="2" />
      </div>
      <div>
        <p class="eyebrow">QQ 群消息总结</p>
        <h1>输入访问密码</h1>
        <p>登录后才能查看群消息、总结历史和本地记录。</p>
      </div>
      <label class="login-field">
        <span>密码</span>
        <input v-model="auth.password" type="password" autocomplete="current-password" autofocus />
      </label>
      <button class="primary-button" type="submit" :disabled="auth.isLoading || !auth.password">
        <ShieldCheck :size="17" />
        <span>{{ auth.isLoading ? "正在登录" : "登录" }}</span>
      </button>
      <p v-if="auth.error" class="login-error">{{ auth.error }}</p>
    </form>
  </main>

  <main v-else-if="!auth.checked" class="login-shell">
    <div class="login-panel">
      <div class="brand-mark login-mark">
        <ShieldCheck :size="24" stroke-width="2" />
      </div>
      <p>正在检查登录状态...</p>
    </div>
  </main>

  <main
    v-else
    class="app-shell"
    :class="{
      'sidebar-is-collapsed': layout.sidebarCollapsed,
      'is-resizing-layout': resizeState.active,
    }"
  >
    <GroupSidebar
      :groups="groups"
      :selected-group-id="selectedGroupId"
      :loading="isRefreshing"
      :collapsed="layout.sidebarCollapsed"
      @refresh="refreshCurrentView({ force: true })"
      @select="selectGroup"
      @toggle="toggleSidebar"
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
          <label class="summary-limit-field">
            <span>总结数量</span>
            <input
              v-model="summaryLimitInput"
              type="number"
              inputmode="numeric"
              min="1"
              :max="MANUAL_SUMMARY_MAX"
              :placeholder="String(MANUAL_SUMMARY_MAX)"
              :disabled="isMutating || isSummaryRunning"
              aria-label="手动总结消息数量"
              @keydown.enter="summarizeSelectedGroup"
            />
            <span>条</span>
          </label>
          <button class="primary-button" type="button" :disabled="!canSummarize" @click="summarizeSelectedGroup">
            <Sparkles :size="17" />
            <span>总结未读</span>
          </button>
          <button type="button" :disabled="!canMutateUnread" @click="markSelectedGroupRead">
            <CheckCheck :size="17" />
            <span>标记已读</span>
          </button>
          <button
            class="auto-summary-toggle"
            type="button"
            :class="{ active: selectedGroupAutoSummaryEnabled }"
            :disabled="!selectedGroupId || isMutating || isSummaryRunning"
            @click="toggleSelectedGroupAutoSummary"
          >
            <Bot :size="17" />
            <span>{{ selectedGroupAutoSummaryEnabled ? "自动总结已开" : "开启自动总结" }}</span>
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
          <strong>{{ unreadTotalCount }}</strong>
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
          <strong>3 秒</strong>
        </div>
      </section>

      <div
        ref="panelGrid"
        class="content-grid"
        :class="{ 'has-collapsed-panels': hasCollapsedPanels }"
        :style="contentGridStyle"
      >
        <div
          class="panel-frame"
          :class="{ collapsed: isPanelCollapsed('unread') }"
          data-panel="unread"
        >
          <button
            v-if="isPanelCollapsed('unread')"
            class="panel-rail"
            type="button"
            title="展开未读消息"
            @click="togglePanel('unread')"
          >
            <Inbox :size="18" />
            <span>未读</span>
            <strong>{{ unreadTotalCount }}</strong>
            <ChevronRight :size="14" />
          </button>
          <template v-else>
            <button class="panel-collapse-button" type="button" title="收起未读消息" @click="togglePanel('unread')">
              <ChevronLeft :size="15" />
            </button>
            <UnreadPanel
              ref="unreadPanel"
              :messages="unreadMessages"
              :total-count="unreadTotalCount"
              :has-more="unread.hasMore"
              :loading="unread.isLoading"
              @load-more="loadMoreUnread"
            />
          </template>
        </div>

        <button
          class="panel-resizer"
          type="button"
          aria-label="调整未读消息和总结历史宽度"
          :disabled="!canResizePanels('unread', 'summary')"
          @pointerdown="startPanelResize('unread', 'summary', $event)"
        >
          <GripVertical :size="15" />
        </button>

        <div
          class="panel-frame"
          :class="{ collapsed: isPanelCollapsed('summary') }"
          data-panel="summary"
        >
          <button
            v-if="isPanelCollapsed('summary')"
            class="panel-rail"
            type="button"
            title="展开总结历史"
            @click="togglePanel('summary')"
          >
            <ScrollText :size="18" />
            <span>总结</span>
            <strong>{{ summaryTotalCount }}</strong>
            <ChevronRight :size="14" />
          </button>
          <template v-else>
            <button class="panel-collapse-button" type="button" title="收起总结历史" @click="togglePanel('summary')">
              <ChevronLeft :size="15" />
            </button>
            <SummaryPanel
              :summaries="summaries"
              :total-count="summaryTotalCount"
              :has-more="summaryHistory.hasMore"
              :loading="summaryHistory.isLoading"
              :marking-read-ids="markingSummaryReadIds"
              @load-more="loadMoreSummaries"
              @mark-read="markSummaryAsRead"
            />
          </template>
        </div>

        <button
          class="panel-resizer"
          type="button"
          aria-label="调整总结历史和历史消息宽度"
          :disabled="!canResizePanels('summary', 'history')"
          @pointerdown="startPanelResize('summary', 'history', $event)"
        >
          <GripVertical :size="15" />
        </button>

        <div
          class="panel-frame"
          :class="{ collapsed: isPanelCollapsed('history') }"
          data-panel="history"
        >
          <button
            v-if="isPanelCollapsed('history')"
            class="panel-rail"
            type="button"
            title="展开历史消息"
            @click="togglePanel('history')"
          >
            <Archive :size="18" />
            <span>历史</span>
            <strong>{{ historyTotalCount }}</strong>
            <ChevronRight :size="14" />
          </button>
          <template v-else>
            <button class="panel-collapse-button" type="button" title="收起历史消息" @click="togglePanel('history')">
              <ChevronLeft :size="15" />
            </button>
            <HistoryPanel
              ref="historyPanel"
              v-model:date="history.date"
              :messages="history.messages"
              :total-count="historyTotalCount"
              :loaded-date="history.loadedDate"
              :has-more="history.hasMore"
              :loading="history.isLoading"
              @load-more="loadMoreHistory"
              @load-date="loadSelectedHistoryDate"
              @clear-date="clearHistoryDate"
              @delete-date="deleteSelectedHistoryDay"
            />
          </template>
        </div>
      </div>
    </section>
  </main>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref } from "vue";
import {
  Archive,
  Bot,
  CheckCheck,
  ChevronLeft,
  ChevronRight,
  CircleAlert,
  GripVertical,
  Inbox,
  Info,
  ScrollText,
  ShieldCheck,
  Sparkles,
} from "@lucide/vue";
import GroupSidebar from "./components/GroupSidebar.vue";
import HistoryPanel from "./components/HistoryPanel.vue";
import SummaryPanel from "./components/SummaryPanel.vue";
import UnreadPanel from "./components/UnreadPanel.vue";
import {
  deleteHistoryDay,
  getActiveSummaryTask,
  getAuthStatus,
  getGroupDetail,
  getHistory,
  getSummaries,
  getSummaryTask,
  getUnreadMessages,
  listGroups,
  login,
  markGroupRead,
  markSummaryRead,
  setGroupAutoSummary,
  summarizeGroup,
} from "./services/api";
import { formatFullTime } from "./utils/format";

const AUTO_REFRESH_INTERVAL_MS = 3000;
const MANUAL_SUMMARY_MAX = 2000;
const UNREAD_PAGE_SIZE = 100;
const HISTORY_PAGE_SIZE = 50;
const SUMMARY_PAGE_SIZE = 5;
const PANEL_IDS = ["unread", "summary", "history"];
const PANEL_MIN_WEIGHT = 0.5;
const PANEL_MIN_WIDTHS = {
  unread: "var(--panel-min-unread, 260px)",
  summary: "var(--panel-min-summary, 300px)",
  history: "var(--panel-min-history, 300px)",
};

const groups = ref([]);
const selectedGroupId = ref(null);
const selectedGroup = ref(null);
const markingSummaryReadIds = ref([]);
const isRefreshing = ref(false);
const isMutating = ref(false);
const summaryLimitInput = ref("");
const unreadPanel = ref(null);
const historyPanel = ref(null);
const panelGrid = ref(null);
const status = reactive({ message: "", type: "" });
const auth = reactive({
  checked: false,
  required: false,
  authenticated: false,
  password: "",
  isLoading: false,
  error: "",
});
const unread = reactive({
  groupId: null,
  messages: [],
  nextCursor: null,
  hasMore: false,
  isLoading: false,
  initialized: false,
  totalCount: 0,
  syncedTotalCount: 0,
});
const history = reactive({
  groupId: null,
  messages: [],
  nextCursor: null,
  hasMore: false,
  isLoading: false,
  initialized: false,
  totalCount: 0,
  date: "",
  loadedDate: "",
});
const summaryHistory = reactive({
  groupId: null,
  summaries: [],
  nextCursor: null,
  hasMore: false,
  isLoading: false,
  initialized: false,
  totalCount: 0,
});
const summaryTask = reactive({
  taskId: "",
  groupId: "",
  status: "",
  requestedLimit: 0,
});
const layout = reactive({
  sidebarCollapsed: false,
  collapsedPanels: {
    unread: false,
    summary: false,
    history: false,
  },
  panelWeights: {
    unread: 0.95,
    summary: 1.15,
    history: 1,
  },
});
const resizeState = reactive({
  active: false,
  leftId: "",
  rightId: "",
  startX: 0,
  startLeftWeight: 0,
  startRightWeight: 0,
  pairWidth: 1,
});

let refreshTimer = null;
let summaryPollGeneration = 0;

const unreadMessages = computed(() => unread.messages);
const summaries = computed(() => summaryHistory.summaries);
const selectedGroupStats = computed(() => {
  const found = groups.value.find((group) => group.group_id === selectedGroupId.value);
  return {
    messageCount: found?.message_count || 0,
    unreadCount: found ? Number(found.unread_count || 0) : null,
  };
});
const unreadTotalCount = computed(() => selectedGroupStats.value.unreadCount ?? unread.totalCount);
const isSummaryRunning = computed(() => ["queued", "running"].includes(summaryTask.status));
const summaryTotalCount = computed(() => summaryHistory.totalCount);
const historyTotalCount = computed(() => {
  if (history.loadedDate) return history.totalCount;
  return selectedGroupStats.value.messageCount || history.totalCount;
});
const selectedGroupAutoSummaryEnabled = computed(() =>
  Boolean(selectedGroup.value?.group?.auto_summary_enabled),
);
const canMutateUnread = computed(() => Boolean(
  selectedGroupId.value
    && unreadTotalCount.value
    && !isMutating.value
    && !isSummaryRunning.value,
));
const canSummarize = computed(() => canMutateUnread.value);
const hasCollapsedPanels = computed(() => PANEL_IDS.some((id) => layout.collapsedPanels[id]));
const contentGridStyle = computed(() => ({
  gridTemplateColumns: [
    panelColumn("unread"),
    "10px",
    panelColumn("summary"),
    "10px",
    panelColumn("history"),
  ].join(" "),
}));

function panelColumn(panelId) {
  if (layout.collapsedPanels[panelId]) {
    return "54px";
  }
  return `minmax(${PANEL_MIN_WIDTHS[panelId]}, ${layout.panelWeights[panelId]}fr)`;
}

function setStatus(message, type = "") {
  status.message = message;
  status.type = type;
}

async function checkAuth() {
  try {
    const result = await getAuthStatus();
    auth.required = Boolean(result.auth_required);
    auth.authenticated = Boolean(result.authenticated);
    auth.error = "";
  } catch (error) {
    auth.required = true;
    auth.authenticated = false;
    auth.error = error.message;
  } finally {
    auth.checked = true;
  }
}

async function loginToApp() {
  if (!auth.password || auth.isLoading) return;
  auth.isLoading = true;
  auth.error = "";
  try {
    await login(auth.password);
    auth.password = "";
    auth.authenticated = true;
    await refreshCurrentView({ force: true });
  } catch (error) {
    auth.error = error.status === 401 ? "密码不正确" : error.message;
  } finally {
    auth.isLoading = false;
  }
}

function toggleSidebar() {
  layout.sidebarCollapsed = !layout.sidebarCollapsed;
}

function isPanelCollapsed(panelId) {
  return Boolean(layout.collapsedPanels[panelId]);
}

function togglePanel(panelId) {
  layout.collapsedPanels[panelId] = !layout.collapsedPanels[panelId];
  if (panelId === "history" && !layout.collapsedPanels[panelId]) {
    nextTick(() => historyPanel.value?.scrollToBottom());
  }
}

function canResizePanels(leftId, rightId) {
  return !layout.collapsedPanels[leftId] && !layout.collapsedPanels[rightId];
}

function startPanelResize(leftId, rightId, event) {
  if (!canResizePanels(leftId, rightId) || event.button !== 0) return;
  const leftElement = panelGrid.value?.querySelector(`[data-panel="${leftId}"]`);
  const rightElement = panelGrid.value?.querySelector(`[data-panel="${rightId}"]`);
  if (!leftElement || !rightElement) return;

  event.preventDefault();
  resizeState.active = true;
  resizeState.leftId = leftId;
  resizeState.rightId = rightId;
  resizeState.startX = event.clientX;
  resizeState.startLeftWeight = layout.panelWeights[leftId];
  resizeState.startRightWeight = layout.panelWeights[rightId];
  resizeState.pairWidth = Math.max(
    leftElement.getBoundingClientRect().width + rightElement.getBoundingClientRect().width,
    1,
  );
  window.addEventListener("pointermove", handlePanelResize);
  window.addEventListener("pointerup", stopPanelResize);
}

function handlePanelResize(event) {
  if (!resizeState.active) return;
  const totalWeight = resizeState.startLeftWeight + resizeState.startRightWeight;
  const deltaWeight = (event.clientX - resizeState.startX) / resizeState.pairWidth * totalWeight;
  const nextLeft = clamp(
    resizeState.startLeftWeight + deltaWeight,
    PANEL_MIN_WEIGHT,
    totalWeight - PANEL_MIN_WEIGHT,
  );
  layout.panelWeights[resizeState.leftId] = Number(nextLeft.toFixed(3));
  layout.panelWeights[resizeState.rightId] = Number((totalWeight - nextLeft).toFixed(3));
}

function stopPanelResize() {
  if (!resizeState.active) return;
  resizeState.active = false;
  window.removeEventListener("pointermove", handlePanelResize);
  window.removeEventListener("pointerup", stopPanelResize);
}

function clamp(value, min, max) {
  return Math.min(Math.max(value, min), max);
}

function resetHistoryState(groupId, date = "") {
  history.groupId = groupId;
  history.messages = [];
  history.nextCursor = null;
  history.hasMore = false;
  history.isLoading = false;
  history.initialized = false;
  history.totalCount = 0;
  history.date = date;
  history.loadedDate = date;
}

function resetUnreadState(groupId) {
  unread.groupId = groupId;
  unread.messages = [];
  unread.nextCursor = null;
  unread.hasMore = false;
  unread.isLoading = false;
  unread.initialized = false;
  unread.totalCount = 0;
  unread.syncedTotalCount = 0;
}

function resetSummaryState(groupId) {
  summaryHistory.groupId = groupId;
  summaryHistory.summaries = [];
  summaryHistory.nextCursor = null;
  summaryHistory.hasMore = false;
  summaryHistory.isLoading = false;
  summaryHistory.initialized = false;
  summaryHistory.totalCount = 0;
}

async function loadGroups() {
  try {
    const data = await listGroups();
    groups.value = data.groups || [];
  } catch (error) {
    handleAuthError(error);
    throw error;
  }
}

async function loadSelectedGroupDetail(groupId = selectedGroupId.value) {
  if (!groupId) return;
  let detail;
  try {
    detail = await getGroupDetail(groupId, 0);
  } catch (error) {
    handleAuthError(error);
    throw error;
  }
  if (selectedGroupId.value === groupId) {
    selectedGroup.value = detail;
  }
}

async function loadUnreadMessages({ reset = false, preserve = false } = {}) {
  const groupId = selectedGroupId.value;
  if (!groupId || unread.isLoading) return;
  if (!reset && unread.initialized && !unread.hasMore) return;

  if (reset || unread.groupId !== groupId) {
    resetUnreadState(groupId);
  }

  const snapshot = preserve ? unreadPanel.value?.getScrollSnapshot() : null;
  unread.isLoading = true;

  try {
    const data = await getUnreadMessages(groupId, {
      cursor: reset ? null : unread.nextCursor,
      limit: UNREAD_PAGE_SIZE,
    });
    if (selectedGroupId.value !== groupId || unread.groupId !== groupId) return;

    const messages = data.messages || [];
    unread.totalCount = Number(data.total_count || 0);
    if (reset) {
      unread.messages = messages;
      unread.syncedTotalCount = unread.totalCount;
    } else {
      const known = new Set(unread.messages.map((message) => message.message_id));
      unread.messages = [
        ...messages.filter((message) => !known.has(message.message_id)),
        ...unread.messages,
      ];
    }
    unread.nextCursor = data.next_cursor || null;
    unread.hasMore = Boolean(data.has_more);
    unread.initialized = true;
  } finally {
    unread.isLoading = false;
    await nextTick();
    if (reset) {
      unreadPanel.value?.scrollToBottom();
    } else if (snapshot) {
      unreadPanel.value?.preservePosition(snapshot.height, snapshot.top);
    }
  }
}

function handleAuthError(error) {
  if (error?.status !== 401) return;
  auth.required = true;
  auth.authenticated = false;
  auth.password = "";
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
      includeTotal: reset,
    });
    if (selectedGroupId.value !== groupId || history.groupId !== groupId) return;

    const messages = data.messages || [];
    if (data.total_count !== undefined) {
      history.totalCount = Number(data.total_count || 0);
    }
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
      includeTotal: reset,
    });
    if (selectedGroupId.value !== groupId || summaryHistory.groupId !== groupId) return;

    const records = data.summaries || [];
    if (data.total_count !== undefined) {
      summaryHistory.totalCount = Number(data.total_count || 0);
    }
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

  const data = await getHistory(groupId, { limit: HISTORY_PAGE_SIZE, includeTotal: false });
  if (selectedGroupId.value !== groupId || history.groupId !== groupId) return;

  const known = new Set(history.messages.map((message) => message.message_id));
  const newer = (data.messages || []).filter((message) => !known.has(message.message_id));
  if (!newer.length) return;

  history.messages = [...history.messages, ...newer];
  await nextTick();
  historyPanel.value?.scrollToBottom();
}

async function syncUnreadAfterRefresh() {
  const groupId = selectedGroupId.value;
  if (!groupId) return;
  if (!unread.initialized || unread.groupId !== groupId) {
    await loadUnreadMessages({ reset: true });
    return;
  }

  const latestTotal = Number(selectedGroupStats.value.unreadCount || 0);
  unread.totalCount = latestTotal;
  if (latestTotal < unread.syncedTotalCount) {
    await loadUnreadMessages({ reset: true });
    return;
  }
  if (latestTotal > unread.syncedTotalCount && unreadPanel.value?.isNearBottom()) {
    await loadUnreadMessages({ reset: true });
  }
}

async function selectGroup(groupId) {
  stopSummaryTaskMonitor();
  selectedGroupId.value = groupId;
  selectedGroup.value = null;
  resetUnreadState(groupId);
  resetHistoryState(groupId);
  resetSummaryState(groupId);
  setStatus("正在加载群消息...");

  try {
    await loadSelectedGroupDetail(groupId);
    await loadUnreadMessages({ reset: true });
    await loadSummaryHistory({ reset: true });
    await loadHistoryMessages({ reset: true });
    await resumeActiveSummaryTask(groupId);
    setStatus("");
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function refreshCurrentView({ silent = false, force = false } = {}) {
  if (!auth.checked || !auth.authenticated) return;
  if (isRefreshing.value || isMutating.value || unread.isLoading || history.isLoading || (!force && document.hidden)) return;
  isRefreshing.value = true;

  try {
    await loadGroups();
    if (!selectedGroupId.value && groups.value.length) {
      selectedGroupId.value = groups.value[0].group_id;
      resetUnreadState(selectedGroupId.value);
      resetHistoryState(selectedGroupId.value);
      resetSummaryState(selectedGroupId.value);
      await loadSelectedGroupDetail(selectedGroupId.value);
      await loadUnreadMessages({ reset: true });
      await loadSummaryHistory({ reset: true });
      await loadHistoryMessages({ reset: true });
      await resumeActiveSummaryTask(selectedGroupId.value);
    } else if (selectedGroupId.value) {
      await loadSelectedGroupDetail(selectedGroupId.value);
      await syncUnreadAfterRefresh();
      if (!summaryHistory.initialized || summaryHistory.groupId !== selectedGroupId.value) {
        await loadSummaryHistory({ reset: true });
      }
      await appendNewHistoryMessagesIfAtBottom();
    }
  } catch (error) {
    handleAuthError(error);
    if (!silent) setStatus(error.message, "error");
  } finally {
    isRefreshing.value = false;
  }
}

function setSummaryTask(task) {
  summaryTask.taskId = task?.task_id || "";
  summaryTask.groupId = task?.group_id || "";
  summaryTask.status = task?.status || "";
  summaryTask.requestedLimit = Number(task?.requested_limit || 0);
}

function stopSummaryTaskMonitor() {
  summaryPollGeneration += 1;
  setSummaryTask(null);
}

function waitForSummaryPoll() {
  return new Promise((resolve) => window.setTimeout(resolve, 2000));
}

async function reloadAfterSummaryTask(task) {
  isMutating.value = true;
  try {
    await loadGroups();
    await loadSelectedGroupDetail(task.group_id);
    await loadUnreadMessages({ reset: true });
    await loadSummaryHistory({ reset: true });
    await loadHistoryMessages({ reset: true, date: history.date });
  } finally {
    isMutating.value = false;
  }
}

async function monitorSummaryTask(initialTask) {
  if (!initialTask?.task_id) return;
  const generation = summaryPollGeneration + 1;
  summaryPollGeneration = generation;
  let task = initialTask;

  while (generation === summaryPollGeneration && selectedGroupId.value === task.group_id) {
    setSummaryTask(task);
    if (task.status === "completed") {
      try {
        await reloadAfterSummaryTask(task);
        if (generation !== summaryPollGeneration) return;
        setStatus(`已完成 ${task.message_count || task.requested_limit} 条消息的总结。`, "success");
      } catch (error) {
        handleAuthError(error);
        setStatus(error.message, "error");
      } finally {
        if (generation === summaryPollGeneration) setSummaryTask(null);
      }
      return;
    }
    if (task.status === "failed") {
      setSummaryTask(null);
      setStatus(`总结失败：${task.error || "后台任务执行失败"}`, "error");
      return;
    }

    const expectedCount = Math.min(task.requested_limit || 0, unreadTotalCount.value);
    setStatus(
      task.status === "queued"
        ? `总结任务已进入后台队列，准备处理 ${expectedCount} 条消息...`
        : `后台正在并行分块总结 ${expectedCount} 条消息...`,
    );
    await waitForSummaryPoll();
    if (generation !== summaryPollGeneration) return;

    try {
      const data = await getSummaryTask(task.group_id, task.task_id);
      task = data.task;
    } catch (error) {
      handleAuthError(error);
      if (error?.status === 401) {
        stopSummaryTaskMonitor();
        return;
      }
      setStatus(`任务状态查询失败，将自动重试：${error.message}`, "error");
    }
  }
}

async function resumeActiveSummaryTask(groupId) {
  const data = await getActiveSummaryTask(groupId);
  if (selectedGroupId.value !== groupId || !data.task) return;
  void monitorSummaryTask(data.task);
}

async function summarizeSelectedGroup() {
  if (!selectedGroupId.value) return;
  const rawLimit = String(summaryLimitInput.value ?? "").trim();
  const limit = rawLimit === "" ? MANUAL_SUMMARY_MAX : Number(rawLimit);
  if (!Number.isInteger(limit) || limit < 1 || limit > MANUAL_SUMMARY_MAX) {
    setStatus(`总结数量必须是 1 到 ${MANUAL_SUMMARY_MAX} 之间的整数。`, "error");
    return;
  }

  isMutating.value = true;
  const expectedCount = Math.min(limit, unreadTotalCount.value);
  setStatus(`正在创建 ${expectedCount} 条消息的后台总结任务...`);

  try {
    const result = await summarizeGroup(selectedGroupId.value, limit);
    setSummaryTask(result.task);
    void monitorSummaryTask(result.task);
  } catch (error) {
    handleAuthError(error);
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
    await loadUnreadMessages({ reset: true });
    setStatus("已标记为已读。", "success");
  } catch (error) {
    handleAuthError(error);
    setStatus(error.message, "error");
  } finally {
    isMutating.value = false;
  }
}

async function toggleSelectedGroupAutoSummary() {
  if (!selectedGroupId.value) return;
  const enabled = !selectedGroupAutoSummaryEnabled.value;
  isMutating.value = true;
  setStatus(enabled ? "正在开启该群自动总结..." : "正在关闭该群自动总结...");

  try {
    const result = await setGroupAutoSummary(selectedGroupId.value, enabled);
    await loadGroups();
    if (selectedGroup.value?.group && result.group) {
      selectedGroup.value = {
        ...selectedGroup.value,
        group: result.group,
      };
    } else {
      await loadSelectedGroupDetail(selectedGroupId.value);
    }
    setStatus(enabled ? "该群已开启自动总结。" : "该群已关闭自动总结。", "success");
  } catch (error) {
    handleAuthError(error);
    setStatus(error.message, "error");
  } finally {
    isMutating.value = false;
  }
}

async function loadMoreHistory() {
  try {
    await loadHistoryMessages({ preserve: true });
  } catch (error) {
    handleAuthError(error);
    setStatus(error.message, "error");
  }
}

async function loadMoreUnread() {
  try {
    await loadUnreadMessages({ preserve: true });
  } catch (error) {
    handleAuthError(error);
    setStatus(error.message, "error");
  }
}

async function loadMoreSummaries() {
  try {
    await loadSummaryHistory();
  } catch (error) {
    handleAuthError(error);
    setStatus(error.message, "error");
  }
}

async function markSummaryAsRead(summaryId) {
  if (!selectedGroupId.value || markingSummaryReadIds.value.includes(summaryId)) return;

  markingSummaryReadIds.value = [...markingSummaryReadIds.value, summaryId];
  try {
    const result = await markSummaryRead(selectedGroupId.value, summaryId);
    const updated = result.summary;
    if (!updated) return;

    summaryHistory.summaries = summaryHistory.summaries.map((summary) =>
      summary.id === updated.id ? { ...summary, ...updated } : summary,
    );
    setStatus("总结已标记为已读。", "success");
  } catch (error) {
    handleAuthError(error);
    setStatus(error.message, "error");
  } finally {
    markingSummaryReadIds.value = markingSummaryReadIds.value.filter((id) => id !== summaryId);
  }
}

async function loadSelectedHistoryDate() {
  if (!selectedGroupId.value || !history.date) return;
  try {
    await loadHistoryMessages({ reset: true, date: history.date });
    setStatus("");
  } catch (error) {
    handleAuthError(error);
    setStatus(error.message, "error");
  }
}

async function clearHistoryDate() {
  if (!selectedGroupId.value) return;
  try {
    await loadHistoryMessages({ reset: true, date: "" });
    setStatus("");
  } catch (error) {
    handleAuthError(error);
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
    await loadUnreadMessages({ reset: true });
    await loadHistoryMessages({ reset: true, date: history.date });
    setStatus(`已删除 ${history.date} 的 ${result.deleted_count || 0} 条本地历史消息。`, "success");
  } catch (error) {
    handleAuthError(error);
    setStatus(error.message, "error");
  } finally {
    isMutating.value = false;
  }
}

onMounted(() => {
  checkAuth().then(() => {
    if (auth.authenticated) {
      refreshCurrentView({ force: true });
    }
  });
  refreshTimer = window.setInterval(() => refreshCurrentView({ silent: true }), AUTO_REFRESH_INTERVAL_MS);
  document.addEventListener("visibilitychange", handleVisibilityChange);
});

onUnmounted(() => {
  summaryPollGeneration += 1;
  if (refreshTimer) window.clearInterval(refreshTimer);
  document.removeEventListener("visibilitychange", handleVisibilityChange);
  stopPanelResize();
});

function handleVisibilityChange() {
  if (!document.hidden && auth.authenticated) {
    refreshCurrentView({ silent: true, force: true });
  }
}
</script>
