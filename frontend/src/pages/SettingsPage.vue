<template>
  <section class="page-intro settings-page-intro">
    <div>
      <span class="eyebrow">设置</span>
      <h1>系统、本地整理与用户配置</h1>
    </div>
  </section>

  <section class="surface">
    <div class="settings-tabs">
      <button
        v-for="item in tabs"
        :key="item.key"
        :class="['settings-tab', activeTab === item.key && 'is-active']"
        type="button"
        @click="setActiveTab(item.key)"
      >
        {{ item.label }}
      </button>
    </div>

    <div v-show="activeTab === 'connections'" class="settings-panel is-active">
      <form class="settings-form" @submit.prevent="saveConnections">
        <div class="settings-grid settings-grid--two">
          <label class="field-card">
            <span>国内 Cookie</span>
            <SecretTextarea v-model="connectionForm.cookie_cn" placeholder="makerworld.com.cn Cookie" />
            <div class="settings-inline-actions">
              <button class="button button-secondary button-small" type="button" :disabled="testing.cookie_cn" @click="testCookie('cn')">
                {{ testing.cookie_cn ? "测试中..." : "测试国内 Cookie" }}
              </button>
              <span class="form-status">{{ statuses.cookie_cn }}</span>
            </div>
          </label>
          <label class="field-card">
            <span>国际 Cookie</span>
            <SecretTextarea v-model="connectionForm.cookie_global" placeholder="makerworld.com Cookie" />
            <div class="settings-inline-actions">
              <button class="button button-secondary button-small" type="button" :disabled="testing.cookie_global" @click="testCookie('global')">
                {{ testing.cookie_global ? "测试中..." : "测试国际 Cookie" }}
              </button>
              <span class="form-status">{{ statuses.cookie_global }}</span>
            </div>
          </label>
        </div>
        <div class="settings-grid settings-grid--three">
          <label class="field-card">
            <span>启用 HTTP 代理</span>
            <button
              :class="['subscription-switch', connectionForm.proxy_enabled && 'is-on']"
              type="button"
              :disabled="testing.proxy"
              @click="connectionForm.proxy_enabled = !connectionForm.proxy_enabled"
            >
              <span class="subscription-switch__track" aria-hidden="true">
                <span class="subscription-switch__thumb"></span>
              </span>
              <span class="subscription-switch__label">{{ connectionForm.proxy_enabled ? "启用中" : "已停用" }}</span>
            </button>
            <small class="archive-form__hint">开启后归档、订阅与源端刷新请求会带上当前代理设置。</small>
          </label>
          <label class="field-card">
            <span>HTTP Proxy</span>
            <input v-model="connectionForm.http_proxy" type="text" placeholder="http://127.0.0.1:7890">
          </label>
          <label class="field-card">
            <span>HTTPS Proxy</span>
            <input v-model="connectionForm.https_proxy" type="text" placeholder="http://127.0.0.1:7890">
          </label>
        </div>
        <div class="settings-inline-actions">
          <button class="button button-secondary" type="button" :disabled="testing.proxy" @click="testProxy">
            {{ testing.proxy ? "测试中..." : "测试 HTTP 代理" }}
          </button>
          <span class="form-status">{{ statuses.proxy }}</span>
        </div>
        <div class="form-footer">
          <button class="button button-primary" type="submit">保存连接设置</button>
          <span class="form-status">{{ statuses.connections }}</span>
        </div>
      </form>
    </div>

    <div v-show="activeTab === 'notifications'" class="settings-panel is-active">
      <form class="settings-form" @submit.prevent="saveNotifications">
        <div class="settings-grid settings-grid--three">
          <label class="field-card">
            <span>启用通知</span>
            <label class="switch">
              <input v-model="notificationsForm.enabled" type="checkbox">
              <span>任务状态更新时触发通知</span>
            </label>
          </label>
          <label class="field-card">
            <span>Telegram Bot Token</span>
            <input v-model="notificationsForm.telegram_bot_token" type="text" placeholder="bot token">
          </label>
          <label class="field-card">
            <span>Telegram Chat ID</span>
            <input v-model="notificationsForm.telegram_chat_id" type="text" placeholder="chat id">
          </label>
        </div>
        <label class="field-card">
          <span>Webhook URL</span>
          <input v-model="notificationsForm.webhook_url" type="text" placeholder="https://example.com/webhook">
        </label>
        <div class="form-footer">
          <button class="button button-primary" type="submit">保存通知设置</button>
          <span class="form-status">{{ statuses.notifications }}</span>
        </div>
      </form>
    </div>

    <div v-show="activeTab === 'organizer'" class="settings-panel is-active">
      <form class="settings-form token-card" @submit.prevent="saveOrganizer">
        <div class="section-card__header">
          <div>
            <span class="eyebrow">本地整理</span>
            <h2>本地 3MF 整理设置</h2>
          </div>
          <RouterLink class="button button-secondary button-small" to="/organizer">
            返回
          </RouterLink>
        </div>

        <div class="settings-grid settings-grid--two">
          <label class="field-card">
            <span>本地整理扫描目录</span>
            <input v-model="organizerForm.source_dir" type="text" placeholder="/app/local">
            <small class="archive-form__hint">Worker 会从这里扫描候选 3MF 文件。</small>
          </label>
          <label class="field-card">
            <span>整理目标目录</span>
            <input v-model="organizerForm.target_dir" type="text" placeholder="/app/archive">
            <small class="archive-form__hint">整理后的模型会写入归档库。</small>
          </label>
        </div>

        <label class="field-card">
          <span>整理模式</span>
          <label class="switch">
            <input v-model="organizerForm.move_files" type="checkbox">
            <span>启用后移动文件；关闭后复制文件并保留原文件</span>
          </label>
        </label>

        <div class="form-footer">
          <button class="button button-primary" type="submit">保存本地整理设置</button>
          <span class="form-status">{{ statuses.organizer }}</span>
        </div>
      </form>

      <section class="settings-form token-card">
        <div class="section-card__header">
          <div>
            <span class="eyebrow">移动端导入</span>
            <h2>iOS 快捷指令</h2>
          </div>
          <div class="settings-inline-actions">
            <button class="button button-secondary button-small" type="button" @click="copyShortcutConfig">
              复制配置
            </button>
            <button class="button button-primary button-small" type="button" @click="resetMobileImportToken">
              生成 Token
            </button>
          </div>
        </div>

        <div class="mobile-import-status-grid">
          <article class="field-card system-update-stat">
            <span>Token 状态</span>
            <strong>{{ mobileImportTokenStatus }}</strong>
            <small>{{ mobileImportTokenMeta }}</small>
          </article>
          <article class="field-card system-update-stat">
            <span>快捷指令提示</span>
            <strong>成功显示“已上传”</strong>
            <small>地址不可用时 iOS 会显示请求失败。</small>
          </article>
        </div>

        <div v-if="mobileImportToken" class="token-output">
          <strong>新 Token</strong>
          <code>{{ mobileImportToken }}</code>
        </div>

        <div class="mobile-import-shortcut">
          <strong>快捷指令流程</strong>
          <ol>
            <li>接收共享表单里的文件。</li>
            <li>在手机快捷指令里填写 Token 和 MakerHub 地址。</li>
            <li>用 Token 请求选定地址的 <code>/api/mobile-import/ping-ipv4</code>。</li>
            <li>地址可用后，把文件上传到 <code>/api/mobile-import/raw-ipv4</code>。</li>
            <li>上传成功提示“已上传”，地址不可用时 iOS 会显示请求失败。</li>
          </ol>
        </div>

        <div class="settings-inline-actions">
          <button class="button button-secondary" type="button" :disabled="!mobileImportEnabled" @click="disableMobileImport">
            停用 Token
          </button>
          <span class="form-status">{{ statuses.mobile_import }}</span>
        </div>
      </section>
    </div>

    <div v-show="activeTab === 'sharing'" class="settings-panel is-active">
      <form class="settings-form token-card share-receive-card" @submit.prevent="previewShareCode">
        <div class="section-card__header">
          <div>
            <span class="eyebrow">接收分享</span>
            <h2>导入分享码</h2>
          </div>
        </div>
        <label class="field-card">
          <span>分享码</span>
          <textarea v-model.trim="shareReceive.code" rows="5" placeholder="粘贴对方 MakerHub 生成的分享码"></textarea>
        </label>
        <div class="settings-inline-actions">
          <button class="button button-secondary" type="submit" :disabled="shareReceive.loadingPreview">
            {{ shareReceive.loadingPreview ? "预览中..." : "预览分享" }}
          </button>
          <button
            class="button button-primary"
            type="button"
            :disabled="!shareReceive.preview?.can_import || shareReceive.importing"
            @click="importShareCode"
          >
            {{ shareReceive.importing ? "导入中..." : "确认导入" }}
          </button>
          <span class="form-status">{{ statuses.share_receive }}</span>
        </div>
        <div v-if="shareReceive.preview" class="share-preview">
          <div class="share-preview__head">
            <strong>{{ shareReceive.preview.manifest?.model_count || 0 }} 个模型</strong>
            <span>{{ formatShareFileCounts(shareReceive.preview.manifest?.file_counts) }}</span>
            <em :class="shareReceive.preview.can_import ? 'is-ok' : 'is-error'">
              {{ shareReceive.preview.can_import ? "可导入" : "发现重复" }}
            </em>
          </div>
          <ol v-if="shareReceive.preview.manifest?.models?.length" class="share-preview__list">
            <li v-for="model in shareReceive.preview.manifest.models" :key="`${model.title}-${model.id}`">
              <span>{{ model.title || model.id || "未命名模型" }}</span>
              <em>{{ formatModelShareCounts(model) }}</em>
            </li>
          </ol>
          <ol v-if="shareReceive.preview.duplicates?.length" class="share-preview__list share-preview__list--duplicates">
            <li v-for="item in shareReceive.preview.duplicates" :key="`${item.share_title}-${item.existing_model_dir}-${item.key}`">
              <span>{{ item.share_title || "分享模型" }} 已存在</span>
              <em>{{ item.existing_title || item.existing_model_dir }}</em>
            </li>
          </ol>
        </div>
      </form>

      <section class="token-card shared-list-card">
        <div class="section-card__header">
          <div>
            <span class="eyebrow">已分享</span>
            <h2>分享记录</h2>
          </div>
          <div class="settings-inline-actions">
            <button class="button button-secondary button-small" type="button" :disabled="sharedSharesLoading" @click="loadSharedShares">
              {{ sharedSharesLoading ? "刷新中..." : "刷新" }}
            </button>
            <button class="button button-secondary button-small" type="button" :disabled="sharedSharesLoading || !expiredShareCount" @click="cleanupExpiredShares">
              清理过期
            </button>
          </div>
        </div>
        <div v-if="sharedShares.length" class="shared-list">
          <article v-for="item in sharedShares" :key="item.id" class="shared-list-item">
            <div class="shared-list-item__main">
              <div class="shared-list-item__title">
                <strong>{{ shareRecordTitle(item) }}</strong>
                <span :class="['shared-list-item__status', item.expired && 'is-expired']">
                  {{ item.expired ? "已过期" : "有效中" }}
                </span>
              </div>
              <div class="shared-list-item__meta">
                <span>{{ item.model_count || 0 }} 个模型</span>
                <span>{{ formatShareFileCounts(item.file_counts) }}</span>
                <span>到期 {{ formatShareDate(item.expires_at) }}</span>
              </div>
              <p>{{ shareRecordModelNames(item) }}</p>
            </div>
            <div class="shared-list-item__actions">
              <button
                class="button button-secondary button-small"
                type="button"
                :disabled="sharedShareCopyingId === item.id"
                @click="copyManagedShareCode(item)"
              >
                {{ sharedShareCopyingId === item.id ? "处理中..." : "复制分享码" }}
              </button>
              <button
                class="button button-danger button-small"
                type="button"
                :disabled="sharedShareRevokingId === item.id"
                @click="revokeManagedShare(item)"
              >
                {{ sharedShareRevokingId === item.id ? "撤销中..." : "撤销" }}
              </button>
            </div>
          </article>
        </div>
        <p v-else class="empty-copy">当前还没有已分享的模型。</p>
        <span class="form-status">{{ statuses.share_manage }}</span>
      </section>

      <form class="settings-form token-card" @submit.prevent="saveSharing">
        <div class="section-card__header">
          <div>
            <span class="eyebrow">模型分享</span>
            <h2>分享设置</h2>
          </div>
        </div>

        <div class="share-settings-row">
          <label class="field-card">
            <span>公开访问地址</span>
            <input v-model.trim="sharingForm.public_base_url" type="url" placeholder="https://makerhub.example.com">
          </label>
          <label class="field-card share-expiry-field">
            <span>默认有效期</span>
            <div class="share-expiry-input">
              <input v-model.number="sharingForm.default_expires_days" type="number" min="1" max="90" step="1">
              <em>天</em>
            </div>
          </label>
          <label class="field-card">
            <span>默认内容</span>
            <div class="share-settings-checks">
              <label class="switch"><input v-model="sharingForm.include_images" type="checkbox"><span>图片</span></label>
              <label class="switch"><input v-model="sharingForm.include_model_files" type="checkbox"><span>模型文件</span></label>
              <label class="switch"><input v-model="sharingForm.include_attachments" type="checkbox"><span>附件</span></label>
              <label class="switch"><input v-model="sharingForm.include_comments" type="checkbox"><span>评论</span></label>
            </div>
          </label>
        </div>
        <p class="archive-form__hint share-settings-hint">分享码会让接收方 MakerHub 从公开访问地址拉取模型快照；请填写对方机器可访问的域名、DDNS、内网穿透或局域网地址。</p>

        <div class="settings-inline-actions">
          <button class="button button-secondary" type="button" :disabled="testing.sharing" @click="testSharing">
            {{ testing.sharing ? "检测中..." : "检测公开访问" }}
          </button>
          <span class="form-status">{{ statuses.sharing_test }}</span>
        </div>

        <div class="form-footer">
          <button class="button button-primary" type="submit">保存分享设置</button>
          <span class="form-status">{{ statuses.sharing }}</span>
        </div>
      </form>
    </div>

    <div v-show="activeTab === 'advanced'" class="settings-panel is-active">
      <form class="settings-form token-card" @submit.prevent="saveAdvanced">
        <div class="section-card__header">
          <div>
            <span class="eyebrow">高级</span>
            <h2>后台任务与资源限制</h2>
          </div>
        </div>

        <div class="settings-grid settings-grid--two">
          <label class="field-card">
            <span>国区每日 3MF 下载上限</span>
            <input v-model.number="threeMfLimitsForm.cn_daily_limit" type="number" min="0" step="1">
            <small class="archive-form__hint">默认 100，填 0 表示不限制；达到上限后，国区缺失 3MF 会暂停到次日 00:00。</small>
          </label>
          <label class="field-card">
            <span>国际区每日 3MF 下载上限</span>
            <input v-model.number="threeMfLimitsForm.global_daily_limit" type="number" min="0" step="1">
            <small class="archive-form__hint">默认 100，填 0 表示不限制；达到上限后，国际区缺失 3MF 会暂停到次日 00:00。</small>
          </label>
        </div>

        <div class="settings-grid settings-grid--three">
          <label class="field-card">
            <span>源端刷新模型并发</span>
            <input v-model.number="advancedForm.remote_refresh_model_workers" type="number" min="1" max="4" step="1">
            <small class="archive-form__hint">默认 2，最多 4；数值越大整轮越快，但后台占用也更高。</small>
          </label>
          <label class="field-card">
            <span>MakerWorld 请求并发</span>
            <input v-model.number="advancedForm.makerworld_request_limit" type="number" min="1" max="8" step="1">
            <small class="archive-form__hint">默认 2，限制页面和 API 抓取同时进行的数量。</small>
          </label>
          <label class="field-card">
            <span>评论资源下载并发</span>
            <input v-model.number="advancedForm.comment_asset_download_limit" type="number" min="1" max="16" step="1">
            <small class="archive-form__hint">默认 4，控制评论头像和评论图片下载。</small>
          </label>
        </div>

        <div class="settings-grid settings-grid--two">
          <label class="field-card">
            <span>3MF 下载并发</span>
            <input v-model.number="advancedForm.three_mf_download_limit" type="number" min="1" max="4" step="1">
            <small class="archive-form__hint">默认 1，避免多个大文件同时下载拖慢 Web 访问。</small>
          </label>
          <label class="field-card">
            <span>磁盘写入 / 索引并发</span>
            <input v-model.number="advancedForm.disk_io_limit" type="number" min="1" max="4" step="1">
            <small class="archive-form__hint">默认 1，限制 meta 写入、缺失 3MF 状态和快照索引更新。</small>
          </label>
        </div>

        <div class="form-footer">
          <button class="button button-primary" type="submit">保存高级设置</button>
          <span class="form-status">{{ statuses.advanced }}</span>
        </div>
      </form>
    </div>

    <div v-show="activeTab === 'system'" class="settings-panel is-active">
      <section class="token-card system-update-card">
        <div class="section-card__header">
          <div>
            <span class="eyebrow">系统</span>
            <h2>容器更新</h2>
          </div>
          <div class="settings-inline-actions">
            <button class="button button-secondary" type="button" :disabled="systemUpdateLoading || systemUpdateSubmitting" @click="loadSystemUpdateStatus({ force: true })">
              {{ systemUpdateLoading ? "读取中..." : "刷新状态" }}
            </button>
            <button class="button button-primary" type="button" :disabled="!canTriggerSystemUpdate" @click="triggerSystemUpdate">
              {{ systemUpdateButtonText }}
            </button>
          </div>
        </div>

        <div class="settings-grid settings-grid--three system-update-grid">
          <article class="field-card system-update-stat">
            <span>当前版本</span>
            <strong>{{ appState.appVersion ? `v${appState.appVersion}` : "读取中" }}</strong>
          </article>
          <article class="field-card system-update-stat">
            <span>最新版本</span>
            <strong>{{ githubVersionText }}</strong>
          </article>
          <article class="field-card system-update-stat">
            <span>更新状态</span>
            <strong>{{ systemUpdateStatusLabel }}</strong>
          </article>
        </div>

        <div class="settings-grid settings-grid--two system-update-grid">
          <article class="field-card system-update-detail">
            <span>App 容器</span>
            <strong>{{ systemUpdate.container_name || "-" }}</strong>
          </article>
          <article class="field-card system-update-detail">
            <span>Worker 容器</span>
            <strong>{{ systemUpdate.worker_container_name || systemUpdate.web_container_name || (systemUpdate.deployment_mode === "single" ? "单容器" : "-") }}</strong>
          </article>
        </div>

        <div class="settings-grid settings-grid--two system-update-grid">
          <article class="field-card system-update-detail">
            <span>部署模式</span>
            <strong>{{ deploymentModeLabel }}</strong>
          </article>
          <article class="field-card system-update-detail">
            <span>一键更新支持</span>
            <strong>{{ systemUpdate.supported ? "已启用" : "未启用" }}</strong>
          </article>
        </div>

        <div class="field-card system-update-manual">
          <span>{{ systemUpdate.supported ? "执行说明" : "如何启用一键更新" }}</span>
          <p v-if="systemUpdate.supported">
            更新会复用当前容器名称、挂载、端口和重启策略。App + Worker 部署下会先更新后台 Worker，再更新 App 容器；页面短暂报错通常只是容器正在重启。
          </p>
          <p v-else>
            首次仍需要手动在部署里挂载 <code>/var/run/docker.sock:/var/run/docker.sock</code>。启用后，这个页面才能直接拉取新镜像并重建容器。
          </p>
          <code class="system-update-code">{{ manualUpdateCommand }}</code>
        </div>

        <section class="system-update-changelog system-maintenance-card">
          <div class="section-card__header">
            <div>
              <span class="eyebrow">系统维护</span>
              <h2>现有库信息补全</h2>
            </div>
            <div class="settings-inline-actions">
              <button class="button button-secondary" type="button" :disabled="profileBackfillLoading || profileBackfillSubmitting" @click="loadProfileBackfillStatus">
                {{ profileBackfillLoading ? "读取中..." : "刷新状态" }}
              </button>
              <button class="button button-primary" type="button" :disabled="profileBackfillSubmitting || profileBackfill.running" @click="triggerProfileBackfill">
                {{ profileBackfill.running ? "扫描中..." : profileBackfillSubmitting ? "提交中..." : "补全现有库信息" }}
              </button>
            </div>
          </div>
          <p class="archive-form__hint">
            这里只负责扫描本地已归档模型，并把缺少打印配置详情、实例展示媒体或评论回复字段的模型加入归档补整理队列。实际补全会在后台归档队列继续执行，不主动消耗 3MF 下载次数。
          </p>
          <div class="settings-grid settings-grid--three system-update-grid">
            <article class="field-card system-update-stat">
              <span>发现缺失</span>
              <strong>{{ profileBackfillStats.scanned }}</strong>
              <small>{{ profileBackfill.started_at ? `最近扫描：${profileBackfill.started_at}` : "尚未执行" }}</small>
            </article>
            <article class="field-card system-update-stat">
              <span>新增入队</span>
              <strong>{{ profileBackfillStats.queued }}</strong>
              <small>已在队列：{{ profileBackfillStats.alreadyQueued }}</small>
            </article>
            <article class="field-card system-update-stat">
              <span>失败</span>
              <strong>{{ profileBackfillStats.failed }}</strong>
              <small>{{ profileBackfill.running ? "正在扫描并入队" : profileBackfill.finished_at ? `最近扫描结束：${profileBackfill.finished_at}` : "等待执行" }}</small>
            </article>
          </div>
          <div class="form-footer">
            <span class="form-status">{{ profileBackfillStatusText }}</span>
          </div>
        </section>

        <form class="system-update-changelog system-runtime-card" @submit.prevent="saveRuntimeResources">
          <div class="section-card__header">
            <div>
              <span class="eyebrow">运行资源</span>
              <h2>App / Worker 调度</h2>
            </div>
            <div class="settings-inline-actions">
              <button class="button button-secondary" type="submit" :disabled="runtimeResourcesSaving">
                {{ runtimeResourcesSaving ? "保存中..." : "保存资源设置" }}
              </button>
              <button class="button button-primary" type="button" :disabled="!canTriggerSystemUpdate || runtimeResourcesSaving" @click="applyRuntimeResources">
                应用并重启容器
              </button>
            </div>
          </div>
          <p class="archive-form__hint">
            App Web 进程数需要重启容器生效。CPU 核心绑定按 Docker 的 CPU 序号填写，例如 <code>0</code>、<code>1-3</code> 或 <code>0,2</code>；留空表示不限制。
          </p>
          <div class="settings-grid settings-grid--three system-update-grid">
            <label class="field-card">
              <span>App Web 进程数</span>
              <input v-model.number="runtimeForm.web_workers" type="number" min="1" max="8" step="1">
              <small class="archive-form__hint">当前容器：{{ currentAppWebWorkers }}；NAS 多核心建议 2-4。</small>
            </label>
            <label class="field-card">
              <span>App CPU 上限</span>
              <input v-model.trim="runtimeForm.app_cpu_limit" type="text" inputmode="decimal" placeholder="例如 2 或 2.5">
              <small class="archive-form__hint">当前：{{ currentAppCpuLimit || "不限" }}</small>
            </label>
            <label class="field-card">
              <span>App CPU 核心绑定</span>
              <input v-model.trim="runtimeForm.app_cpuset_cpus" type="text" placeholder="例如 0 或 0-1">
              <small class="archive-form__hint">当前：{{ currentAppCpuset || "未绑定" }}</small>
            </label>
          </div>
          <div class="settings-grid settings-grid--three system-update-grid">
            <label class="field-card">
              <span>App CPU 权重</span>
              <input v-model.number="runtimeForm.app_cpu_shares" type="number" min="0" max="262144" step="1">
              <small class="archive-form__hint">默认 1024，0 表示不写入。</small>
            </label>
            <label class="field-card">
              <span>Worker CPU 上限</span>
              <input v-model.trim="runtimeForm.worker_cpu_limit" type="text" inputmode="decimal" placeholder="例如 3">
              <small class="archive-form__hint">当前：{{ currentWorkerCpuLimit || "不限" }}</small>
            </label>
            <label class="field-card">
              <span>Worker CPU 核心绑定</span>
              <input v-model.trim="runtimeForm.worker_cpuset_cpus" type="text" placeholder="例如 1-3">
              <small class="archive-form__hint">当前：{{ currentWorkerCpuset || "未绑定" }}</small>
            </label>
          </div>
          <div class="settings-grid settings-grid--two system-update-grid">
            <label class="field-card">
              <span>Worker CPU 权重</span>
              <input v-model.number="runtimeForm.worker_cpu_shares" type="number" min="0" max="262144" step="1">
              <small class="archive-form__hint">默认 512，降低后台任务抢占。</small>
            </label>
            <article class="field-card system-update-detail">
              <span>生效方式</span>
              <strong>{{ systemUpdate.supported ? "可网页重建" : "需要手动重建" }}</strong>
              <small>{{ systemUpdate.supported ? "保存后点击应用并重启容器。" : systemUpdate.support_reason || "当前没有 Docker socket 权限。" }}</small>
            </article>
          </div>
          <div class="form-footer">
            <span class="form-status">{{ statuses.runtime }}</span>
          </div>
        </form>

        <section class="system-update-changelog">
          <div class="section-card__header">
            <div>
              <span class="eyebrow">更新日志</span>
              <h2>最近版本记录</h2>
            </div>
            <span class="count-pill">{{ changelogEntries.length }} 条</span>
          </div>
          <p class="archive-form__hint">{{ changelogSummaryText }}</p>
          <div v-if="changelogEntries.length" class="system-update-changelog__list">
            <article v-for="entry in changelogEntries" :key="`${entry.date}-${entry.version}`" class="field-card system-update-changelog__entry">
              <div class="system-update-changelog__head">
                <strong>{{ entry.version ? `v${entry.version}` : "更新记录" }}</strong>
                <span>{{ entry.date || "-" }}</span>
              </div>
              <ul class="system-update-changelog__items">
                <li v-for="item in entry.items || []" :key="item">{{ item }}</li>
              </ul>
            </article>
          </div>
          <p v-else class="empty-copy">暂时还没有读取到线上更新日志。</p>
        </section>

        <div class="form-footer">
          <span class="form-status">{{ statuses.system_update || systemUpdate.message || "当前还没有进行中的网页更新任务。" }}</span>
        </div>
      </section>
    </div>

    <div v-show="activeTab === 'user'" class="settings-panel is-active">
      <form class="settings-form token-card" @submit.prevent="saveTheme">
        <div class="section-card__header">
          <div>
            <span class="eyebrow">主题</span>
            <h2>整体显示模式</h2>
          </div>
        </div>
        <ThemeSegment :value="themePreference" @change="themePreference = $event" />
        <p class="archive-form__hint">“自动”会跟随你设备当前的浅色 / 深色模式。</p>
        <div class="form-footer">
          <button class="button button-primary" type="submit">保存主题设置</button>
          <span class="form-status">{{ statuses.theme }}</span>
        </div>
      </form>

      <form class="settings-form" @submit.prevent="saveUser">
        <div class="settings-grid settings-grid--three">
          <label class="field-card">
            <span>用户名</span>
            <input v-model="userForm.username" type="text" placeholder="admin">
          </label>
          <label class="field-card">
            <span>显示名称</span>
            <input v-model="userForm.display_name" type="text" placeholder="Admin">
          </label>
          <label class="field-card">
            <span>密码提示</span>
            <input v-model="userForm.password_hint" type="text" placeholder="请改成强密码">
          </label>
        </div>
        <div class="form-footer">
          <button class="button button-primary" type="submit">保存用户信息</button>
          <span class="form-status">{{ statuses.user }}</span>
        </div>
      </form>

      <form class="settings-form token-card" @submit.prevent="savePassword">
        <div class="section-card__header">
          <div>
            <span class="eyebrow">密码</span>
            <h2>修改登录密码</h2>
          </div>
        </div>
        <div class="settings-grid settings-grid--three">
          <label class="field-card">
            <span>当前密码</span>
            <input v-model="passwordForm.current_password" type="password" autocomplete="current-password" placeholder="当前密码">
          </label>
          <label class="field-card">
            <span>新密码</span>
            <input v-model="passwordForm.new_password" type="password" autocomplete="new-password" placeholder="至少 4 位">
          </label>
          <label class="field-card">
            <span>确认新密码</span>
            <input v-model="passwordForm.confirm_password" type="password" autocomplete="new-password" placeholder="再次输入新密码">
          </label>
        </div>
        <div class="form-footer">
          <button class="button button-primary" type="submit">更新密码</button>
          <span class="form-status">{{ statuses.password }}</span>
        </div>
      </form>

      <section class="token-card">
        <div class="section-card__header">
          <div>
            <span class="eyebrow">Token</span>
            <h2>API Token 管理</h2>
          </div>
        </div>
        <form class="token-create-form" @submit.prevent="createToken">
          <input v-model.trim="tokenName" type="text" placeholder="例如：家里 NAS / 自动化脚本">
          <button class="button button-primary" type="submit">生成 Token</button>
        </form>
        <p class="archive-form__hint">后续通过 API 调用归档时，可在 <code>Authorization: Bearer &lt;token&gt;</code> 里传入。</p>
        <div v-if="newToken" class="token-output">
          <strong>新 Token</strong>
          <code>{{ newToken }}</code>
        </div>
        <span class="form-status">{{ statuses.tokens }}</span>
        <div class="token-list">
          <article v-for="item in tokenItems" :key="item.id" class="token-item">
            <div>
              <strong>{{ item.name }}</strong>
              <span>{{ item.token_prefix }}...</span>
            </div>
            <div class="token-item__meta">
              <span>创建于 {{ item.created_at || "-" }}</span>
              <span>最近使用 {{ item.last_used_at || "未使用" }}</span>
            </div>
            <button class="button button-secondary button-small" type="button" @click="revokeToken(item.id)">撤销</button>
          </article>
          <p v-if="!tokenItems.length" class="empty-copy">当前还没有 API Token。</p>
        </div>
      </section>
    </div>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { RouterLink, useRoute, useRouter } from "vue-router";

