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
        <div class="gallery-card__badges">
          <span class="gallery-card__source">{{ model.source_label }}</span>
          <span
            v-if="model.local_flags?.deleted"
            class="gallery-card__source gallery-card__source--local-deleted"
            title="该模型已在 MakerHub 端删除，默认不会出现在模型库中。"
          >
            本地已删
          </span>
          <span
            v-if="model.subscription_flags?.deleted_on_source"
            class="gallery-card__source gallery-card__source--danger"
            :title="deletedSourceTitle"
          >
            源已删除
          </span>
        </div>
      </div>

      <div class="gallery-card__stats">
        <span class="gallery-stat">
          <span class="gallery-stat__icon" aria-hidden="true" v-html="icons.like"></span>
          <span class="gallery-stat__value">{{ formatStat(model.stats?.likes) }}</span>
        </span>
        <span class="gallery-stat">
          <span class="gallery-stat__icon" aria-hidden="true" v-html="icons.favorite"></span>
          <span class="gallery-stat__value">{{ formatStat(model.stats?.favorites) }}</span>
        </span>
        <span class="gallery-stat">
          <span class="gallery-stat__icon" aria-hidden="true" v-html="icons.download"></span>
          <span class="gallery-stat__value">{{ formatStat(model.stats?.downloads) }}</span>
        </span>
      </div>

      <div class="gallery-card__dates">
        <span><strong>采集</strong><em>{{ model.collect_date || "未知" }}</em></span>
        <span><strong>发布</strong><em>{{ model.publish_date || "未知" }}</em></span>
      </div>

      <div class="gallery-card__actions">
        <div class="gallery-card__local-flags">
          <button
            type="button"
            :class="['gallery-flag', model.local_flags?.favorite && 'is-active']"
            :title="model.local_flags?.favorite ? '取消本地收藏' : '本地收藏'"
            aria-label="本地收藏"
            @click.stop="$emit('favorite', model.model_dir)"
          >
            <span aria-hidden="true" v-html="model.local_flags?.favorite ? icons.favoriteFilled : icons.favoriteOutline"></span>
          </button>
          <button
            type="button"
            :class="['gallery-flag', model.local_flags?.printed && 'is-active']"
            :title="model.local_flags?.printed ? '取消已打印' : '标记已打印'"
            aria-label="已打印"
            @click.stop="$emit('printed', model.model_dir)"
          >
            <span aria-hidden="true" v-html="model.local_flags?.printed ? icons.printedFilled : icons.printedOutline"></span>
          </button>
        </div>
        <button class="gallery-delete" type="button" title="在 MakerHub 中删除" aria-label="在 MakerHub 中删除" @click.stop="$emit('delete', model.model_dir)">
          <span aria-hidden="true" v-html="icons.delete"></span>
        </button>
      </div>
    </div>
  </article>
</template>

<script setup>
import { computed, ref } from "vue";
import { useRouter } from "vue-router";


const props = defineProps({
  model: {
    type: Object,
    required: true,
  },
});

defineEmits(["favorite", "printed", "delete"]);

const router = useRouter();

const icons = {
  like: '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M7.4 8.2V16H4.7A1.7 1.7 0 0 1 3 14.3V9.9c0-.94.76-1.7 1.7-1.7h2.7Z"/><path d="M7.4 8.2 10 3.9c.42-.68 1.5-.38 1.5.42v2.48h2.66c1.15 0 1.99 1.1 1.68 2.2l-1.46 5.2A1.7 1.7 0 0 1 12.74 16H7.4"/></svg>',
  favorite: '<svg viewBox="0 0 20 20" fill="currentColor"><path d="m10 2.4 2.27 4.6 5.08.74-3.67 3.58.86 5.06L10 13.98l-4.54 2.4.86-5.06-3.67-3.58L7.73 7 10 2.4Z"/></svg>',
  download: '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M10 3.5v8.2"/><path d="m6.8 8.8 3.2 3.2 3.2-3.2"/><path d="M4 15.3h12"/></svg>',
  favoriteOutline: '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m10 16.5-1.08-.98C5.1 12.05 2.6 9.78 2.6 6.97c0-2.03 1.6-3.57 3.62-3.57 1.15 0 2.25.54 2.94 1.4.69-.86 1.79-1.4 2.94-1.4 2.02 0 3.62 1.54 3.62 3.57 0 2.81-2.5 5.08-6.32 8.56L10 16.5Z"/></svg>',
  favoriteFilled: '<svg viewBox="0 0 20 20" fill="currentColor"><path d="M10 16.5 8.92 15.5C5.1 12.05 2.6 9.78 2.6 6.97 2.6 4.94 4.2 3.4 6.22 3.4c1.15 0 2.25.54 2.94 1.4.69-.86 1.79-1.4 2.94-1.4 2.02 0 3.62 1.54 3.62 3.57 0 2.81-2.5 5.08-6.32 8.56L10 16.5Z"/></svg>',
  printedOutline: '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="10" cy="10" r="6.7"/><path d="m7.2 10.1 1.9 1.9 3.7-3.8"/></svg>',
  printedFilled: '<svg viewBox="0 0 20 20" fill="currentColor"><path d="M10 2.2a7.8 7.8 0 1 0 0 15.6 7.8 7.8 0 0 0 0-15.6Zm-1.1 10.96-3-2.98 1.16-1.17 1.83 1.83 4.06-4.08 1.17 1.17-5.22 5.23Z"/></svg>',
  delete: '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4.9 6.1h10.2"/><path d="M8.1 6.1V4.7c0-.77.63-1.4 1.4-1.4h1c.77 0 1.4.63 1.4 1.4v1.4"/><path d="m6.2 6.1.72 9a1.4 1.4 0 0 0 1.4 1.29h3.32a1.4 1.4 0 0 0 1.4-1.29l.72-9"/><path d="M8.6 8.9v4.3"/><path d="M11.4 8.9v4.3"/></svg>',
};

const coverSrc = ref(props.model.cover_url || "");
const authorAvatarSrc = ref(props.model.author?.avatar_url || "");

const titleInitial = computed(() => String(props.model.title || "M").trim().slice(0, 1) || "M");
const authorName = computed(() => String(props.model.author?.name || "未知作者").trim() || "未知作者");
const authorInitial = computed(() => String(props.model.author?.name || "作").trim().slice(0, 1) || "作");
const deletedSourceTitle = computed(() => {
  const items = props.model.subscription_flags?.deleted_sources || [];
  if (!items.length) {
    return "源端已删除该模型";
  }
  return `源端已删除该模型：${items.map((item) => item.name || item.url || "未命名来源").join("、")}`;
});

function goDetail() {
  router.push({
    name: "model-detail",
    params: {
      modelDir: String(props.model.model_dir || ""),
    },
  });
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
