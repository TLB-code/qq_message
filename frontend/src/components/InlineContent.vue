<template>
  <template v-for="(token, index) in tokens" :key="index">
    <strong v-if="token.type === 'strong'">{{ token.text }}</strong>
    <code v-else-if="token.type === 'code'">{{ token.text }}</code>
    <template v-else>{{ token.text }}</template>
  </template>
</template>

<script setup>
defineProps({
  tokens: {
    type: Array,
    required: true,
  },
});
</script>