import SecretTextarea from "../components/SecretTextarea.vue";
import ThemeSegment from "../components/ThemeSegment.vue";
import { appState, applyConfigPayload, refreshConfig, saveThemePreference } from "../lib/appState";
import { apiRequest } from "../lib/api";


const route = useRoute();
const router = useRouter();

const tabs = [
  { key: "system", label: "系统" },
  { key: "connections", label: "连接设置" },
  { key: "organizer", label: "本地整理" },
  { key: "sharing", label: "模型分享" },
  { key: "advanced", label: "高级" },
  { key: "user", label: "用户" },
  { key: "notifications", label: "通知" },
];

const activeTab = ref("system");
const themePreference = ref("auto");
const tokenName = ref("");
const newToken = ref("");
const tokenItems = ref([]);
const mobileImportToken = ref("");
const sharedShares = ref([]);
const sharedSharesLoading = ref(false);
const sharedShareCopyingId = ref("");
const sharedShareRevokingId = ref("");
const systemUpdate = ref(defaultSystemUpdateState());
const systemUpdateLoading = ref(false);
const systemUpdateSubmitting = ref(false);
const runtimeResourcesSaving = ref(false);
const profileBackfill = ref(defaultProfileBackfillState());
const profileBackfillLoading = ref(false);
const profileBackfillSubmitting = ref(false);

