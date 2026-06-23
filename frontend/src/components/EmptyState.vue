<template>
  <div class="empty-state">
    <component :is="icon" :size="26" stroke-width="1.8" />
    <div>
      <p>{{ title }}</p>
      <span v-if="description">{{ description }}</span>
    </div>
  </div>
</template>

<script setup>
import { Inbox } from "@lucide/vue";

defineProps({
  icon: {
    type: Object,
    default: Inbox,
  },
  title: {
    type: String,
    required: true,
  },
  description: {
    type: String,
    default: "",
  },
});
</script>
