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
        <div v-else-if="part.type === 'audio'" class="message-audio">
          <div class="message-audio-header">
            <span class="message-media-label">{{ part.label || "语音" }}</span>
            <span v-if="part.name" class="message-media-name">{{ part.name }}</span>
          </div>
          <audio
            v-if="part.status === 'ready' && part.playback_url"
            class="message-audio-player"
            controls
            preload="metadata"
            :src="part.playback_url"
          />
          <div v-else class="message-audio-status">
            <span>{{ voiceStatus(part) }}</span>
            <button
              v-if="part.retry_url && part.status === 'failed'"
              class="message-audio-retry"
              type="button"
              title="重新处理语音"
              :disabled="retryingMediaIds.has(part.media_id)"
              @click="retryVoice(part)"
            >
              <RotateCw :size="14" aria-hidden="true" />
              <span>重试</span>
            </button>
          </div>
        </div>
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
import { RotateCw } from "@lucide/vue";
import { computed, ref } from "vue";

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
const retryingMediaIds = ref(new Set());

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

function voiceStatus(part) {
  if (retryingMediaIds.value.has(part.media_id)) return "正在重新处理语音...";
  const labels = {
    pending: "语音等待处理",
    processing: "语音正在转码...",
    failed: "语音处理失败",
    unavailable: "语音文件不可用",
  };
  return labels[part.status] || "语音尚未准备完成";
}

async function retryVoice(part) {
  if (!part.retry_url || retryingMediaIds.value.has(part.media_id)) return;
  retryingMediaIds.value = new Set(retryingMediaIds.value).add(part.media_id);
  try {
    const response = await fetch(part.retry_url, {
      method: "POST",
      credentials: "same-origin",
    });
    if (!response.ok) {
      const contentType = response.headers.get("content-type") || "";
      const detail = contentType.includes("application/json")
        ? (await response.json()).error
        : await response.text();
      throw new Error(detail || `HTTP ${response.status}`);
    }
    window.setTimeout(() => clearRetrying(part.media_id), 4000);
  } catch (error) {
    clearRetrying(part.media_id);
    console.error("Voice retry failed", error);
  }
}

function clearRetrying(mediaId) {
  retryingMediaIds.value = new Set(
    [...retryingMediaIds.value].filter((item) => item !== mediaId),
  );
}

function mediaLabel(part) {
  const labels = {
    image: "图片",
    sticker: "表情包",
    face: "QQ 表情",
    audio: "语音",
    attachment: "附件",
  };
  return labels[part.type] || part.type || "消息";
}
</script>
