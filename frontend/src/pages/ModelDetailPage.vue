<template>
  <section v-if="loading" class="surface empty-state">
    <h2>正在加载模型详情</h2>
    <p>请稍候。</p>
  </section>

  <section v-else-if="errorMessage" class="surface empty-state">
    <h2>读取详情失败</h2>
    <p>{{ errorMessage }}</p>
  </section>

  <div v-else-if="detail" class="mw-detail-layout">
    <section class="mw-card">
      <div class="mw-head">
        <div class="mw-head__main">
          <div class="mw-head__author">
            <img
              v-if="detail.author?.avatar_url"
              class="mw-head__avatar"
              :src="authorAvatarSrc"
              :alt="detail.author.name"
              @error="onAuthorAvatarError"
            >
            <span v-else class="mw-head__avatar avatar-placeholder">{{ detail.author?.name?.slice(0, 1) || "?" }}</span>
            <div class="mw-head__identity">
              <h1>{{ detail.title }}</h1>
              <div class="mw-head__subline">
                <span>{{ detail.author?.name || "未知作者" }}</span>
                <span class="mw-follow-pill">已归档</span>
              </div>
            </div>
          </div>
        </div>
        <div class="mw-head__aside">
          <div class="mw-head__chips">
            <span class="mw-chip mw-chip--solid">{{ detail.source_label }}</span>
            <span
              v-if="detail.subscription_flags?.deleted_on_source"
              class="mw-chip mw-chip--danger"
              :title="deletedSourceTitle"
            >
              源已删除
            </span>
            <span v-for="tag in detail.tags?.slice(0, 2) || []" :key="tag" class="mw-chip">{{ tag }}</span>
          </div>
        </div>
      </div>

      <div class="mw-hero">
        <div class="mw-gallery">
          <div class="mw-gallery__cover">
            <img
              v-if="currentMedia.src"
              class="mw-gallery__image"
              :src="currentMedia.src"
              :alt="currentMedia.alt || detail.title"
              @error="onMainMediaError"
            >
            <div v-else class="media-placeholder media-placeholder--large">{{ detail.title.slice(0, 1) }}</div>
            <button
              v-if="currentMedia.src"
              class="mw-gallery__preview"
              type="button"
              @click="openLightbox(currentMedia.src)"
            >
              查看大图
            </button>
          </div>

          <div v-if="detail.gallery?.length" class="mw-gallery__thumbs">
            <button
              v-for="(image, index) in detail.gallery"
              :key="`${image.url}-${index}`"
              :class="['mw-gallery__thumb', currentMedia.key === `gallery:${index}` && 'is-active']"
              type="button"
              @click="selectGallery(index)"
            >
              <img
                :src="image.url"
                :alt="`${detail.title} ${index + 1}`"
                loading="lazy"
                @error="swapEventImage($event, image.fallback_url)"
              >
            </button>
          </div>
        </div>

        <aside class="mw-config-panel">
          <div class="mw-config-panel__header">
            <h2>打印配置 ({{ detail.instances?.length || 0 }})</h2>
          </div>

          <div v-if="detail.instances?.length" class="mw-profile-list">
            <button
              v-for="profile in detail.instances"
              :key="profile.instance_key"
              :class="['mw-profile-card', 'mw-profile-card--text', activeInstance?.instance_key === profile.instance_key && 'is-active']"
              type="button"
              @click="selectInstance(profile)"
            >
              <div class="mw-profile-card__body">
                <div class="mw-profile-card__title">{{ profile.title }}</div>
                <div class="mw-profile-card__meta">
                  <span v-if="profile.machine">{{ profile.machine }}</span>
                  <span v-if="profile.plates">{{ profile.plates }} 盘</span>
                  <span v-if="profile.download_count">{{ profile.download_count }} 下载</span>
                  <span v-if="profile.time">{{ profile.time }}</span>
                  <span v-if="profile.rating">★ {{ profile.rating }}</span>
                </div>
              </div>
            </button>
          </div>
          <p v-else class="empty-copy">当前没有可展示的打印配置。</p>

          <div v-if="activeInstance" class="mw-instance-panels">
            <section class="mw-instance-panel is-active">
              <p v-if="activeInstance.summary" class="mw-instance-panel__summary">{{ activeInstance.summary }}</p>
              <a
                v-if="activeInstance.file_available && activeInstance.file_url"
                class="mw-download-button"
                :href="activeInstance.file_url"
                download
              >
                下载 3MF
              </a>
              <span
                v-else
                class="mw-download-button is-disabled"
                :title="activeInstance.file_status_message || '3MF 还未获取到'"
              >
                {{ activeInstanceDownloadLabel }}
              </span>
              <p v-if="!activeInstance.file_available && activeInstance.file_status_message" class="mw-instance-panel__note">
                {{ activeInstance.file_status_message }}
              </p>
            </section>
          </div>
        </aside>
      </div>

      <div class="mw-action-bar">
        <div
          v-for="item in actionStats"
          :key="item.key"
          class="mw-action-bar__button"
          :data-kind="item.key"
          :title="`${item.label} ${formatStat(item.value)}`"
        >
          <span class="mw-action-bar__icon" aria-hidden="true" v-html="item.icon"></span>
          <strong class="mw-action-bar__value">{{ formatStat(item.value) }}</strong>
        </div>
      </div>

      <div class="mw-statline">
        <span>采集于 {{ detail.collect_date || "未知时间" }}</span>
        <span>发布于 {{ detail.publish_date || "未知时间" }}</span>
        <span v-if="detail.origin_url">
          <a :href="detail.origin_url" target="_blank" rel="noreferrer">原始链接</a>
        </span>
      </div>
    </section>

    <section class="mw-content-stack">
      <article class="mw-section-card">
        <div class="mw-section-card__header">
          <div>
            <span class="eyebrow">描述</span>
            <h2>模型说明</h2>
          </div>
        </div>
        <div v-if="detail.summary_html" class="rich-content" v-html="detail.summary_html"></div>
        <p v-else class="empty-copy">{{ detail.summary_text || "当前没有描述内容。" }}</p>
      </article>

      <article v-if="attachmentGroups.length" class="mw-section-card">
        <div class="mw-section-card__header">
          <div>
            <span class="eyebrow">文档</span>
            <h2>文档 ({{ detail.attachments?.length || 0 }})</h2>
          </div>
        </div>
        <div class="doc-groups">
          <section v-for="group in attachmentGroups" :key="group.label" class="doc-group">
            <h3 class="doc-group__title">{{ group.label }} ({{ group.items.length }})</h3>
            <div class="docs-list">
              <article v-for="attachment in group.items" :key="`${group.label}-${attachment.name}`" class="doc-card">
                <div :class="['doc-card__icon', `doc-card__icon--${attachment.ext}`]">{{ (attachment.ext || "file").toUpperCase() }}</div>
                <div class="doc-card__body">
                  <strong>{{ attachment.name }}</strong>
                  <span>{{ attachment.category_label }}</span>
                </div>
                <div class="doc-card__actions">
                  <a
                    v-if="attachment.url || attachment.fallback_url"
                    class="doc-action"
                    :href="attachment.url || attachment.fallback_url"
                    target="_blank"
                    rel="noreferrer"
                  >
                    查看
                  </a>
                  <a
                    v-if="attachment.url || attachment.fallback_url"
                    class="doc-action"
                    :href="attachment.url || attachment.fallback_url"
                    download
                  >
                    下载
                  </a>
                  <span v-else class="doc-action is-disabled">不可用</span>
                </div>
              </article>
            </div>
          </section>
        </div>
      </article>

      <article class="mw-section-card">
        <div class="mw-section-card__header">
          <div>
            <span class="eyebrow">标签</span>
            <h2>标签</h2>
          </div>
        </div>
        <div v-if="detail.tags?.length" class="tag-row">
          <span v-for="tag in detail.tags" :key="tag" class="tag-chip">{{ tag }}</span>
        </div>
        <p v-else class="empty-copy">当前没有标签。</p>
      </article>

      <article class="mw-section-card">
        <div class="mw-section-card__header">
          <div>
            <span class="eyebrow">评论</span>
            <h2>评论与反馈 ({{ detail.comments?.length || 0 }})</h2>
          </div>
        </div>
        <div v-if="detail.comments?.length" class="comment-list">
          <article
            v-for="(comment, index) in detail.comments"
            :key="`${comment.author}-${comment.time}-${index}`"
            class="comment-item"
          >
            <div class="comment-item__header">
              <div class="model-author">
                <img
                  v-if="comment.avatar_url"
                  :src="comment.avatar_url"
                  :alt="comment.author"
                  @error="swapEventImage($event, comment.avatar_remote_url)"
                >
                <span v-else class="avatar-placeholder">{{ comment.author?.slice(0, 1) || "?" }}</span>
                <span>{{ comment.author }}</span>
              </div>
              <span>{{ comment.time }}</span>
            </div>
            <div class="comment-item__body">{{ comment.content }}</div>
            <div v-if="comment.images?.length" class="comment-gallery">
              <button
                v-for="(image, imageIndex) in comment.images"
                :key="`${comment.author}-${imageIndex}`"
                class="comment-gallery__item"
                type="button"
                @click="openLightbox(image.full_url || image.thumb_url)"
              >
                <img
                  :src="image.thumb_url"
                  :alt="`${comment.author} 评论图片 ${imageIndex + 1}`"
                  loading="lazy"
                  @error="swapEventImage($event, image.fallback_url)"
                >
              </button>
            </div>
          </article>
        </div>
        <p v-else class="empty-copy">当前没有同步到评论内容。</p>
      </article>
    </section>

    <div v-if="lightboxSrc" class="lightbox" @click="closeLightbox">
      <button class="lightbox__backdrop" type="button" aria-label="关闭预览"></button>
      <div class="lightbox__dialog" @click.stop>
        <button class="lightbox__close" type="button" aria-label="关闭" @click="closeLightbox">×</button>
        <img class="lightbox__image" :src="lightboxSrc" alt="预览图片">
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute } from "vue-router";

