<template>
  <article class="model-card model-card--interactive" @click="goDetail">
    <div class="model-card__cover">
      <img
        v-if="model.cover_url"
        :src="coverSrc"
        :alt="model.title"
        loading="lazy"
        @error="onCoverError"
      >
      <div v-else class="media-placeholder">{{ model.title.slice(0, 1) }}</div>
      <span class="source-pill">{{ model.source_label }}</span>
      <label :class="['model-card__checkbox', (selectionMode || selected) && 'is-visible']" @click.stop>
        <input
          type="checkbox"
          :checked="selected"
          @change="$emit('toggle', model.model_dir)"
        >
      </label>
    </div>
    <div class="model-card__body">
      <h2>{{ model.title }}</h2>
      <div class="model-author">
        <img
          v-if="model.author.avatar_url"
          :src="authorAvatarSrc"
          :alt="model.author.name"
          loading="lazy"
          @error="onAuthorAvatarError"
        >
        <span v-else class="avatar-placeholder">{{ model.author.name.slice(0, 1) }}</span>
        <span>{{ model.author.name }}</span>
      </div>
      <div class="tag-row">
        <span v-for="tag in model.tags.slice(0, 4)" :key="tag" class="tag-chip">{{ tag }}</span>
      </div>
      <div class="meta-line">采集于 {{ model.collect_date || "未知时间" }}</div>
      <div class="meta-line">发布于 {{ model.publish_date || "未知时间" }}</div>
    </div>
  </article>
</template>

<script setup>
import { computed, ref } from "vue";
import { useRouter } from "vue-router";

import { encodeModelPath } from "../lib/helpers";


const props = defineProps({
  model: {
    type: Object,
    required: true,
  },
  selectionMode: {
    type: Boolean,
    default: false,
  },
  selected: {
    type: Boolean,
    default: false,
  },
});

defineEmits(["toggle"]);

const router = useRouter();

const coverSrc = ref(props.model.cover_url || "");
const authorAvatarSrc = ref(props.model.author?.avatar_url || "");

const detailPath = computed(() => props.model.detail_path || encodeModelPath(props.model.model_dir));

function goDetail() {
  router.push(detailPath.value);
}

function onCoverError() {
  if (props.model.cover_remote_url && coverSrc.value !== props.model.cover_remote_url) {
    coverSrc.value = props.model.cover_remote_url;
  }
}

function onAuthorAvatarError() {
  if (props.model.author?.avatar_remote_url && authorAvatarSrc.value !== props.model.author.avatar_remote_url) {
    authorAvatarSrc.value = props.model.author.avatar_remote_url;
  }
}
</script>
