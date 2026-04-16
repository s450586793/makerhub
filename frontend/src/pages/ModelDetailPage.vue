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
    <RouterLink class="mw-back-link" to="/models">返回模型库</RouterLink>

    <section class="mw-card">
      <div class="mw-hero">
        <div class="mw-hero__gallery-column">
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
                <span class="mw-gallery__preview-icon" aria-hidden="true">
                  <svg viewBox="0 0 24 24" fill="currentColor">
                    <path d="M4.5 16.19V8.96l6.56 3.65v7.23L4.5 16.19Z" />
                    <path d="M19.5 16.19V8.96l-6.56 3.65v7.23l6.56-3.65Z" />
                    <path d="M12 10.98 5.49 7.37 12 3.75l6.51 3.62L12 10.98Z" />
                  </svg>
                </span>
                <span>3D 预览</span>
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
        </div>

        <aside class="mw-hero__sidebar">
          <header class="mw-head">
            <div class="mw-head__top">
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
                    <span
                      v-if="detail.subscription_flags?.deleted_on_source"
                      class="mw-chip mw-chip--danger"
                      :title="deletedSourceTitle"
                    >
                      源已删除
                    </span>
                  </div>
                </div>
              </div>
            </div>

            <div class="mw-head__crumbs">
              <span class="mw-crumb">{{ detail.source_label }}</span>
              <template v-for="crumb in headCrumbs" :key="crumb">
                <span class="mw-crumb__sep">&gt;</span>
                <span class="mw-crumb">{{ crumb }}</span>
              </template>
            </div>
          </header>

          <div class="mw-stat-row">
            <div
              v-for="item in actionStats"
              :key="item.key"
              class="mw-stat-pill"
              :data-kind="item.key"
              :title="`${item.label} ${formatStat(item.value)}`"
            >
              <span class="mw-stat-pill__icon" aria-hidden="true" v-html="item.icon"></span>
              <strong class="mw-stat-pill__value">{{ formatStat(item.value) }}</strong>
            </div>
            <span class="mw-stat-row__publish">发布于 {{ detail.publish_date || "未知时间" }}</span>
          </div>

          <div class="mw-statline">
            <span>采集于 {{ detail.collect_date || "未知时间" }}</span>
            <span v-if="activeInstance?.publish_date">实例上传于 {{ activeInstance.publish_date }}</span>
          </div>

          <div class="mw-action-strip">
            <a
              v-if="heroDownloadHref"
              class="mw-download-button mw-download-button--hero"
              :href="heroDownloadHref"
              download
            >
              {{ heroDownloadLabel }}
            </a>
            <span
              v-else
              class="mw-download-button mw-download-button--hero is-disabled"
              :title="heroDownloadStatus"
            >
              {{ heroDownloadLabel }}
            </span>
            <a
              v-if="detail.origin_url"
              class="mw-inline-link mw-inline-link--ghost"
              :href="detail.origin_url"
              target="_blank"
              rel="noreferrer"
            >
              原始链接
            </a>
          </div>

          <section v-if="activeInstance" class="mw-active-profile">
            <div class="mw-active-profile__heading">
              <strong>{{ activeInstance.title }}</strong>
              <span>{{ activeInstance.machine || "通用" }}</span>
            </div>
            <p v-if="activeInstance.summary" class="mw-active-profile__summary">{{ activeInstance.summary }}</p>
          </section>

          <aside class="mw-config-panel">
          <div class="mw-config-panel__header">
            <h2>打印配置 <span>({{ detail.instances?.length || 0 }})</span></h2>

            <div v-if="machineFilters.length" class="mw-config-panel__filters">
              <span class="mw-filter-pill is-active">全部</span>
              <span v-for="machine in machineFilters.slice(0, 10)" :key="machine" class="mw-filter-pill">{{ machine }}</span>
            </div>
          </div>
          <div class="mw-config-panel__divider"></div>

          <div v-if="detail.instances?.length" class="mw-profile-list">
            <div
              v-for="(profile, profileIndex) in detail.instances"
              :key="profile.instance_key"
              class="mw-profile-entry"
              @mouseenter="openProfilePopover(profile)"
              @mouseleave="closeProfilePopover(profile.instance_key)"
              @focusin="openProfilePopover(profile)"
              @focusout="handleProfileEntryFocusOut($event, profile)"
            >
              <button
                :class="[
                  'mw-profile-card',
                  activeInstance?.instance_key === profile.instance_key && 'is-active',
                  isProfilePopoverOpen(profile) && 'is-previewing',
                ]"
                :aria-expanded="isProfilePopoverOpen(profile) ? 'true' : 'false'"
                type="button"
                @click="selectInstance(profile)"
              >
                <div class="mw-profile-card__thumb-wrap">
                  <img
                    v-if="profilePreview(profile)"
                    class="mw-profile-card__thumb"
                    :src="profilePreview(profile)"
                    :alt="profile.title"
                    loading="lazy"
                    @error="swapEventImage($event, profilePreviewFallback(profile))"
                  >
                  <span v-else class="mw-profile-card__thumb avatar-placeholder">{{ detail.title.slice(0, 1) }}</span>
                </div>

                <div class="mw-profile-card__body">
                  <div class="mw-profile-card__title">{{ profile.title }}</div>
                  <div class="mw-profile-card__meta">
                    <span class="mw-profile-card__badge">{{ profile.machine || "通用" }}</span>
                    <span v-if="profile.time" class="mw-profile-card__meta-item">
                      <svg class="mw-profile-card__meta-icon" viewBox="0 0 16 17" fill="none">
                        <path d="M15 8.52342C15 4.64449 11.866 1.5 8 1.5V8.52342L12.9395 13.5C14.2123 12.2282 15 10.4681 15 8.52342Z" fill="currentColor" opacity=".24"></path>
                        <path d="M8 1.1a7.4 7.4 0 1 0 0 14.8 7.4 7.4 0 0 0 0-14.8Zm0 1.2a6.2 6.2 0 1 1 0 12.4 6.2 6.2 0 0 1 0-12.4Zm0 3.6a.6.6 0 0 1 .6.6v1.75l1.82 1.82a.6.6 0 1 1-.84.84L7.58 8.92A.6.6 0 0 1 7.4 8.5v-2a.6.6 0 0 1 .6-.6Z" fill="currentColor"></path>
                      </svg>
                      <span>{{ profile.time }}</span>
                    </span>
                    <span v-if="profile.plates" class="mw-profile-card__meta-item">{{ profile.plates }} 盘</span>
                    <span v-if="profile.download_count" class="mw-profile-card__meta-item">{{ formatStat(profile.download_count) }} 下载</span>
                    <span v-if="profile.rating" class="mw-profile-card__meta-item">★ {{ profile.rating }}</span>
                  </div>
                </div>
              </button>

              <section
                v-if="isProfilePopoverOpen(profile)"
                :class="['mw-profile-popover', profilePopoverPlacement(profileIndex, detail.instances.length)]"
              >
                <div v-if="profileMedia(profile).length" class="mw-profile-popover__gallery">
                  <button
                    class="mw-profile-popover__stage"
                    type="button"
                    @click="openLightbox(popoverCurrentMedia(profile)?.url || popoverCurrentMedia(profile)?.fallback_url || '')"
                  >
                    <img
                      v-if="popoverCurrentMedia(profile)?.url || popoverCurrentMedia(profile)?.fallback_url"
                      class="mw-profile-popover__stage-image"
                      :src="popoverCurrentMedia(profile)?.url || popoverCurrentMedia(profile)?.fallback_url"
                      :alt="`${profile.title} ${popoverCurrentMedia(profile)?.label || ''}`.trim()"
                      loading="lazy"
                      @error="swapEventImage($event, popoverCurrentMedia(profile)?.fallback_url)"
                    >
                    <span v-else class="mw-profile-popover__stage-image avatar-placeholder">{{ detail.title.slice(0, 1) }}</span>
                    <span class="mw-profile-popover__stage-badge">{{ popoverCurrentMedia(profile)?.label || "预览" }}</span>
                  </button>

                  <div class="mw-profile-popover__gallery-meta">
                    <span>{{ popoverCurrentMedia(profile)?.kind === "plate" ? "分盘预览" : "配置图集" }}</span>
                    <span>{{ currentPopoverMediaIndex(profile) + 1 }} / {{ profileMedia(profile).length }}</span>
                  </div>

                  <div v-if="profileMedia(profile).length > 1" class="mw-profile-popover__media-strip">
                    <button
                      v-for="(media, mediaIndex) in profileMedia(profile)"
                      :key="`${profile.instance_key}-${media.label}-${mediaIndex}`"
                      :class="['mw-profile-popover__media-thumb', currentPopoverMediaIndex(profile) === mediaIndex && 'is-active']"
                      type="button"
                      @mouseenter="selectPopoverMedia(profile, mediaIndex)"
                      @focus="selectPopoverMedia(profile, mediaIndex)"
                      @click="selectPopoverMedia(profile, mediaIndex)"
                    >
                      <span class="mw-profile-popover__media-thumb-figure">
                        <img
                          v-if="media.url || media.fallback_url"
                          :src="media.url || media.fallback_url"
                          :alt="`${profile.title} ${media.label}`.trim()"
                          loading="lazy"
                          @error="swapEventImage($event, media.fallback_url)"
                        >
                        <span v-else class="avatar-placeholder">{{ detail.title.slice(0, 1) }}</span>
                      </span>
                      <span class="mw-profile-popover__media-thumb-label">{{ media.label }}</span>
                    </button>
                  </div>
                </div>

                <div class="mw-profile-popover__hero-body">
                  <div class="mw-profile-popover__eyebrow">打印配置</div>
                  <h3>{{ profile.title }}</h3>
                  <div class="mw-profile-popover__meta">
                    <span>{{ profile.machine || "通用" }}</span>
                    <span v-if="profile.time">{{ profile.time }}</span>
                    <span v-if="profile.publish_date">上传于 {{ profile.publish_date }}</span>
                  </div>
                </div>

                <div class="mw-profile-popover__stats">
                  <span class="mw-profile-popover__stat">
                    <strong>盘数</strong>
                    <em>{{ profile.plates || 0 }}</em>
                  </span>
                  <span class="mw-profile-popover__stat">
                    <strong>下载</strong>
                    <em>{{ formatStat(profile.download_count) }}</em>
                  </span>
                  <span class="mw-profile-popover__stat">
                    <strong>打印</strong>
                    <em>{{ formatStat(profile.print_count) }}</em>
                  </span>
                  <span class="mw-profile-popover__stat">
                    <strong>评分</strong>
                    <em>{{ profile.rating || "-" }}</em>
                  </span>
                </div>

                <p class="mw-profile-popover__summary">
                  {{ profile.summary || "该打印配置可单独查看图集、分盘图片与 3MF 状态。" }}
                </p>

                <div class="mw-profile-popover__actions">
                  <button class="button button-secondary button-small" type="button" @click="selectInstance(profile)">
                    查看配置
                  </button>
                  <a
                    v-if="profile.file_available && profile.file_url"
                    class="mw-download-button mw-download-button--compact"
                    :href="profile.file_url"
                    download
                  >
                    下载 3MF
                  </a>
                  <span
                    v-else
                    class="mw-download-button mw-download-button--compact is-disabled"
                    :title="profile.file_status_message || '3MF 还未获取到'"
                  >
                    {{ profile.file_name ? "3MF 还未获取到" : "当前没有 3MF" }}
                  </span>
                </div>

                <p v-if="profile.file_status_message" class="mw-profile-popover__note">
                  {{ profile.file_status_message }}
                </p>
              </section>
            </div>
          </div>
          <p v-else class="empty-copy">当前没有可展示的打印配置。</p>

          <div v-if="activeInstance && !hoverPopoverEnabled" class="mw-instance-panels mw-instance-panels--inline">
            <section class="mw-instance-panel is-active">
              <div class="mw-instance-panel__meta">
                <span v-if="activeInstance.publish_date">上传于 {{ activeInstance.publish_date }}</span>
                <span v-if="activeInstance.plates">{{ activeInstance.plates }} 盘</span>
                <span v-if="activeInstance.download_count">{{ formatStat(activeInstance.download_count) }} 下载</span>
              </div>
              <p v-if="activeInstance.media?.length" class="mw-instance-panel__hint">P1/P2/P3 与配置图集仅在打印配置悬浮窗内查看。</p>
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
        </aside>
      </div>
    </section>

    <nav class="mw-detail-nav" aria-label="详情导航">
      <a
        v-for="section in detailSections"
        :key="section.id"
        class="mw-detail-nav__link"
        :href="`#${section.id}`"
      >
        {{ section.label }}
      </a>
    </nav>

    <section class="mw-content-stack">
      <article id="detail-description" class="mw-section-card">
        <div class="mw-section-card__header">
          <h2>描述</h2>
        </div>
        <div v-if="detail.summary_html" class="rich-content" v-html="detail.summary_html"></div>
        <p v-else class="empty-copy">{{ detail.summary_text || "当前没有描述内容。" }}</p>
        <div v-if="detail.tags?.length" class="mw-tag-wall">
          <span v-for="tag in detail.tags" :key="tag" class="mw-chip mw-chip--tag">{{ tag }}</span>
        </div>
      </article>

      <article id="detail-docs" class="mw-section-card mw-section-card--docs">
        <div class="mw-section-card__header">
          <div class="mw-section-card__heading">
            <h2>文档 ({{ detail.attachments?.length || 0 }})</h2>
            <p class="mw-section-card__hint">可以在这里补传组装图、说明书或其他附件。</p>
          </div>
        </div>
        <div v-if="attachmentGroups.length" class="mw-doc-groups">
          <section v-for="group in attachmentGroups" :key="group.label" class="doc-group">
            <h3 class="doc-group__title">{{ group.label }} ({{ group.items.length }})</h3>
            <div class="mw-doc-cards">
              <article
                v-for="attachment in group.items"
                :key="attachment.id"
                class="mw-doc-card"
              >
                <div :class="docIconClass(attachment)">{{ attachmentExtLabel(attachment) }}</div>
                <div class="mw-doc-card__body">
                  <strong>{{ attachment.name }}</strong>
                  <div class="mw-doc-card__meta-row">
                    <span>{{ attachment.category_label }}</span>
                    <span v-if="attachment.is_manual" class="mw-doc-badge">手动上传</span>
                    <span v-if="attachment.uploaded_at_label">{{ attachment.uploaded_at_label }}</span>
                  </div>
                </div>
                <div class="mw-doc-card__actions">
                  <a
                    :class="['button button-secondary button-small mw-doc-action', !attachmentDownloadUrl(attachment) && 'is-disabled']"
                    :href="attachmentDownloadUrl(attachment) || undefined"
                    :target="attachmentDownloadUrl(attachment) ? '_blank' : undefined"
                    :rel="attachmentDownloadUrl(attachment) ? 'noreferrer' : undefined"
                  >
                    打开
                  </a>
                  <button
                    v-if="attachment.is_image && attachmentDownloadUrl(attachment)"
                    class="button button-secondary button-small mw-doc-action"
                    type="button"
                    @click="openLightbox(attachmentDownloadUrl(attachment))"
                  >
                    预览
                  </button>
                  <button
                    v-if="attachment.can_delete"
                    :disabled="deletingAttachmentId === attachment.id"
                    class="button button-secondary button-small mw-doc-action mw-doc-action--danger"
                    type="button"
                    @click="removeAttachment(attachment)"
                  >
                    {{ deletingAttachmentId === attachment.id ? "删除中..." : "删除" }}
                  </button>
                </div>
              </article>
            </div>
          </section>
        </div>
        <p v-else class="empty-copy">当前没有同步到文档附件，你可以在下方上传组装图或说明文件。</p>

        <form class="mw-attachment-upload" @submit.prevent="submitAttachmentUpload">
          <div class="mw-attachment-upload__fields">
            <select v-model="attachmentForm.category" class="mw-attachment-upload__select">
              <option v-for="item in attachmentCategories" :key="item.value" :value="item.value">{{ item.label }}</option>
            </select>
            <input
              v-model.trim="attachmentForm.name"
              class="mw-attachment-upload__input"
              type="text"
              placeholder="附件名称，可选"
            >
            <label class="mw-attachment-upload__file">
              <input ref="attachmentFileInput" type="file" @change="onAttachmentFileChange">
              <span>{{ attachmentForm.file?.name || "选择附件" }}</span>
            </label>
            <button
              :disabled="attachmentUploading"
              class="button button-primary button-small"
              type="submit"
            >
              {{ attachmentUploading ? "上传中..." : "上传附件" }}
            </button>
          </div>
          <p class="mw-attachment-upload__hint">支持上传组装图、PDF、压缩包等文件，文件会保存到当前模型目录。</p>
          <p v-if="attachmentUploadMessage" class="mw-attachment-upload__status is-success">{{ attachmentUploadMessage }}</p>
          <p v-if="attachmentUploadError" class="mw-attachment-upload__status is-error">{{ attachmentUploadError }}</p>
        </form>
      </article>

      <article id="detail-comments" class="mw-section-card">
        <div class="mw-section-card__header">
          <h2>评论 &amp; 评分 ({{ detail.comments?.length || 0 }})</h2>
        </div>
        <div v-if="detail.comments?.length" class="comment-list">
          <article
            v-for="(comment, index) in detail.comments"
            :key="`${comment.author}-${comment.time}-${index}`"
            class="comment-item"
          >
            <div class="comment-item__avatar">
              <div class="model-author">
                <img
                  v-if="comment.avatar_url"
                  :src="comment.avatar_url"
                  :alt="comment.author"
                  @error="swapEventImage($event, comment.avatar_remote_url)"
                >
                <span v-else class="avatar-placeholder">{{ comment.author?.slice(0, 1) || "?" }}</span>
              </div>
            </div>
            <div class="comment-item__content">
              <div class="comment-item__header">
                <span class="comment-item__author">{{ comment.author }}</span>
                <span>{{ comment.time }}</span>
              </div>
              <div class="comment-item__body">{{ comment.content }}</div>
              <div
                v-if="comment.images?.length"
                :class="['comment-gallery', commentGalleryClass(comment.images.length)]"
              >
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
const attachmentFileInput = ref(null);
const attachmentUploading = ref(false);
const attachmentUploadMessage = ref("");
const attachmentUploadError = ref("");
const deletingAttachmentId = ref("");
const hoverPopoverEnabled = ref(false);
const previewedInstanceKey = ref("");
const popoverMediaState = ref({});

