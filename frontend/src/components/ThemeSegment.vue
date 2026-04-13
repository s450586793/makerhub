<template>
  <div :class="['theme-toggle', compact && 'theme-toggle--compact']">
    <button
      v-for="option in options"
      :key="option.value"
      :class="['theme-toggle__option', value === option.value && 'is-active']"
      type="button"
      @click="$emit('change', option.value)"
    >
      {{ option.label }}
    </button>
  </div>
</template>

<script setup>
defineProps({
  value: {
    type: String,
    default: "auto",
  },
  compact: {
    type: Boolean,
    default: false,
  },
});

defineEmits(["change"]);

const options = [
  { value: "light", label: "浅色" },
  { value: "dark", label: "深色" },
  { value: "auto", label: "自动" },
];
</script>
