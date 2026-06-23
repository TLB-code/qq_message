<template>
  <div class="message-content" :class="{ 'has-parts': normalizedParts.length }">
    <template v-if="normalizedParts.length">
      <template v-for="(part, index) in normalizedParts" :key="`${part.type}-${index}`">
        <span v-if="part.type === 'text'" class="message-text">{{ part.text }}</span>
        <span v-else-if="part.type === 'at'" class="message-at">{{ part.text }}</span>
        <div v-else-if="part.type === 'reply'" class="message-reply">
          <span>{{ part.text || "回复消息" }}</span>
        </div>
        <a
          v-else-if="isImageLike(part) && part.url"
          class="message-media message-media-image"
          :href="part.url"
          target="_blank"
          rel="noreferrer"
        >
          <img
            :src="imageSource(part)"
            :alt="part.name || part.label || '图片'"
            loading="lazy"
            decoding="async"
            referrerpolicy="no-referrer"
            @error="handleImageError($event, part)"
          />
          <span class="message-media-meta">
            <span class="message-media-label">{{ part.label || mediaLabel(part) }}</span>
            <span v-if="part.name" class="message-media-name">{{ part.name }}</span>
          </span>
        </a>
        <span v-else class="message-chip" :class="`message-chip-${part.type || 'unknown'}`">
          <span class="message-media-label">{{ part.label || mediaLabel(part) }}</span>
          <span v-if="part.name" class="message-media-name">{{ part.name }}</span>
        </span>
      </template>
    </template>
    <template v-else>{{ fallback }}</template>
  </div>
</template>

<script setup>
import { computed } from "vue";

const props = defineProps({
  message: {
    type: Object,
    required: true,
  },
});

const normalizedParts = computed(() => {
  if (!Array.isArray(props.message.display_parts)) return [];
  return props.message.display_parts.filter(Boolean);
});

const fallback = computed(() => props.message.content || "");

function isImageLike(part) {
  return part.type === "image" || part.type === "sticker";
}

function imageSource(part) {
  return part.proxy_url || part.url;
}

function handleImageError(event, part) {
  if (part.proxy_url && event.currentTarget.src !== part.url) {
    event.currentTarget.src = part.url;
  }
}

function mediaLabel(part) {
  const labels = {
    image: "图片",
    sticker: "表情包",
    face: "QQ 表情",
    attachment: "附件",
  };
  return labels[part.type] || part.type || "消息";
}
</script>
