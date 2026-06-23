<template>
  <aside class="sidebar">
    <div class="brand">
      <div class="brand-mark">
        <MessageSquareText :size="22" stroke-width="2" />
      </div>
      <div>
        <h1>QQ 群消息总结</h1>
        <p>DeepSeek 摘要工作台</p>
      </div>
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
            <span class="group-stats">{{ group.message_count || 0 }} 条本地消息</span>
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
  </aside>
</template>

<script setup>
import { MessageSquareText, MessagesSquare, RefreshCw } from "@lucide/vue";
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
});

defineEmits(["refresh", "select"]);
</script>
