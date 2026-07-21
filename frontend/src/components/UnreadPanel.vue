<template>
  <section class="panel unread-panel">
    <div class="panel-header">
      <div>
        <h2>未读消息</h2>
        <p>总共 {{ totalCount }} 条 · 已加载 {{ messages.length }} 条</p>
      </div>
      <span class="count-badge">{{ totalCount }}</span>
    </div>

    <div ref="scroller" class="unread-scroll" @scroll="handleScroll">
      <div
        v-if="messages.length"
        class="unread-virtual-list"
        :style="{ height: `${virtualTotalSize}px` }"
      >
        <div class="unread-marker unread-marker-virtual">
          <LoaderCircle v-if="loading && hasMore" :size="15" class="spinning" />
          <span v-else>{{ hasMore ? "上滑加载更早未读消息" : "已显示全部未读消息" }}</span>
        </div>

        <div
          v-for="virtualRow in virtualRows"
          :key="virtualRow.key"
          :ref="measureVirtualRow"
          class="unread-virtual-row"
          :data-index="virtualRow.index"
          :style="virtualRowStyle(virtualRow)"
        >
          <MessageItem :message="messages[virtualRow.index]" />
        </div>
      </div>

      <EmptyState
        v-if="!messages.length && !loading"
        title="暂无未读"
        description="新消息进来后这里会自动刷新"
        :icon="Inbox"
      />
    </div>
  </section>
</template>

<script setup>
import { computed, ref } from "vue";
import { useVirtualizer } from "@tanstack/vue-virtual";
import { Inbox, LoaderCircle } from "@lucide/vue";
import EmptyState from "./EmptyState.vue";
import MessageItem from "./MessageItem.vue";

const UNREAD_MARKER_HEIGHT = 34;
const ESTIMATED_MESSAGE_HEIGHT = 86;
const VIRTUAL_OVERSCAN = 8;

const props = defineProps({
  messages: {
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
});

const emit = defineEmits(["load-more"]);
const scroller = ref(null);
const virtualizer = useVirtualizer(computed(() => ({
  count: props.messages.length,
  getScrollElement: () => scroller.value,
  estimateSize: () => ESTIMATED_MESSAGE_HEIGHT,
  getItemKey: (index) => props.messages[index]?.message_id ?? index,
  overscan: VIRTUAL_OVERSCAN,
  paddingStart: UNREAD_MARKER_HEIGHT,
})));
const virtualRows = computed(() => virtualizer.value.getVirtualItems());
const virtualTotalSize = computed(() => virtualizer.value.getTotalSize());

function measureVirtualRow(element) {
  if (element) virtualizer.value.measureElement(element);
}

function virtualRowStyle(virtualRow) {
  return { transform: `translateY(${virtualRow.start}px)` };
}

function handleScroll(event) {
  if (event.currentTarget.scrollTop <= 24) emit("load-more");
}

function scrollToBottom() {
  requestAnimationFrame(() => {
    if (!scroller.value || !props.messages.length) return;
    virtualizer.value.scrollToEnd();
  });
}

function preservePosition(previousHeight, previousTop) {
  requestAnimationFrame(() => {
    if (!scroller.value) return;
    const nextTop = virtualizer.value.getTotalSize() - previousHeight + previousTop;
    virtualizer.value.scrollToOffset(Math.max(nextTop, 0));
  });
}

function isNearBottom() {
  if (!scroller.value) return true;
  const distance = virtualizer.value.getTotalSize() - scroller.value.scrollTop - scroller.value.clientHeight;
  return distance <= 80;
}

function getScrollSnapshot() {
  if (!scroller.value) return { height: 0, top: 0 };
  return { height: virtualizer.value.getTotalSize(), top: scroller.value.scrollTop };
}

defineExpose({ getScrollSnapshot, isNearBottom, preservePosition, scrollToBottom });
</script>
