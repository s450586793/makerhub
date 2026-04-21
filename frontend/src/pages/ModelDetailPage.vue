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
          <header class="mw-head mw-head--gallery">
            <div class="mw-head__identity mw-head__identity--gallery">
              <h1>{{ detail.title }}</h1>
              <div class="mw-head__subline">
                <span>{{ detail.author?.name || "未知作者" }}</span>
                <span class="mw-follow-pill">已归档</span>
                <span
                  v-if="detail.local_flags?.deleted"
                  class="mw-chip"
                  title="该模型已在 MakerHub 端删除，默认不会出现在模型库中。"
                >
                  本地已删
                </span>
                <span
                  v-if="detail.subscription_flags?.deleted_on_source"
                  class="mw-chip mw-chip--danger"
                  :title="deletedSourceTitle"
                >
                  源已删除
                </span>
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
            <div v-if="activeProfileFacts.length" class="mw-active-profile__facts">
              <span
                v-for="item in activeProfileFacts"
                :key="item.key"
                :class="['mw-active-profile__fact', item.key === 'rating' && 'mw-active-profile__fact--rating']"
              >
                <span class="mw-active-profile__fact-icon" aria-hidden="true" v-html="item.icon"></span>
                <span>{{ item.value }}</span>
              </span>
            </div>
            <div v-if="activeProfileFilaments.length" class="mw-profile-filaments">
              <span v-if="activeProfileNeedAms" class="mw-profile-ams" aria-hidden="true" v-html="AMS_ICON"></span>
              <span
                v-for="(filament, filamentIndex) in activeProfileFilaments"
                :key="`active-${filament.material}-${filament.color}-${filamentIndex}`"
                class="mw-profile-filament-chip"
                :style="filamentChipStyle(filament)"
              >
                <span>{{ filament.material || "耗材" }}｜</span>
                <span>{{ filament.weight_label || formatFilamentWeight(filament.weight) }}</span>
              </span>
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
              :ref="(element) => setProfileEntryRef(profile.instance_key, element)"
              @mouseenter="openProfilePopover(profile, $event.currentTarget)"
              @mouseleave="closeProfilePopover(profile.instance_key)"
              @focusin="openProfilePopover(profile, $event.currentTarget)"
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
                @click="handleProfileCardClick(profile)"
              >
                <div class="mw-profile-card__thumb-wrap">
                  <img
                    v-if="profile.preview_url_resolved"
                    class="mw-profile-card__thumb"
                    :src="profile.preview_url_resolved"
                    :alt="profile.title"
                    loading="lazy"
                    @error="swapEventImage($event, profile.preview_fallback_resolved)"
                  >
                  <span v-else class="mw-profile-card__thumb avatar-placeholder">{{ detail.title.slice(0, 1) }}</span>
                </div>

                <div class="mw-profile-card__body">
                  <div class="mw-profile-card__title">{{ profile.title }}</div>
                  <div class="mw-profile-card__meta">
                    <span class="mw-profile-card__badge">{{ profile.machine || "通用" }}</span>
                    <span v-if="profile.source_deleted" class="mw-profile-card__meta-item mw-profile-card__meta-item--danger">
                      已删除
                    </span>
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
                :class="[
                  'mw-profile-popover',
                  profilePopoverPlacement(profile, profileIndex, detail.instances.length),
                ]"
              >
                <div v-if="profile.media_resolved?.length" class="mw-profile-popover__gallery">
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
                    <span>{{ currentPopoverMediaIndex(profile) + 1 }} / {{ profile.media_resolved.length }}</span>
                  </div>

                  <div v-if="profile.media_resolved.length > 1" class="mw-profile-popover__media-strip">
                    <button
                      v-for="(media, mediaIndex) in profile.media_resolved"
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
                  <span v-if="profile.source_deleted">源端已删除</span>
                </div>
              </div>

                <div class="mw-profile-popover__stats">
                  <span class="mw-profile-popover__stat">
                    <strong>时长</strong>
                    <em>{{ profile.time || "-" }}</em>
                  </span>
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

                <div v-if="profileFilaments(profile).length" class="mw-profile-filaments mw-profile-filaments--popover">
                  <span v-if="profileNeedAms(profile)" class="mw-profile-ams" aria-hidden="true" v-html="AMS_ICON"></span>
                  <span
                    v-for="(filament, filamentIndex) in profileFilaments(profile)"
                    :key="`${profile.instance_key}-${filament.material}-${filament.color}-${filamentIndex}`"
                    class="mw-profile-filament-chip"
                    :style="filamentChipStyle(filament)"
                  >
                    <span>{{ filament.material || "耗材" }}｜</span>
                    <span>{{ filament.weight_label || formatFilamentWeight(filament.weight) }}</span>
                  </span>
                </div>

                <p class="mw-profile-popover__summary">
                  {{ profile.summary || "该打印配置可单独查看图集、分盘图片与 3MF 状态。" }}
                </p>

                <div class="mw-profile-popover__actions">
                  <button class="button button-secondary button-small" type="button" @click="selectInstance(profile)">
                    查看配置
                  </button>
                </div>

                <p v-if="profile.file_status_message" class="mw-profile-popover__note">
                  {{ profile.file_status_message }}
                </p>
                <p v-if="profile.source_deleted_message" class="mw-profile-popover__note">
                  {{ profile.source_deleted_message }}
                </p>
              </section>
            </div>
          </div>
          <p v-else class="empty-copy">当前没有可展示的打印配置。</p>
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
          <h2>评论 &amp; 评分 ({{ commentsTotal }})</h2>
        </div>
        <template v-if="commentsTotal > 0">
          <div v-if="commentsReady" class="comment-list">
            <article
              v-for="(comment, index) in visibleComments"
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
                  :class="['comment-gallery', comment.gallery_class]"
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
          <div v-else class="comment-list comment-list--pending">
            <p class="empty-copy">正在分批加载评论内容…</p>
          </div>
          <div v-if="hasMoreComments" class="mw-comments-more">
            <button class="button button-secondary" type="button" @click="loadMoreComments" :disabled="commentsLoadingMore">
              {{ commentsLoadingMore ? "加载中..." : "加载更多评论" }}
            </button>
            <span class="mw-comments-more__meta">
              已显示 {{ visibleComments.length }} / {{ commentsTotal }}
            </span>
          </div>
          <p v-if="commentsLoadError" class="mw-comments-more__error">{{ commentsLoadError }}</p>
        </template>
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
import { computed, onBeforeUnmount, onErrorCaptured, onMounted, ref, shallowRef, watch } from "vue";
import { useRoute } from "vue-router";

