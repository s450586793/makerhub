<template>
  <div class="secret-field">
    <textarea
      v-if="revealed"
      :value="modelValue"
      :rows="rows"
      wrap="soft"
      :placeholder="placeholder"
      spellcheck="false"
      @input="$emit('update:modelValue', $event.target.value)"
    />
    <textarea
      v-else
      :value="maskedDisplay"
      :rows="rows"
      wrap="soft"
      readonly
      :placeholder="placeholder"
      spellcheck="false"
    />
    <button class="button button-secondary button-small" type="button" @click="revealed = !revealed">
      {{ revealed ? "隐藏" : "显示" }}
    </button>
  </div>
</template>

<script setup>
import { computed, ref } from "vue";


const props = defineProps({
  modelValue: {
    type: String,
    default: "",
  },
  placeholder: {
    type: String,
    default: "",
  },
  rows: {
    type: Number,
    default: 9,
  },
});

defineEmits(["update:modelValue"]);

const revealed = ref(false);

const maskedDisplay = computed(() => {
  const secret = String(props.modelValue || "");
  if (!secret) {
    return "";
  }

  const chunks = [];
  for (let index = 0; index < secret.length; index += 64) {
    chunks.push("•".repeat(Math.min(64, secret.length - index)));
  }
  return chunks.join("\n");
});
</script>
