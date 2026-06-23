<template>
  <article class="message-item">
    <div class="message-avatar" aria-hidden="true">{{ avatarText }}</div>
    <div class="message-main">
      <div class="message-meta">
        <span class="sender">{{ message.sender_name || "未知成员" }}</span>
        <span>{{ formatTime(message.timestamp) }}</span>
      </div>
      <MessageContent :message="message" />
    </div>
  </article>
</template>

<script setup>
import { computed } from "vue";
import MessageContent from "./MessageContent.vue";
import { formatTime } from "../utils/format";

const props = defineProps({
  message: {
    type: Object,
    required: true,
  },
});

const avatarText = computed(() => {
  const name = String(props.message.sender_name || "?").trim();
  return name.slice(0, 1).toUpperCase();
});
</script>
