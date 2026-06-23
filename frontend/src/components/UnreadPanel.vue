<template>
  <section class="panel unread-panel">
    <div class="panel-header">
      <div>
        <h2>未读消息</h2>
        <p>本次最多总结 500 条，剩余消息可再次点击总结</p>
      </div>
      <span class="count-badge">{{ unread.length }}</span>
    </div>

    <div v-if="unread.length" class="message-list">
      <MessageItem v-for="message in unread" :key="message.message_id" :message="message" />
    </div>
    <EmptyState
      v-else
      title="暂无未读"
      description="新消息进来后这里会自动刷新"
      :icon="Inbox"
    />
  </section>
</template>

<script setup>
import { Inbox } from "@lucide/vue";
import EmptyState from "./EmptyState.vue";
import MessageItem from "./MessageItem.vue";

defineProps({
  unread: {
    type: Array,
    required: true,
  },
});
</script>
