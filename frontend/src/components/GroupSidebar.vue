<template>
  <aside class="sidebar" :class="{ collapsed }">
    <button
      v-if="collapsed"
      class="sidebar-rail"
      type="button"
      title="展开群聊列表"
      @click="$emit('toggle')"
    >
      <MessageSquareText :size="22" />
      <span>群聊</span>
      <ChevronRight :size="15" />
    </button>

    <template v-else>
      <div class="brand">
        <div class="brand-mark">
          <MessageSquareText :size="22" stroke-width="2" />
        </div>
        <div>
          <h1>QQ 群消息总结</h1>
          <p>DeepSeek 摘要工作台</p>
        </div>
        <button class="sidebar-toggle" type="button" title="收起群聊列表" @click="$emit('toggle')">
          <ChevronLeft :size="16" />
        </button>
      </div>

      <button class="refresh-button" type="button" :disabled="loading" @click="$emit('refresh')">
        <RefreshCw :size="16" :class="{ spinning: loading }" />
        <span>刷新群列表</span>
      </button>

      <div class="sidebar-section-title">群聊</div>
      <div v-if="groups.length" class="group-list">
        <button
          v-for="group in groups"
          :key="group.group_id"
          class="group-item"
          :class="{ active: group.group_id === selectedGroupId }"
          type="button"
          @click="$emit('select', group.group_id)"
        >
          <span class="group-item-main">
            <span class="group-avatar">{{ group.group_name?.slice(0, 1) || "群" }}</span>
            <span class="group-copy">
              <span class="group-name">{{ group.group_name || group.group_id }}</span>
              <span class="group-stats">
                <span>{{ group.message_count || 0 }} 条本地消息</span>
                <span v-if="group.auto_summary_enabled" class="group-auto-badge">自动</span>
              </span>
            </span>
          </span>
          <span class="unread-pill" :class="{ quiet: !group.unread_count }">
            {{ clampCount(group.unread_count) }}
          </span>
        </button>
      </div>
      <EmptyState
        v-else
        title="还没有群消息"
        description="NapCat 收到群消息后会显示在这里"
        :icon="MessagesSquare"
      />
    </template>
  </aside>
</template>

<script setup>
import { ChevronLeft, ChevronRight, MessageSquareText, MessagesSquare, RefreshCw } from "@lucide/vue";
import EmptyState from "./EmptyState.vue";
import { clampCount } from "../utils/format";

defineProps({
  groups: {
    type: Array,
    required: true,
  },
  selectedGroupId: {
    type: String,
    default: null,
  },
  loading: {
    type: Boolean,
    default: false,
  },
  collapsed: {
    type: Boolean,
    default: false,
  },
});

defineEmits(["refresh", "select", "toggle"]);
</script>