const connectionForm = reactive({
  cookie_cn: "",
  cookie_global: "",
  proxy_enabled: false,
  http_proxy: "",
  https_proxy: "",
});
const notificationsForm = reactive({
  enabled: false,
  telegram_bot_token: "",
  telegram_chat_id: "",
  webhook_url: "",
});
const threeMfLimitsForm = reactive({
  cn_daily_limit: 100,
  global_daily_limit: 100,
});
const advancedForm = reactive({
  remote_refresh_model_workers: 2,
  makerworld_request_limit: 2,
  comment_asset_download_limit: 4,
  three_mf_download_limit: 1,
  disk_io_limit: 1,
});
const runtimeForm = reactive({
  web_workers: 1,
  app_cpu_limit: "",
  app_cpuset_cpus: "",
  app_cpu_shares: 1024,
  worker_cpu_limit: "",
  worker_cpuset_cpus: "",
  worker_cpu_shares: 512,
});
const organizerForm = reactive({
  source_dir: "",
  target_dir: "",
  move_files: true,
});
const sharingForm = reactive({
  public_base_url: "",
  default_expires_days: 7,
  include_images: true,
  include_model_files: true,
  model_file_types: ["3mf", "stl", "step", "obj"],
  include_attachments: true,
  attachment_file_types: ["pdf", "excel"],
  include_comments: true,
});
const shareReceive = reactive({
  code: "",
  loadingPreview: false,
  importing: false,
  preview: null,
});
const userForm = reactive({
  username: "admin",
  display_name: "Admin",
  password_hint: "",
});
const passwordForm = reactive({
  current_password: "",
  new_password: "",
  confirm_password: "",
});
const statuses = reactive({
  connections: "",
  cookie_cn: "",
  cookie_global: "",
  proxy: "",
  organizer: "",
  mobile_import: "",
  sharing: "",
  sharing_test: "",
  share_receive: "",
  share_manage: "",
  advanced: "",
  notifications: "",
  user: "",
  password: "",
  tokens: "",
  theme: "",
  system_update: "",
  runtime: "",
  profile_backfill: "",
});
const testing = reactive({
  cookie_cn: false,
  cookie_global: false,
  proxy: false,
  sharing: false,
});
let systemUpdateTimer = null;
let profileBackfillTimer = null;

