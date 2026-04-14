<template>
  <article class="model-card model-card--interactive gallery-card" @click="goDetail">
    <div class="model-card__cover gallery-card__cover">
      <img
        v-if="model.cover_url"
        :src="coverSrc"
        :alt="model.title"
        loading="lazy"
        @error="onCoverError"
      >
      <div v-else class="media-placeholder">{{ titleInitial }}</div>
      <label :class="['model-card__checkbox', (selectionMode || selected) && 'is-visible']" @click.stop>
        <input
          type="checkbox"
          :checked="selected"
          @change="$emit('toggle', model.model_dir)"
        >
      </label>
    </div>

    <div class="model-card__body gallery-card__body">
      <h2 class="gallery-card__title">{{ model.title || "未命名模型" }}</h2>

      <div class="card-meta gallery-card__meta">
        <div class="model-author gallery-card__author">
          <img
            v-if="model.author?.avatar_url"
            :src="authorAvatarSrc"
            :alt="authorName"
            loading="lazy"
            @error="onAuthorAvatarError"
          >
          <span v-else class="avatar-placeholder">{{ authorInitial }}</span>
          <span>{{ authorName }}</span>
        </div>
        <span class="gallery-card__source">{{ model.source_label }}</span>
      </div>

      <div class="gallery-card__stats">
        <span class="gallery-stat">
          <strong>点赞</strong>
          <span>{{ formatStat(model.stats?.likes) }}</span>
        </span>
        <span class="gallery-stat">
          <strong>收藏</strong>
          <span>{{ formatStat(model.stats?.favorites) }}</span>
        </span>
        <span class="gallery-stat">
          <strong>下载</strong>
          <span>{{ formatStat(model.stats?.downloads) }}</span>
        </span>
      </div>

      <div class="gallery-card__dates">
        <span>采集 {{ model.collect_date || "未知" }}</span>
        <span>发布 {{ model.publish_date || "未知" }}</span>
      </div>

      <div class="gallery-card__actions">
        <div class="gallery-card__local-flags">
          <button
            type="button"
            :class="['gallery-flag', model.local_flags?.favorite && 'is-active']"
            @click.stop="$emit('favorite', model.model_dir)"
          >
            本地收藏
          </button>
          <button
            type="button"
            :class="['gallery-flag', model.local_flags?.printed && 'is-active']"
            @click.stop="$emit('printed', model.model_dir)"
          >
            已打印
          </button>
        </div>
        <button class="gallery-delete" type="button" @click.stop="$emit('delete', model.model_dir)">
          删除
        </button>
      </div>
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

defineEmits(["toggle", "favorite", "printed", "delete"]);

const router = useRouter();

const coverSrc = ref(props.model.cover_url || "");
const authorAvatarSrc = ref(props.model.author?.avatar_url || "");

const detailPath = computed(() => props.model.detail_path || encodeModelPath(props.model.model_dir));
const titleInitial = computed(() => String(props.model.title || "M").trim().slice(0, 1) || "M");
const authorName = computed(() => String(props.model.author?.name || "未知作者").trim() || "未知作者");
const authorInitial = computed(() => String(props.model.author?.name || "作").trim().slice(0, 1) || "作");

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

function formatStat(value) {
  return new Intl.NumberFormat("zh-CN").format(Number(value || 0));
}
</script>