import { apiRequest } from "../lib/api";


const route = useRoute();

const loading = ref(true);
const errorMessage = ref("");
const detail = shallowRef(null);
const currentMedia = ref({
  key: "",
  src: "",
  fallback: "",
  alt: "",
});
const activeInstanceKey = ref("");
const lightboxSrc = ref("");
const attachmentFileInput = ref(null);
const attachmentUploading = ref(false);
const attachmentUploadMessage = ref("");
const attachmentUploadError = ref("");
const deletingAttachmentId = ref("");
const hoverPopoverEnabled = ref(false);
const previewedInstanceKey = ref("");
const profileEntryRefs = new Map();
const popoverPlacementState = ref({});
const popoverMediaState = ref({});
const commentsReady = ref(false);
const comments = shallowRef([]);
const commentsTotal = ref(0);
const commentsNextOffset = ref(null);
const commentsLoadingMore = ref(false);
const commentsLoadError = ref("");

let hoverPopoverMediaQuery = null;
let hoverPopoverMediaListener = null;
let commentsRenderFrame = 0;

const INITIAL_COMMENT_BATCH = 20;
const STAT_FORMATTER = new Intl.NumberFormat("zh-CN");
const PROFILE_FACT_ICONS = {
  clock: '<svg width="16" height="17" viewBox="0 0 16 17" fill="none"><path d="M15 8.52342C15 4.64449 11.866 1.5 8 1.5V8.52342L12.9395 13.5C14.2123 12.2282 15 10.4681 15 8.52342Z" fill="var(--mui-palette-colorSystem-grey200)"></path><path fill-rule="evenodd" clip-rule="evenodd" d="M7.9999 2.10039C4.46528 2.10039 1.5999 4.96577 1.5999 8.50039C1.5999 12.035 4.46528 14.9004 7.9999 14.9004C11.5345 14.9004 14.3999 12.035 14.3999 8.50039C14.3999 4.96577 11.5345 2.10039 7.9999 2.10039ZM0.399902 8.50039C0.399902 4.30303 3.80254 0.900391 7.9999 0.900391C12.1973 0.900391 15.5999 4.30303 15.5999 8.50039C15.5999 12.6978 12.1973 16.1004 7.9999 16.1004C3.80254 16.1004 0.399902 12.6978 0.399902 8.50039Z" fill="var(--mui-palette-colorSystem-grey700)"></path><path fill-rule="evenodd" clip-rule="evenodd" d="M7.9999 5.90039C8.33127 5.90039 8.5999 6.16902 8.5999 6.50039V8.50039C8.5999 8.83176 8.33127 9.10039 7.9999 9.10039C7.66853 9.10039 7.3999 8.83176 7.3999 8.50039V6.50039C7.3999 6.16902 7.66853 5.90039 7.9999 5.90039Z" fill="var(--mui-palette-colorSystem-grey700)"></path><path fill-rule="evenodd" clip-rule="evenodd" d="M7.57564 8.07613C7.80995 7.84181 8.18985 7.84181 8.42417 8.07613L10.4242 10.0761C10.6585 10.3104 10.6585 10.6903 10.4242 10.9247C10.1899 11.159 9.80995 11.159 9.57564 10.9247L7.57564 8.92465C7.34132 8.69034 7.34132 8.31044 7.57564 8.07613Z" fill="var(--mui-palette-colorSystem-grey700)"></path><path fill-rule="evenodd" clip-rule="evenodd" d="M0.5 8.5C0.5 8.22386 0.723858 8 1 8H2.5C2.77614 8 3 8.22386 3 8.5C3 8.77614 2.77614 9 2.5 9H1C0.723858 9 0.5 8.77614 0.5 8.5Z" fill="var(--mui-palette-colorSystem-grey700)"></path><path fill-rule="evenodd" clip-rule="evenodd" d="M13 8.5C13 8.22386 13.2239 8 13.5 8H15C15.2761 8 15.5 8.22386 15.5 8.5C15.5 8.77614 15.2761 9 15 9H13.5C13.2239 9 13 8.77614 13 8.5Z" fill="var(--mui-palette-colorSystem-grey700)"></path><path fill-rule="evenodd" clip-rule="evenodd" d="M8 16C7.72386 16 7.5 15.7761 7.5 15.5L7.5 14C7.5 13.7239 7.72386 13.5 8 13.5C8.27614 13.5 8.5 13.7239 8.5 14L8.5 15.5C8.5 15.7761 8.27614 16 8 16Z" fill="var(--mui-palette-colorSystem-grey700)"></path><path fill-rule="evenodd" clip-rule="evenodd" d="M8 3.5C7.72386 3.5 7.5 3.27614 7.5 3L7.5 1.5C7.5 1.22386 7.72386 1 8 1C8.27614 1 8.5 1.22386 8.5 1.5L8.5 3C8.5 3.27614 8.27614 3.5 8 3.5Z" fill="var(--mui-palette-colorSystem-grey700)"></path></svg>',
  plates: '<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M1.5 2.5C1.5 1.94772 1.94772 1.5 2.5 1.5H7.66667V14.5H2.5C1.94771 14.5 1.5 14.0523 1.5 13.5V2.5Z" fill="var(--mui-palette-colorSystem-grey200)"></path><path fill-rule="evenodd" clip-rule="evenodd" d="M0.89978 2.50015C0.89978 1.61649 1.61612 0.900146 2.49978 0.900146H7.66645L7.68191 0.900342H13.4997C14.3834 0.900342 15.0997 1.61669 15.0997 2.50034V7.93624C15.1019 7.95711 15.103 7.9783 15.103 7.99976V13.4998C15.103 14.3834 14.3867 15.0998 13.503 15.0998H7.68831C7.68106 15.1 7.67377 15.1001 7.66645 15.1001H2.49978C1.61612 15.1001 0.89978 14.3838 0.89978 13.5001V2.50015ZM13.8997 7.39976H8.26645V2.10034H13.4997C13.7206 2.10034 13.8997 2.27943 13.8997 2.50034V7.39976ZM8.2697 8.60034V13.8998H13.503C13.7239 13.8998 13.903 13.7207 13.903 13.4998V8.60034H8.2697ZM2.49978 2.10015H7.06638V8.00034L7.06645 8.00929V13.9001H2.49978C2.27887 13.9001 2.09978 13.7211 2.09978 13.5001V2.50015C2.09978 2.27923 2.27887 2.10015 2.49978 2.10015Z" fill="var(--mui-palette-colorSystem-grey700)"></path></svg>',
  nozzle: '<svg width="1em" height="1em" viewBox="0 0 24 24" fill="none"><path d="M16.0005 14H8.00049H4.00049V18H20.0005V14H16.0005Z" fill="var(--mui-palette-colorSystem-grey300)"></path><path fill-rule="evenodd" clip-rule="evenodd" d="M7.99951 1.25C7.5853 1.25 7.24951 1.58579 7.24951 2V5.184L5.9016 5.40865C5.57474 5.46313 5.35393 5.77226 5.4084 6.09912C5.46288 6.42599 5.77202 6.6468 6.09888 6.59232L7.24951 6.40055V8.184L5.9016 8.40865C5.57474 8.46313 5.35393 8.77226 5.4084 9.09912C5.46288 9.42599 5.77202 9.6468 6.09888 9.59232L7.24951 9.40055V11.184L5.9016 11.4086C5.57474 11.4631 5.35393 11.7723 5.4084 12.0991C5.46288 12.426 5.77202 12.6468 6.09888 12.5923L7.24951 12.4005V13.25H4.00049C3.58627 13.25 3.25049 13.5858 3.25049 14V18C3.25049 18.4142 3.58627 18.75 4.00049 18.75H7.24951V19.5C7.24951 19.6989 7.32853 19.8897 7.46918 20.0303L9.96918 22.5303C10.1098 22.671 10.3006 22.75 10.4995 22.75H13.4995C13.6984 22.75 13.8892 22.671 14.0298 22.5303L16.5298 20.0303C16.6705 19.8897 16.7495 19.6989 16.7495 19.5V18.75H20.0005C20.4147 18.75 20.7505 18.4142 20.7505 18V14C20.7505 13.5858 20.4147 13.25 20.0005 13.25H16.7495V10.8172L18.0989 10.5923C18.4257 10.5378 18.6466 10.2287 18.5921 9.90184C18.5376 9.57498 18.2285 9.35417 17.9016 9.40865L16.7495 9.60066V7.81722L18.0989 7.59232C18.4257 7.53784 18.6466 7.22871 18.5921 6.90184C18.5376 6.57498 18.2285 6.35417 17.9016 6.40865L16.7495 6.60066V4.81722L18.0989 4.59232C18.4257 4.53784 18.6466 4.22871 18.5921 3.90184C18.5376 3.57498 18.2285 3.35417 17.9016 3.40865L16.7495 3.60066V2C16.7495 1.58579 16.4137 1.25 15.9995 1.25H7.99951ZM15.2495 13.25V11.0672L8.74951 12.1505V13.25H15.2495ZM15.2495 18.75H8.74951V19.1893L10.8102 21.25H13.1889L15.2495 19.1893V18.75ZM8.74951 7.934V6.15055L15.2495 5.06722V6.85066L8.74951 7.934ZM8.74951 4.934L15.2495 3.85066V2.75H8.74951V4.934ZM8.74951 9.15055L15.2495 8.06722V9.85066L8.74951 10.934V9.15055ZM4.75049 17.25V14.75H19.2505V17.25H4.75049Z" fill="var(--mui-palette-colorSystem-grey800)"></path></svg>',
  filament: '<svg width="1em" height="1em" viewBox="0 0 24 24" fill="none"><path fill-rule="evenodd" clip-rule="evenodd" d="M3.82997 5.53396C2.96342 7.14327 2.39985 9.4283 2.39985 12.0016C2.39985 14.5748 2.96342 16.8599 3.82997 18.4692C4.71626 20.1151 5.78984 20.8516 6.74985 20.8516C7.70987 20.8516 8.78345 20.1151 9.66974 18.4692C10.5363 16.8599 11.0999 14.5748 11.0999 12.0016C11.0999 9.4283 10.5363 7.14327 9.66974 5.53396C8.78345 3.88799 7.70987 3.15156 6.74985 3.15156C5.78984 3.15156 4.71626 3.88799 3.82997 5.53396ZM2.24512 4.68058C3.25896 2.79774 4.81037 1.35156 6.74985 1.35156C8.68934 1.35156 10.2408 2.79774 11.2546 4.68058C12.2882 6.60008 12.8999 9.19005 12.8999 12.0016C12.8999 14.8131 12.2882 17.403 11.2546 19.3225C10.2408 21.2054 8.68934 22.6516 6.74985 22.6516C4.81037 22.6516 3.25896 21.2054 2.24512 19.3225C1.21154 17.403 0.599854 14.8131 0.599854 12.0016C0.599854 9.19005 1.21154 6.60008 2.24512 4.68058Z" fill="var(--mui-palette-colorSystem-grey800)"></path><path fill-rule="evenodd" clip-rule="evenodd" d="M13.1115 6H7.5C6.25736 6 5.25 8.2129 5.25 12C5.25 15.7871 6.25736 18 7.5 18H13.1115C12.4151 16.3455 12 14.2628 12 12C12 9.73721 12.4151 7.65445 13.1115 6Z" fill="var(--mui-palette-colorSystem-grey300)"></path><path fill-rule="evenodd" clip-rule="evenodd" d="M13.4616 5.25H7.5C6.38005 5.25 5.63974 6.22446 5.21184 7.30333C4.75749 8.44891 4.5 10.052 4.5 12C4.5 13.948 4.75749 15.5511 5.21184 16.6967C5.63974 17.7755 6.38005 18.75 7.5 18.75H13.4616C13.2243 18.2907 13.0109 17.7884 12.8253 17.25H7.5C7.37731 17.25 6.99262 17.118 6.60618 16.1437C6.24619 15.236 6 13.8391 6 12C6 10.1609 6.24619 8.764 6.60618 7.85635C6.99262 6.88199 7.37731 6.75 7.5 6.75H12.8253C13.0109 6.21157 13.2243 5.70925 13.4616 5.25Z" fill="var(--mui-palette-colorSystem-grey800)"></path><path fill-rule="evenodd" clip-rule="evenodd" d="M14.33 5.53396C13.4634 7.14327 12.8999 9.4283 12.8999 12.0016C12.8999 14.5748 13.4634 16.8599 14.33 18.4692C15.2163 20.1151 16.2898 20.8516 17.2499 20.8516C18.2099 20.8516 19.2834 20.1151 20.1697 18.4692C21.0363 16.8599 21.5999 14.5748 21.5999 12.0016C21.5999 9.4283 21.0363 7.14327 20.1697 5.53396C19.2834 3.88799 18.2099 3.15156 17.2499 3.15156C16.2898 3.15156 15.2163 3.88799 14.33 5.53396ZM12.7451 4.68058C13.759 2.79774 15.3104 1.35156 17.2499 1.35156C19.1893 1.35156 20.7408 2.79774 21.7546 4.68058C22.7882 6.60008 23.3999 9.19005 23.3999 12.0016C23.3999 14.8131 22.7882 17.403 21.7546 19.3225C20.7408 21.2054 19.1893 22.6516 17.2499 22.6516C15.3104 22.6516 13.759 21.2054 12.7451 19.3225C11.7115 17.403 11.0999 14.8131 11.0999 12.0016C11.0999 9.19005 11.7115 6.60008 12.7451 4.68058Z" fill="var(--mui-palette-colorSystem-grey800)"></path><ellipse cx="17.25" cy="12" rx="1.5" ry="3" fill="var(--mui-palette-colorSystem-grey300)" stroke="var(--mui-palette-colorSystem-grey800)" stroke-width="0.6" stroke-linejoin="round"></ellipse></svg>',
  rating: '<svg viewBox="0 0 20 20" fill="currentColor"><path d="m10 2.5 2.3 4.65 5.14.75-3.72 3.62.88 5.12L10 14.19l-4.6 2.45.88-5.12L2.56 7.9l5.14-.75L10 2.5Z"/></svg>',
};
const AMS_ICON = '<svg width="2.3em" height="1.2em" viewBox="0 0 46 24" fill="none"><rect x="0.5" y="0.5" width="45" height="23" rx="1.5" fill="#212121" stroke="#000"></rect><path opacity="0.6" d="M1 2a1 1 0 011-1h10v22H2a1 1 0 01-1-1V2z" fill="#E14747"></path><path opacity="0.6" d="M12 1h11v22H12V1z" fill="#FEC90D"></path><path opacity="0.6" d="M23 1h11v22H23V1z" fill="var(--mui-palette-primary-main)"></path><path opacity="0.6" d="M34 1h10a1 1 0 011 1v20a1 1 0 01-1 1H34V1z" fill="#0E65E9"></path><path d="M15.822 17l-.62-2.04h-3.124L11.457 17H9.5l3.023-8.602h2.221L17.78 17h-1.957zm-1.054-3.563l-.622-1.992c-.039-.133-.091-.302-.158-.51a57.03 57.03 0 00-.193-.638 15.253 15.253 0 01-.152-.568 58.37 58.37 0 01-.492 1.717l-.616 1.991h2.233zM22.49 17l-2.062-6.72h-.053l.035.726c.02.32.037.662.053 1.025.015.364.023.692.023.985V17h-1.623V8.434h2.473l2.027 6.55h.035l2.15-6.55h2.474V17h-1.694v-4.055c0-.27.004-.58.012-.931.012-.352.025-.686.04-1.002.017-.32.028-.56.036-.721h-.053L24.154 17H22.49zm12.75-2.379c0 .508-.123.95-.369 1.324-.246.375-.605.664-1.078.867-.469.204-1.04.305-1.711.305a6.42 6.42 0 01-.873-.058 6.022 6.022 0 01-.814-.17 5.096 5.096 0 01-.739-.287v-1.688c.407.18.828.342 1.266.486a4.13 4.13 0 001.3.217c.297 0 .536-.039.715-.117a.817.817 0 00.399-.322.892.892 0 00.123-.469.772.772 0 00-.217-.55 2.265 2.265 0 00-.597-.429c-.25-.132-.534-.275-.85-.427a9.757 9.757 0 01-.65-.34 3.801 3.801 0 01-.668-.498 2.4 2.4 0 01-.522-.71c-.133-.28-.2-.616-.2-1.007 0-.512.118-.95.352-1.312.235-.364.569-.641 1.002-.832.438-.196.953-.293 1.547-.293.446 0 .87.052 1.272.158.406.101.83.25 1.271.445l-.586 1.412a9.706 9.706 0 00-1.06-.369 3.44 3.44 0 00-.955-.135c-.227 0-.42.037-.58.112a.781.781 0 00-.364.304.822.822 0 00-.123.452c0 .203.059.375.176.515.121.137.3.27.54.399.241.128.542.279.901.45.438.208.811.425 1.12.651.312.223.552.486.72.791.168.3.252.676.252 1.125z" fill="#fff"></path></svg>';

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

