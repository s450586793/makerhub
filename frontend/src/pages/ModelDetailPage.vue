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
      <div :class="['mw-hero', detail.gallery?.length && 'mw-hero--has-gallery-strip']">
        <header class="mw-head mw-head--gallery mw-hero__head">
          <div class="mw-head__top">
            <RouterLink class="button button-secondary mw-head__back" :to="detailBackTarget">{{ detailBackLabel }}</RouterLink>

            <div class="mw-head__author">
              <img
                v-if="detail.author?.avatar_url || detail.author?.avatar_remote_url"
                class="mw-head__avatar"
                :src="detail.author?.avatar_url || detail.author?.avatar_remote_url"
                :alt="detail.author?.name || '作者头像'"
                @error="swapEventImage($event, detail.author?.avatar_remote_url)"
              >
              <span v-else class="mw-head__avatar avatar-placeholder">{{ (detail.author?.name || detail.title || "?").slice(0, 1) }}</span>

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
            </div>

            <div class="mw-head__crumbs">
              <span class="mw-crumb">{{ detail.source_label }}</span>
              <template v-for="crumb in headCrumbs" :key="crumb">
                <span class="mw-crumb__sep">&gt;</span>
                <span class="mw-crumb">{{ crumb }}</span>
              </template>
              <button
                v-if="isLocalModel"
                class="button button-secondary button-small mw-local-edit-trigger"
                type="button"
                @click="openLocalEditDialog"
              >
                编辑
              </button>
            </div>
          </div>
        </header>

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
                v-if="activeModelPreviewFile"
                class="mw-gallery__preview"
                type="button"
                @click="openModelPreview"
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

            <div
              v-if="detail.gallery?.length"
              :class="[
                'mw-thumb-rail',
                'mw-thumb-rail--gallery',
                mainGalleryRail.overflow && 'is-overflowing',
                mainGalleryRail.canScrollPrev && 'has-prev',
                mainGalleryRail.canScrollNext && 'has-next',
              ]"
            >
              <button
                v-if="mainGalleryRail.overflow"
                class="mw-thumb-rail__control is-prev"
                type="button"
                :disabled="!mainGalleryRail.canScrollPrev"
                aria-label="向左查看缩略图"
                @click="scrollMainGalleryThumbs('prev')"
              >
                <span class="mw-thumb-rail__control-icon" aria-hidden="true" v-html="THUMB_RAIL_ICONS.prev"></span>
              </button>
              <div
                :ref="setMainGalleryThumbsRef"
                class="mw-gallery__thumbs"
                @scroll="syncMainGalleryRail"
              >
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
              <button
                v-if="mainGalleryRail.overflow"
                class="mw-thumb-rail__control is-next"
                type="button"
                :disabled="!mainGalleryRail.canScrollNext"
                aria-label="向右查看缩略图"
                @click="scrollMainGalleryThumbs('next')"
              >
                <span class="mw-thumb-rail__control-icon" aria-hidden="true" v-html="THUMB_RAIL_ICONS.next"></span>
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
          </div>

          <div class="mw-statline">
            <span class="mw-stat-row__publish">发布于 {{ detail.publish_date || "未知时间" }}</span>
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
            <button
              class="mw-inline-link mw-inline-link--ghost"
              type="button"
              @click="openShareDialog"
            >
              分享
            </button>
          </div>

          <aside class="mw-config-panel">
          <div class="mw-config-panel__header">
            <h2>打印配置 <span>({{ detail.instances?.length || 0 }})</span></h2>

            <div v-if="machineFilters.length" class="mw-config-panel__filters">
              <span class="mw-filter-pill is-active">全部</span>
              <span v-for="machine in machineFilters.slice(0, 10)" :key="machine" class="mw-filter-pill">{{ machine }}</span>
            </div>
          </div>
          <div class="mw-config-panel__divider"></div>

          <div v-if="detail.instances?.length" class="mw-profile-list" @scroll="handleProfileListScroll">
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
                    <span v-if="profileTimeLabel(profile)" class="mw-profile-card__meta-item">
                      <span class="mw-profile-card__meta-icon" aria-hidden="true" v-html="PROFILE_FACT_ICONS.clock"></span>
                      <span>{{ profileTimeLabel(profile) }}</span>
                    </span>
                    <span v-if="profile.plates" class="mw-profile-card__meta-item">
                      <span class="mw-profile-card__meta-icon" aria-hidden="true" v-html="PROFILE_FACT_ICONS.plates"></span>
                      <span>{{ profile.plates }} 盘</span>
                    </span>
                    <span v-if="formatProfileRating(profile.rating)" class="mw-profile-card__meta-item mw-profile-card__meta-item--rating">
                      <span class="mw-profile-card__meta-icon" aria-hidden="true" v-html="PROFILE_FACT_ICONS.rating"></span>
                      <span>{{ formatProfileRating(profile.rating) }}</span>
                    </span>
                  </div>
                </div>
              </button>

              <section
                v-if="isProfilePopoverOpen(profile)"
                :class="[
                  'mw-profile-popover',
                  profilePopoverPlacement(profile, profileIndex, detail.instances.length),
                ]"
                :style="profilePopoverStyle(profile, profileIndex, detail.instances.length)"
              >
                <div v-if="profile.media_resolved?.length" class="mw-profile-popover__gallery">
                  <div
                    :class="[
                      'mw-thumb-rail',
                      'mw-thumb-rail--popover',
                      profileMediaRailState(profile.instance_key).overflow && 'is-overflowing',
                      profileMediaRailState(profile.instance_key).canScrollPrev && 'has-prev',
                      profileMediaRailState(profile.instance_key).canScrollNext && 'has-next',
                    ]"
                  >
                    <button
                      v-if="profileMediaRailState(profile.instance_key).overflow"
                      class="mw-thumb-rail__control is-prev"
                      type="button"
                      :disabled="!profileMediaRailState(profile.instance_key).canScrollPrev"
                      aria-label="向左查看配置图片"
                      @click="scrollProfileMediaStrip(profile.instance_key, 'prev')"
                    >
                      <span class="mw-thumb-rail__control-icon" aria-hidden="true" v-html="THUMB_RAIL_ICONS.prev"></span>
                    </button>
                    <div
                      :ref="(element) => setProfileMediaStripRef(profile.instance_key, element)"
                      class="mw-profile-popover__media-strip"
                      @scroll="syncProfileMediaRail(profile.instance_key)"
                    >
                    <button
                      v-for="(media, mediaIndex) in profile.media_resolved"
                      :key="`${profile.instance_key}-${media.label}-${mediaIndex}`"
                      class="mw-profile-popover__media-thumb"
                      type="button"
                      :title="media.label || `预览 ${mediaIndex + 1}`"
                      @click="openLightbox(media.url || media.fallback_url || '', `${profile.title} ${media.label || `预览 ${mediaIndex + 1}`}`.trim())"
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
                    </button>
                    </div>
                    <button
                      v-if="profileMediaRailState(profile.instance_key).overflow"
                      class="mw-thumb-rail__control is-next"
                      type="button"
                      :disabled="!profileMediaRailState(profile.instance_key).canScrollNext"
                      aria-label="向右查看配置图片"
                      @click="scrollProfileMediaStrip(profile.instance_key, 'next')"
                    >
                      <span class="mw-thumb-rail__control-icon" aria-hidden="true" v-html="THUMB_RAIL_ICONS.next"></span>
                    </button>
                  </div>
                </div>

                <div class="mw-profile-popover__hero-body">
                  <div class="mw-profile-popover__eyebrow">打印配置</div>
                  <h3>{{ profile.title }}</h3>
                <div class="mw-profile-popover__meta">
                  <span>{{ profile.machine || "通用" }}</span>
                  <span v-if="profileTimeLabel(profile)">{{ profileTimeLabel(profile) }}</span>
                  <span v-if="profile.publish_date">上传于 {{ profile.publish_date }}</span>
                  <span v-if="profile.source_deleted">源端已删除</span>
                </div>
              </div>

                <div v-if="profilePopoverFacts(profile).length" class="mw-active-profile__facts mw-profile-popover__facts">
                  <span
                    v-for="item in profilePopoverFacts(profile)"
                    :key="`${profile.instance_key}-${item.key}`"
                    :class="['mw-active-profile__fact', item.key === 'rating' && 'mw-active-profile__fact--rating']"
                    :title="`${item.label} ${item.value}`"
                  >
                    <span class="mw-active-profile__fact-icon" aria-hidden="true" v-html="item.icon"></span>
                    <span>{{ item.value }}</span>
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
                    <span>{{ formatFilamentChipLabel(filament) }}</span>
                  </span>
                </div>

                <p class="mw-profile-popover__summary">
                  {{ profile.summary || "该打印配置可单独查看图集、分盘图片与 3MF 状态。" }}
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
        <div v-if="visibleDetailTags.length" class="mw-tag-wall">
          <span v-for="tag in visibleDetailTags" :key="tag" class="mw-chip mw-chip--tag">{{ tag }}</span>
        </div>
      </article>

      <article v-if="showDocsSection" id="detail-docs" class="mw-section-card mw-section-card--docs">
        <div class="mw-section-card__header">
          <div class="mw-section-card__heading">
            <h2>文档 ({{ detail.attachments?.length || 0 }})</h2>
            <p v-if="!isLocalModel" class="mw-section-card__hint">可以在这里补传组装图、说明书或其他附件。</p>
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
                  <a
                    :class="['button button-secondary button-small mw-doc-action', !attachmentDownloadUrl(attachment) && 'is-disabled']"
                    :href="attachmentDownloadUrl(attachment) || undefined"
                    :download="attachmentDownloadName(attachment)"
                    :rel="attachmentDownloadUrl(attachment) ? 'noreferrer' : undefined"
                  >
                    下载
                  </a>
                  <button
                    v-if="attachment.is_image && attachmentDownloadUrl(attachment)"
                    class="button button-secondary button-small mw-doc-action"
                    type="button"
                    @click="openLightbox(attachmentDownloadUrl(attachment), attachment.name || '附件预览')"
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
        <p v-else-if="!isLocalModel" class="empty-copy">当前没有同步到文档附件，你可以在下方上传组装图或说明文件。</p>

        <form v-if="!isLocalModel" class="mw-attachment-upload" @submit.prevent="submitAttachmentUpload">
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

      <article v-if="showCommentsSection" id="detail-comments" class="mw-section-card">
        <div class="mw-section-card__header mw-comment-section__header">
          <div>
            <h2>评论 &amp; 评分 ({{ commentsTotal }})</h2>
          </div>
        </div>

        <div v-if="commentArchiveMismatch" class="mw-comment-sync-notice">
          <div class="mw-comment-sync-notice__body">
            <strong>评论归档不完整</strong>
            <span>源端记录 {{ formatStat(commentsTotal) }} 条，本地已归档 {{ formatStat(commentsArchivedTotal) }} 条。</span>
          </div>
          <button
            class="button button-primary button-small"
            type="button"
            :disabled="sourceBackfillLoading"
            @click="submitSourceBackfill"
          >
            {{ sourceBackfillLoading ? "已提交" : "补全评论" }}
          </button>
        </div>
        <p v-if="sourceBackfillMessage" class="mw-source-backfill-status is-success">{{ sourceBackfillMessage }}</p>
        <p v-if="sourceBackfillError" class="mw-source-backfill-status is-error">{{ sourceBackfillError }}</p>

        <div class="mw-comment-toolbar">
          <div class="mw-comment-toolbar__sorts">
            <button
              v-for="sort in commentSortOptions"
              :key="sort.value"
              :class="['mw-comment-toolbar__sort', commentSortKey === sort.value && 'is-active']"
              type="button"
              @click="commentSortKey = sort.value"
            >
              {{ sort.label }}
            </button>
          </div>
        </div>

        <template v-if="commentsTotal > 0">
          <div v-if="commentsReady" class="mw-comment-list">
            <article
              v-for="(comment, index) in visibleComments"
              :key="comment.id || `${comment.author}-${comment.time}-${index}`"
              class="mw-comment-thread"
            >
              <div class="mw-comment-thread__avatar">
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
              <div class="mw-comment-thread__content">
                <div class="mw-comment-thread__top">
                  <div class="mw-comment-thread__identity">
                    <div class="mw-comment-thread__author-row">
                      <a
                        v-if="comment.author_url"
                        class="mw-comment-thread__author"
                        :href="comment.author_url"
                        target="_blank"
                        rel="noreferrer"
                      >
                        {{ comment.author }}
                      </a>
                      <span v-else class="mw-comment-thread__author">{{ comment.author }}</span>
                      <div v-if="comment.rating > 0" class="mw-comment-rating" :aria-label="`评分 ${comment.rating}`">
                        <span
                          v-for="starIndex in 5"
                          :key="starIndex"
                          :class="['mw-comment-rating__star', starIndex <= Math.round(comment.rating) && 'is-active']"
                        >★</span>
                        <span class="mw-comment-rating__value">{{ comment.rating_label }}</span>
                      </div>
                    </div>
                    <div v-if="comment.badges?.length" class="mw-comment-badges">
                      <span
                        v-for="badge in comment.badges"
                        :key="badge"
                        :class="['mw-comment-badge', commentBadgeClass(badge)]"
                      >
                        {{ badge }}
                      </span>
                    </div>
                  </div>
                </div>

                <div v-if="comment.reply_to" class="mw-comment-thread__reply-target">
                  回复 <span>@{{ comment.reply_to }}</span>：
                </div>
                <div v-if="comment.content" class="mw-comment-thread__body">{{ comment.content }}</div>

                <div
                  v-if="comment.images?.length"
                  :class="['comment-gallery', comment.gallery_class, 'mw-comment-gallery']"
                >
                  <button
                    v-for="(image, imageIndex) in comment.images"
                    :key="`${comment.id || comment.author}-${imageIndex}`"
                    class="comment-gallery__item"
                    type="button"
                    @click="openLightbox(image.full_url || image.thumb_url, `${comment.author} 评论图片 ${imageIndex + 1}`)"
                  >
                    <img
                      :src="image.thumb_url"
                      :alt="`${comment.author} 评论图片 ${imageIndex + 1}`"
                      loading="lazy"
                      @error="swapEventImage($event, image.fallback_url)"
                    >
                  </button>
                </div>

                <div class="mw-comment-thread__footer">
                  <div class="mw-comment-thread__meta">
                    <span>{{ comment.time_display }}</span>
                    <span v-if="comment.status_copy">{{ comment.status_copy }}</span>
                  </div>
                  <div class="mw-comment-thread__actions">
                    <span class="mw-comment-action">
                      <span class="mw-comment-action__icon" v-html="COMMENT_UI_ICONS.like"></span>
                      <span>{{ formatStat(comment.like_count) }}</span>
                    </span>
                    <span class="mw-comment-action">
                      <span class="mw-comment-action__icon" v-html="COMMENT_UI_ICONS.reply"></span>
                      <span>{{ comment.reply_count > 0 ? formatStat(comment.reply_count) : "回复" }}</span>
                    </span>
                  </div>
                </div>

                <div v-if="comment.replies?.length" class="mw-comment-replies">
                  <article
                    v-for="(reply, replyIndex) in visibleCommentReplies(comment)"
                    :key="reply.id || `${reply.author}-${reply.time}-${replyIndex}`"
                    class="mw-comment-reply"
                  >
                    <div class="mw-comment-reply__avatar">
                      <div class="model-author">
                        <img
                          v-if="reply.avatar_url"
                          :src="reply.avatar_url"
                          :alt="reply.author"
                          @error="swapEventImage($event, reply.avatar_remote_url)"
                        >
                        <span v-else class="avatar-placeholder">{{ reply.author?.slice(0, 1) || "?" }}</span>
                      </div>
                    </div>
                    <div class="mw-comment-reply__content">
                      <div class="mw-comment-reply__header">
                        <span class="mw-comment-reply__author">{{ reply.author }}</span>
                        <span class="mw-comment-reply__time">{{ reply.time_display }}</span>
                      </div>
                      <div v-if="reply.reply_to" class="mw-comment-thread__reply-target">
                        回复 <span>@{{ reply.reply_to }}</span>：
                      </div>
                      <div v-if="reply.content" class="mw-comment-reply__body">{{ reply.content }}</div>
                      <div
                        v-if="reply.images?.length"
                        :class="['comment-gallery', reply.gallery_class, 'mw-comment-gallery', 'is-reply']"
                      >
                        <button
                          v-for="(image, imageIndex) in reply.images"
                          :key="`${reply.id || reply.author}-${imageIndex}`"
                          class="comment-gallery__item"
                          type="button"
                          @click="openLightbox(image.full_url || image.thumb_url, `${reply.author} 评论图片 ${imageIndex + 1}`)"
                        >
                          <img
                            :src="image.thumb_url"
                            :alt="`${reply.author} 评论图片 ${imageIndex + 1}`"
                            loading="lazy"
                            @error="swapEventImage($event, image.fallback_url)"
                          >
                        </button>
                      </div>
                      <div class="mw-comment-reply__footer">
                        <span class="mw-comment-action">
                          <span class="mw-comment-action__icon" v-html="COMMENT_UI_ICONS.like"></span>
                          <span>{{ formatStat(reply.like_count) }}</span>
                        </span>
                        <span class="mw-comment-action">
                          <span class="mw-comment-action__icon" v-html="COMMENT_UI_ICONS.reply"></span>
                          <span>{{ reply.reply_count > 0 ? formatStat(reply.reply_count) : "回复" }}</span>
                        </span>
                      </div>
                    </div>
                  </article>
                </div>

                <button
                  v-if="commentHasExpandableReplies(comment)"
                  class="mw-comment-thread__expand"
                  type="button"
                  @click.stop="toggleCommentReplies(comment)"
                >
                  {{
                    commentRepliesExpanded(comment)
                      ? "收起回复"
                      : `查看更多 ${formatStat(hiddenCommentReplyCount(comment))} 条回复`
                  }}
                </button>

                <button
                  v-if="commentCanLoadMoreReplies(comment)"
                  class="mw-comment-thread__expand"
                  type="button"
                  :disabled="commentsLoadingMore"
                  @click.stop="loadMoreRepliesForComment(comment)"
                >
                  {{ commentsLoadingMore ? "加载中..." : `继续加载 ${formatStat(commentMissingReplyCount(comment))} 条回复` }}
                </button>

                <span
                  v-else-if="comment.reply_count > 0 && !comment.replies?.length"
                  class="mw-comment-thread__expand mw-comment-thread__expand--static"
                >
                  共 {{ formatStat(comment.reply_count) }} 回复
                </span>
              </div>
            </article>
          </div>
          <div v-else class="comment-list comment-list--pending">
            <p class="empty-copy">正在分批加载评论内容…</p>
          </div>
          <div
            v-if="hasMoreComments && commentsAutoLoadSupported"
            ref="commentsLoadMoreTrigger"
            class="mw-comments-autoload-trigger"
            aria-hidden="true"
          ></div>
          <div v-if="hasMoreComments || commentsLoadingMore" class="mw-comments-more">
            <button
              v-if="hasMoreComments && !commentsAutoLoadSupported"
              class="button button-secondary"
              type="button"
              @click="loadMoreComments"
              :disabled="commentsLoadingMore"
            >
              {{ commentsLoadingMore ? "加载中..." : "加载更多评论" }}
            </button>
            <span class="mw-comments-more__meta">
              {{
                commentsLoadingMore
                  ? "正在加载更多评论…"
                  : (hasMoreComments
                      ? (commentsAutoLoadSupported ? "继续下滑自动加载更多评论" : "点击加载更多评论")
                      : "评论已全部加载")
              }}
            </span>
            <span class="mw-comments-more__meta">
              已显示 {{ visibleComments.length }} / {{ commentsTotal }}
            </span>
          </div>
          <p v-if="commentsLoadError" class="mw-comments-more__error">{{ commentsLoadError }}</p>
        </template>
        <p v-else class="empty-copy">当前没有同步到评论内容。</p>
      </article>
    </section>

    <div
      v-if="lightbox.open && lightbox.src"
      class="lightbox"
      role="dialog"
      aria-modal="true"
      aria-label="图片预览"
      @click="closeLightbox"
    >
      <div class="lightbox__dialog" @click.stop>
        <img class="lightbox__image" :src="lightbox.src" :alt="lightbox.alt || '预览图片'">
      </div>
    </div>

    <div
      v-if="modelPreview.open"
      class="model-preview-dialog"
      role="dialog"
      aria-modal="true"
      aria-labelledby="model-preview-title"
      @click="closeModelPreview"
    >
      <div class="model-preview-dialog__panel" @click.stop>
        <header class="model-preview-dialog__header">
          <div class="model-preview-dialog__title">
            <span>3D 预览</span>
            <strong id="model-preview-title">{{ modelPreview.title || detail.title }}</strong>
          </div>
          <div class="model-preview-dialog__actions">
            <button
              class="model-preview-dialog__reset"
              type="button"
              :disabled="modelPreview.loading || Boolean(modelPreview.error)"
              title="重置视角"
              @click="resetModelPreviewCamera"
            >
              <span aria-hidden="true" v-html="MODEL_PREVIEW_ICONS.reset"></span>
              重置视角
            </button>
            <button
              class="model-preview-dialog__close"
              type="button"
              aria-label="关闭 3D 预览"
              title="关闭"
              @click="closeModelPreview"
            >
              <span aria-hidden="true" v-html="MODEL_PREVIEW_ICONS.close"></span>
            </button>
          </div>
        </header>
        <div ref="modelPreviewStageRef" class="model-preview-dialog__stage">
          <canvas ref="modelPreviewCanvasRef" class="model-preview-dialog__canvas"></canvas>
          <div v-if="modelPreview.loading" class="model-preview-dialog__state">
            <strong>正在加载 3D 模型</strong>
            <span>{{ modelPreview.detail || "请稍候。" }}</span>
          </div>
          <div v-else-if="modelPreview.error" class="model-preview-dialog__state model-preview-dialog__state--error">
            <strong>3D 预览加载失败</strong>
            <span>{{ modelPreview.error }}</span>
            <span v-if="modelPreview.detail">{{ modelPreview.detail }}</span>
          </div>
        </div>
      </div>
    </div>

    <div
      v-if="localEditDialog.open"
      class="submit-dialog mw-local-edit-dialog"
      role="dialog"
      aria-modal="true"
      aria-labelledby="local-edit-dialog-title"
      @click="closeLocalEditDialog"
    >
      <div class="submit-dialog__panel mw-local-edit-dialog__panel" @click.stop>
        <div class="mw-local-edit-dialog__header">
          <div>
            <h2 id="local-edit-dialog-title">编辑</h2>
            <p>维护标题、描述、图册和模型文件。</p>
          </div>
          <button class="button button-secondary button-small" type="button" @click="closeLocalEditDialog">关闭</button>
        </div>

        <form class="mw-local-edit-block" @submit.prevent="submitLocalMetadata">
          <label class="mw-local-edit-label" for="local-model-title">标题</label>
          <input
            id="local-model-title"
            v-model="localEditDialog.title"
            class="mw-local-edit-input"
            type="text"
            maxlength="180"
            autocomplete="off"
          >
          <label class="mw-local-edit-label" for="local-model-description">描述</label>
          <textarea
            id="local-model-description"
            v-model="localEditDialog.description"
            class="mw-local-edit-textarea"
            rows="7"
          ></textarea>
          <div class="mw-local-edit-actions">
            <button class="button button-primary button-small" type="submit" :disabled="localEditBusy">
              保存
            </button>
          </div>
        </form>

        <section class="mw-local-edit-block">
          <div class="mw-local-edit-block__head">
            <h3>模型文件</h3>
            <label class="button button-secondary button-small mw-local-edit-picker">
              <input type="file" multiple accept=".3mf,.stl,.step,.stp,.obj" @change="onLocalModelFilesChange">
              <span>添加文件</span>
            </label>
          </div>
          <div v-if="detail.instances?.length" class="mw-local-edit-list">
            <article v-for="profile in detail.instances" :key="profile.instance_key" class="mw-local-edit-row">
              <div>
                <strong>{{ profile.title || profile.file_name || "模型文件" }}</strong>
                <span>{{ profile.file_kind || "文件" }} · {{ profile.file_name || "未命名" }}</span>
              </div>
              <button
                class="button button-secondary button-small mw-doc-action--danger"
                type="button"
                :disabled="localEditBusy || detail.instances.length <= 1"
                @click="deleteLocalModelFile(profile)"
              >
                删除
              </button>
            </article>
          </div>
          <p v-else class="empty-copy">当前没有模型文件。</p>
        </section>

        <section class="mw-local-edit-block">
          <div class="mw-local-edit-block__head">
            <h3>图册图片</h3>
            <label class="button button-secondary button-small mw-local-edit-picker">
              <input type="file" multiple accept="image/*" @change="onLocalModelImagesChange">
              <span>添加图片</span>
            </label>
          </div>
          <div v-if="editableGallery.length" class="mw-local-edit-gallery">
            <article v-for="image in editableGallery" :key="image.rel_path || image.url" class="mw-local-edit-image">
              <img :src="image.url" :alt="detail.title || '图册图片'">
              <div class="mw-local-edit-image__actions">
                <span v-if="image.is_cover" class="mw-local-edit-cover-badge">封面</span>
                <button
                  v-else
                  class="button button-secondary button-small"
                  type="button"
                  :disabled="localEditBusy || !image.rel_path"
                  @click="setLocalModelCoverImage(image)"
                >
                  设为封面
                </button>
                <button
                  class="button button-secondary button-small mw-doc-action--danger"
                  type="button"
                  :disabled="localEditBusy || !image.rel_path"
                  @click="deleteLocalModelImage(image)"
                >
                  删除
                </button>
              </div>
            </article>
          </div>
          <p v-else class="empty-copy">当前没有图册图片。</p>
        </section>

        <p v-if="localEditDialog.message" class="mw-local-edit-status is-success">{{ localEditDialog.message }}</p>
        <p v-if="localEditDialog.error" class="mw-local-edit-status is-error">{{ localEditDialog.error }}</p>
      </div>
    </div>
  </div>

  <ShareDialog
    :visible="shareDialogVisible"
    :model-dirs="shareDialogModelDirs"
    :show-count="false"
    @close="closeShareDialog"
  />
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onErrorCaptured, onMounted, ref, shallowRef, watch } from "vue";
import { useRoute } from "vue-router";

