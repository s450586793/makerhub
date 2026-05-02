<template>
  <article
    :class="[
      'source-library-card',
      card.card_kind === 'author' ? 'source-library-card--author' : 'source-library-card--collection',
    ]"
    @click="$emit('open', card)"
  >
    <template v-if="card.card_kind === 'author'">
      <div class="source-library-card__author-shell">
        <div class="source-library-card__author-head">
          <div class="source-library-card__avatar-shell">
            <img
              v-if="card.avatar_url"
              :src="card.avatar_url"
              :alt="card.title"
              loading="lazy"
            >
            <div v-else class="source-library-card__avatar-placeholder">{{ titleInitial }}</div>
            <span class="source-library-card__badge">{{ card.site_badge || "MW" }}</span>
          </div>
          <div class="source-library-card__author-copy">
            <div class="source-library-card__title-row">
              <h2>{{ card.title }}</h2>
              <span v-if="card.verified" class="source-library-card__verified" aria-hidden="true">✓</span>
            </div>
            <p>{{ card.subtitle || "MakerWorld 作者" }}</p>
          </div>

          <div class="source-library-card__stats source-library-card__stats--author">
            <div
              v-for="stat in displayStats"
              :key="stat.label"
              class="source-library-card__stat"
            >
              <strong>{{ formatCompact(stat.value) }}</strong>
              <span>{{ stat.label }}</span>
            </div>
          </div>
        </div>
      </div>

      <div class="source-library-card__preview-grid source-library-card__preview-grid--author">
        <div
          v-for="(preview, index) in previewTiles"
          :key="preview?.model_dir || `preview-${index}`"
          class="source-library-card__preview-tile"
        >
          <img
            v-if="preview?.cover_url"
            :src="preview.cover_url"
            :alt="preview.title || card.title"
            loading="lazy"
          >
          <div v-else class="source-library-card__preview-placeholder">
            {{ tileInitial(preview?.title || card.title || index + 1) }}
          </div>
        </div>
      </div>
    </template>

    <template v-else>
      <div class="source-library-card__author-shell source-library-card__author-shell--collection">
        <div class="source-library-card__author-head">
          <div class="source-library-card__avatar-shell source-library-card__avatar-shell--collection">
            <img
              v-if="collectionAvatarUrl"
              :src="collectionAvatarUrl"
              :alt="card.title"
              loading="lazy"
            >
            <div v-else class="source-library-card__avatar-placeholder">{{ titleInitial }}</div>
            <span class="source-library-card__badge">{{ card.site_badge || "MW" }}</span>
          </div>
          <div class="source-library-card__author-copy">
            <div class="source-library-card__title-row">
              <h2>{{ card.title }}</h2>
            </div>
            <p>{{ card.subtitle || fallbackSubtitle }}</p>
          </div>

          <div class="source-library-card__stats source-library-card__stats--collection">
            <div
              v-for="stat in displayStats"
              :key="stat.label"
              class="source-library-card__stat"
            >
              <strong>{{ formatCompact(stat.value) }}</strong>
              <span>{{ stat.label }}</span>
            </div>
          </div>
        </div>
      </div>

      <div class="source-library-card__preview-grid source-library-card__preview-grid--collection">
        <div
          v-for="(preview, index) in previewTiles"
          :key="preview?.model_dir || `preview-${index}`"
          :class="['source-library-card__preview-tile', index === 3 && overflowCount > 0 && 'is-overflow']"
        >
          <img
            v-if="preview?.cover_url"
            :src="preview.cover_url"
            :alt="preview.title || card.title"
            loading="lazy"
          >
          <div v-else class="source-library-card__preview-placeholder">
            {{ tileInitial(preview?.title || card.title || index + 1) }}
          </div>
          <div v-if="index === 3 && overflowCount > 0" class="source-library-card__overflow-mask">
            <strong>+{{ overflowCount }}</strong>
          </div>
        </div>
      </div>
    </template>
  </article>
</template>

<script setup>
import { computed } from "vue";


const props = defineProps({
  card: {
    type: Object,
    required: true,
  },
});

defineEmits(["open"]);

const titleInitial = computed(() => tileInitial(props.card.title || "M"));
const collectionAvatarUrl = computed(() => props.card.avatar_url || props.card.cover_url || "");
const fallbackSubtitle = computed(() => {
  if (props.card.kind === "collection") return "MakerWorld 合集";
  if (props.card.kind === "favorite") return "MakerWorld 收藏夹";
  return props.card.site === "local" ? "MakerHub 本地状态" : "MakerWorld 来源";
});
const previewTiles = computed(() => {
  const previews = Array.isArray(props.card.preview_models) ? props.card.preview_models.slice(0, 4) : [];
  if (!previews.length && props.card.cover_url) {
    previews.push({
      cover_url: props.card.cover_url,
      title: props.card.title,
      model_dir: `cover-${props.card.key || props.card.title || "source"}`,
    });
  }
  while (previews.length < 4) {
    previews.push(null);
  }
  return previews;
});
const overflowCount = computed(() => {
  const total = Number(props.card.model_count || 0);
  return total > 4 ? total - 4 : 0;
});
const displayStats = computed(() => {
  const stats = Array.isArray(props.card.stats) ? props.card.stats.filter((item) => item && item.label) : [];
  return props.card.card_kind === "author" ? stats.slice(0, 3) : stats.slice(0, 2);
});

function formatCompact(value) {
  const numeric = Number(value || 0);
  if (!Number.isFinite(numeric)) {
    return String(value || "0");
  }
  if (numeric >= 1_000_000) {
    return `${(numeric / 1_000_000).toFixed(numeric >= 10_000_000 ? 0 : 1)} M`;
  }
  if (numeric >= 1_000) {
    return `${(numeric / 1_000).toFixed(numeric >= 10_000 ? 0 : 1)} k`;
  }
  return new Intl.NumberFormat("zh-CN").format(numeric);
}

function tileInitial(value) {
  const text = String(value || "").trim();
  return text.slice(0, 1) || "M";
}
</script>