function decodeRouteValue(value) {
  const raw = String(value ?? "");
  if (!raw) {
    return "";
  }
  try {
    return decodeURIComponent(raw);
  } catch {
    return raw;
  }
}

const modelDir = computed(() => {
  const raw = route.params.modelDir;
  if (Array.isArray(raw)) {
    return raw
      .map((item) => decodeRouteValue(item))
      .filter(Boolean)
      .join("/");
  }
  return decodeRouteValue(raw);
});

const activeInstance = computed(() => {
  return detail.value?.instances?.find((item) => item.instance_key === activeInstanceKey.value) || null;
});

const activeProfileFacts = computed(() => {
  const source = activeInstance.value || detail.value?.profile_summary || null;
  if (!source) {
    return [];
  }

  const profileDetails = source.profile_details || {};
  const items = [];
  const plates = Number(source.plates || profileDetails.plate_count || 0);
  if (plates > 0) {
    items.push({ key: "plates", icon: PROFILE_FACT_ICONS.plates, value: `${plates} 盘` });
  }
  const time = source.time || profileDetails.print_time_label || "";
  if (time) {
    items.push({ key: "time", icon: PROFILE_FACT_ICONS.clock, value: String(time) });
  }
  const nozzleLabel = source.nozzle_diameter_label || profileDetails.nozzle_diameter_label || "";
  if (nozzleLabel) {
    items.push({ key: "nozzle", icon: PROFILE_FACT_ICONS.nozzle, value: nozzleLabel });
  }
  const filamentWeightLabel = source.filament_weight_label || profileDetails.filament_weight_label || "";
  if (filamentWeightLabel) {
    items.push({ key: "filament", icon: PROFILE_FACT_ICONS.filament, value: filamentWeightLabel });
  }
  if (String(source.rating ?? "").trim()) {
    items.push({ key: "rating", icon: PROFILE_FACT_ICONS.rating, value: formatRating(source.rating) });
  }
  return items;
});