import ShareDialog from "../components/ShareDialog.vue";
import { apiRequest } from "../lib/api";
import { formatProfileRating } from "../lib/helpers";
import { getPageCache, setPageCache } from "../lib/pageCache";
import {
  buildInteractivePreviewScene,
  disposeModelPreviewObject,
  formatModelPreviewSize,
  frameModelPreviewObject,
  guardModelPreviewFileSize,
  loadModelPreviewObject,
} from "../lib/threePreview";


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
const lightbox = ref({
  open: false,
  src: "",
  alt: "预览图片",
});
const modelPreview = ref({
  open: false,
  loading: false,
  error: "",
  fileUrl: "",
  title: "",
  detail: "",
});
const modelPreviewCanvasRef = ref(null);
const modelPreviewStageRef = ref(null);
const attachmentFileInput = ref(null);
const attachmentUploading = ref(false);
const attachmentUploadMessage = ref("");
const attachmentUploadError = ref("");
const deletingAttachmentId = ref("");
const hoverPopoverEnabled = ref(false);
const previewedInstanceKey = ref("");
const profileEntryRefs = new Map();
const popoverPlacementState = ref({});
const commentsReady = ref(false);
const comments = shallowRef([]);
const rawComments = shallowRef([]);
const commentsTotal = ref(0);
const commentsArchivedTotal = ref(0);
const commentsNextOffset = ref(null);
const commentsLoadingMore = ref(false);
const commentsLoadError = ref("");
const sourceBackfillLoading = ref(false);
const sourceBackfillMessage = ref("");
const sourceBackfillError = ref("");
const shareDialogVisible = ref(false);
const localEditBusy = ref(false);
const localEditDialog = ref({
  open: false,
  title: "",
  description: "",
  message: "",
  error: "",
});
const commentSortKey = ref("hot");
const commentsLoadMoreTrigger = ref(null);
const commentsAutoLoadSupported = ref(false);
const expandedCommentReplies = ref({});
const mainGalleryThumbsRef = ref(null);
const mainGalleryRail = ref(createThumbRailState());
const profileMediaStripRefs = new Map();
const profileMediaRails = ref({});