const config = computed(() => appState.config);
const systemUpdateActive = computed(() => ["queued", "launching_helper", "running", "pending_startup"].includes(systemUpdate.value.status));
const canTriggerSystemUpdate = computed(() => (
  systemUpdate.value.supported
  && !systemUpdateActive.value
  && !systemUpdateLoading.value
  && !systemUpdateSubmitting.value
));
const currentAppWebWorkers = computed(() => (
  normalizeBoundedInt(systemUpdate.value.resources?.app?.web_workers, 1, 1, 8)
));
const currentAppCpuLimit = computed(() => String(systemUpdate.value.resources?.app?.cpu_limit || ""));
const currentAppCpuset = computed(() => String(systemUpdate.value.resources?.app?.cpuset_cpus || ""));
const currentWorkerCpuLimit = computed(() => String(systemUpdate.value.resources?.worker?.cpu_limit || ""));
const currentWorkerCpuset = computed(() => String(systemUpdate.value.resources?.worker?.cpuset_cpus || ""));
const systemUpdateButtonText = computed(() => (
  appState.githubUpdateAvailable ? "更新到最新版本" : "重新拉取 latest"
));
const githubVersionText = computed(() => {
  if (appState.githubLatestVersion) {
    return `v${appState.githubLatestVersion}`;
  }
  if (appState.githubVersionError) {
    return "读取失败";
  }
  return "读取中";
});
const expiredShareCount = computed(() => sharedShares.value.filter((item) => item.expired).length);
const systemUpdateStatusLabel = computed(() => {
  const labelMap = {
    idle: "空闲",
    queued: "已提交",
    launching_helper: "启动中",
    running: "执行中",
    pending_startup: "重启中",
    succeeded: "已完成",
    failed: "失败",
  };
  return labelMap[systemUpdate.value.status] || "未知";
});
const deploymentModeLabel = computed(() => {
  if (systemUpdate.value.deployment_mode === "app-worker") {
    return "App + Worker";
  }
  if (systemUpdate.value.deployment_mode === "split") {
    return "前后端分离";
  }
  return "单容器";
});
const manualUpdateCommand = computed(() => {
  if (systemUpdate.value.deployment_mode === "app-worker") {
    return "docker compose pull makerhub-app makerhub-worker && docker compose up -d makerhub-app makerhub-worker";
  }
  if (systemUpdate.value.deployment_mode === "split") {
    return "docker compose pull makerhub-api makerhub-web && docker compose up -d makerhub-api makerhub-web";
  }
  return "docker compose pull makerhub && docker compose up -d makerhub";
});
const changelogEntries = computed(() => (
  Array.isArray(systemUpdate.value.github_changelog) ? systemUpdate.value.github_changelog : []
));
const changelogSummaryText = computed(() => {
  if (systemUpdate.value.github_changelog_error) {
    return `更新日志读取失败：${systemUpdate.value.github_changelog_error}`;
  }
  if (systemUpdate.value.github_changelog_checked_at) {
    return `线上 README 更新记录，最近检查时间 ${systemUpdate.value.github_changelog_checked_at}`;
  }
  return "会优先读取 GitHub 仓库 README 中的最新更新记录。";
});
const mobileImportEnabled = computed(() => Boolean(config.value?.mobile_import?.enabled));
const mobileImportTokenStatus = computed(() => {
  if (mobileImportEnabled.value) {
    return "已启用";
  }
  if (config.value?.mobile_import?.token_prefix) {
    return "已停用";
  }
  return "未生成";
});
const mobileImportTokenMeta = computed(() => {
  const mobileImport = config.value?.mobile_import || {};
  if (!mobileImport.token_prefix) {
    return "生成后只显示一次完整 Token。";
  }
  const used = mobileImport.last_used_at ? `最近使用：${mobileImport.last_used_at}` : "最近使用：未使用";
  return `${mobileImport.token_prefix}... / ${used}`;
});
const profileBackfillStats = computed(() => {
  const result = profileBackfill.value.last_result || {};
  return {
    scanned: Number(result.scanned_candidates || 0),
    queued: Number(result.queued_count || 0),
    alreadyQueued: Number(result.already_queued_count || 0),
    failed: Number(result.failed_count || 0),
  };
});
const profileBackfillStatusText = computed(() => {
  if (statuses.profile_backfill) {
    return statuses.profile_backfill;
  }
  if (profileBackfill.value.last_error) {
    return profileBackfill.value.last_error;
  }
  if (profileBackfill.value.running) {
    return "现有库信息补全正在后台扫描并入队，页面会自动刷新状态。";
  }
  const result = profileBackfill.value.last_result || {};
  if (profileBackfill.value.finished_at && Object.keys(result).length > 0) {
    return `扫描完成：发现 ${profileBackfillStats.value.scanned} 个缺信息模型，新增入队 ${profileBackfillStats.value.queued} 个，已在队列 ${profileBackfillStats.value.alreadyQueued} 个，失败 ${profileBackfillStats.value.failed} 个。`;
  }
  return profileBackfill.value.message || "这里只负责扫描并加入归档队列；实际补全会在后台继续执行，可到任务页查看进度。";
});