const activeProfileFilaments = computed(() => profileFilaments(activeInstance.value));
const activeProfileNeedAms = computed(() => profileNeedAms(activeInstance.value));

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
    return "源端已删除该模型";
  }
  return `源端已删除该模型：${items.map((item) => item.name || item.url || "未命名来源").join("、")}`;
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

const visibleComments = computed(() => {
  if (!commentsReady.value) {
    return [];
  }
  return comments.value;
});

const hasMoreComments = computed(() => {
  return commentsReady.value && commentsNextOffset.value !== null;
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
    return decodeRouteValue(normalized.replace(/^profileId-/i, ""));
  }
  return decodeRouteValue(normalized);
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

function handleProfileCardClick(profile) {
  if (!profile) {
    return;
  }
  selectInstance(profile);
  updateProfilePopoverPlacement(profile);
  if (hoverPopoverEnabled.value) {
    openProfilePopover(profile);
    return;
  }
  if (previewedInstanceKey.value === profile.instance_key) {
    previewedInstanceKey.value = "";
    return;
  }
  previewedInstanceKey.value = profile.instance_key;
  if (!(profile.instance_key in popoverMediaState.value)) {
    selectPopoverMedia(profile, 0);
  }
}

function isProfilePopoverOpen(profile) {
  return previewedInstanceKey.value === profile?.instance_key;
}

function setProfileEntryRef(instanceKey, element) {
  if (!instanceKey) {
    return;
  }
  if (element instanceof HTMLElement) {
    profileEntryRefs.set(instanceKey, element);
    return;
  }
  profileEntryRefs.delete(instanceKey);
}

function updateProfilePopoverPlacement(profile, entryElement = null) {
  if (!profile?.instance_key || typeof window === "undefined") {
    return;
  }
  const host = entryElement instanceof HTMLElement ? entryElement : profileEntryRefs.get(profile.instance_key);
  if (!(host instanceof HTMLElement)) {
    return;
  }
  const rect = host.getBoundingClientRect();
  const viewportPadding = 16;
  const horizontalGap = 18;
  const estimatedPopoverWidth = Math.min(380, Math.max(window.innerWidth - 140, 260));
  const canOpenLeft = rect.left >= estimatedPopoverWidth + horizontalGap + viewportPadding;
  popoverPlacementState.value = {
    ...popoverPlacementState.value,
    [profile.instance_key]: canOpenLeft ? "left" : "below",
  };
}

function openProfilePopover(profile, entryElement = null) {
  if (!hoverPopoverEnabled.value || !profile) {
    return;
  }
  updateProfilePopoverPlacement(profile, entryElement);
  previewedInstanceKey.value = profile.instance_key;
  if (!(profile.instance_key in popoverMediaState.value)) {
    selectPopoverMedia(profile, 0);
  }
}

function closeProfilePopover(instanceKey = "", options = {}) {
  const { force = false } = options;
  if (!hoverPopoverEnabled.value && !force) {
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
  closeProfilePopover(profile?.instance_key || "", { force: !hoverPopoverEnabled.value });
}

function profilePopoverPlacement(profile, index, total) {
  if (popoverPlacementState.value[profile?.instance_key] === "below") {
    return "is-below";
  }
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

function handleWindowPointerDown(event) {
  if (hoverPopoverEnabled.value || !previewedInstanceKey.value) {
    return;
  }
  const target = event.target;
  if (target instanceof Element && target.closest(".mw-profile-entry")) {
    return;
  }
  previewedInstanceKey.value = "";
}

function handleWindowResize() {
  if (!previewedInstanceKey.value || !detail.value?.instances?.length) {
    return;
  }
  const profile = detail.value.instances.find((item) => item.instance_key === previewedInstanceKey.value);
  if (!profile) {
    return;
  }
  updateProfilePopoverPlacement(profile);
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

function openLightbox(src) {
  lightboxSrc.value = src;
  document.body.classList.add("is-lightbox-open");
}

function closeLightbox() {
  lightboxSrc.value = "";
  document.body.classList.remove("is-lightbox-open");
}

function formatStat(value) {
  return STAT_FORMATTER.format(Number(value || 0));
}

function formatRating(value) {
  const numeric = Number(value);
  if (Number.isFinite(numeric)) {
    return numeric.toFixed(1);
  }
  return String(value || "");
}

function formatFilamentWeight(value) {
  const numeric = Number(value || 0);
  if (!Number.isFinite(numeric) || numeric <= 0) {
    return "";
  }
  return `${numeric % 1 === 0 ? numeric.toFixed(0) : numeric.toFixed(1)} g`;
}

function profileFilaments(profile) {
  if (!profile) {
    return [];
  }
  const direct = Array.isArray(profile.filaments) ? profile.filaments : [];
  if (direct.length) {
    return direct;
  }
  const details = profile.profile_details || {};
  return Array.isArray(details.filaments) ? details.filaments : [];
}

function profileNeedAms(profile) {
  if (!profile) {
    return false;
  }
  const filaments = profileFilaments(profile);
  return Boolean(profile.need_ams || profile.profile_details?.need_ams || filaments.some((item) => item?.ams));
}

function parseCssColor(color) {
  const raw = String(color || "").trim();
  if (!raw) {
    return null;
  }
  if (/^#[0-9a-f]{6}$/i.test(raw)) {
    return {
      r: Number.parseInt(raw.slice(1, 3), 16),
      g: Number.parseInt(raw.slice(3, 5), 16),
      b: Number.parseInt(raw.slice(5, 7), 16),
    };
  }
  const rgbMatch = raw.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/i);
  if (rgbMatch) {
    return {
      r: Number(rgbMatch[1]),
      g: Number(rgbMatch[2]),
      b: Number(rgbMatch[3]),
    };
  }
  return null;
}

function filamentChipStyle(filament) {
  const color = String(filament?.color || "").trim() || "#e5e7eb";
  const rgb = parseCssColor(color);
  const luminance = rgb ? (rgb.r * 0.299 + rgb.g * 0.587 + rgb.b * 0.114) : 180;
  const textColor = luminance > 185 ? "rgb(43, 43, 43)" : "rgb(255, 255, 255)";
  const isNearWhite = rgb && rgb.r > 235 && rgb.g > 235 && rgb.b > 235;
  return {
    background: color,
    color: textColor,
    border: isNearWhite ? "1px solid rgb(51, 51, 51)" : "0",
  };
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

function buildProfileMedia(profile) {
  const mediaItems = Array.isArray(profile?.media)
    ? profile.media.filter((item) => item && (item.url || item.fallback_url))
    : [];
  if (mediaItems.length) {
    return mediaItems;
  }
  const previewUrl = profile?.preview_url_resolved || "";
  const previewFallback = profile?.preview_fallback_resolved || "";
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
  const mediaItems = Array.isArray(profile?.media_resolved) ? profile.media_resolved : [];
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
  const mediaItems = Array.isArray(profile?.media_resolved) ? profile.media_resolved : [];
  return mediaItems[currentPopoverMediaIndex(profile)] || null;
}

function selectPopoverMedia(profile, index) {
  if (!profile?.instance_key) {
    return;
  }
  const mediaItems = Array.isArray(profile?.media_resolved) ? profile.media_resolved : [];
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

function prepareDetailPayload(payload) {
  if (!payload || typeof payload !== "object") {
    return payload;
  }

  const instances = Array.isArray(payload.instances)
    ? payload.instances.map((instance) => {
        const previewUrl = instance?.thumbnail_url || instance?.primary_image_url || "";
        const previewFallback = instance?.thumbnail_fallback_url || instance?.primary_image_fallback_url || "";
        const mediaResolved = buildProfileMedia({
          ...instance,
          preview_url_resolved: previewUrl,
          preview_fallback_resolved: previewFallback,
        });
        return {
          ...instance,
          preview_url_resolved: previewUrl,
          preview_fallback_resolved: previewFallback,
          media_resolved: mediaResolved,
        };
      })
    : [];

  return {
    ...payload,
    instances,
  };
}

function prepareComments(items) {
  if (!Array.isArray(items)) {
    return [];
  }
  return items.map((comment) => ({
    ...comment,
    gallery_class: commentGalleryClass(Array.isArray(comment.images) ? comment.images.length : 0),
  }));
}

function scheduleCommentsRender() {
  commentsReady.value = false;
  if (typeof window === "undefined") {
    commentsReady.value = true;
    return;
  }
  if (commentsRenderFrame) {
    window.cancelAnimationFrame(commentsRenderFrame);
  }
  commentsRenderFrame = window.requestAnimationFrame(() => {
    commentsRenderFrame = window.requestAnimationFrame(() => {
      commentsReady.value = true;
      commentsRenderFrame = 0;
    });
  });
}

async function loadMoreComments() {
  if (commentsLoadingMore.value || commentsNextOffset.value === null) {
    return;
  }
  commentsLoadingMore.value = true;
  commentsLoadError.value = "";
  try {
    const payload = await apiRequest(
      `/api/models/${encodeURIComponent(modelDir.value)}/comments?offset=${commentsNextOffset.value}&limit=${INITIAL_COMMENT_BATCH}`,
    );
    comments.value = [...comments.value, ...prepareComments(payload.items || [])];
    commentsTotal.value = Number(payload.total || commentsTotal.value || comments.value.length);
    commentsNextOffset.value = payload.next_offset ?? null;
  } catch (error) {
    commentsLoadError.value = error instanceof Error ? error.message : "评论加载失败。";
  } finally {
    commentsLoadingMore.value = false;
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
    const payload = await apiRequest(`/api/models/${encodeURIComponent(modelDir.value)}/attachments`, {
      method: "POST",
      body: formData,
    });
    detail.value = prepareDetailPayload(payload.detail);
    comments.value = prepareComments(payload.detail?.comments || []);
    commentsTotal.value = Number(payload.detail?.comments_total || comments.value.length);
    commentsNextOffset.value = payload.detail?.comments_next_offset ?? null;
    scheduleCommentsRender();
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
    const payload = await apiRequest(`/api/models/${encodeURIComponent(modelDir.value)}/attachments/${encodeURIComponent(attachment.id)}`, {
      method: "DELETE",
    });
    detail.value = prepareDetailPayload(payload.detail);
    comments.value = prepareComments(payload.detail?.comments || []);
    commentsTotal.value = Number(payload.detail?.comments_total || comments.value.length);
    commentsNextOffset.value = payload.detail?.comments_next_offset ?? null;
    scheduleCommentsRender();
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
  profileEntryRefs.clear();
  previewedInstanceKey.value = "";
  popoverPlacementState.value = {};
  popoverMediaState.value = {};
  commentsReady.value = false;
  comments.value = [];
  commentsTotal.value = 0;
  commentsNextOffset.value = null;
  commentsLoadingMore.value = false;
  commentsLoadError.value = "";
  try {
    const payload = prepareDetailPayload(await apiRequest(`/api/models/${encodeURIComponent(modelDir.value)}`));
    detail.value = payload;
    comments.value = prepareComments(payload.comments || []);
    commentsTotal.value = Number(payload.comments_total || comments.value.length);
    commentsNextOffset.value = payload.comments_next_offset ?? null;
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
    scheduleCommentsRender();
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : "读取模型失败。";
  } finally {
    loading.value = false;
  }
}

watch(modelDir, (value) => {
  resetAttachmentUploadState({ keepCategory: false });
  if (!value) {
    profileEntryRefs.clear();
    detail.value = null;
    comments.value = [];
    commentsTotal.value = 0;
    commentsNextOffset.value = null;
    commentsLoadingMore.value = false;
    commentsLoadError.value = "";
    loading.value = false;
    errorMessage.value = "模型不存在。";
    return;
  }
  load();
}, { immediate: true });

onErrorCaptured((error) => {
  errorMessage.value = error instanceof Error ? error.message : "模型详情渲染失败。";
  loading.value = false;
  return false;
});

onMounted(() => {
  if (typeof window === "undefined") {
    return;
  }
  hoverPopoverMediaQuery = window.matchMedia("(hover: hover) and (pointer: fine)");
  applyHoverPopoverEnabled(hoverPopoverMediaQuery.matches);
  hoverPopoverMediaListener = (event) => applyHoverPopoverEnabled(event.matches);
  if (typeof hoverPopoverMediaQuery.addEventListener === "function") {
    hoverPopoverMediaQuery.addEventListener("change", hoverPopoverMediaListener);
  } else if (typeof hoverPopoverMediaQuery.addListener === "function") {
    hoverPopoverMediaQuery.addListener(hoverPopoverMediaListener);
  }
  window.addEventListener("hashchange", handleHashChange);
  window.addEventListener("pointerdown", handleWindowPointerDown);
  window.addEventListener("resize", handleWindowResize);
});
onBeforeUnmount(() => {
  document.body.classList.remove("is-lightbox-open");
  profileEntryRefs.clear();
  commentsReady.value = false;
  comments.value = [];
  commentsTotal.value = 0;
  commentsNextOffset.value = null;
  commentsLoadingMore.value = false;
  commentsLoadError.value = "";
  if (commentsRenderFrame && typeof window !== "undefined") {
    window.cancelAnimationFrame(commentsRenderFrame);
  }
  if (hoverPopoverMediaQuery && hoverPopoverMediaListener) {
    if (typeof hoverPopoverMediaQuery.removeEventListener === "function") {
      hoverPopoverMediaQuery.removeEventListener("change", hoverPopoverMediaListener);
    } else if (typeof hoverPopoverMediaQuery.removeListener === "function") {
      hoverPopoverMediaQuery.removeListener(hoverPopoverMediaListener);
    }
  }
  if (typeof window !== "undefined") {
    window.removeEventListener("hashchange", handleHashChange);
    window.removeEventListener("pointerdown", handleWindowPointerDown);
    window.removeEventListener("resize", handleWindowResize);
  }
});
</script>