let hoverPopoverMediaQuery = null;
let hoverPopoverMediaListener = null;
let commentsRenderFrame = 0;
let commentsLoadMoreObserver = null;
let railResizeObserver = null;
let modelPreviewFrame = 0;
let modelPreviewRenderer = null;
let modelPreviewScene = null;
let modelPreviewCamera = null;
let modelPreviewControls = null;
let modelPreviewMesh = null;
let modelPreviewGrid = null;
let modelPreviewResizeObserver = null;
let modelPreviewHome = null;
let modelPreviewRequestId = 0;
const MODEL_PREVIEW_MAX_BYTES = 80 * 1024 * 1024;

const INITIAL_COMMENT_BATCH = 20;
const COMMENT_REPLY_PREVIEW_COUNT = 3;
const DETAIL_CACHE_PREFIX = "model-detail:";
const COMMENT_CHILD_KEYS = [
  "replies",
  "children",
  "subComments",
  "subCommentList",
  "subCommentVos",
  "subCommentVOList",
  "replyList",
  "replys",
  "replyVos",
  "replyVOList",
  "commentReplies",
  "commentReply",
  "commentReplyVos",
  "commentReplyList",
  "replyComments",
  "replyInfoList",
  "childComments",
];
const COMMENT_CHILD_CONTAINER_KEYS = ["items", "list", "rows", "records", "results", "nodes", "edges", "data"];
const COMMENT_CHILD_NODE_KEYS = ["node", "item", "record", "comment", "reply", "child"];
const STAT_FORMATTER = new Intl.NumberFormat("zh-CN");
const PROFILE_FACT_ICONS = {
  clock: '<svg width="16" height="17" viewBox="0 0 16 17" fill="none"><path d="M15 8.52342C15 4.64449 11.866 1.5 8 1.5V8.52342L12.9395 13.5C14.2123 12.2282 15 10.4681 15 8.52342Z" fill="var(--mui-palette-colorSystem-grey200)"></path><path fill-rule="evenodd" clip-rule="evenodd" d="M7.9999 2.10039C4.46528 2.10039 1.5999 4.96577 1.5999 8.50039C1.5999 12.035 4.46528 14.9004 7.9999 14.9004C11.5345 14.9004 14.3999 12.035 14.3999 8.50039C14.3999 4.96577 11.5345 2.10039 7.9999 2.10039ZM0.399902 8.50039C0.399902 4.30303 3.80254 0.900391 7.9999 0.900391C12.1973 0.900391 15.5999 4.30303 15.5999 8.50039C15.5999 12.6978 12.1973 16.1004 7.9999 16.1004C3.80254 16.1004 0.399902 12.6978 0.399902 8.50039Z" fill="var(--mui-palette-colorSystem-grey700)"></path><path fill-rule="evenodd" clip-rule="evenodd" d="M7.9999 5.90039C8.33127 5.90039 8.5999 6.16902 8.5999 6.50039V8.50039C8.5999 8.83176 8.33127 9.10039 7.9999 9.10039C7.66853 9.10039 7.3999 8.83176 7.3999 8.50039V6.50039C7.3999 6.16902 7.66853 5.90039 7.9999 5.90039Z" fill="var(--mui-palette-colorSystem-grey700)"></path><path fill-rule="evenodd" clip-rule="evenodd" d="M7.57564 8.07613C7.80995 7.84181 8.18985 7.84181 8.42417 8.07613L10.4242 10.0761C10.6585 10.3104 10.6585 10.6903 10.4242 10.9247C10.1899 11.159 9.80995 11.159 9.57564 10.9247L7.57564 8.92465C7.34132 8.69034 7.34132 8.31044 7.57564 8.07613Z" fill="var(--mui-palette-colorSystem-grey700)"></path><path fill-rule="evenodd" clip-rule="evenodd" d="M0.5 8.5C0.5 8.22386 0.723858 8 1 8H2.5C2.77614 8 3 8.22386 3 8.5C3 8.77614 2.77614 9 2.5 9H1C0.723858 9 0.5 8.77614 0.5 8.5Z" fill="var(--mui-palette-colorSystem-grey700)"></path><path fill-rule="evenodd" clip-rule="evenodd" d="M13 8.5C13 8.22386 13.2239 8 13.5 8H15C15.2761 8 15.5 8.22386 15.5 8.5C15.5 8.77614 15.2761 9 15 9H13.5C13.2239 9 13 8.77614 13 8.5Z" fill="var(--mui-palette-colorSystem-grey700)"></path><path fill-rule="evenodd" clip-rule="evenodd" d="M8 16C7.72386 16 7.5 15.7761 7.5 15.5L7.5 14C7.5 13.7239 7.72386 13.5 8 13.5C8.27614 13.5 8.5 13.7239 8.5 14L8.5 15.5C8.5 15.7761 8.27614 16 8 16Z" fill="var(--mui-palette-colorSystem-grey700)"></path><path fill-rule="evenodd" clip-rule="evenodd" d="M8 3.5C7.72386 3.5 7.5 3.27614 7.5 3L7.5 1.5C7.5 1.22386 7.72386 1 8 1C8.27614 1 8.5 1.22386 8.5 1.5L8.5 3C8.5 3.27614 8.27614 3.5 8 3.5Z" fill="var(--mui-palette-colorSystem-grey700)"></path></svg>',
  plates: '<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M1.5 2.5C1.5 1.94772 1.94772 1.5 2.5 1.5H7.66667V14.5H2.5C1.94771 14.5 1.5 14.0523 1.5 13.5V2.5Z" fill="var(--mui-palette-colorSystem-grey200)"></path><path fill-rule="evenodd" clip-rule="evenodd" d="M0.89978 2.50015C0.89978 1.61649 1.61612 0.900146 2.49978 0.900146H7.66645L7.68191 0.900342H13.4997C14.3834 0.900342 15.0997 1.61669 15.0997 2.50034V7.93624C15.1019 7.95711 15.103 7.9783 15.103 7.99976V13.4998C15.103 14.3834 14.3867 15.0998 13.503 15.0998H7.68831C7.68106 15.1 7.67377 15.1001 7.66645 15.1001H2.49978C1.61612 15.1001 0.89978 14.3838 0.89978 13.5001V2.50015ZM13.8997 7.39976H8.26645V2.10034H13.4997C13.7206 2.10034 13.8997 2.27943 13.8997 2.50034V7.39976ZM8.2697 8.60034V13.8998H13.503C13.7239 13.8998 13.903 13.7207 13.903 13.4998V8.60034H8.2697ZM2.49978 2.10015H7.06638V8.00034L7.06645 8.00929V13.9001H2.49978C2.27887 13.9001 2.09978 13.7211 2.09978 13.5001V2.50015C2.09978 2.27923 2.27887 2.10015 2.49978 2.10015Z" fill="var(--mui-palette-colorSystem-grey700)"></path></svg>',
  nozzle: '<svg width="1em" height="1em" viewBox="0 0 24 24" fill="none"><path d="M16.0005 14H8.00049H4.00049V18H20.0005V14H16.0005Z" fill="var(--mui-palette-colorSystem-grey300)"></path><path fill-rule="evenodd" clip-rule="evenodd" d="M7.99951 1.25C7.5853 1.25 7.24951 1.58579 7.24951 2V5.184L5.9016 5.40865C5.57474 5.46313 5.35393 5.77226 5.4084 6.09912C5.46288 6.42599 5.77202 6.6468 6.09888 6.59232L7.24951 6.40055V8.184L5.9016 8.40865C5.57474 8.46313 5.35393 8.77226 5.4084 9.09912C5.46288 9.42599 5.77202 9.6468 6.09888 9.59232L7.24951 9.40055V11.184L5.9016 11.4086C5.57474 11.4631 5.35393 11.7723 5.4084 12.0991C5.46288 12.426 5.77202 12.6468 6.09888 12.5923L7.24951 12.4005V13.25H4.00049C3.58627 13.25 3.25049 13.5858 3.25049 14V18C3.25049 18.4142 3.58627 18.75 4.00049 18.75H7.24951V19.5C7.24951 19.6989 7.32853 19.8897 7.46918 20.0303L9.96918 22.5303C10.1098 22.671 10.3006 22.75 10.4995 22.75H13.4995C13.6984 22.75 13.8892 22.671 14.0298 22.5303L16.5298 20.0303C16.6705 19.8897 16.7495 19.6989 16.7495 19.5V18.75H20.0005C20.4147 18.75 20.7505 18.4142 20.7505 18V14C20.7505 13.5858 20.4147 13.25 20.0005 13.25H16.7495V10.8172L18.0989 10.5923C18.4257 10.5378 18.6466 10.2287 18.5921 9.90184C18.5376 9.57498 18.2285 9.35417 17.9016 9.40865L16.7495 9.60066V7.81722L18.0989 7.59232C18.4257 7.53784 18.6466 7.22871 18.5921 6.90184C18.5376 6.57498 18.2285 6.35417 17.9016 6.40865L16.7495 6.60066V4.81722L18.0989 4.59232C18.4257 4.53784 18.6466 4.22871 18.5921 3.90184C18.5376 3.57498 18.2285 3.35417 17.9016 3.40865L16.7495 3.60066V2C16.7495 1.58579 16.4137 1.25 15.9995 1.25H7.99951ZM15.2495 13.25V11.0672L8.74951 12.1505V13.25H15.2495ZM15.2495 18.75H8.74951V19.1893L10.8102 21.25H13.1889L15.2495 19.1893V18.75ZM8.74951 7.934V6.15055L15.2495 5.06722V6.85066L8.74951 7.934ZM8.74951 4.934L15.2495 3.85066V2.75H8.74951V4.934ZM8.74951 9.15055L15.2495 8.06722V9.85066L8.74951 10.934V9.15055ZM4.75049 17.25V14.75H19.2505V17.25H4.75049Z" fill="var(--mui-palette-colorSystem-grey800)"></path></svg>',
  filament: '<svg width="1em" height="1em" viewBox="0 0 24 24" fill="none"><path fill-rule="evenodd" clip-rule="evenodd" d="M3.82997 5.53396C2.96342 7.14327 2.39985 9.4283 2.39985 12.0016C2.39985 14.5748 2.96342 16.8599 3.82997 18.4692C4.71626 20.1151 5.78984 20.8516 6.74985 20.8516C7.70987 20.8516 8.78345 20.1151 9.66974 18.4692C10.5363 16.8599 11.0999 14.5748 11.0999 12.0016C11.0999 9.4283 10.5363 7.14327 9.66974 5.53396C8.78345 3.88799 7.70987 3.15156 6.74985 3.15156C5.78984 3.15156 4.71626 3.88799 3.82997 5.53396ZM2.24512 4.68058C3.25896 2.79774 4.81037 1.35156 6.74985 1.35156C8.68934 1.35156 10.2408 2.79774 11.2546 4.68058C12.2882 6.60008 12.8999 9.19005 12.8999 12.0016C12.8999 14.8131 12.2882 17.403 11.2546 19.3225C10.2408 21.2054 8.68934 22.6516 6.74985 22.6516C4.81037 22.6516 3.25896 21.2054 2.24512 19.3225C1.21154 17.403 0.599854 14.8131 0.599854 12.0016C0.599854 9.19005 1.21154 6.60008 2.24512 4.68058Z" fill="var(--mui-palette-colorSystem-grey800)"></path><path fill-rule="evenodd" clip-rule="evenodd" d="M13.1115 6H7.5C6.25736 6 5.25 8.2129 5.25 12C5.25 15.7871 6.25736 18 7.5 18H13.1115C12.4151 16.3455 12 14.2628 12 12C12 9.73721 12.4151 7.65445 13.1115 6Z" fill="var(--mui-palette-colorSystem-grey300)"></path><path fill-rule="evenodd" clip-rule="evenodd" d="M13.4616 5.25H7.5C6.38005 5.25 5.63974 6.22446 5.21184 7.30333C4.75749 8.44891 4.5 10.052 4.5 12C4.5 13.948 4.75749 15.5511 5.21184 16.6967C5.63974 17.7755 6.38005 18.75 7.5 18.75H13.4616C13.2243 18.2907 13.0109 17.7884 12.8253 17.25H7.5C7.37731 17.25 6.99262 17.118 6.60618 16.1437C6.24619 15.236 6 13.8391 6 12C6 10.1609 6.24619 8.764 6.60618 7.85635C6.99262 6.88199 7.37731 6.75 7.5 6.75H12.8253C13.0109 6.21157 13.2243 5.70925 13.4616 5.25Z" fill="var(--mui-palette-colorSystem-grey800)"></path><path fill-rule="evenodd" clip-rule="evenodd" d="M14.33 5.53396C13.4634 7.14327 12.8999 9.4283 12.8999 12.0016C12.8999 14.5748 13.4634 16.8599 14.33 18.4692C15.2163 20.1151 16.2898 20.8516 17.2499 20.8516C18.2099 20.8516 19.2834 20.1151 20.1697 18.4692C21.0363 16.8599 21.5999 14.5748 21.5999 12.0016C21.5999 9.4283 21.0363 7.14327 20.1697 5.53396C19.2834 3.88799 18.2099 3.15156 17.2499 3.15156C16.2898 3.15156 15.2163 3.88799 14.33 5.53396ZM12.7451 4.68058C13.759 2.79774 15.3104 1.35156 17.2499 1.35156C19.1893 1.35156 20.7408 2.79774 21.7546 4.68058C22.7882 6.60008 23.3999 9.19005 23.3999 12.0016C23.3999 14.8131 22.7882 17.403 21.7546 19.3225C20.7408 21.2054 19.1893 22.6516 17.2499 22.6516C15.3104 22.6516 13.759 21.2054 12.7451 19.3225C11.7115 17.403 11.0999 14.8131 11.0999 12.0016C11.0999 9.19005 11.7115 6.60008 12.7451 4.68058Z" fill="var(--mui-palette-colorSystem-grey800)"></path><ellipse cx="17.25" cy="12" rx="1.5" ry="3" fill="var(--mui-palette-colorSystem-grey300)" stroke="var(--mui-palette-colorSystem-grey800)" stroke-width="0.6" stroke-linejoin="round"></ellipse></svg>',
  download: '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.85" stroke-linecap="round" stroke-linejoin="round"><path d="M10 3.6v8.1"/><path d="m6.9 8.8 3.1 3.2 3.1-3.2"/><path d="M4.2 15.4h11.6"/></svg>',
  prints: '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.85" stroke-linecap="round" stroke-linejoin="round"><path d="M6 7.2V3.8h8v3.4"/><path d="M5.1 15.8h9.8v-4.7H5.1v4.7Z"/><path d="M4.3 7.2h11.4A1.3 1.3 0 0 1 17 8.5v3.1h-2.1"/><path d="M3 11.6V8.5a1.3 1.3 0 0 1 1.3-1.3"/><circle cx="14.3" cy="9.2" r=".7" fill="currentColor" stroke="none"/></svg>',
  rating: '<svg viewBox="0 0 20 20" fill="currentColor"><path d="m10 2.5 2.3 4.65 5.14.75-3.72 3.62.88 5.12L10 14.19l-4.6 2.45.88-5.12L2.56 7.9l5.14-.75L10 2.5Z"/></svg>',
};
const MODEL_PREVIEW_ICONS = {
  reset: '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6.7 7.1H3.8V4.2"/><path d="M4.1 7a6.3 6.3 0 1 1 1 6.7"/><path d="m3.8 7.1 2-2"/></svg>',
  close: '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><path d="m5.5 5.5 9 9"/><path d="m14.5 5.5-9 9"/></svg>',
};
const AMS_ICON = '<svg width="2.3em" height="1.2em" viewBox="0 0 46 24" fill="none"><rect x="0.5" y="0.5" width="45" height="23" rx="1.5" fill="#212121" stroke="#000"></rect><path opacity="0.6" d="M1 2a1 1 0 011-1h10v22H2a1 1 0 01-1-1V2z" fill="#E14747"></path><path opacity="0.6" d="M12 1h11v22H12V1z" fill="#FEC90D"></path><path opacity="0.6" d="M23 1h11v22H23V1z" fill="var(--mui-palette-primary-main)"></path><path opacity="0.6" d="M34 1h10a1 1 0 011 1v20a1 1 0 01-1 1H34V1z" fill="#0E65E9"></path><path d="M15.822 17l-.62-2.04h-3.124L11.457 17H9.5l3.023-8.602h2.221L17.78 17h-1.957zm-1.054-3.563l-.622-1.992c-.039-.133-.091-.302-.158-.51a57.03 57.03 0 00-.193-.638 15.253 15.253 0 01-.152-.568 58.37 58.37 0 01-.492 1.717l-.616 1.991h2.233zM22.49 17l-2.062-6.72h-.053l.035.726c.02.32.037.662.053 1.025.015.364.023.692.023.985V17h-1.623V8.434h2.473l2.027 6.55h.035l2.15-6.55h2.474V17h-1.694v-4.055c0-.27.004-.58.012-.931.012-.352.025-.686.04-1.002.017-.32.028-.56.036-.721h-.053L24.154 17H22.49zm12.75-2.379c0 .508-.123.95-.369 1.324-.246.375-.605.664-1.078.867-.469.204-1.04.305-1.711.305a6.42 6.42 0 01-.873-.058 6.022 6.022 0 01-.814-.17 5.096 5.096 0 01-.739-.287v-1.688c.407.18.828.342 1.266.486a4.13 4.13 0 001.3.217c.297 0 .536-.039.715-.117a.817.817 0 00.399-.322.892.892 0 00.123-.469.772.772 0 00-.217-.55 2.265 2.265 0 00-.597-.429c-.25-.132-.534-.275-.85-.427a9.757 9.757 0 01-.65-.34 3.801 3.801 0 01-.668-.498 2.4 2.4 0 01-.522-.71c-.133-.28-.2-.616-.2-1.007 0-.512.118-.95.352-1.312.235-.364.569-.641 1.002-.832.438-.196.953-.293 1.547-.293.446 0 .87.052 1.272.158.406.101.83.25 1.271.445l-.586 1.412a9.706 9.706 0 00-1.06-.369 3.44 3.44 0 00-.955-.135c-.227 0-.42.037-.58.112a.781.781 0 00-.364.304.822.822 0 00-.123.452c0 .203.059.375.176.515.121.137.3.27.54.399.241.128.542.279.901.45.438.208.811.425 1.12.651.312.223.552.486.72.791.168.3.252.676.252 1.125z" fill="#fff"></path></svg>';
const COMMENT_UI_ICONS = {
  image: '<svg viewBox="0 0 18 16" fill="none"><rect x="1.5" y="1.5" width="15" height="13" rx="1.75" stroke="currentColor" stroke-width="1.2"></rect><circle cx="5.25" cy="5" r="1.25" fill="currentColor"></circle><path d="m3 12.5 3.25-3.25a1 1 0 0 1 1.45.05l1.52 1.75 2.64-3.35a1 1 0 0 1 1.53-.06l1.63 1.86" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"></path></svg>',
  send: '<svg viewBox="0 0 16 16" fill="none"><path fill-rule="evenodd" clip-rule="evenodd" d="M11.4001 8.33291C11.4001 8.00154 11.6687 7.73291 12.0001 7.73291H14.6667C14.9981 7.73291 15.2667 8.00154 15.2667 8.33291V13.6662C15.2667 13.9976 14.9981 14.2662 14.6667 14.2662H13.2486L12.4243 15.0905C12.19 15.3248 11.8101 15.3248 11.5758 15.0905L10.7515 14.2662H7.3334C7.00203 14.2662 6.7334 13.9976 6.7334 13.6662V10.9996C6.7334 10.6682 7.00203 10.3996 7.3334 10.3996H11.4001V8.33291ZM12.6001 8.93291V10.9996C12.6001 11.3309 12.3314 11.5996 12.0001 11.5996H7.9334V13.0662H11.0001C11.1592 13.0662 11.3118 13.1295 11.4243 13.242L12.0001 13.8177L12.5758 13.242C12.6883 13.1295 12.8409 13.0662 13.0001 13.0662H14.0667V8.93291H12.6001Z" fill="currentColor"></path><path fill-rule="evenodd" clip-rule="evenodd" d="M0.733398 2.9999C0.733398 2.66853 1.00203 2.3999 1.3334 2.3999H12.0001C12.3314 2.3999 12.6001 2.66853 12.6001 2.9999V10.9999C12.6001 11.3313 12.3314 11.5999 12.0001 11.5999H5.91526L4.75766 12.7575C4.52335 12.9918 4.14345 12.9918 3.90913 12.7575L2.75154 11.5999H1.3334C1.00203 11.5999 0.733398 11.3313 0.733398 10.9999V2.9999ZM1.9334 3.5999V10.3999H3.00007C3.1592 10.3999 3.31181 10.4631 3.42433 10.5756L4.3334 11.4847L5.24247 10.5756C5.35499 10.4631 5.5076 10.3999 5.66673 10.3999H11.4001V3.5999H1.9334Z" fill="currentColor"></path><path fill-rule="evenodd" clip-rule="evenodd" d="M5.7334 6.9999C5.7334 6.66853 6.00203 6.3999 6.3334 6.3999H6.66673C6.9981 6.3999 7.26673 6.66853 7.26673 6.9999C7.26673 7.33127 6.9981 7.5999 6.66673 7.5999H6.3334C6.00203 7.5999 5.7334 7.33127 5.7334 6.9999Z" fill="currentColor"></path><path fill-rule="evenodd" clip-rule="evenodd" d="M8.06665 6.9999C8.06665 6.66853 8.33528 6.3999 8.66665 6.3999H8.99998C9.33135 6.3999 9.59998 6.66853 9.59998 6.9999C9.59998 7.33127 9.33135 7.5999 8.99998 7.5999H8.66665C8.33528 7.5999 8.06665 7.33127 8.06665 6.9999Z" fill="currentColor"></path><path fill-rule="evenodd" clip-rule="evenodd" d="M3.40002 6.9999C3.40002 6.66853 3.66865 6.3999 4.00002 6.3999H4.33336C4.66473 6.3999 4.93336 6.66853 4.93336 6.9999C4.93336 7.33127 4.66473 7.5999 4.33336 7.5999H4.00002C3.66865 7.5999 3.40002 7.33127 3.40002 6.9999Z" fill="currentColor"></path></svg>',
  chevron: '<svg viewBox="0 0 20 20" fill="none"><path fill-rule="evenodd" clip-rule="evenodd" d="M15.0606 7.75174C15.3358 8.06132 15.3079 8.53538 14.9983 8.81056L10.4983 12.8106C10.2141 13.0632 9.7859 13.0632 9.50174 12.8106L5.00174 8.81056C4.69215 8.53538 4.66426 8.06132 4.93945 7.75174C5.21464 7.44215 5.68869 7.41426 5.99828 7.68945L10 11.2465L14.0017 7.68945C14.3113 7.41426 14.7854 7.44215 15.0606 7.75174Z" fill="currentColor"></path></svg>',
  more: '<svg viewBox="0 0 4 18" fill="none"><path fill-rule="evenodd" clip-rule="evenodd" d="M2 3.5a1.5 1.5 0 100-3 1.5 1.5 0 000 3zM3.5 9a1.5 1.5 0 11-3 0 1.5 1.5 0 013 0zm0 7a1.5 1.5 0 11-3 0 1.5 1.5 0 013 0z" fill="currentColor"></path></svg>',
  like: '<svg viewBox="0 0 16 16" fill="none"><path d="M6.42 6.833 8.24 3.816c.297-.491 1.06-.275 1.06.304v1.744h1.862c.805 0 1.392.77 1.177 1.54L11.317 11.04a1.2 1.2 0 0 1-1.156.87H6.42V6.833Z" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"></path><path d="M6.42 6.833H4.533c-.589 0-1.067.478-1.067 1.067v2.943c0 .589.478 1.067 1.067 1.067H6.42V6.833Z" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"></path></svg>',
  reply: '<svg viewBox="0 0 16 16" fill="none"><path d="M11 13.666H7.335V11h4.667V8.333h2.666v5.333h-1.666l-1 1-1-1z" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"></path><path d="M1.334 3h10.667v8H5.667l-1.333 1.333L3.001 11H1.334V3z" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"></path><path d="M6.334 7h.333M8.666 7h.333M4 7h.333" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"></path></svg>',
};
const THUMB_RAIL_ICONS = {
  prev: '<svg viewBox="0 0 20 20" fill="none"><path d="m12.5 4.75-5.25 5.25 5.25 5.25" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"></path></svg>',
  next: '<svg viewBox="0 0 20 20" fill="none"><path d="m7.5 4.75 5.25 5.25-5.25 5.25" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"></path></svg>',
};

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

