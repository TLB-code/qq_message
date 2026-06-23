<template>
  <section class="panel history-panel">
    <div class="panel-header history-header">
      <div>
        <h2>历史消息</h2>
        <p>{{ loadedDate ? "当前日期" : "总共" }} {{ totalCount }} 条 · 已加载 {{ messages.length }} 条</p>
      </div>
      <span class="count-badge">{{ totalCount }}</span>
    </div>

    <div class="history-tools">
      <label>
        <CalendarDays :size="15" />
        <input
          :value="date"
          type="date"
          aria-label="选择历史日期"
          @change="$emit('update:date', $event.target.value)"
        />
      </label>
      <button type="button" :disabled="!date || loading" @click="$emit('load-date')">
        <Search :size="15" />
      </button>
      <button type="button" :disabled="loading" @click="$emit('clear-date')">
        <Clock3 :size="15" />
      </button>
      <button class="danger-icon-button" type="button" :disabled="!date || loading" @click="$emit('delete-date')">
        <Trash2 :size="15" />
      </button>
    </div>

    <div ref="scroller" class="history-scroll" @scroll="handleScroll">
      <div v-if="messages.length" class="history-marker">
        <LoaderCircle v-if="loading && hasMore" :size="15" class="spinning" />
        <span v-else>{{ hasMore ? "上滑加载更早消息" : "已到最早消息" }}</span>
      </div>

      <MessageItem v-for="message in messages" :key="message.message_id" :message="message" />

      <EmptyState
        v-if="!messages.length"
        title="暂无历史消息"
        :description="date ? '这一天没有本地记录' : '选择群聊后会自动加载最近消息'"
        :icon="Archive"
      />
    </div>
  </section>
</template>

<script setup>
import { ref } from "vue";
import { Archive, CalendarDays, Clock3, LoaderCircle, Search, Trash2 } from "@lucide/vue";
import EmptyState from "./EmptyState.vue";
import MessageItem from "./MessageItem.vue";

defineProps({
  messages: {
    type: Array,
    required: true,
  },
  totalCount: {
    type: Number,
    default: 0,
  },
  loadedDate: {
    type: String,
    default: "",
  },
  date: {
    type: String,
    default: "",
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

const emit = defineEmits(["load-more", "load-date", "clear-date", "delete-date", "update:date"]);
const scroller = ref(null);

function handleScroll(event) {
  if (event.currentTarget.scrollTop <= 24) {
    emit("load-more");
  }
}

function scrollToBottom() {
  requestAnimationFrame(() => {
    if (!scroller.value) return;
    scroller.value.scrollTop = scroller.value.scrollHeight;
  });
}

function preservePosition(previousHeight, previousTop) {
  requestAnimationFrame(() => {
    if (!scroller.value) return;
    scroller.value.scrollTop = scroller.value.scrollHeight - previousHeight + previousTop;
  });
}

function isNearBottom() {
  if (!scroller.value) return true;
  const distance = scroller.value.scrollHeight - scroller.value.scrollTop - scroller.value.clientHeight;
  return distance <= 80;
}

function getScrollSnapshot() {
  if (!scroller.value) return { height: 0, top: 0 };
  return {
    height: scroller.value.scrollHeight,
    top: scroller.value.scrollTop,
  };
}

defineExpose({
  getScrollSnapshot,
  isNearBottom,
  preservePosition,
  scrollToBottom,
});
</script>