let hoverPopoverMediaQuery = null;
let hoverPopoverMediaListener = null;

const attachmentForm = ref({
  category: "assembly",
  name: "",
  file: null,
});

const attachmentCategories = [
  { value: "assembly", label: "组装图" },
  { value: "guide", label: "组装指南" },
  { value: "manual", label: "使用手册" },
  { value: "bom", label: "BOM 清单" },
  { value: "other", label: "其他附件" },
];

const detailSections = [
  { id: "detail-description", label: "描述" },
  { id: "detail-docs", label: "文档" },
  { id: "detail-comments", label: "评论" },
];

const modelDir = computed(() => decodeURIComponent(route.path.replace(/^\/models\//, "")));

const activeInstance = computed(() => {
  return detail.value?.instances?.find((item) => item.instance_key === activeInstanceKey.value) || null;
});

const headCrumbs = computed(() => {
  const crumbs = [];
  for (const tag of detail.value?.tags || []) {
    const label = String(tag || "").trim();
    if (!label || crumbs.includes(label)) {
      continue;
    }
    crumbs.push(label);
    if (crumbs.length >= 2) {
      break;
    }
  }
  return crumbs;
});

const deletedSourceTitle = computed(() => {
  const items = detail.value?.subscription_flags?.deleted_sources || [];
  if (!items.length) {
    return "订阅源已删除该模型";
  }
  return `订阅源已删除该模型：${items.map((item) => item.name || item.url || "未命名订阅").join("、")}`;
});

const machineFilters = computed(() => {
  const seen = new Set();
  const items = [];
  for (const instance of detail.value?.instances || []) {
    const label = String(instance.machine || "").trim();
    if (!label || seen.has(label)) {
      continue;
    }
    seen.add(label);
    items.push(label);
  }
  return items;
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

const heroDownloadHref = computed(() => {
  if (activeInstance.value?.file_available && activeInstance.value?.file_url) {
    return activeInstance.value.file_url;
  }
  return "";
});

const heroDownloadStatus = computed(() => {
  return activeInstance.value?.file_status_message || "3MF 还未获取到";
});

const heroDownloadLabel = computed(() => {
  if (!activeInstance.value) {
    return "选择打印配置";
  }
  if (activeInstance.value.file_available && activeInstance.value.file_url) {
    return "下载 3MF";
  }
  if (activeInstance.value.file_name) {
    return "3MF 还未获取到";
  }
  return "当前没有 3MF";
});

function applyHoverPopoverEnabled(matches) {
  hoverPopoverEnabled.value = matches;
  if (!matches) {
    previewedInstanceKey.value = "";
  }
}

function parseProfileHash() {
  if (typeof window === "undefined") {
    return "";
  }
  const rawHash = String(window.location.hash || "").trim();
  if (!rawHash) {
    return "";
  }
  const normalized = rawHash.replace(/^#/, "");
  if (!normalized) {
    return "";
  }
  if (/^profileId-/i.test(normalized)) {
    return decodeURIComponent(normalized.replace(/^profileId-/i, ""));
  }
  return decodeURIComponent(normalized);
}

function findInstanceByHash(items = []) {
  const hashValue = parseProfileHash();
  if (!hashValue) {
    return null;
  }
  return items.find((item) => String(item.instance_key) === hashValue) || null;
}

function syncProfileHash(instance) {
  if (typeof window === "undefined") {
    return;
  }
  const url = new URL(window.location.href);
  if (instance?.instance_key) {
    url.hash = `profileId-${encodeURIComponent(instance.instance_key)}`;
  } else {
    url.hash = "";
  }
  window.history.replaceState(window.history.state, "", url.toString());
}

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

function selectInstance(instance, options = {}) {
  const { syncHash = true } = options;
  if (!instance) {
    return;
  }
  activeInstanceKey.value = instance.instance_key;
  if (!detail.value?.gallery?.length && (instance.primary_image_url || instance.primary_image_fallback_url)) {
    setMainMedia(
      `instance:${instance.instance_key}`,
      instance.primary_image_url || "",
      instance.primary_image_fallback_url || "",
      instance.title,
    );
  }
  if (syncHash) {
    syncProfileHash(instance);
  }
}

function isProfilePopoverOpen(profile) {
  return hoverPopoverEnabled.value && previewedInstanceKey.value === profile?.instance_key;
}

function openProfilePopover(profile) {
  if (!hoverPopoverEnabled.value || !profile) {
    return;
  }
  previewedInstanceKey.value = profile.instance_key;
  if (!(profile.instance_key in popoverMediaState.value)) {
    selectPopoverMedia(profile, 0);
  }
}

function closeProfilePopover(instanceKey = "") {
  if (!hoverPopoverEnabled.value) {
    return;
  }
  if (!instanceKey || previewedInstanceKey.value === instanceKey) {
    previewedInstanceKey.value = "";
  }
}

function handleProfileEntryFocusOut(event, profile) {
  if (event.currentTarget?.contains(event.relatedTarget)) {
    return;
  }
  closeProfilePopover(profile?.instance_key || "");
}

function profilePopoverPlacement(index, total) {
  if (index === 0) {
    return "is-align-top";
  }
  if (index >= total - 1) {
    return "is-align-bottom";
  }
  return "";
}

function handleHashChange() {
  if (!detail.value?.instances?.length) {
    return;
  }
  const matched = findInstanceByHash(detail.value.instances);
  if (matched) {
    selectInstance(matched, { syncHash: false });
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

function attachmentDownloadUrl(attachment) {
  return attachment?.url || attachment?.fallback_url || "";
}

function attachmentExtLabel(attachment) {
  return String(attachment?.ext || "file").slice(0, 4).toUpperCase();
}

function docIconClass(attachment) {
  return [
    "mw-doc-card__icon",
    attachment?.ext === "pdf" && "mw-doc-card__icon--pdf",
    attachment?.is_image && "mw-doc-card__icon--image",
  ];
}

function commentGalleryClass(count) {
  if (count <= 1) return "comment-gallery--1";
  if (count === 2) return "comment-gallery--2";
  if (count === 3) return "comment-gallery--3";
  return "comment-gallery--more";
}

function profilePreview(profile) {
  return profile?.thumbnail_url || profile?.primary_image_url || "";
}

function profilePreviewFallback(profile) {
  return profile?.thumbnail_fallback_url || profile?.primary_image_fallback_url || "";
}

function profileMedia(profile) {
  const mediaItems = Array.isArray(profile?.media)
    ? profile.media.filter((item) => item && (item.url || item.fallback_url))
    : [];
  if (mediaItems.length) {
    return mediaItems;
  }
  const previewUrl = profilePreview(profile);
  const previewFallback = profilePreviewFallback(profile);
  if (!previewUrl && !previewFallback) {
    return [];
  }
  return [
    {
      label: "预览",
      kind: "preview",
      url: previewUrl || previewFallback,
      fallback_url: previewFallback,
    },
  ];
}

function currentPopoverMediaIndex(profile) {
  const mediaItems = profileMedia(profile);
  if (!mediaItems.length || !profile?.instance_key) {
    return 0;
  }
  const currentIndex = Number(popoverMediaState.value[profile.instance_key] ?? 0);
  if (!Number.isInteger(currentIndex) || currentIndex < 0 || currentIndex >= mediaItems.length) {
    return 0;
  }
  return currentIndex;
}

function popoverCurrentMedia(profile) {
  const mediaItems = profileMedia(profile);
  return mediaItems[currentPopoverMediaIndex(profile)] || null;
}

function selectPopoverMedia(profile, index) {
  if (!profile?.instance_key) {
    return;
  }
  const mediaItems = profileMedia(profile);
  if (!mediaItems[index]) {
    return;
  }
  popoverMediaState.value = {
    ...popoverMediaState.value,
    [profile.instance_key]: index,
  };
}

function resetAttachmentUploadState(options = {}) {
  const { keepCategory = true, clearFeedback = true } = options;
  if (clearFeedback) {
    attachmentUploadMessage.value = "";
    attachmentUploadError.value = "";
  }
  attachmentForm.value = {
    category: keepCategory ? attachmentForm.value.category : "assembly",
    name: "",
    file: null,
  };
  if (attachmentFileInput.value) {
    attachmentFileInput.value.value = "";
  }
}

function onAttachmentFileChange(event) {
  const [file] = event.target.files || [];
  attachmentForm.value.file = file || null;
  attachmentUploadMessage.value = "";
  attachmentUploadError.value = "";
}

async function submitAttachmentUpload() {
  if (!attachmentForm.value.file) {
    attachmentUploadError.value = "请选择要上传的附件。";
    return;
  }

  attachmentUploading.value = true;
  attachmentUploadMessage.value = "";
  attachmentUploadError.value = "";

  const formData = new FormData();
  formData.set("file", attachmentForm.value.file);
  formData.set("category", attachmentForm.value.category);
  if (attachmentForm.value.name) {
    formData.set("name", attachmentForm.value.name);
  }

  try {
    const payload = await apiRequest(`/api/models/${encodeURI(modelDir.value)}/attachments`, {
      method: "POST",
      body: formData,
    });
    detail.value = payload.detail;
    attachmentUploadMessage.value = payload.message || "附件已上传。";
    resetAttachmentUploadState({ clearFeedback: false });
  } catch (error) {
    attachmentUploadError.value = error instanceof Error ? error.message : "附件上传失败。";
  } finally {
    attachmentUploading.value = false;
  }
}

async function removeAttachment(attachment) {
  if (!attachment?.can_delete || !attachment?.id) {
    return;
  }
  if (!window.confirm(`确认删除附件“${attachment.name}”吗？`)) {
    return;
  }

  deletingAttachmentId.value = attachment.id;
  attachmentUploadMessage.value = "";
  attachmentUploadError.value = "";

  try {
    const payload = await apiRequest(`/api/models/${encodeURI(modelDir.value)}/attachments/${encodeURIComponent(attachment.id)}`, {
      method: "DELETE",
    });
    detail.value = payload.detail;
    attachmentUploadMessage.value = payload.message || "附件已删除。";
  } catch (error) {
    attachmentUploadError.value = error instanceof Error ? error.message : "附件删除失败。";
  } finally {
    deletingAttachmentId.value = "";
  }
}

async function load() {
  loading.value = true;
  errorMessage.value = "";
  previewedInstanceKey.value = "";
  popoverMediaState.value = {};
  try {
    const payload = await apiRequest(`/api/models/${encodeURI(modelDir.value)}`);
    detail.value = payload;
    authorAvatarSrc.value = payload.author?.avatar_url || "";
    const initialInstance = findInstanceByHash(payload.instances || []) || payload.instances?.[0] || null;
    activeInstanceKey.value = initialInstance?.instance_key || "";
    if (findInstanceByHash(payload.instances || [])) {
      selectInstance(initialInstance, { syncHash: false });
    } else if (payload.gallery?.length) {
      selectGallery(0);
    } else if (initialInstance) {
      selectInstance(initialInstance, { syncHash: false });
    } else {
      setMainMedia("", payload.cover_url || "", payload.cover_remote_url || "", payload.title);
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : "读取模型失败。";
  } finally {
    loading.value = false;
  }
}

watch(modelDir, () => {
  resetAttachmentUploadState({ keepCategory: false });
  load();
});

onMounted(load);
onMounted(() => {
  if (typeof window === "undefined") {
    return;
  }
  hoverPopoverMediaQuery = window.matchMedia("(min-width: 1241px) and (hover: hover) and (pointer: fine)");
  applyHoverPopoverEnabled(hoverPopoverMediaQuery.matches);
  hoverPopoverMediaListener = (event) => applyHoverPopoverEnabled(event.matches);
  if (typeof hoverPopoverMediaQuery.addEventListener === "function") {
    hoverPopoverMediaQuery.addEventListener("change", hoverPopoverMediaListener);
  } else if (typeof hoverPopoverMediaQuery.addListener === "function") {
    hoverPopoverMediaQuery.addListener(hoverPopoverMediaListener);
  }
  window.addEventListener("hashchange", handleHashChange);
});
onBeforeUnmount(() => {
  document.body.classList.remove("is-lightbox-open");
  if (hoverPopoverMediaQuery && hoverPopoverMediaListener) {
    if (typeof hoverPopoverMediaQuery.removeEventListener === "function") {
      hoverPopoverMediaQuery.removeEventListener("change", hoverPopoverMediaListener);
    } else if (typeof hoverPopoverMediaQuery.removeListener === "function") {
      hoverPopoverMediaQuery.removeListener(hoverPopoverMediaListener);
    }
  }
  if (typeof window !== "undefined") {
    window.removeEventListener("hashchange", handleHashChange);
  }
});
</script>