const detailSections = computed(() => {
  const sections = [
    { id: "detail-description", label: "描述" },
  ];
  if (showDocsSection.value) {
    sections.push({ id: "detail-docs", label: "文档" });
  }
  if (showCommentsSection.value) {
    sections.push({ id: "detail-comments", label: "评论" });
  }
  return sections;
});
const commentSortOptions = [
  { value: "hot", label: "热门" },
  { value: "likes", label: "最多点赞" },
  { value: "latest", label: "最新" },
  { value: "replies", label: "最多回复" },
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

function decodeArchivePath(value) {
  const raw = String(value || "").trim();
  if (!raw.startsWith("/archive/")) {
    return "";
  }
  const withoutPrefix = raw.slice("/archive/".length).split("#", 1)[0].split("?", 1)[0];
  try {
    return decodeURIComponent(withoutPrefix);
  } catch {
    return withoutPrefix;
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
const shareDialogModelDirs = computed(() => (modelDir.value ? [modelDir.value] : []));

function normalizeInternalReturnPath(value) {
  const raw = String(value || "").trim();
  if (!raw || !raw.startsWith("/") || raw.startsWith("//")) {
    return "";
  }
  return raw;
}

function normalizeReturnContext(value) {
  const raw = Array.isArray(value) ? value[0] : value;
  const normalized = String(raw || "").trim();
  return ["subscriptions", "organizer"].includes(normalized) ? normalized : "";
}

function returnContextFromPath(value) {
  const raw = normalizeInternalReturnPath(value);
  if (!raw) {
    return "";
  }
  try {
    const url = new URL(raw, "http://makerhub.local");
    const explicit = normalizeReturnContext(url.searchParams.get("nav_context"));
    if (explicit) {
      return explicit;
    }
    if (url.pathname.startsWith("/models/state/")) {
      return "organizer";
    }
    if (url.pathname.startsWith("/models/source/local/")) {
      return "organizer";
    }
    if (url.pathname.startsWith("/models/source/")) {
      return "subscriptions";
    }
  } catch {
    return "";
  }
  return "";
}

function historyBackPath() {
  if (typeof window === "undefined") {
    return "";
  }
  return normalizeInternalReturnPath(window.history?.state?.back || "");
}

const detailBackContext = computed(() => {
  const returnTo = Array.isArray(route.query.return_to) ? route.query.return_to[0] : route.query.return_to;
  return normalizeReturnContext(route.query.return_context)
    || returnContextFromPath(returnTo)
    || returnContextFromPath(historyBackPath());
});
const detailBackTarget = computed(() => {
  const returnTo = Array.isArray(route.query.return_to) ? route.query.return_to[0] : route.query.return_to;
  const normalizedReturnTo = normalizeInternalReturnPath(returnTo);
  if (normalizedReturnTo) {
    return normalizedReturnTo;
  }
  const normalizedHistoryBack = historyBackPath();
  if (normalizedHistoryBack) {
    return normalizedHistoryBack;
  }
  if (detailBackContext.value === "subscriptions") {
    return "/subscriptions";
  }
  if (detailBackContext.value === "organizer") {
    return "/organizer";
  }
  return "/models";
});
const detailBackLabel = computed(() => {
  return "返回";
});

const activeInstance = computed(() => {
  return detail.value?.instances?.find((item) => item.instance_key === activeInstanceKey.value) || null;
});

const activeModelPreviewFile = computed(() => {
  const instance = activeInstance.value;
  if (!instance?.file_available || !instance.file_url) {
    return null;
  }
  const kind = String(instance.file_kind || "").trim().toUpperCase();
  const name = String(instance.file_name || "").trim().toLowerCase();
  if (kind === "STL" || kind === "OBJ" || kind === "3MF" || name.endsWith(".stl") || name.endsWith(".obj") || name.endsWith(".3mf")) {
    return instance;
  }
  return null;
});

const headCrumbs = computed(() => {
  const crumbs = [];
  for (const tag of visibleDetailTags.value) {
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

const visibleDetailTags = computed(() => {
  const tags = Array.isArray(detail.value?.tags) ? detail.value.tags : [];
  if (!isLocalModel.value) {
    return tags;
  }
  return tags.filter((tag) => String(tag || "").trim() !== "本地导入");
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

const showDocsSection = computed(() => {
  return !isLocalModel.value || attachmentGroups.value.length > 0;
});

const visibleComments = computed(() => {
  if (!commentsReady.value) {
    return [];
  }
  return [...comments.value].sort(compareComments);
});

const hasMoreComments = computed(() => {
  return commentsReady.value && commentsNextOffset.value !== null;
});

const isLocalModel = computed(() => String(detail.value?.source || "").toLowerCase() === "local");

const showCommentsSection = computed(() => !isLocalModel.value);

const sourceBackfillAvailable = computed(() => {
  return ["cn", "global"].includes(String(detail.value?.source || "").toLowerCase()) && Boolean(detail.value?.origin_url);
});

const commentArchiveMismatch = computed(() => {
  return sourceBackfillAvailable.value && commentsTotal.value > 0 && commentsTotal.value !== commentsArchivedTotal.value;
});

const editableGallery = computed(() => {
  if (!isLocalModel.value || !Array.isArray(detail.value?.gallery)) {
    return [];
  }
  const coverArchivePath = decodeArchivePath(detail.value?.cover_url || "");
  const currentModelDir = String(detail.value?.model_dir || modelDir.value || "").replace(/^\/+|\/+$/g, "");
  const coverRelPath = currentModelDir && coverArchivePath.startsWith(`${currentModelDir}/`)
    ? coverArchivePath.slice(currentModelDir.length + 1)
    : coverArchivePath;
  return detail.value.gallery.map((image) => {
    const url = String(image?.url || "");
    const archivePath = decodeArchivePath(url);
    const relPath = currentModelDir && archivePath.startsWith(`${currentModelDir}/`)
      ? archivePath.slice(currentModelDir.length + 1)
      : archivePath;
    return {
      ...image,
      rel_path: relPath,
      is_cover: Boolean(coverRelPath && relPath === coverRelPath),
    };
  }).filter((image) => image.rel_path);
});

const actionStats = computed(() => {
  const items = [
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
  ];
  return isLocalModel.value ? items.filter((item) => item.key !== "comments") : items;
});

const heroDownloadHref = computed(() => {
  if (activeInstance.value?.file_available && activeInstance.value?.file_url) {
    return activeInstance.value.file_url;
  }
  return "";
});

const heroDownloadLabel = computed(() => {
  if (!activeInstance.value) {
    return "选择打印配置";
  }
  if (activeInstance.value.file_available && activeInstance.value.file_url) {
    return activeInstance.value.download_label || `下载 ${activeInstance.value.file_kind || "文件"}`;
  }
  if (activeInstance.value.file_name) {
    return activeInstance.value.file_status_message || `${activeInstance.value.file_kind || "文件"} 还未获取到`;
  }
  return "当前没有模型文件";
});

function createThumbRailState() {
  return {
    overflow: false,
    canScrollPrev: false,
    canScrollNext: false,
  };
}

function sameThumbRailState(left, right) {
  return Boolean(
    left
    && right
    && left.overflow === right.overflow
    && left.canScrollPrev === right.canScrollPrev
    && left.canScrollNext === right.canScrollNext,
  );
}

function measureThumbRail(element) {
  if (!(element instanceof HTMLElement)) {
    return createThumbRailState();
  }
  const maxScrollLeft = Math.max(element.scrollWidth - element.clientWidth, 0);
  const tolerance = 2;
  return {
    overflow: maxScrollLeft > tolerance,
    canScrollPrev: element.scrollLeft > tolerance,
    canScrollNext: element.scrollLeft < maxScrollLeft - tolerance,
  };
}

function observeThumbRail(element) {
  if (railResizeObserver && element instanceof HTMLElement) {
    railResizeObserver.observe(element);
  }
}

function unobserveThumbRail(element) {
  if (railResizeObserver && element instanceof HTMLElement) {
    railResizeObserver.unobserve(element);
  }
}

function setMainGalleryThumbsRef(element) {
  const nextElement = element instanceof HTMLElement ? element : null;
  if (mainGalleryThumbsRef.value && mainGalleryThumbsRef.value !== nextElement) {
    unobserveThumbRail(mainGalleryThumbsRef.value);
  }
  mainGalleryThumbsRef.value = nextElement;
  if (nextElement) {
    observeThumbRail(nextElement);
    syncMainGalleryRail();
    return;
  }
  mainGalleryRail.value = createThumbRailState();
}

function syncMainGalleryRail() {
  mainGalleryRail.value = measureThumbRail(mainGalleryThumbsRef.value);
}

function profileMediaRailState(instanceKey) {
  return profileMediaRails.value[instanceKey] || createThumbRailState();
}

function setProfileMediaStripRef(instanceKey, element) {
  if (!instanceKey) {
    return;
  }
  const nextElement = element instanceof HTMLElement ? element : null;
  const previousElement = profileMediaStripRefs.get(instanceKey);
  if (previousElement && previousElement !== nextElement) {
    unobserveThumbRail(previousElement);
  }
  if (nextElement) {
    profileMediaStripRefs.set(instanceKey, nextElement);
    observeThumbRail(nextElement);
    syncProfileMediaRail(instanceKey);
    return;
  }
  profileMediaStripRefs.delete(instanceKey);
  if (!(instanceKey in profileMediaRails.value)) {
    return;
  }
  const nextStates = { ...profileMediaRails.value };
  delete nextStates[instanceKey];
  profileMediaRails.value = nextStates;
}

function syncProfileMediaRail(instanceKey) {
  if (!instanceKey) {
    return;
  }
  const nextState = measureThumbRail(profileMediaStripRefs.get(instanceKey));
  const currentState = profileMediaRails.value[instanceKey];
  if (sameThumbRailState(currentState, nextState)) {
    return;
  }
  profileMediaRails.value = {
    ...profileMediaRails.value,
    [instanceKey]: nextState,
  };
}

function syncThumbRailByElement(element) {
  if (!(element instanceof HTMLElement)) {
    return;
  }
  if (element === mainGalleryThumbsRef.value) {
    syncMainGalleryRail();
    return;
  }
  for (const [instanceKey, railElement] of profileMediaStripRefs.entries()) {
    if (railElement === element) {
      syncProfileMediaRail(instanceKey);
      return;
    }
  }
}

function syncAllThumbRails() {
  syncMainGalleryRail();
  for (const instanceKey of profileMediaStripRefs.keys()) {
    syncProfileMediaRail(instanceKey);
  }
}

function clearProfileMediaStripRefs() {
  for (const element of profileMediaStripRefs.values()) {
    unobserveThumbRail(element);
  }
  profileMediaStripRefs.clear();
  profileMediaRails.value = {};
}

function scrollThumbRail(element, direction) {
  if (!(element instanceof HTMLElement)) {
    return;
  }
  const viewport = Math.max(element.clientWidth - 72, 140);
  const offset = direction === "prev" ? -viewport : viewport;
  element.scrollBy({
    left: offset,
    behavior: "smooth",
  });
}

function scrollMainGalleryThumbs(direction) {
  scrollThumbRail(mainGalleryThumbsRef.value, direction);
}

function scrollProfileMediaStrip(instanceKey, direction) {
  scrollThumbRail(profileMediaStripRefs.get(instanceKey), direction);
}

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
  nextTick(() => syncProfileMediaRail(profile.instance_key));
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
  const nextPlacement = canOpenLeft ? "left" : "below";
  const popoverGap = nextPlacement === "left" ? horizontalGap : 10;
  const left = nextPlacement === "left" ? rect.left - popoverGap - estimatedPopoverWidth : rect.left + 6;
  const right = nextPlacement === "left" ? "auto" : Math.max(window.innerWidth - rect.right + 6, viewportPadding);
  popoverPlacementState.value = {
    ...popoverPlacementState.value,
    [profile.instance_key]: {
      placement: nextPlacement,
      top: rect.top,
      belowTop: rect.bottom + popoverGap,
      center: rect.top + (rect.height / 2),
      bottom: rect.bottom,
      left,
      right,
    },
  };
}

function openProfilePopover(profile, entryElement = null) {
  if (!hoverPopoverEnabled.value || !profile) {
    return;
  }
  updateProfilePopoverPlacement(profile, entryElement);
  previewedInstanceKey.value = profile.instance_key;
  nextTick(() => syncProfileMediaRail(profile.instance_key));
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
  const state = popoverPlacementState.value[profile?.instance_key];
  if (state?.placement === "below") {
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

function profilePopoverStyle(profile, index = 0, total = 0) {
  const state = popoverPlacementState.value[profile?.instance_key];
  if (!state) {
    return {};
  }
  if (state.placement === "below") {
    return {
      top: `${Math.round(state.belowTop)}px`,
      left: `${Math.round(state.left)}px`,
      right: `${Math.round(state.right)}px`,
    };
  }
  let top = state.center;
  if (index === 0) {
    top = state.top;
  } else if (index >= total - 1) {
    top = state.bottom;
  }
  return {
    top: `${Math.round(top)}px`,
    left: `${Math.round(state.left)}px`,
  };
}

function handleProfileListScroll() {
  if (previewedInstanceKey.value) {
    closeProfilePopover(previewedInstanceKey.value, { force: true });
  }
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
  syncAllThumbRails();
  resizeModelPreviewRenderer();
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

function openLightbox(src, alt = "预览图片") {
  const resolvedSrc = String(src || "").trim();
  if (!resolvedSrc) {
    return;
  }
  lightbox.value = {
    open: true,
    src: resolvedSrc,
    alt: String(alt || "预览图片").trim() || "预览图片",
  };
  if (typeof document !== "undefined") {
    document.body.classList.add("is-lightbox-open");
  }
}

function closeLightbox() {
  lightbox.value = {
    open: false,
    src: "",
    alt: "预览图片",
  };
  if (typeof document !== "undefined" && !modelPreview.value.open) {
    document.body.classList.remove("is-lightbox-open");
  }
}

function disposeModelPreviewScene() {
  if (modelPreviewFrame && typeof window !== "undefined") {
    window.cancelAnimationFrame(modelPreviewFrame);
  }
  modelPreviewFrame = 0;
  if (modelPreviewResizeObserver) {
    modelPreviewResizeObserver.disconnect();
    modelPreviewResizeObserver = null;
  }
  if (modelPreviewControls) {
    modelPreviewControls.dispose();
    modelPreviewControls = null;
  }
  if (modelPreviewMesh) {
    disposeModelPreviewObject(modelPreviewMesh);
    modelPreviewMesh = null;
  }
  if (modelPreviewGrid) {
    if (modelPreviewGrid.geometry) {
      modelPreviewGrid.geometry.dispose();
    }
    if (modelPreviewGrid.material) {
      modelPreviewGrid.material.dispose();
    }
    modelPreviewGrid = null;
  }
  if (modelPreviewRenderer) {
    modelPreviewRenderer.dispose();
    modelPreviewRenderer = null;
  }
  modelPreviewScene = null;
  modelPreviewCamera = null;
  modelPreviewHome = null;
}

function resizeModelPreviewRenderer() {
  const stage = modelPreviewStageRef.value;
  if (!stage || !modelPreviewRenderer || !modelPreviewCamera) {
    return;
  }
  const width = Math.max(1, stage.clientWidth || 1);
  const height = Math.max(1, stage.clientHeight || 1);
  modelPreviewRenderer.setSize(width, height, false);
  modelPreviewCamera.aspect = width / height;
  modelPreviewCamera.updateProjectionMatrix();
  modelPreviewRenderer.render(modelPreviewScene, modelPreviewCamera);
}

function resetModelPreviewCamera() {
  if (!modelPreviewHome || !modelPreviewCamera || !modelPreviewControls) {
    return;
  }
  modelPreviewCamera.position.copy(modelPreviewHome.position);
  modelPreviewControls.target.copy(modelPreviewHome.target);
  modelPreviewControls.update();
}

function startModelPreviewLoop() {
  if (typeof window === "undefined") {
    return;
  }
  const render = () => {
    if (!modelPreviewRenderer || !modelPreviewScene || !modelPreviewCamera) {
      modelPreviewFrame = 0;
      return;
    }
    modelPreviewControls?.update();
    modelPreviewRenderer.render(modelPreviewScene, modelPreviewCamera);
    modelPreviewFrame = window.requestAnimationFrame(render);
  };
  if (!modelPreviewFrame) {
    modelPreviewFrame = window.requestAnimationFrame(render);
  }
}

async function mountModelPreviewScene(fileUrl, requestId) {
  const stage = modelPreviewStageRef.value;
  const canvas = modelPreviewCanvasRef.value;
  if (!stage || !canvas) {
    throw new Error("预览窗口还未准备好。");
  }
  disposeModelPreviewScene();

  const knownSize = Number(activeModelPreviewFile.value?.file_size || 0);
  if (knownSize > MODEL_PREVIEW_MAX_BYTES) {
    const label = formatModelPreviewSize(knownSize);
    const limitLabel = formatModelPreviewSize(MODEL_PREVIEW_MAX_BYTES);
    throw new Error(`模型文件 ${label || "过大"}，超过网页预览上限 ${limitLabel}。`);
  }
  const fileSize = knownSize || await guardModelPreviewFileSize(fileUrl, MODEL_PREVIEW_MAX_BYTES);
  if (requestId !== modelPreviewRequestId || !modelPreview.value.open) {
    return false;
  }
  if (fileSize) {
    modelPreview.value = {
      ...modelPreview.value,
      detail: `模型大小 ${formatModelPreviewSize(fileSize)}，正在初始化预览。`,
    };
  }

  const THREE = await import("three");
  const { OrbitControls } = await import("three/examples/jsm/controls/OrbitControls.js");
  if (requestId !== modelPreviewRequestId || !modelPreview.value.open) {
    return false;
  }

  const sceneBundle = buildInteractivePreviewScene(THREE, canvas);
  modelPreviewRenderer = sceneBundle.renderer;
  modelPreviewScene = sceneBundle.scene;
  modelPreviewCamera = sceneBundle.camera;
  modelPreviewGrid = sceneBundle.grid;

  modelPreviewControls = new OrbitControls(modelPreviewCamera, canvas);
  modelPreviewControls.enableDamping = true;
  modelPreviewControls.dampingFactor = 0.08;
  modelPreviewControls.screenSpacePanning = false;

  resizeModelPreviewRenderer();
  const object = await loadModelPreviewObject(THREE, fileUrl, modelPreview.value.title);
  if (requestId !== modelPreviewRequestId || !modelPreview.value.open) {
    disposeModelPreviewObject(object);
    disposeModelPreviewScene();
    return false;
  }
  modelPreviewMesh = object;
  modelPreviewScene.add(object);
  modelPreviewHome = frameModelPreviewObject(THREE, object, modelPreviewCamera, modelPreviewControls, modelPreviewGrid);
  resizeModelPreviewRenderer();

  if (typeof ResizeObserver !== "undefined") {
    modelPreviewResizeObserver = new ResizeObserver(() => resizeModelPreviewRenderer());
    modelPreviewResizeObserver.observe(stage);
  }
  startModelPreviewLoop();
  return true;
}

async function openModelPreview() {
  const previewFile = activeModelPreviewFile.value;
  if (!previewFile?.file_url) {
    return;
  }
  modelPreview.value = {
    open: true,
    loading: true,
    error: "",
    fileUrl: previewFile.file_url,
    title: previewFile.title || previewFile.file_name || detail.value?.title || "3D 模型",
    detail: "正在检查模型大小。",
  };
  if (typeof document !== "undefined") {
    document.body.classList.add("is-lightbox-open");
  }
  const requestId = ++modelPreviewRequestId;
  await nextTick();
  try {
    const mounted = await mountModelPreviewScene(previewFile.file_url, requestId);
    if (!mounted || requestId !== modelPreviewRequestId) {
      return;
    }
    modelPreview.value = {
      ...modelPreview.value,
      loading: false,
      error: "",
      detail: "",
    };
  } catch (error) {
    if (requestId !== modelPreviewRequestId) {
      return;
    }
    disposeModelPreviewScene();
    modelPreview.value = {
      ...modelPreview.value,
      loading: false,
      error: error instanceof Error ? error.message : "无法读取该 STL 文件。",
      detail: "大文件请用下载按钮保存后在本地切片软件或建模软件中查看。",
    };
  }
}

function closeModelPreview() {
  modelPreviewRequestId += 1;
  disposeModelPreviewScene();
  modelPreview.value = {
    open: false,
    loading: false,
    error: "",
    fileUrl: "",
    title: "",
    detail: "",
  };
  if (typeof document !== "undefined" && !lightbox.value.open) {
    document.body.classList.remove("is-lightbox-open");
  }
}

function handleWindowKeydown(event) {
  if (event.key === "Escape" && lightbox.value.open) {
    closeLightbox();
  } else if (event.key === "Escape" && modelPreview.value.open) {
    closeModelPreview();
  }
}

function formatStat(value) {
  return STAT_FORMATTER.format(Number(value || 0));
}

function profilePopoverFacts(profile) {
  if (!profile) {
    return [];
  }
  const items = [];
  if (Number(profile.plates || 0) > 0) {
    items.push({ key: "plates", label: "盘数", icon: PROFILE_FACT_ICONS.plates, value: `${profile.plates} 盘` });
  }
  const timeLabel = profileTimeLabel(profile);
  if (timeLabel) {
    items.push({ key: "time", label: "时长", icon: PROFILE_FACT_ICONS.clock, value: timeLabel });
  }
  const nozzleLabel = profileNozzleLabel(profile);
  if (nozzleLabel) {
    items.push({ key: "nozzle", label: "喷嘴直径", icon: PROFILE_FACT_ICONS.nozzle, value: nozzleLabel });
  }
  const filamentWeightLabel = profileFilamentWeightLabel(profile);
  if (filamentWeightLabel) {
    items.push({ key: "filament", label: "消耗耗材", icon: PROFILE_FACT_ICONS.filament, value: filamentWeightLabel });
  }
  return items;
}

function extractPositiveNumber(value) {
  if (value === "" || value === null || value === undefined) {
    return 0;
  }
  if (typeof value === "number") {
    return Number.isFinite(value) && value > 0 ? value : 0;
  }
  const match = String(value).replace(/,/g, "").match(/-?\d+(?:\.\d+)?/);
  if (!match) {
    return 0;
  }
  const parsed = Number(match[0]);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
}

function formatFilamentWeight(value) {
  const numeric = extractPositiveNumber(value);
  if (!numeric) {
    return "";
  }
  return `${numeric % 1 === 0 ? numeric.toFixed(0) : numeric.toFixed(1)} g`;
}

function formatProfileDuration(value) {
  const seconds = extractPositiveNumber(value);
  if (!seconds) {
    return "";
  }
  if (seconds >= 3600) {
    const hours = seconds / 3600;
    return `${hours % 1 === 0 ? hours.toFixed(0) : hours.toFixed(1)} h`;
  }
  const minutes = seconds / 60;
  return `${minutes % 1 === 0 ? minutes.toFixed(0) : minutes.toFixed(1)} min`;
}

function profileTimeLabel(profile) {
  if (!profile) {
    return "";
  }
  const explicit = String(profile.time || profile.time_label || profile.duration_label || "").trim();
  if (explicit) {
    return explicit;
  }
  return formatProfileDuration(
    profile.print_time_seconds
      || profile.duration
      || profile.profile_details?.print_time_seconds
      || profile.profile_details?.printTimeSeconds,
  );
}

function filamentWeightValue(filament) {
  if (!filament) {
    return 0;
  }
  const candidates = [
    filament.weight_label,
    filament.weightLabel,
    filament.weight,
    filament.weight_g,
    filament.weightG,
    filament.usedWeight,
    filament.used_weight,
    filament.usedWeightG,
    filament.used_weight_g,
    filament.usedG,
    filament.used_g,
    filament.filamentWeight,
    filament.filamentWeightG,
    filament.filament_weight,
    filament.filament_weight_g,
    filament.materialWeight,
    filament.materialWeightG,
    filament.material_weight,
    filament.material_weight_g,
    filament.grams,
    filament.gram,
    filament.usedGrams,
    filament.usage,
    filament.usageG,
    filament.used,
    filament.consume,
    filament.consumeG,
    filament.consumption,
    filament.consumptionG,
  ];
  for (const candidate of candidates) {
    const numeric = extractPositiveNumber(candidate);
    if (numeric) {
      return numeric;
    }
  }
  return 0;
}

function profileTotalFilamentWeight(profile) {
  if (!profile) {
    return 0;
  }
  const candidates = [
    profile.filament_weight,
    profile.filament_weight_label,
    profile.profile_details?.filament_weight,
    profile.profile_details?.filament_weight_label,
  ];
  for (const candidate of candidates) {
    const numeric = extractPositiveNumber(candidate);
    if (numeric) {
      return numeric;
    }
  }
  return 0;
}

function formatFilamentChipWeight(filament) {
  return formatFilamentWeight(filamentWeightValue(filament));
}

function formatFilamentChipLabel(filament) {
  const material = String(filament?.material || "耗材").trim() || "耗材";
  const weightLabel = formatFilamentChipWeight(filament);
  return weightLabel ? `${material}｜${weightLabel}` : material;
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

function profileNozzleLabel(profile) {
  if (!profile) {
    return "";
  }
  const explicit = String(profile.nozzle_diameter_label || profile.profile_details?.nozzle_diameter_label || "").trim();
  if (explicit) {
    return explicit;
  }
  const numeric = extractPositiveNumber(profile.nozzle_diameter || profile.profile_details?.nozzle_diameter);
  if (!numeric) {
    return "";
  }
  return `${numeric % 1 === 0 ? numeric.toFixed(0) : numeric.toFixed(2).replace(/\.?0+$/, "")} mm`;
}

function profileFilamentWeightLabel(profile) {
  if (!profile) {
    return "";
  }
  const explicit = String(profile.filament_weight_label || profile.profile_details?.filament_weight_label || "").trim();
  if (explicit) {
    return explicit;
  }
  const direct = profileTotalFilamentWeight(profile);
  if (direct) {
    return formatFilamentWeight(direct);
  }
  const total = profileFilaments(profile).reduce((sum, filament) => sum + filamentWeightValue(filament), 0);
  return formatFilamentWeight(total);
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

function attachmentDownloadName(attachment) {
  return String(attachment?.name || attachment?.id || "attachment").trim() || "attachment";
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

function normalizeCommentNumber(value) {
  const numeric = Number(value ?? 0);
  return Number.isFinite(numeric) && numeric > 0 ? numeric : 0;
}

function commentTimestamp(value) {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value > 1e12 ? value : value * 1000;
  }
  const raw = String(value ?? "").trim();
  if (!raw) {
    return 0;
  }
  if (/^\d{10,13}$/.test(raw)) {
    const numeric = Number(raw);
    return raw.length === 13 ? numeric : numeric * 1000;
  }
  const direct = Date.parse(raw);
  if (Number.isFinite(direct)) {
    return direct;
  }
  const normalized = raw.includes("T") ? raw : raw.replace(" ", "T");
  const parsed = Date.parse(normalized);
  return Number.isFinite(parsed) ? parsed : 0;
}

function padDatePart(value) {
  return String(value).padStart(2, "0");
}

function formatCommentTime(value, fallback = "") {
  const timestamp = commentTimestamp(value);
  if (!timestamp) {
    return String(fallback || value || "").trim();
  }
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) {
    return String(fallback || value || "").trim();
  }
  return [
    date.getFullYear(),
    padDatePart(date.getMonth() + 1),
    padDatePart(date.getDate()),
  ].join("-") + ` ${padDatePart(date.getHours())}:${padDatePart(date.getMinutes())}`;
}

function extractCommentBadges(comment) {
  const source = Array.isArray(comment?.badges) ? comment.badges : [];
  const badges = [];
  for (const badge of source) {
    const label = typeof badge === "string"
      ? badge.trim()
      : String(badge?.label || badge?.name || badge?.title || "").trim();
    if (label && !badges.includes(label)) {
      badges.push(label);
    }
  }
  if ((comment?.is_pinned || comment?.isPinned || comment?.isTop) && !badges.includes("置顶")) {
    badges.push("置顶");
  }
  if ((comment?.is_boosted || comment?.isBoost || comment?.isBoosted) && !badges.includes("已助力")) {
    badges.push("已助力");
  }
  if (
    (comment?.has_designer_reply || comment?.designerReplied || comment?.hasDesignerReply || comment?.isOfficialReply)
    && !badges.includes("设计师已回复")
  ) {
    badges.push("设计师已回复");
  }
  const profileName = String(comment?.profile_name || comment?.profileName || comment?.profileTitle || "").trim();
  if (profileName && !badges.includes(profileName)) {
    badges.push(profileName);
  }
  return badges;
}

function extractReplyTarget(comment) {
  const directCandidates = [
    comment?.reply_to,
    comment?.replyToName,
    comment?.replyUserName,
    comment?.replyNickName,
    comment?.targetUserName,
    comment?.parentAuthor,
    comment?.parentUserName,
    comment?.toUserName,
    comment?.beRepliedUserName,
  ];
  for (const candidate of directCandidates) {
    const label = String(candidate || "").trim();
    if (label) {
      return label;
    }
  }
  const nestedCandidates = [
    comment?.replyToUser,
    comment?.replyUser,
    comment?.targetUser,
    comment?.beRepliedUser,
    comment?.parentUser,
  ];
  for (const candidate of nestedCandidates) {
    const label = String(
      candidate?.nickname
      || candidate?.nickName
      || candidate?.name
      || candidate?.userName
      || candidate?.username
      || "",
    ).trim();
    if (label) {
      return label;
    }
  }
  return "";
}

function commentStatusCopy(comment) {
  if (comment.badges.includes("设计师已回复")) {
    return "设计师已回复";
  }
  if (comment.badges.includes("置顶")) {
    return "已置顶";
  }
  if (comment.badges.includes("已助力")) {
    return "已助力";
  }
  return "";
}

function computeCommentHotScore(comment) {
  const ageHours = comment.timestamp > 0 ? Math.max((Date.now() - comment.timestamp) / 36e5, 0) : 0;
  const freshness = comment.timestamp > 0 ? Math.max(0, 96 - ageHours) : 0;
  return (comment.like_count * 4) + (comment.reply_count * 7) + (comment.rating * 18) + freshness;
}

function isCommentLikeItem(item) {
  return Boolean(item && typeof item === "object" && (
    "id" in item
    || "commentId" in item
    || "rootCommentId" in item
    || "content" in item
    || "commentContent" in item
    || "comment" in item
    || "message" in item
    || "text" in item
    || "replyCount" in item
    || "subCommentCount" in item
    || "childrenCount" in item
    || "commentTime" in item
    || "createTime" in item
    || "createdAt" in item
  ));
}

function extractCommentChildren(value, depth = 0) {
  if (depth > 4 || value == null) {
    return [];
  }
  if (Array.isArray(value)) {
    const children = [];
    for (const item of value) {
      if (isCommentLikeItem(item)) {
        children.push(item);
        continue;
      }
      if (item && typeof item === "object") {
        children.push(...extractCommentChildren(item, depth + 1));
      }
    }
    return children;
  }
  if (value && typeof value === "object") {
    if (isCommentLikeItem(value)) {
      return [value];
    }
    for (const key of [...COMMENT_CHILD_CONTAINER_KEYS, ...COMMENT_CHILD_NODE_KEYS]) {
      const children = extractCommentChildren(value[key], depth + 1);
      if (children.length) {
        return children;
      }
    }
  }
  return [];
}

function commentReplyItems(comment) {
  if (!comment || typeof comment !== "object") {
    return [];
  }
  const replies = [];
  const seen = new Set();
  for (const key of COMMENT_CHILD_KEYS) {
    for (const item of extractCommentChildren(comment[key])) {
      if (seen.has(item)) {
        continue;
      }
      seen.add(item);
      replies.push(item);
    }
  }
  return replies;
}

function commentIdentityKey(comment) {
  const explicit = String(comment?.id || comment?.commentId || "").trim();
  if (explicit) {
    return explicit;
  }
  const authorSource = comment?.author;
  const author = String(
    (authorSource && typeof authorSource === "object"
      ? (authorSource.name || authorSource.nickname || authorSource.username || authorSource.userName)
      : authorSource)
    || comment?.userName
    || comment?.nickname
    || comment?.authorName
    || comment?.creatorName
    || "",
  ).trim();
  const content = String(
    comment?.content
    || comment?.commentContent
    || comment?.comment
    || comment?.message
    || comment?.text
    || "",
  ).trim();
  const timeValue = String(
    comment?.time
    || comment?.createdAt
    || comment?.createTime
    || comment?.commentTime
    || comment?.updatedAt
    || "",
  ).trim();
  const rootCommentId = String(comment?.rootCommentId || comment?.root_comment_id || "").trim();
  return [rootCommentId, author, timeValue, content].join("|");
}

function commentReplyCountValue(comment) {
  return normalizeCommentNumber(comment?.reply_count ?? comment?.replyCount ?? comment?.subCommentCount ?? comment?.childrenCount);
}

function normalizeThreadedCommentNode(comment) {
  if (!comment || typeof comment !== "object") {
    return null;
  }
  const normalized = { ...comment };
  const replies = commentReplyItems(comment)
    .map((item) => normalizeThreadedCommentNode(item))
    .filter(Boolean);
  normalized.replies = mergeThreadedCommentList([], replies);
  normalized.replyCount = Math.max(normalized.replies.length, commentReplyCountValue(normalized));
  return normalized;
}

function mergeThreadedCommentItem(existing, fresh) {
  const merged = { ...existing };
  for (const [key, value] of Object.entries(fresh || {})) {
    if (key === "replies") {
      continue;
    }
    if (value == null || value === "" || (Array.isArray(value) && value.length === 0)) {
      continue;
    }
    if (typeof value === "object" && !Array.isArray(value) && Object.keys(value).length === 0) {
      continue;
    }
    merged[key] = value;
  }

  const mergedReplies = mergeThreadedCommentList(
    Array.isArray(existing?.replies) ? existing.replies : [],
    Array.isArray(fresh?.replies) ? fresh.replies : [],
  );
  merged.replies = mergedReplies;
  merged.replyCount = Math.max(mergedReplies.length, commentReplyCountValue(existing), commentReplyCountValue(fresh));
  return merged;
}

function mergeThreadedCommentList(existingItems, freshItems) {
  const merged = [];
  const mergedByKey = new Map();

  function upsert(item) {
    if (!item || typeof item !== "object") {
      return;
    }
    const normalized = { ...item };
    normalized.replies = mergeThreadedCommentList([], commentReplyItems(item));
    normalized.replyCount = Math.max(normalized.replies.length, commentReplyCountValue(normalized));
    const key = commentIdentityKey(normalized);
    if (mergedByKey.has(key)) {
      mergedByKey.set(key, mergeThreadedCommentItem(mergedByKey.get(key), normalized));
      return;
    }
    merged.push(normalized);
    mergedByKey.set(key, normalized);
  }

  for (const item of existingItems || []) {
    upsert(item);
  }
  for (const item of freshItems || []) {
    upsert(item);
  }

  return merged.map((item) => mergedByKey.get(commentIdentityKey(item)) || item);
}

function isFlatReplyCandidate(comment) {
  if (!comment || typeof comment !== "object") {
    return false;
  }
  if (extractReplyTarget(comment)) {
    return true;
  }
  const commentType = String(comment?.commentType || comment?.comment_type || "").trim().toLowerCase();
  return Boolean(commentType && !["0", "root", "comment", "main"].includes(commentType));
}

function threadTopLevelComments(items) {
  const roots = [];
  const rootsByKey = new Map();
  const pendingReplies = new Map();
  let currentFallbackRootKey = "";

  function replySlotsRemaining(rootKey) {
    const root = rootsByKey.get(rootKey);
    if (!root) {
      return 0;
    }
    return Math.max(commentReplyCountValue(root) - (Array.isArray(root.replies) ? root.replies.length : 0), 0);
  }

  function attachPending(rootKey) {
    const pendingItems = pendingReplies.get(rootKey) || [];
    if (!pendingItems.length) {
      return;
    }
    const root = rootsByKey.get(rootKey);
    if (!root) {
      return;
    }
    root.replies = mergeThreadedCommentList(root.replies || [], pendingItems);
    root.replyCount = Math.max(root.replies.length, commentReplyCountValue(root));
    pendingReplies.delete(rootKey);
  }

  function upsertRoot(item) {
    const key = commentIdentityKey(item);
    if (rootsByKey.has(key)) {
      const merged = mergeThreadedCommentItem(rootsByKey.get(key), item);
      rootsByKey.set(key, merged);
      const index = roots.findIndex((entry) => commentIdentityKey(entry) === key);
      if (index >= 0) {
        roots[index] = merged;
      }
      attachPending(key);
      currentFallbackRootKey = replySlotsRemaining(key) > 0 ? key : "";
      return;
    }
    roots.push(item);
    rootsByKey.set(key, item);
    attachPending(key);
    currentFallbackRootKey = replySlotsRemaining(key) > 0 ? key : "";
  }

  function addReply(rootKey, reply) {
    const root = rootsByKey.get(rootKey);
    if (!root) {
      pendingReplies.set(rootKey, [...(pendingReplies.get(rootKey) || []), reply]);
      return;
    }
    root.replies = mergeThreadedCommentList(root.replies || [], [reply]);
    root.replyCount = Math.max(root.replies.length, commentReplyCountValue(root));
    currentFallbackRootKey = replySlotsRemaining(rootKey) > 0 ? rootKey : "";
  }

  for (const item of items || []) {
    const normalized = normalizeThreadedCommentNode(item);
    if (!normalized) {
      continue;
    }
    const commentKey = commentIdentityKey(normalized);
    const explicitRootKey = String(item?.rootCommentId || item?.root_comment_id || "").trim();
    if (explicitRootKey && explicitRootKey !== commentKey) {
      addReply(explicitRootKey, normalized);
      continue;
    }
    if (
      currentFallbackRootKey
      && currentFallbackRootKey !== commentKey
      && replySlotsRemaining(currentFallbackRootKey) > 0
      && isFlatReplyCandidate(item)
    ) {
      addReply(currentFallbackRootKey, normalized);
      continue;
    }
    upsertRoot(normalized);
  }

  for (const replies of pendingReplies.values()) {
    for (const reply of replies) {
      upsertRoot(reply);
    }
  }

  return roots;
}

function prepareCommentItem(comment, depth = 0, threadPath = "0") {
  if (!comment || typeof comment !== "object") {
    return null;
  }
  const rawTime = comment.time || comment.createdAt || comment.createTime || comment.updatedAt || "";
  const timestamp = commentTimestamp(comment.timestamp || rawTime);
  const images = Array.isArray(comment.images)
    ? comment.images.filter((image) => image && (image.thumb_url || image.full_url || image.fallback_url))
    : [];
  const badges = extractCommentBadges(comment);
  const repliesSource = commentReplyItems(comment);
  const replies = depth < 2
    ? repliesSource.map((item, index) => prepareCommentItem(item, depth + 1, `${threadPath}.${index}`)).filter(Boolean)
    : [];
  const rating = Math.min(Math.max(normalizeCommentNumber(comment.rating), 0), 5);
  const likeCount = normalizeCommentNumber(comment.like_count ?? comment.likeCount ?? comment.praiseCount);
  const replyCount = Math.max(
    replies.length,
    normalizeCommentNumber(comment.reply_count ?? comment.replyCount ?? comment.subCommentCount ?? comment.childrenCount),
  );
  return {
    ...comment,
    author: String(comment.author || comment.userName || comment.nickname || comment.authorName || "匿名用户").trim() || "匿名用户",
    author_url: String(comment.author_url || comment.authorUrl || "").trim(),
    avatar_url: String(comment.avatar_url || comment.avatarUrl || "").trim(),
    avatar_remote_url: String(comment.avatar_remote_url || comment.avatarRemoteUrl || "").trim(),
    content: String(comment.content || comment.comment || comment.text || comment.message || "").trim(),
    time: String(rawTime || "").trim(),
    time_display: formatCommentTime(rawTime, comment.time_display),
    timestamp,
    images,
    gallery_class: commentGalleryClass(images.length),
    badges,
    reply_to: extractReplyTarget(comment),
    replies,
    like_count: likeCount,
    reply_count: replyCount,
    rating,
    rating_label: rating ? rating.toFixed(rating % 1 === 0 ? 0 : 1) : "",
    _thread_key: `${threadPath}:${commentIdentityKey(comment)}`,
    status_copy: commentStatusCopy({ badges }),
    hot_score: computeCommentHotScore({
      timestamp,
      like_count: likeCount,
      reply_count: replyCount,
      rating,
    }),
  };
}

function commentBadgeClass(badge) {
  if (badge.includes("置顶")) return "mw-comment-badge--pinned";
  if (badge.includes("设计师")) return "mw-comment-badge--designer";
  if (badge.includes("助力")) return "mw-comment-badge--boosted";
  return "mw-comment-badge--plain";
}

function commentReplyKey(comment) {
  const threadKey = String(comment?._thread_key || "").trim();
  if (threadKey) {
    return threadKey;
  }
  return String(comment?.id || `${comment?.author || ""}|${comment?.time || ""}|${comment?.content || ""}`).trim();
}

function commentRepliesExpanded(comment) {
  const key = commentReplyKey(comment);
  return key ? Boolean(expandedCommentReplies.value[key]) : false;
}

function commentHasExpandableReplies(comment) {
  return Array.isArray(comment?.replies) && comment.replies.length > COMMENT_REPLY_PREVIEW_COUNT;
}

function visibleCommentReplies(comment) {
  const replies = Array.isArray(comment?.replies) ? comment.replies : [];
  if (!commentHasExpandableReplies(comment) || commentRepliesExpanded(comment)) {
    return replies;
  }
  return replies.slice(0, COMMENT_REPLY_PREVIEW_COUNT);
}

function hiddenCommentReplyCount(comment) {
  const replies = Array.isArray(comment?.replies) ? comment.replies.length : 0;
  return Math.max(replies - COMMENT_REPLY_PREVIEW_COUNT, 0);
}

function commentMissingReplyCount(comment) {
  const replies = Array.isArray(comment?.replies) ? comment.replies.length : 0;
  return Math.max(normalizeCommentNumber(comment?.reply_count) - replies, 0);
}

function commentCanLoadMoreReplies(comment) {
  return commentMissingReplyCount(comment) > 0 && commentsNextOffset.value !== null;
}

function findPreparedComment(key) {
  return comments.value.find((item) => commentReplyKey(item) === key) || null;
}

async function loadMoreRepliesForComment(comment) {
  const key = commentReplyKey(comment);
  if (!key || commentsLoadingMore.value) {
    return;
  }
  let attempts = 0;
  while (commentsNextOffset.value !== null && attempts < 20) {
    const current = findPreparedComment(key);
    if (!current || commentMissingReplyCount(current) <= 0) {
      return;
    }
    await loadMoreComments();
    attempts += 1;
  }
}

function toggleCommentReplies(comment) {
  const key = commentReplyKey(comment);
  if (!key || !commentHasExpandableReplies(comment)) {
    return;
  }
  expandedCommentReplies.value = {
    ...expandedCommentReplies.value,
    [key]: !expandedCommentReplies.value[key],
  };
}

function disconnectCommentsLoadMoreObserver() {
  if (commentsLoadMoreObserver) {
    commentsLoadMoreObserver.disconnect();
    commentsLoadMoreObserver = null;
  }
}

async function syncCommentsLoadMoreObserver() {
  disconnectCommentsLoadMoreObserver();
  if (
    typeof window === "undefined"
    || !commentsAutoLoadSupported.value
    || !commentsReady.value
    || !hasMoreComments.value
  ) {
    return;
  }
  await nextTick();
  const target = commentsLoadMoreTrigger.value;
  if (!(target instanceof HTMLElement)) {
    return;
  }
  commentsLoadMoreObserver = new window.IntersectionObserver((entries) => {
    if (entries.some((entry) => entry.isIntersecting)) {
      loadMoreComments();
    }
  }, {
    root: null,
    rootMargin: "0px 0px 240px 0px",
    threshold: 0,
  });
  commentsLoadMoreObserver.observe(target);
}

function compareComments(left, right) {
  if (commentSortKey.value === "likes") {
    return (right.like_count - left.like_count) || (right.reply_count - left.reply_count) || (right.timestamp - left.timestamp);
  }
  if (commentSortKey.value === "latest") {
    return (right.timestamp - left.timestamp) || (right.like_count - left.like_count) || (right.reply_count - left.reply_count);
  }
  if (commentSortKey.value === "replies") {
    return (right.reply_count - left.reply_count) || (right.like_count - left.like_count) || (right.timestamp - left.timestamp);
  }
  return (right.hot_score - left.hot_score) || (right.like_count - left.like_count) || (right.timestamp - left.timestamp);
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

function resetLocalEditFeedback() {
  localEditDialog.value.message = "";
  localEditDialog.value.error = "";
}

function openLocalEditDialog() {
  if (!isLocalModel.value) {
    return;
  }
  localEditDialog.value = {
    open: true,
    title: detail.value?.title || "",
    description: detail.value?.summary_text || "",
    message: "",
    error: "",
  };
}

function closeLocalEditDialog() {
  if (localEditBusy.value) {
    return;
  }
  localEditDialog.value.open = false;
}

async function submitLocalMetadata() {
  if (!isLocalModel.value || localEditBusy.value) {
    return;
  }
  localEditBusy.value = true;
  resetLocalEditFeedback();
  try {
    const payload = await apiRequest(`/api/models/${encodeURIComponent(modelDir.value)}/local/metadata`, {
      method: "PATCH",
      body: {
        title: localEditDialog.value.title,
        description: localEditDialog.value.description,
      },
    });
    await applyDetailPayload(payload.detail);
    localEditDialog.value.title = payload.detail?.title || localEditDialog.value.title;
    localEditDialog.value.description = payload.detail?.summary_text || localEditDialog.value.description;
    localEditDialog.value.message = payload.message || "模型信息已更新。";
  } catch (error) {
    localEditDialog.value.error = error instanceof Error ? error.message : "保存失败。";
  } finally {
    localEditBusy.value = false;
  }
}

async function uploadLocalEditFiles(endpoint, files, fallbackMessage) {
  if (!isLocalModel.value || localEditBusy.value || !files.length) {
    return;
  }
  localEditBusy.value = true;
  resetLocalEditFeedback();
  const formData = new FormData();
  for (const file of files) {
    formData.append("files", file);
  }
  try {
    const payload = await apiRequest(`/api/models/${encodeURIComponent(modelDir.value)}/local/${endpoint}`, {
      method: "POST",
      body: formData,
    });
    await applyDetailPayload(payload.detail);
    localEditDialog.value.message = payload.message || fallbackMessage;
  } catch (error) {
    localEditDialog.value.error = error instanceof Error ? error.message : "上传失败。";
  } finally {
    localEditBusy.value = false;
  }
}

async function onLocalModelFilesChange(event) {
  const files = [...(event.target.files || [])];
  event.target.value = "";
  await uploadLocalEditFiles("files", files, "模型文件已添加。");
}

async function onLocalModelImagesChange(event) {
  const files = [...(event.target.files || [])];
  event.target.value = "";
  await uploadLocalEditFiles("images", files, "图片已添加。");
}

async function deleteLocalModelFile(profile) {
  if (!isLocalModel.value || localEditBusy.value || !profile?.instance_key) {
    return;
  }
  if ((detail.value?.instances?.length || 0) <= 1) {
    localEditDialog.value.error = "至少保留一个模型文件。";
    return;
  }
  if (!window.confirm(`确认删除模型文件“${profile.file_name || profile.title || "未命名"}”吗？`)) {
    return;
  }
  localEditBusy.value = true;
  resetLocalEditFeedback();
  try {
    const payload = await apiRequest(`/api/models/${encodeURIComponent(modelDir.value)}/local/files`, {
      method: "DELETE",
      body: {
        instance_key: profile.instance_key,
      },
    });
    await applyDetailPayload(payload.detail);
    localEditDialog.value.message = payload.message || "模型文件已删除。";
  } catch (error) {
    localEditDialog.value.error = error instanceof Error ? error.message : "模型文件删除失败。";
  } finally {
    localEditBusy.value = false;
  }
}

async function deleteLocalModelImage(image) {
  if (!isLocalModel.value || localEditBusy.value || !image?.rel_path) {
    return;
  }
  if (!window.confirm("确认删除这张图册图片吗？")) {
    return;
  }
  localEditBusy.value = true;
  resetLocalEditFeedback();
  try {
    const payload = await apiRequest(`/api/models/${encodeURIComponent(modelDir.value)}/local/images`, {
      method: "DELETE",
      body: {
        rel_path: image.rel_path,
      },
    });
    await applyDetailPayload(payload.detail);
    localEditDialog.value.message = payload.message || "图片已删除。";
  } catch (error) {
    localEditDialog.value.error = error instanceof Error ? error.message : "图片删除失败。";
  } finally {
    localEditBusy.value = false;
  }
}

async function setLocalModelCoverImage(image) {
  if (!isLocalModel.value || localEditBusy.value || !image?.rel_path || image.is_cover) {
    return;
  }
  localEditBusy.value = true;
  resetLocalEditFeedback();
  try {
    const payload = await apiRequest(`/api/models/${encodeURIComponent(modelDir.value)}/local/images/cover`, {
      method: "PATCH",
      body: {
        rel_path: image.rel_path,
      },
    });
    await applyDetailPayload(payload.detail);
    localEditDialog.value.message = payload.message || "封面图已更新。";
  } catch (error) {
    localEditDialog.value.error = error instanceof Error ? error.message : "封面图更新失败。";
  } finally {
    localEditBusy.value = false;
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

function detailCacheKey(value = modelDir.value) {
  const cleanValue = String(value || "").trim();
  return cleanValue ? `${DETAIL_CACHE_PREFIX}${cleanValue}` : "";
}

function resetDetailViewState({ clearDetail = true } = {}) {
  closeLightbox();
  closeModelPreview();
  profileEntryRefs.clear();
  clearProfileMediaStripRefs();
  mainGalleryRail.value = createThumbRailState();
  previewedInstanceKey.value = "";
  popoverPlacementState.value = {};
  commentsReady.value = false;
  comments.value = [];
  rawComments.value = [];
  commentsTotal.value = 0;
  commentsArchivedTotal.value = 0;
  commentsNextOffset.value = null;
  commentsLoadingMore.value = false;
  commentsLoadError.value = "";
  sourceBackfillLoading.value = false;
  sourceBackfillMessage.value = "";
  sourceBackfillError.value = "";
  if (clearDetail) {
    localEditDialog.value = {
      open: false,
      title: "",
      description: "",
      message: "",
      error: "",
    };
    localEditBusy.value = false;
  }
  expandedCommentReplies.value = {};
  disconnectCommentsLoadMoreObserver();
  if (clearDetail) {
    detail.value = null;
    activeInstanceKey.value = "";
    setMainMedia("", "", "", "");
  }
}

async function applyDetailPayload(payload, { syncHash = true, cache = true } = {}) {
  const preparedPayload = prepareDetailPayload(payload);
  detail.value = preparedPayload;
  rawComments.value = Array.isArray(preparedPayload?.comments) ? preparedPayload.comments : [];
  comments.value = prepareComments(rawComments.value);
  commentsTotal.value = Number(preparedPayload?.comments_total || comments.value.length);
  commentsArchivedTotal.value = Number(preparedPayload?.comments_archived_total ?? commentsTotal.value);
  commentsNextOffset.value = preparedPayload?.comments_next_offset ?? null;
  const matchedInstance = syncHash ? findInstanceByHash(preparedPayload?.instances || []) : null;
  const initialInstance = matchedInstance || preparedPayload?.instances?.[0] || null;
  activeInstanceKey.value = initialInstance?.instance_key || "";
  if (matchedInstance) {
    selectInstance(initialInstance, { syncHash: false });
  } else if (preparedPayload?.gallery?.length) {
    selectGallery(0);
  } else if (initialInstance) {
    selectInstance(initialInstance, { syncHash: false });
  } else {
    setMainMedia("", preparedPayload?.cover_url || "", preparedPayload?.cover_remote_url || "", preparedPayload?.title || "");
  }
  if (cache && preparedPayload) {
    setPageCache(detailCacheKey(preparedPayload.model_dir || modelDir.value), {
      detail: preparedPayload,
    });
  }
  scheduleCommentsRender();
  await nextTick();
  syncAllThumbRails();
}

function prepareComments(items) {
  if (!Array.isArray(items)) {
    return [];
  }
  return threadTopLevelComments(items)
    .map((comment, index) => prepareCommentItem(comment, 0, `root:${index}`))
    .filter(Boolean);
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
    rawComments.value = [...rawComments.value, ...(Array.isArray(payload.items) ? payload.items : [])];
    comments.value = prepareComments(rawComments.value);
    commentsTotal.value = Number(payload.total || commentsTotal.value || comments.value.length);
    commentsArchivedTotal.value = Number(payload.archived_total ?? commentsArchivedTotal.value ?? comments.value.length);
    commentsNextOffset.value = payload.next_offset ?? null;
  } catch (error) {
    commentsLoadError.value = error instanceof Error ? error.message : "评论加载失败。";
  } finally {
    commentsLoadingMore.value = false;
  }
}

async function submitSourceBackfill() {
  if (!sourceBackfillAvailable.value || sourceBackfillLoading.value) {
    return;
  }
  sourceBackfillLoading.value = true;
  sourceBackfillMessage.value = "";
  sourceBackfillError.value = "";
  try {
    const payload = await apiRequest(`/api/models/${encodeURIComponent(modelDir.value)}/source-backfill`, {
      method: "POST",
      body: {},
    });
    sourceBackfillMessage.value = payload.message || "补全任务已加入队列。";
  } catch (error) {
    sourceBackfillError.value = error instanceof Error ? error.message : "补全任务提交失败。";
  } finally {
    sourceBackfillLoading.value = false;
  }
}

function onAttachmentFileChange(event) {
  const [file] = event.target.files || [];
  attachmentForm.value.file = file || null;
  attachmentUploadMessage.value = "";
  attachmentUploadError.value = "";
}

function openShareDialog() {
  shareDialogVisible.value = true;
}

function closeShareDialog() {
  shareDialogVisible.value = false;
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
    await applyDetailPayload(payload.detail);
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
    await applyDetailPayload(payload.detail);
    attachmentUploadMessage.value = payload.message || "附件已删除。";
  } catch (error) {
    attachmentUploadError.value = error instanceof Error ? error.message : "附件删除失败。";
  } finally {
    deletingAttachmentId.value = "";
  }
}

async function load() {
  const cached = getPageCache(detailCacheKey());
  const hasCachedDetail = Boolean(cached?.detail);
  loading.value = !hasCachedDetail;
  errorMessage.value = "";
  resetDetailViewState({ clearDetail: !hasCachedDetail });
  if (hasCachedDetail) {
    await applyDetailPayload(cached.detail, { cache: false });
  }
  try {
    const payload = await apiRequest(`/api/models/${encodeURIComponent(modelDir.value)}`);
    await applyDetailPayload(payload);
  } catch (error) {
    if (!hasCachedDetail) {
      errorMessage.value = error instanceof Error ? error.message : "读取模型失败。";
    }
  } finally {
    loading.value = false;
  }
}

watch(modelDir, (value) => {
  resetAttachmentUploadState({ keepCategory: false });
  if (!value) {
    resetDetailViewState();
    loading.value = false;
    errorMessage.value = "模型不存在。";
    return;
  }
  load();
}, { immediate: true });

watch([commentsReady, hasMoreComments, () => visibleComments.value.length], () => {
  syncCommentsLoadMoreObserver();
}, { flush: "post" });

watch(previewedInstanceKey, async (value) => {
  if (!value) {
    return;
  }
  await nextTick();
  syncProfileMediaRail(value);
}, { flush: "post" });

onErrorCaptured((error) => {
  errorMessage.value = error instanceof Error ? error.message : "模型详情渲染失败。";
  loading.value = false;
  return false;
});

onMounted(() => {
  if (typeof window === "undefined") {
    return;
  }
  commentsAutoLoadSupported.value = "IntersectionObserver" in window;
  if ("ResizeObserver" in window) {
    railResizeObserver = new window.ResizeObserver((entries) => {
      for (const entry of entries) {
        syncThumbRailByElement(entry.target);
      }
    });
    if (mainGalleryThumbsRef.value) {
      observeThumbRail(mainGalleryThumbsRef.value);
    }
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
  window.addEventListener("keydown", handleWindowKeydown);
  window.addEventListener("pointerdown", handleWindowPointerDown);
  window.addEventListener("resize", handleWindowResize);
});
onBeforeUnmount(() => {
  closeLightbox();
  closeModelPreview();
  profileEntryRefs.clear();
  clearProfileMediaStripRefs();
  mainGalleryThumbsRef.value = null;
  mainGalleryRail.value = createThumbRailState();
  commentsReady.value = false;
  comments.value = [];
  rawComments.value = [];
  commentsTotal.value = 0;
  commentsArchivedTotal.value = 0;
  commentsNextOffset.value = null;
  commentsLoadingMore.value = false;
  commentsLoadError.value = "";
  sourceBackfillLoading.value = false;
  sourceBackfillMessage.value = "";
  sourceBackfillError.value = "";
  localEditBusy.value = false;
  expandedCommentReplies.value = {};
  disconnectCommentsLoadMoreObserver();
  if (commentsRenderFrame && typeof window !== "undefined") {
    window.cancelAnimationFrame(commentsRenderFrame);
  }
  if (railResizeObserver) {
    railResizeObserver.disconnect();
    railResizeObserver = null;
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
    window.removeEventListener("keydown", handleWindowKeydown);
    window.removeEventListener("pointerdown", handleWindowPointerDown);
    window.removeEventListener("resize", handleWindowResize);
  }
});
</script>