function defaultSystemUpdateState() {
  return {
    status: "idle",
    phase: "idle",
    message: "",
    request_id: "",
    requested_at: "",
    started_at: "",
    finished_at: "",
    requested_by: "",
    helper_container_id: "",
    replacement_container_id: "",
    container_name: "",
    image_ref: "",
    deployment_mode: "",
    web_container_name: "",
    web_image_ref: "",
    web_replacement_container_id: "",
    worker_container_name: "",
    worker_image_ref: "",
    worker_replacement_container_id: "",
    resources: {},
    target_version: "",
    current_version: "",
    supported: false,
    support_reason: "",
    docker_socket_mounted: false,
    github_changelog: [],
    github_changelog_checked_at: "",
    github_changelog_error: "",
    github_changelog_source: "",
  };
}

function defaultProfileBackfillState() {
  return {
    running: false,
    started_at: "",
    finished_at: "",
    last_error: "",
    last_result: {},
    message: "",
  };
}

function applySystemUpdateStatus(payload) {
  systemUpdate.value = {
    ...defaultSystemUpdateState(),
    ...(payload || {}),
  };
}

function applyProfileBackfillStatus(payload) {
  profileBackfill.value = {
    ...defaultProfileBackfillState(),
    ...(payload || {}),
  };
}

function normalizeDailyThreeMfLimit(value, fallback = 100) {
  if (value === "" || value === null || value === undefined) {
    return fallback;
  }
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return fallback;
  }
  return Math.max(0, Math.trunc(numeric));
}

function normalizeBoundedInt(value, fallback, min, max) {
  if (value === "" || value === null || value === undefined) {
    return fallback;
  }
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return fallback;
  }
  return Math.min(Math.max(Math.trunc(numeric), min), max);
}

function normalizeCpuText(value) {
  return String(value || "").trim();
}

function runtimePayload() {
  return {
    web_workers: normalizeBoundedInt(runtimeForm.web_workers, 1, 1, 8),
    app_cpu_limit: normalizeCpuText(runtimeForm.app_cpu_limit),
    app_cpuset_cpus: normalizeCpuText(runtimeForm.app_cpuset_cpus).replace(/\s+/g, ""),
    app_cpu_shares: normalizeBoundedInt(runtimeForm.app_cpu_shares, 1024, 0, 262144),
    worker_cpu_limit: normalizeCpuText(runtimeForm.worker_cpu_limit),
    worker_cpuset_cpus: normalizeCpuText(runtimeForm.worker_cpuset_cpus).replace(/\s+/g, ""),
    worker_cpu_shares: normalizeBoundedInt(runtimeForm.worker_cpu_shares, 512, 0, 262144),
  };
}

function formatShareFileCounts(counts) {
  const payload = counts && typeof counts === "object" ? counts : {};
  const total = Number(payload.total || 0);
  const model = Number(payload.model || 0);
  const image = Number(payload.image || 0);
  const attachment = Number(payload.attachment || 0);
  const parts = [`${total} 个文件`];
  if (model) {
    parts.push(`${model} 个模型文件`);
  }
  if (image) {
    parts.push(`${image} 张图片`);
  }
  if (attachment) {
    parts.push(`${attachment} 个附件`);
  }
  return parts.join(" · ");
}

function formatModelShareCounts(model) {
  const modelFiles = Number(model?.model_file_count || 0);
  const images = Number(model?.image_count || 0);
  const attachments = Number(model?.attachment_count || 0);
  const parts = [];
  if (modelFiles) {
    parts.push(`${modelFiles} 个模型文件`);
  }
  if (images) {
    parts.push(`${images} 张图片`);
  }
  if (attachments) {
    parts.push(`${attachments} 个附件`);
  }
  return parts.length ? parts.join(" · ") : `${Number(model?.file_count || 0)} 个文件`;
}

