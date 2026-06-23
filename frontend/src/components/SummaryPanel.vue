<template>
  <section class="panel summary-panel">
    <div class="panel-header">
      <div>
        <h2>总结历史</h2>
        <p>最新总结会显示在最上方</p>
      </div>
      <span class="count-badge">{{ summaries.length }}</span>
    </div>

    <div v-if="summaries.length" class="summary-list">
      <article v-for="(summary, index) in summaries" :key="summary.id" class="summary-card">
        <header class="summary-card-head">
          <div>
            <h3>{{ index === 0 ? "最新总结" : `历史总结 #${summary.id}` }}</h3>
            <p>{{ formatFullTime(summary.created_at) }} · {{ summary.model }}</p>
          </div>
          <span>{{ summary.message_count || 0 }} 条</span>
        </header>
        <SummaryMarkdown :markdown="summary.summary" />
      </article>
    </div>
    <EmptyState
      v-else
      title="还没有总结"
      description="选择群聊后点击总结未读生成第一条摘要"
      :icon="ScrollText"
    />
  </section>
</template>

<script setup>
import { ScrollText } from "@lucide/vue";
import EmptyState from "./EmptyState.vue";
import SummaryMarkdown from "./SummaryMarkdown.vue";
import { formatFullTime } from "../utils/format";

defineProps({
  summaries: {
    type: Array,
    required: true,
  },
});
</script>
