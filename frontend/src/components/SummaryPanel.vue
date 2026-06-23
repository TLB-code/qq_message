<template>
  <section class="panel summary-panel">
    <div class="panel-header">
      <div>
        <h2>总结历史</h2>
        <p>总共 {{ totalCount }} 条 · 已加载 {{ summaries.length }} 条</p>
      </div>
      <span class="count-badge">{{ totalCount }}</span>
    </div>

    <div v-if="summaries.length" class="summary-list" @scroll="handleScroll">
      <article v-for="(summary, index) in summaries" :key="summary.id" class="summary-card">
        <header class="summary-card-head">
          <div>
            <h3>{{ index === 0 ? "最新总结" : `历史总结 #${summary.id}` }}</h3>
            <p>{{ formatFullTime(summary.created_at) }} · {{ summary.model }}</p>
          </div>
          <div class="summary-card-actions">
            <span>{{ summary.message_count || 0 }} 条</span>
            <button
              class="summary-read-button"
              type="button"
              :class="{ read: summary.is_read }"
              :disabled="Boolean(summary.is_read) || markingReadIds.includes(summary.id)"
              @click="$emit('mark-read', summary.id)"
            >
              <Check v-if="summary.is_read" :size="14" />
              <LoaderCircle v-else-if="markingReadIds.includes(summary.id)" :size="14" class="spinning" />
              <Circle v-else :size="14" />
              <span>{{ summary.is_read ? "已读" : "标记已读" }}</span>
            </button>
          </div>
        </header>
        <SummaryMarkdown :markdown="summary.summary" />
      </article>

      <div class="summary-load-marker">
        <LoaderCircle v-if="loading" :size="15" class="spinning" />
        <span v-else>{{ hasMore ? "继续下滑加载更多总结" : "已加载全部总结" }}</span>
      </div>
    </div>
    <EmptyState
      v-else
      title="还没有总结"
      :description="loading ? '正在加载总结历史' : '选择群聊后点击总结未读生成第一条摘要'"
      :icon="ScrollText"
    />
  </section>
</template>

<script setup>
import { Check, Circle, LoaderCircle, ScrollText } from "@lucide/vue";
import EmptyState from "./EmptyState.vue";
import SummaryMarkdown from "./SummaryMarkdown.vue";
import { formatFullTime } from "../utils/format";

defineProps({
  summaries: {
    type: Array,
    required: true,
  },
  totalCount: {
    type: Number,
    default: 0,
  },
  hasMore: {
    type: Boolean,
    default: false,
  },
  loading: {
    type: Boolean,
    default: false,
  },
  markingReadIds: {
    type: Array,
    default: () => [],
  },
});

const emit = defineEmits(["load-more", "mark-read"]);

function handleScroll(event) {
  const target = event.currentTarget;
  const distanceFromBottom = target.scrollHeight - target.scrollTop - target.clientHeight;
  if (distanceFromBottom <= 48) {
    emit("load-more");
  }
}
</script>