function formatShareDate(value) {
  if (!value) {
    return "-";
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return String(value);
  }
  return parsed.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function shareRecordTitle(item) {
  const models = Array.isArray(item?.models) ? item.models : [];
  const firstTitle = models[0]?.title || models[0]?.id || "";
  if (!firstTitle) {
    return item?.id ? `分享 ${item.id.slice(0, 8)}` : "未命名分享";
  }
  if (models.length <= 1) {
    return firstTitle;
  }
  return `${firstTitle} 等 ${models.length} 个模型`;
}

function shareRecordModelNames(item) {
  const models = Array.isArray(item?.models) ? item.models : [];
  if (!models.length) {
    return "没有模型信息";
  }
  return models.map((model) => model.title || model.id || "未命名模型").slice(0, 4).join("、")
    + (models.length > 4 ? ` 等 ${models.length} 个` : "");
}

function applyConfigToForms(payload) {
  const cookies = {};
  for (const item of payload.cookies || []) {
    cookies[item.platform] = item.cookie || "";
  }
  connectionForm.cookie_cn = cookies.cn || "";
  connectionForm.cookie_global = cookies.global || "";
  connectionForm.proxy_enabled = Boolean(payload.proxy?.enabled);
  connectionForm.http_proxy = payload.proxy?.http_proxy || "";
  connectionForm.https_proxy = payload.proxy?.https_proxy || "";
  threeMfLimitsForm.cn_daily_limit = normalizeDailyThreeMfLimit(payload.three_mf_limits?.cn_daily_limit);
  threeMfLimitsForm.global_daily_limit = normalizeDailyThreeMfLimit(payload.three_mf_limits?.global_daily_limit);
  advancedForm.remote_refresh_model_workers = normalizeBoundedInt(payload.advanced?.remote_refresh_model_workers, 2, 1, 4);
  advancedForm.makerworld_request_limit = normalizeBoundedInt(payload.advanced?.makerworld_request_limit, 2, 1, 8);
  advancedForm.comment_asset_download_limit = normalizeBoundedInt(payload.advanced?.comment_asset_download_limit, 4, 1, 16);
  advancedForm.three_mf_download_limit = normalizeBoundedInt(payload.advanced?.three_mf_download_limit, 1, 1, 4);
  advancedForm.disk_io_limit = normalizeBoundedInt(payload.advanced?.disk_io_limit, 1, 1, 4);
  runtimeForm.web_workers = normalizeBoundedInt(payload.runtime?.web_workers, 1, 1, 8);
  runtimeForm.app_cpu_limit = normalizeCpuText(payload.runtime?.app_cpu_limit);
  runtimeForm.app_cpuset_cpus = normalizeCpuText(payload.runtime?.app_cpuset_cpus);
  runtimeForm.app_cpu_shares = normalizeBoundedInt(payload.runtime?.app_cpu_shares, 1024, 0, 262144);
  runtimeForm.worker_cpu_limit = normalizeCpuText(payload.runtime?.worker_cpu_limit);
  runtimeForm.worker_cpuset_cpus = normalizeCpuText(payload.runtime?.worker_cpuset_cpus);
  runtimeForm.worker_cpu_shares = normalizeBoundedInt(payload.runtime?.worker_cpu_shares, 512, 0, 262144);
  organizerForm.source_dir = payload.organizer?.source_dir || "";
  organizerForm.target_dir = payload.organizer?.target_dir || "";
  organizerForm.move_files = payload.organizer?.move_files !== false;
  mobileImportToken.value = "";
  sharingForm.public_base_url = payload.sharing?.public_base_url || "";
  sharingForm.default_expires_days = normalizeBoundedInt(payload.sharing?.default_expires_days, 7, 1, 90);
  sharingForm.include_images = payload.sharing?.include_images !== false;
  sharingForm.include_model_files = payload.sharing?.include_model_files !== false;
  sharingForm.model_file_types = Array.isArray(payload.sharing?.model_file_types) && payload.sharing.model_file_types.length
    ? [...payload.sharing.model_file_types]
    : ["3mf", "stl", "step", "obj"];
  sharingForm.include_attachments = payload.sharing?.include_attachments !== false;
  sharingForm.attachment_file_types = Array.isArray(payload.sharing?.attachment_file_types) && payload.sharing.attachment_file_types.length
    ? [...payload.sharing.attachment_file_types]
    : ["pdf", "excel"];
  sharingForm.include_comments = payload.sharing?.include_comments !== false;

  notificationsForm.enabled = Boolean(payload.notifications?.enabled);
  notificationsForm.telegram_bot_token = payload.notifications?.telegram_bot_token || "";
  notificationsForm.telegram_chat_id = payload.notifications?.telegram_chat_id || "";
  notificationsForm.webhook_url = payload.notifications?.webhook_url || "";

  userForm.username = payload.user?.username || "admin";
  userForm.display_name = payload.user?.display_name || "Admin";
  userForm.password_hint = payload.user?.password_hint || "";
  themePreference.value = payload.user?.theme_preference || "auto";
  tokenItems.value = payload.api_tokens || [];
}

function setActiveTab(tab) {
  activeTab.value = tabs.some((item) => item.key === tab) ? tab : "system";
  if (route.query.tab !== activeTab.value) {
    router.replace({ path: "/settings", query: { tab: activeTab.value } });
  }
  if (activeTab.value === "system" && !systemUpdateLoading.value && !systemUpdateSubmitting.value) {
    loadSystemUpdateStatus({ force: true });
    loadProfileBackfillStatus({ silent: true });
  }
  if (activeTab.value === "sharing" && !sharedSharesLoading.value) {
    loadSharedShares({ silent: true });
  }
}

async function load() {
  const payload = config.value || await refreshConfig();
  applyConfigToForms(payload);
  setActiveTab(typeof route.query.tab === "string" ? route.query.tab : "system");
}

async function loadSharedShares(options = {}) {
  const { silent = false } = options;
  if (!silent) {
    sharedSharesLoading.value = true;
    statuses.share_manage = "";
  }
  try {
    const payload = await apiRequest("/api/sharing/shares");
    sharedShares.value = Array.isArray(payload.items) ? payload.items : [];
  } catch (error) {
    if (!silent) {
      statuses.share_manage = error instanceof Error ? error.message : "读取分享记录失败。";
    }
  } finally {
    if (!silent) {
      sharedSharesLoading.value = false;
    }
  }
}

function clearSystemUpdateTimer() {
  if (systemUpdateTimer) {
    window.clearTimeout(systemUpdateTimer);
    systemUpdateTimer = null;
  }
}

function scheduleSystemUpdatePolling() {
  clearSystemUpdateTimer();
  if (!systemUpdateActive.value) {
    return;
  }
  systemUpdateTimer = window.setTimeout(() => {
    loadSystemUpdateStatus({ silent: true });
  }, 3000);
}

function clearProfileBackfillTimer() {
  if (profileBackfillTimer) {
    window.clearTimeout(profileBackfillTimer);
    profileBackfillTimer = null;
  }
}

function clearTimers() {
  clearSystemUpdateTimer();
  clearProfileBackfillTimer();
}

function scheduleProfileBackfillPolling() {
  clearProfileBackfillTimer();
  if (!profileBackfill.value.running) {
    return;
  }
  profileBackfillTimer = window.setTimeout(() => {
    loadProfileBackfillStatus({ silent: true });
  }, 3000);
}

async function loadSystemUpdateStatus(options = {}) {
  const { silent = false, force = false } = options;
  const wasActive = systemUpdateActive.value;
  if (!silent) {
    systemUpdateLoading.value = true;
    statuses.system_update = "";
  }
  try {
    const query = force ? "?force=true" : "";
    const payload = await apiRequest(`/api/system/update${query}`);
    applySystemUpdateStatus(payload);
    if (typeof payload.github_latest_version === "string") {
      appState.githubLatestVersion = payload.github_latest_version;
    }
    if (typeof payload.github_version_checked_at === "string") {
      appState.githubVersionCheckedAt = payload.github_version_checked_at;
    }
    if (typeof payload.github_version_error === "string") {
      appState.githubVersionError = payload.github_version_error;
    }
    if (typeof payload.github_update_available === "boolean") {
      appState.githubUpdateAvailable = payload.github_update_available;
    }
    statuses.system_update = "";
    if (systemUpdate.value.status === "succeeded" && appState.appVersion !== systemUpdate.value.current_version) {
      await refreshConfig();
    }
  } catch (error) {
    if (wasActive) {
      statuses.system_update = "正在等待服务重启完成，页面恢复后会自动继续读取状态。";
    } else if (!silent) {
      statuses.system_update = error instanceof Error ? error.message : "读取更新状态失败。";
    }
  } finally {
    if (!silent) {
      systemUpdateLoading.value = false;
    }
    scheduleSystemUpdatePolling();
  }
}

async function triggerSystemUpdate() {
  return runSystemUpdate("这会拉取最新镜像并重建当前 MakerHub 容器，页面会短暂不可用。确定继续吗？");
}

async function runSystemUpdate(confirmMessage) {
  const shouldProceed = window.confirm(confirmMessage);
  if (!shouldProceed) {
    return;
  }
  systemUpdateSubmitting.value = true;
  statuses.system_update = "";
  try {
    const payload = await apiRequest("/api/system/update", {
      method: "POST",
      body: {
        target_version: appState.githubLatestVersion || "",
        force: !appState.githubUpdateAvailable,
      },
    });
    applySystemUpdateStatus(payload);
    statuses.system_update = payload.message || "更新任务已提交，服务即将短暂重启。";
  } catch (error) {
    statuses.system_update = error instanceof Error ? error.message : "提交更新任务失败。";
  } finally {
    systemUpdateSubmitting.value = false;
    scheduleSystemUpdatePolling();
  }
}

async function saveRuntimeResources() {
  runtimeResourcesSaving.value = true;
  statuses.runtime = "";
  try {
    const payload = await apiRequest("/api/config/runtime", {
      method: "POST",
      body: runtimePayload(),
    });
    applyConfigPayload(payload);
    applyConfigToForms(payload);
    statuses.runtime = "运行资源设置已保存，重启容器后生效。";
  } catch (error) {
    statuses.runtime = error instanceof Error ? error.message : "保存运行资源失败。";
  } finally {
    runtimeResourcesSaving.value = false;
  }
}

async function applyRuntimeResources() {
  await saveRuntimeResources();
  if (statuses.runtime && !statuses.runtime.includes("已保存")) {
    return;
  }
  await runSystemUpdate("这会按当前运行资源设置重建 App / Worker 容器，页面会短暂不可用。确定继续吗？");
}

async function loadProfileBackfillStatus(options = {}) {
  const { silent = false } = options;
  if (!silent) {
    profileBackfillLoading.value = true;
    statuses.profile_backfill = "";
  }
  try {
    const payload = await apiRequest("/api/admin/archive/profile-backfill");
    applyProfileBackfillStatus(payload);
  } catch (error) {
    if (!silent) {
      statuses.profile_backfill = error instanceof Error ? error.message : "读取信息补全状态失败。";
    }
  } finally {
    if (!silent) {
      profileBackfillLoading.value = false;
    }
    scheduleProfileBackfillPolling();
  }
}

async function triggerProfileBackfill() {
  const shouldProceed = window.confirm("会扫描本地归档库，并把缺少打印配置详情、实例展示媒体或评论回复字段的模型加入归档补整理队列。这里只负责扫描和入队，不会主动下载 3MF。确定继续吗？");
  if (!shouldProceed) {
    return;
  }
  profileBackfillSubmitting.value = true;
  statuses.profile_backfill = "";
  try {
    const payload = await apiRequest("/api/admin/archive/profile-backfill", {
      method: "POST",
    });
    applyProfileBackfillStatus(payload);
    statuses.profile_backfill = payload.message || "现有库信息补全扫描已提交，缺失模型会继续在归档队列后台处理。";
  } catch (error) {
    statuses.profile_backfill = error instanceof Error ? error.message : "提交信息补全失败。";
  } finally {
    profileBackfillSubmitting.value = false;
    scheduleProfileBackfillPolling();
  }
}

async function saveConnections() {
  try {
    await apiRequest("/api/config/cookies", {
      method: "POST",
      body: [
        { platform: "cn", cookie: connectionForm.cookie_cn },
        { platform: "global", cookie: connectionForm.cookie_global },
      ],
    });
    await apiRequest("/api/config/proxy", {
      method: "POST",
      body: {
        enabled: connectionForm.proxy_enabled,
        http_proxy: connectionForm.http_proxy,
        https_proxy: connectionForm.https_proxy,
      },
    });
    await refreshConfig();
    statuses.connections = "连接设置已保存。";
  } catch (error) {
    statuses.connections = error instanceof Error ? error.message : "保存失败。";
  }
}

async function saveAdvanced() {
  try {
    await apiRequest("/api/config/three-mf-limits", {
      method: "POST",
      body: {
        cn_daily_limit: normalizeDailyThreeMfLimit(threeMfLimitsForm.cn_daily_limit),
        global_daily_limit: normalizeDailyThreeMfLimit(threeMfLimitsForm.global_daily_limit),
      },
    });
    const payload = await apiRequest("/api/config/advanced", {
      method: "POST",
      body: {
        remote_refresh_model_workers: normalizeBoundedInt(advancedForm.remote_refresh_model_workers, 2, 1, 4),
        makerworld_request_limit: normalizeBoundedInt(advancedForm.makerworld_request_limit, 2, 1, 8),
        comment_asset_download_limit: normalizeBoundedInt(advancedForm.comment_asset_download_limit, 4, 1, 16),
        three_mf_download_limit: normalizeBoundedInt(advancedForm.three_mf_download_limit, 1, 1, 4),
        disk_io_limit: normalizeBoundedInt(advancedForm.disk_io_limit, 1, 1, 4),
      },
    });
    applyConfigPayload(payload);
    statuses.advanced = "高级设置已保存。";
  } catch (error) {
    statuses.advanced = error instanceof Error ? error.message : "保存失败。";
  }
}

async function saveOrganizer() {
  try {
    const payload = await apiRequest("/api/config/organizer", {
      method: "POST",
      body: { ...organizerForm },
    });
    applyConfigPayload(payload);
    statuses.organizer = "本地整理设置已保存。";
  } catch (error) {
    statuses.organizer = error instanceof Error ? error.message : "保存失败。";
  }
}

function buildShortcutConfigText() {
  const lines = [
    "MakerHub iOS 快捷指令配置",
    `Token: ${mobileImportToken.value || "<在 MakerHub 设置里生成后粘贴>"}`,
    "MakerHub 地址: <在手机快捷指令里填写，例如 http://192.168.1.20:1111 或 https://你的公网地址>",
    "",
    "流程: 从共享表单接收文件；先 GET MakerHub 地址 /api/mobile-import/ping-ipv4?token=Token；可用后 POST 文件到 /api/mobile-import/raw-ipv4?token=Token；上传成功提示 已上传。",
  ];
  return lines.join("\n");
}

async function copyText(value) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(value);
    return true;
  }
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.setAttribute("readonly", "readonly");
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  document.body.appendChild(textarea);
  textarea.select();
  textarea.setSelectionRange(0, value.length);
  const copied = document.execCommand("copy");
  document.body.removeChild(textarea);
  return copied;
}

