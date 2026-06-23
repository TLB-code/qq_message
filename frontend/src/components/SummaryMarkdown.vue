<template>
  <div class="summary-markdown">
    <template v-for="(block, index) in blocks" :key="index">
      <h3 v-if="block.type === 'heading' && block.level <= 2">
        <InlineContent :tokens="block.content" />
      </h3>
      <h4 v-else-if="block.type === 'heading'">
        <InlineContent :tokens="block.content" />
      </h4>
      <ul v-else-if="block.type === 'list'">
        <li v-for="(item, itemIndex) in block.items" :key="itemIndex">
          <InlineContent :tokens="item" />
        </li>
      </ul>
      <p v-else>
        <InlineContent :tokens="block.content" />
      </p>
    </template>
  </div>
</template>

<script setup>
import { computed } from "vue";
import InlineContent from "./InlineContent.vue";
import { parseSummaryMarkdown } from "../utils/summaryMarkdown";

const props = defineProps({
  markdown: {
    type: String,
    default: "",
  },
});

const blocks = computed(() => parseSummaryMarkdown(props.markdown));
</script>
