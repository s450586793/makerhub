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
                v-if="activeInstance.file_url"
                class="mw-download-button"
                :href="activeInstance.file_url"
                download
              >
                下载 3MF
              </a>
            </section>
          </div>
        </aside>
      </div>

      <div class="mw-action-bar">
        <div class="mw-action-bar__button">下载 {{ detail.stats?.downloads || 0 }}</div>
        <div class="mw-action-bar__button">点赞 {{ detail.stats?.likes || 0 }}</div>
        <div class="mw-action-bar__button">收藏 {{ detail.stats?.favorites || 0 }}</div>
        <div class="mw-action-bar__button">评论 {{ detail.stats?.comments || 0 }}</div>
        <div class="mw-action-bar__button">打印 {{ detail.stats?.prints || 0 }}</div>
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
