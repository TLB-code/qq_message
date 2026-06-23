<template>
  <section class="panel summary-panel">
    <div class="panel-header">
      <div>
        <h2>总结历史</h2>
        <p>每次加载 5 条，向下滚动查看更多</p>
      </div>
      <span class="count-badge">{{ summaries.length }}</span>
    </div>

    <div v-if="summaries.length" class="summary-list" @scroll="handleScroll">
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
import { LoaderCircle, ScrollText } from "@lucide/vue";
import EmptyState from "./EmptyState.vue";
import SummaryMarkdown from "./SummaryMarkdown.vue";
import { formatFullTime } from "../utils/format";

defineProps({
  summaries: {
    type: Array,
    required: true,
  },
  hasMore: {
    type: Boolean,
    default: false,
  },
  loading: {
    type: Boolean,
    default: false,
  },
});

const emit = defineEmits(["load-more"]);

function handleScroll(event) {
  const target = event.currentTarget;
  const distanceFromBottom = target.scrollHeight - target.scrollTop - target.clientHeight;
  if (distanceFromBottom <= 48) {
    emit("load-more");
  }
}
</script>