async function copyShortcutConfig() {
  statuses.mobile_import = "";
  try {
    const copied = await copyText(buildShortcutConfigText());
    statuses.mobile_import = copied ? "快捷指令配置已复制。" : "浏览器阻止复制，请手动复制配置。";
  } catch (error) {
    statuses.mobile_import = error instanceof Error ? error.message : "复制失败。";
  }
}

async function resetMobileImportToken() {
  if (config.value?.mobile_import?.token_prefix && !window.confirm("生成新 Token 会让旧快捷指令失效。确定继续吗？")) {
    return;
  }
  statuses.mobile_import = "";
  try {
    const payload = await apiRequest("/api/config/mobile-import/token", {
      method: "POST",
      body: { enabled: true },
    });
    mobileImportToken.value = payload.token || "";
    applyConfigPayload(payload);
    statuses.mobile_import = "Token 已生成。请把新 Token 填入 iOS 快捷指令。";
  } catch (error) {
    statuses.mobile_import = error instanceof Error ? error.message : "生成 Token 失败。";
  }
}

async function disableMobileImport() {
  if (!window.confirm("停用后，手机快捷指令将无法继续上传文件。确定停用吗？")) {
    return;
  }
  statuses.mobile_import = "";
  try {
    const payload = await apiRequest("/api/config/mobile-import/disable", {
      method: "POST",
    });
    mobileImportToken.value = "";
    applyConfigPayload(payload);
    statuses.mobile_import = "移动端导入 Token 已停用。";
  } catch (error) {
    statuses.mobile_import = error instanceof Error ? error.message : "停用失败。";
  }
}