import { apiRequest } from "../lib/api";


const route = useRoute();

const loading = ref(true);
const errorMessage = ref("");
const detail = ref(null);
const currentMedia = ref({
  key: "",
  src: "",
  fallback: "",
  alt: "",
});
const activeInstanceKey = ref("");
const authorAvatarSrc = ref("");
const lightboxSrc = ref("");

const modelDir = computed(() => decodeURIComponent(route.path.replace(/^\/models\//, "")));

const activeInstance = computed(() => {
  return detail.value?.instances?.find((item) => item.instance_key === activeInstanceKey.value) || null;
});

const deletedSourceTitle = computed(() => {
  const items = detail.value?.subscription_flags?.deleted_sources || [];
  if (!items.length) {
    return "订阅源已删除该模型";
  }
  return `订阅源已删除该模型：${items.map((item) => item.name || item.url || "未命名订阅").join("、")}`;
});

const attachmentGroups = computed(() => {
  const groups = new Map();
  for (const item of detail.value?.attachments || []) {
    const label = item.category_label || "附件文件";
    if (!groups.has(label)) {
      groups.set(label, []);
    }
    groups.get(label).push(item);
  }
  return [...groups.entries()].map(([label, items]) => ({ label, items }));
});

const actionStats = computed(() => [
  {
    key: "downloads",
    label: "下载",
    value: detail.value?.stats?.downloads || 0,
    icon: '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.85" stroke-linecap="round" stroke-linejoin="round"><path d="M10 3.6v8.1"/><path d="m6.9 8.8 3.1 3.2 3.1-3.2"/><path d="M4.2 15.4h11.6"/></svg>',
  },
  {
    key: "likes",
    label: "点赞",
    value: detail.value?.stats?.likes || 0,
    icon: '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.85" stroke-linecap="round" stroke-linejoin="round"><path d="M7.4 8.2V16H4.7A1.7 1.7 0 0 1 3 14.3V9.9c0-.94.76-1.7 1.7-1.7h2.7Z"/><path d="M7.4 8.2 10 3.9c.42-.68 1.5-.38 1.5.42v2.48h2.66c1.15 0 1.99 1.1 1.68 2.2l-1.46 5.2A1.7 1.7 0 0 1 12.74 16H7.4"/></svg>',
  },
  {
    key: "favorites",
    label: "收藏",
    value: detail.value?.stats?.favorites || 0,
    icon: '<svg viewBox="0 0 20 20" fill="currentColor"><path d="m10 2.4 2.27 4.6 5.08.74-3.67 3.58.86 5.06L10 13.98l-4.54 2.4.86-5.06-3.67-3.58L7.73 7 10 2.4Z"/></svg>',
  },
  {
    key: "comments",
    label: "评论",
    value: detail.value?.stats?.comments || 0,
    icon: '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.85" stroke-linecap="round" stroke-linejoin="round"><path d="M4.1 5.4A2.4 2.4 0 0 1 6.5 3h7a2.4 2.4 0 0 1 2.4 2.4v5.2a2.4 2.4 0 0 1-2.4 2.4H9l-3.9 3v-3H6.5a2.4 2.4 0 0 1-2.4-2.4V5.4Z"/></svg>',
  },
  {
    key: "prints",
    label: "打印",
    value: detail.value?.stats?.prints || 0,
    icon: '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.85" stroke-linecap="round" stroke-linejoin="round"><path d="M6 7.2V3.8h8v3.4"/><path d="M5.1 15.8h9.8v-4.7H5.1v4.7Z"/><path d="M4.3 7.2h11.4A1.3 1.3 0 0 1 17 8.5v3.1h-2.1"/><path d="M3 11.6V8.5a1.3 1.3 0 0 1 1.3-1.3"/><circle cx="14.3" cy="9.2" r=".7" fill="currentColor" stroke="none"/></svg>',
  },
]);

const activeInstanceDownloadLabel = computed(() => {
  if (activeInstance.value?.file_name) {
    return "3MF 还未获取到";
  }
  return "当前没有 3MF";
});

function setMainMedia(key, src, fallback = "", alt = "") {
  currentMedia.value = {
    key,
    src: src || fallback || "",
    fallback: fallback || "",
    alt,
  };
}

function selectGallery(index) {
  const image = detail.value?.gallery?.[index];
  if (!image) return;
  setMainMedia(`gallery:${index}`, image.url, image.fallback_url || "", `${detail.value.title} ${index + 1}`);
}

function selectInstance(instance) {
  activeInstanceKey.value = instance.instance_key;
  if (instance.primary_image_url || instance.primary_image_fallback_url) {
    setMainMedia(
      `instance:${instance.instance_key}`,
      instance.primary_image_url || "",
      instance.primary_image_fallback_url || "",
      instance.title,
    );
  }
}

function swapEventImage(event, fallbackUrl) {
  const image = event.target;
  if (fallbackUrl && image.src !== fallbackUrl) {
    image.src = fallbackUrl;
  }
}

function onMainMediaError() {
  if (currentMedia.value.fallback && currentMedia.value.src !== currentMedia.value.fallback) {
    currentMedia.value = {
      ...currentMedia.value,
      src: currentMedia.value.fallback,
    };
  }
}

function onAuthorAvatarError() {
  if (detail.value?.author?.avatar_remote_url && authorAvatarSrc.value !== detail.value.author.avatar_remote_url) {
    authorAvatarSrc.value = detail.value.author.avatar_remote_url;
  }
}

function openLightbox(src) {
  lightboxSrc.value = src;
  document.body.classList.add("is-lightbox-open");
}

function closeLightbox() {
  lightboxSrc.value = "";
  document.body.classList.remove("is-lightbox-open");
}

function formatStat(value) {
  return new Intl.NumberFormat("zh-CN").format(Number(value || 0));
}

async function load() {
  loading.value = true;
  errorMessage.value = "";
  try {
    const payload = await apiRequest(`/api/models/${encodeURI(modelDir.value)}`);
    detail.value = payload;
    authorAvatarSrc.value = payload.author?.avatar_url || "";
    activeInstanceKey.value = payload.instances?.[0]?.instance_key || "";
    if (payload.gallery?.length) {
      selectGallery(0);
    } else if (payload.instances?.length) {
      selectInstance(payload.instances[0]);
    } else {
      setMainMedia("", payload.cover_url || "", payload.cover_remote_url || "", payload.title);
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : "读取模型失败。";
  } finally {
    loading.value = false;
  }
}

watch(modelDir, load);

onMounted(load);
onBeforeUnmount(() => {
  document.body.classList.remove("is-lightbox-open");
});
</script>