async function saveSharing() {
  try {
    const payload = await apiRequest("/api/config/sharing", {
      method: "POST",
      body: { ...sharingForm },
    });
    applyConfigPayload(payload);
    statuses.sharing = "分享设置已保存。";
  } catch (error) {
    statuses.sharing = error instanceof Error ? error.message : "保存失败。";
  }
}

function buildProxyPayload() {
  return {
    enabled: connectionForm.proxy_enabled,
    http_proxy: connectionForm.http_proxy,
    https_proxy: connectionForm.https_proxy,
  };
}

async function testCookie(platform) {
  const key = platform === "global" ? "cookie_global" : "cookie_cn";
  const cookie = platform === "global" ? connectionForm.cookie_global : connectionForm.cookie_cn;
  testing[key] = true;
  statuses[key] = "";
  try {
    const response = await apiRequest("/api/config/cookies/test", {
      method: "POST",
      body: {
        platform,
        cookie,
        proxy: buildProxyPayload(),
      },
    });
    statuses[key] = response.message || "测试完成。";
  } catch (error) {
    statuses[key] = error instanceof Error ? error.message : "测试失败。";
  } finally {
    testing[key] = false;
  }
}

async function testProxy() {
  testing.proxy = true;
  statuses.proxy = "";
  try {
    const response = await apiRequest("/api/config/proxy/test", {
      method: "POST",
      body: buildProxyPayload(),
    });
    statuses.proxy = response.message || "测试完成。";
  } catch (error) {
    statuses.proxy = error instanceof Error ? error.message : "测试失败。";
  } finally {
    testing.proxy = false;
  }
}

async function testSharing() {
  testing.sharing = true;
  statuses.sharing_test = "";
  try {
    const response = await apiRequest("/api/config/sharing/test", {
      method: "POST",
      body: { ...sharingForm },
    });
    statuses.sharing_test = response.message || "公开访问地址可用。";
  } catch (error) {
    statuses.sharing_test = error instanceof Error ? error.message : "检测失败。";
  } finally {
    testing.sharing = false;
  }
}

async function previewShareCode() {
  if (!shareReceive.code) {
    statuses.share_receive = "请先粘贴分享码。";
    return;
  }
  shareReceive.loadingPreview = true;
  shareReceive.preview = null;
  statuses.share_receive = "";
  try {
    const response = await apiRequest("/api/sharing/receive/preview", {
      method: "POST",
      body: { share_code: shareReceive.code },
    });
    shareReceive.preview = response;
    statuses.share_receive = response.message || "分享预览完成。";
  } catch (error) {
    statuses.share_receive = error instanceof Error ? error.message : "分享预览失败。";
  } finally {
    shareReceive.loadingPreview = false;
  }
}

async function importShareCode() {
  if (!shareReceive.preview?.can_import) {
    statuses.share_receive = "分享中存在重复模型，不能导入。";
    return;
  }
  shareReceive.importing = true;
  statuses.share_receive = "";
  try {
    const response = await apiRequest("/api/sharing/receive/import", {
      method: "POST",
      body: { share_code: shareReceive.code },
    });
    statuses.share_receive = response.message || "分享已导入。";
    shareReceive.preview = null;
    shareReceive.code = "";
  } catch (error) {
    statuses.share_receive = error instanceof Error ? error.message : "分享导入失败。";
  } finally {
    shareReceive.importing = false;
  }
}

async function copyManagedShareCode(item) {
  if (!item?.id) {
    return;
  }
  sharedShareCopyingId.value = item.id;
  statuses.share_manage = "";
  let shareCode = item.share_code || "";
  try {
    if (!shareCode) {
      const response = await apiRequest(`/api/sharing/shares/${encodeURIComponent(item.id)}/code`, {
        method: "POST",
      });
      shareCode = response.share_code || "";
      if (shareCode) {
        item.share_code = shareCode;
      }
    }
    if (!shareCode) {
      statuses.share_manage = "分享码生成失败。";
      return;
    }
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(shareCode);
      statuses.share_manage = "分享码已复制。";
      return;
    }
    const textarea = document.createElement("textarea");
    textarea.value = shareCode;
    textarea.setAttribute("readonly", "readonly");
    textarea.style.position = "fixed";
    textarea.style.left = "-9999px";
    document.body.appendChild(textarea);
    textarea.select();
    textarea.setSelectionRange(0, shareCode.length);
    const copied = document.execCommand("copy");
    document.body.removeChild(textarea);
    statuses.share_manage = copied ? "分享码已复制。" : "浏览器阻止了复制，请打开分享弹窗重新生成后手动复制。";
  } catch (error) {
    statuses.share_manage = error instanceof Error ? error.message : "复制分享码失败。";
  } finally {
    sharedShareCopyingId.value = "";
  }
}

async function revokeManagedShare(item) {
  if (!item?.id) {
    return;
  }
  if (!window.confirm(`确认撤销“${shareRecordTitle(item)}”这个分享吗？撤销后对方将不能再通过这个分享码导入。`)) {
    return;
  }
  sharedShareRevokingId.value = item.id;
  statuses.share_manage = "";
  try {
    await apiRequest(`/api/sharing/shares/${encodeURIComponent(item.id)}`, {
      method: "DELETE",
    });
    statuses.share_manage = "分享已撤销。";
    await loadSharedShares({ silent: true });
  } catch (error) {
    statuses.share_manage = error instanceof Error ? error.message : "撤销分享失败。";
  } finally {
    sharedShareRevokingId.value = "";
  }
}

async function cleanupExpiredShares() {
  if (!expiredShareCount.value) {
    statuses.share_manage = "当前没有过期分享。";
    return;
  }
  if (!window.confirm(`确认清理 ${expiredShareCount.value} 条过期分享记录吗？`)) {
    return;
  }
  sharedSharesLoading.value = true;
  statuses.share_manage = "";
  try {
    const payload = await apiRequest("/api/sharing/shares/cleanup", {
      method: "POST",
      body: { include_expired: true },
    });
    statuses.share_manage = payload.message || "过期分享已清理。";
    await loadSharedShares({ silent: true });
  } catch (error) {
    statuses.share_manage = error instanceof Error ? error.message : "清理过期分享失败。";
  } finally {
    sharedSharesLoading.value = false;
  }
}

async function saveNotifications() {
  try {
    const payload = await apiRequest("/api/config/notifications", {
      method: "POST",
      body: { ...notificationsForm },
    });
    applyConfigPayload(payload);
    statuses.notifications = "通知设置已保存。";
  } catch (error) {
    statuses.notifications = error instanceof Error ? error.message : "保存失败。";
  }
}

async function saveUser() {
  try {
    const payload = await apiRequest("/api/config/user", {
      method: "POST",
      body: { ...userForm },
    });
    applyConfigPayload(payload);
    statuses.user = "用户信息已保存。";
  } catch (error) {
    statuses.user = error instanceof Error ? error.message : "保存失败。";
  }
}

async function saveTheme() {
  try {
    await saveThemePreference(themePreference.value);
    statuses.theme = "主题设置已保存。";
  } catch (error) {
    statuses.theme = error instanceof Error ? error.message : "保存失败。";
  }
}

async function savePassword() {
  if (passwordForm.new_password !== passwordForm.confirm_password) {
    statuses.password = "两次输入的新密码不一致。";
    return;
  }
  try {
    await apiRequest("/api/auth/password", {
      method: "POST",
      body: {
        current_password: passwordForm.current_password,
        new_password: passwordForm.new_password,
      },
    });
    statuses.password = "密码已更新，请重新登录。";
    setTimeout(() => {
      window.location.assign("/login");
    }, 500);
  } catch (error) {
    statuses.password = error instanceof Error ? error.message : "更新失败。";
  }
}

async function createToken() {
  try {
    const response = await apiRequest("/api/auth/tokens", {
      method: "POST",
      body: { name: tokenName.value },
    });
    newToken.value = response.token || "";
    tokenItems.value = response.items || [];
    tokenName.value = "";
    statuses.tokens = "Token 已生成。";
  } catch (error) {
    statuses.tokens = error instanceof Error ? error.message : "生成失败。";
  }
}

async function revokeToken(tokenId) {
  try {
    const response = await apiRequest(`/api/auth/tokens/${encodeURIComponent(tokenId)}`, {
      method: "DELETE",
    });
    tokenItems.value = response.items || [];
    statuses.tokens = "Token 已撤销。";
  } catch (error) {
    statuses.tokens = error instanceof Error ? error.message : "撤销失败。";
  }
}

watch(() => route.query.tab, (value) => {
  setActiveTab(typeof value === "string" ? value : "system");
});

onMounted(load);
onBeforeUnmount(clearTimers);
</script>
