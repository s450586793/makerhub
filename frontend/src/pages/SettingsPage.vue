<template>
  <section class="surface surface--filters page-intro settings-page-intro app-page-toolbar">
    <div class="app-page-toolbar__copy">
      <span class="eyebrow">设置</span>
      <div class="app-page-toolbar__title-row">
        <h1>系统、本地整理与用户配置</h1>
      </div>
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

    <div v-show="activeTab === 'accounts'" class="settings-panel is-active">
      <section class="settings-form token-card online-account-card">
        <div class="section-card__header">
          <div>
            <span class="eyebrow">线上账号</span>
            <h2>平台账号</h2>
          </div>
          <button class="button button-primary button-small" type="button" @click="openAccountDialog">
            添加账号
          </button>
        </div>

        <div v-if="onlineAccountItems.length" class="online-account-list">
          <article v-for="item in onlineAccountItems" :key="item.platform" class="online-account-item">
            <div class="online-account-item__avatar">
              <img v-if="item.avatar_url" :src="item.avatar_url" :alt="item.displayName">
              <span v-else>{{ item.avatarFallback }}</span>
            </div>
            <div class="online-account-item__main">
              <div class="online-account-item__title">
                <strong>{{ item.displayName }}</strong>
                <span :class="['shared-list-item__status', item.statusClass]">{{ item.statusLabel }}</span>
              </div>
              <div class="online-account-item__meta">
                <span>{{ item.platformLabel }}</span>
                <span v-if="item.handle">@{{ item.handle }}</span>
                <span v-if="item.username" class="online-account-item__phone">{{ item.username }}</span>
                <span>{{ item.updatedText }}</span>
              </div>
              <div class="online-account-item__stats">
                <span>关注作者 <strong>{{ item.followedAuthorCountText }}</strong></span>
                <span>关注收藏夹 <strong>{{ item.followedCollectionCountText }}</strong></span>
                <span>默认收藏夹 <strong>{{ item.defaultFavoritesCountText }}</strong></span>
              </div>
              <p>{{ item.message }}</p>
              <p v-if="item.sourceSyncText" class="online-account-item__sync-note">{{ item.sourceSyncText }}</p>
            </div>
            <div class="online-account-item__actions">
              <button
                class="button button-secondary button-small"
                type="button"
                :disabled="testing[`cookie_${item.platform}`]"
                @click="testSavedAccount(item.platform)"
              >
                {{ testing[`cookie_${item.platform}`] ? "测试中..." : "测试" }}
              </button>
              <button
                class="button button-secondary button-small"
                type="button"
                :disabled="testing[`sync_${item.platform}`]"
                @click="syncSavedAccount(item.platform)"
              >
                {{ testing[`sync_${item.platform}`] ? "同步中..." : "同步" }}
              </button>
              <button class="button button-secondary button-small" type="button" @click="openAccountDialog(item.platform)">
                重新登录
              </button>
              <button class="button button-danger button-small" type="button" @click="deleteAccount(item.platform)">
                删除
              </button>
            </div>
          </article>
        </div>
        <p v-else class="empty-copy">当前还没有线上账号。</p>
        <span v-if="statuses.accounts" class="form-status">{{ statuses.accounts }}</span>
      </section>

      <div
        v-if="accountDialogOpen"
        class="submit-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="account-create-dialog-title"
        @click="closeAccountDialog"
      >
        <div class="submit-dialog__panel token-create-dialog__panel" @click.stop>
          <h2 id="account-create-dialog-title">添加线上账号</h2>
          <form class="token-create-dialog__form" @submit.prevent="submitAccountLogin">
            <label class="field-card">
              <span>平台</span>
              <select v-model="accountDialog.platform">
                <option value="cn">MakerWorld 国区</option>
                <option value="global">MakerWorld 国际</option>
              </select>
            </label>
            <label class="field-card">
              <span>{{ accountLoginLabel }}</span>
              <input
                v-model.trim="accountDialog.username"
                :type="accountLoginInputType"
                :autocomplete="accountLoginAutocomplete"
                :inputmode="accountLoginInputMode"
                :placeholder="accountLoginPlaceholder"
              >
            </label>
            <label class="field-card">
              <span>验证码</span>
              <div class="online-account-code-field">
                <input v-model.trim="accountDialog.verification_code" type="text" autocomplete="one-time-code" inputmode="numeric" maxlength="8" :placeholder="accountCodePlaceholder">
                <button
                  class="button button-secondary button-small"
                  type="button"
                  :disabled="accountDialog.saving || accountDialog.sendingCode || accountDialog.codeCountdown > 0"
                  @click="sendAccountSmsCode"
                >
                  {{ accountDialog.codeCountdown > 0 ? `${accountDialog.codeCountdown}s` : (accountDialog.sendingCode ? "发送中..." : "发送验证码") }}
                </button>
              </div>
            </label>
            <label class="online-account-consent">
              <input v-model="accountDialog.consent" type="checkbox">
              <span>我已阅读并同意用户协议和隐私政策。</span>
            </label>
            <p class="archive-form__hint">{{ accountLoginHint }}</p>
            <p v-if="accountDialog.error" class="form-status is-error">{{ accountDialog.error }}</p>
            <div class="submit-dialog__actions">
              <button class="button button-secondary" type="button" :disabled="accountDialog.saving" @click="closeAccountDialog">取消</button>
              <button class="button button-primary" type="submit" :disabled="accountDialog.saving">
                {{ accountDialog.saving ? "登录中..." : "确定" }}
              </button>
            </div>
          </form>
        </div>
      </div>
    </div>

    <div v-show="activeTab === 'proxy'" class="settings-panel is-active">
      <form class="settings-form token-card" @submit.prevent="saveProxySettings">
        <div class="section-card__header">
          <div>
            <span class="eyebrow">代理</span>
            <h2>HTTP 代理</h2>
          </div>
        </div>
        <div class="settings-grid settings-grid--three">
          <label class="field-card">
            <span>启用 HTTP 代理</span>
            <button
              :class="['subscription-switch', proxyForm.enabled && 'is-on']"
              type="button"
              :disabled="testing.proxy || proxySaving"
              @click="proxyForm.enabled = !proxyForm.enabled"
            >
              <span class="subscription-switch__track" aria-hidden="true">
                <span class="subscription-switch__thumb"></span>
              </span>
              <span class="subscription-switch__label">{{ proxyForm.enabled ? "启用中" : "已停用" }}</span>
            </button>
            <small class="archive-form__hint">开启后归档、订阅与源端刷新请求会带上当前代理设置；国内站会按代理策略直连。</small>
          </label>
          <label class="field-card">
            <span>HTTP Proxy</span>
            <input v-model="proxyForm.http_proxy" type="text" placeholder="http://127.0.0.1:7890">
          </label>
          <label class="field-card">
            <span>HTTPS Proxy</span>
            <input v-model="proxyForm.https_proxy" type="text" placeholder="http://127.0.0.1:7890">
          </label>
        </div>
        <div class="settings-inline-actions">
          <button class="button button-secondary" type="button" :disabled="testing.proxy" @click="testProxy">
            {{ testing.proxy ? "测试中..." : "测试 HTTP 代理" }}
          </button>
          <span class="form-status">{{ statuses.proxy }}</span>
        </div>
        <div class="form-footer">
          <button class="button button-primary" type="submit" :disabled="proxySaving">
            {{ proxySaving ? "保存中..." : "保存代理设置" }}
          </button>
          <span class="form-status">{{ statuses.proxy_save }}</span>
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
            <input v-model="organizerForm.source_dir" type="text" placeholder="/app/data/local">
            <small class="archive-form__hint">Worker 会从这里扫描候选 3MF 文件。</small>
          </label>
          <label class="field-card">
            <span>整理目标目录</span>
            <input v-model="organizerForm.target_dir" type="text" placeholder="/app/data/archive">
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
    </div>

    <div v-show="activeTab === 'tokens'" class="settings-panel is-active">
      <section class="settings-form token-card">
        <div class="section-card__header">
          <div>
            <span class="eyebrow">Token</span>
            <h2>访问凭证管理</h2>
          </div>
          <div class="settings-inline-actions">
            <button class="button button-secondary button-small" type="button" @click="copyShortcutConfig">
              复制快捷指令配置
            </button>
            <button class="button button-primary button-small" type="button" @click="openTokenDialog">
              生成 Token
            </button>
          </div>
        </div>

        <div class="token-table-wrap">
          <table class="token-table">
            <thead>
              <tr>
                <th>名称</th>
                <th>过期时间</th>
                <th>权限</th>
                <th>Token 字符</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in tokenItems" :key="item.id" :class="['token-table__row', tokenStatusClass(item)]">
                <td>
                  <div class="token-table__name">
                    <strong>{{ item.name }}</strong>
                    <span :class="['shared-list-item__status', tokenStatusClass(item)]">{{ tokenStatusLabel(item) }}</span>
                  </div>
                </td>
                <td>{{ formatTokenDate(item.expires_at) }}</td>
                <td>
                  <div class="token-permission-tags">
                    <span v-for="permission in tokenPermissionLabels(item.permissions)" :key="permission">{{ permission }}</span>
                  </div>
                </td>
                <td>
                  <code class="token-table__value">{{ item.token_value || `${item.token_prefix}...` }}</code>
                </td>
                <td>
                  <div class="token-table__actions">
                    <button class="button button-secondary button-small" type="button" :disabled="!item.token_value" @click="copyTokenValue(item)">
                      复制
                    </button>
                    <button class="button button-secondary button-small" type="button" :disabled="!item.token_value || !hasTokenPermission(item, 'mobile_import')" @click="copyShortcutConfig(item)">
                      快捷指令
                    </button>
                    <button class="button button-danger button-small" type="button" :disabled="item.status === 'revoked'" @click="revokeToken(item.id)">
                      撤销
                    </button>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
          <p v-if="!tokenItems.length" class="empty-copy">当前还没有 Token。</p>
        </div>

        <div class="mobile-import-shortcut">
          <strong>iOS 快捷指令流程</strong>
          <ol>
            <li>创建带“本地导入”权限的 Token。</li>
            <li>在手机快捷指令里填写 Token 和 MakerHub 地址。</li>
            <li>用 Token 请求选定地址的 <code>/api/mobile-import/ping-ipv4</code>。</li>
            <li>地址可用后，把文件上传到 <code>/api/mobile-import/raw-ipv4</code>。</li>
            <li>上传成功提示“已上传”，地址不可用时 iOS 会显示请求失败。</li>
          </ol>
        </div>

        <span class="form-status">{{ statuses.tokens }}</span>
      </section>

      <div
        v-if="tokenDialogOpen"
        class="submit-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="token-create-dialog-title"
        @click="closeTokenDialog"
      >
        <div class="submit-dialog__panel token-create-dialog__panel" @click.stop>
          <h2 id="token-create-dialog-title">生成 Token</h2>
          <form class="token-create-dialog__form" @submit.prevent="createToken">
            <label class="field-card">
              <span>名称</span>
              <input v-model.trim="tokenForm.name" type="text" placeholder="例如：我的 iPhone / 自动化脚本">
            </label>
            <label class="field-card">
              <span>过期时间</span>
              <select v-model.number="tokenForm.expires_days">
                <option :value="7">7 天</option>
                <option :value="30">30 天</option>
                <option :value="90">90 天</option>
                <option :value="365">365 天</option>
                <option :value="0">永不过期</option>
              </select>
            </label>
            <label class="field-card">
              <span>权限</span>
              <div class="token-permission-grid">
                <label v-for="permission in tokenPermissionOptions" :key="permission.value" class="switch">
                  <input v-model="tokenForm.permissions" type="checkbox" :value="permission.value">
                  <span>{{ permission.label }}</span>
                </label>
              </div>
            </label>
            <div class="submit-dialog__actions">
              <button class="button button-secondary" type="button" @click="closeTokenDialog">取消</button>
              <button class="button button-primary" type="submit">生成 Token</button>
            </div>
          </form>
        </div>
      </div>
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
            <small class="archive-form__hint">这是 MakerHub 自动下载保护额度，不是 MakerWorld 账号手动下载限制。默认 100，填 0 表示不限制；达到后国区缺失 3MF 会暂停到次日 00:00。</small>
          </label>
          <label class="field-card">
            <span>国际区每日 3MF 下载上限</span>
            <input v-model.number="threeMfLimitsForm.global_daily_limit" type="number" min="0" step="1">
            <small class="archive-form__hint">这是 MakerHub 自动下载保护额度，不是 MakerWorld 账号手动下载限制。默认 100，填 0 表示不限制；达到后国际区缺失 3MF 会暂停到次日 00:00。</small>
          </label>
        </div>

        <div class="settings-grid settings-grid--two">
          <label class="field-card">
            <span>抓取模式</span>
            <select v-model="advancedForm.scraping_engine">
              <option value="scrapling_first">自动模式</option>
              <option value="legacy">兼容模式</option>
              <option value="scrapling_only">增强模式</option>
            </select>
            <small class="archive-form__hint">推荐自动模式。源站接口不稳定或返回异常时，会自动换一种方式重试。</small>
          </label>
          <label class="field-card field-card--switch">
            <span>验证页自动处理</span>
            <label class="switch"><input v-model="advancedForm.scrapling_browser_fallback" type="checkbox"><span>需要时启用隐身浏览器</span></label>
            <small class="archive-form__hint">遇到验证页、登录页或接口返回异常时再启用，适合归档、源端刷新和评论补全。</small>
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

      <form class="settings-form token-card system-runtime-card" @submit.prevent="saveRuntimeResources">
        <div class="section-card__header">
          <div>
            <span class="eyebrow">容器进程</span>
            <h2>App / Worker 数量</h2>
          </div>
          <div class="settings-inline-actions">
            <button class="button button-secondary" type="submit" :disabled="runtimeResourcesSaving">
              {{ runtimeResourcesSaving ? "保存中..." : "保存进程设置" }}
            </button>
            <button class="button button-primary" type="button" :disabled="!canTriggerSystemUpdate || runtimeResourcesSaving" @click="applyRuntimeResources">
              应用并重启容器
            </button>
          </div>
        </div>
        <p class="archive-form__hint">
          App Web 进程数负责页面和 API 响应；Worker 并发数控制后台归档、源端刷新、下载和磁盘任务的并行强度。保存后需要重启容器生效。
        </p>
        <div class="settings-grid settings-grid--three system-update-grid">
          <label class="field-card">
            <span>App Web 进程数</span>
            <input v-model.number="runtimeForm.web_workers" type="number" min="1" max="8" step="1">
            <small class="archive-form__hint">当前容器：{{ currentAppWebWorkers }}；NAS 多核心建议 2-4。</small>
          </label>
          <label class="field-card">
            <span>Worker 并发数</span>
            <input v-model.number="runtimeForm.worker_concurrency" type="number" min="1" max="4" step="1">
            <small class="archive-form__hint">当前容器：{{ currentWorkerConcurrency }}；建议 2，任务多时可设 3-4。</small>
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
            <strong>{{ systemUpdate.compose_migration_required ? "需改 compose" : systemUpdate.supported ? "已启用" : "未启用" }}</strong>
          </article>
        </div>

        <div class="field-card system-update-manual">
          <span>{{ systemUpdate.compose_migration_required ? "需要先修改 compose" : systemUpdate.supported ? "执行说明" : "如何启用一键更新" }}</span>
          <template v-if="systemUpdate.compose_migration_required">
            <p>{{ systemUpdate.compose_migration_reason || systemUpdate.support_reason }}</p>
            <pre class="system-update-code system-update-code--pre">{{ systemUpdate.compose_example }}</pre>
          </template>
          <p v-if="systemUpdate.supported">
            更新会复用当前容器名称、挂载、端口和重启策略。App + Worker 部署下会先更新后台 Worker，再更新 App 容器；页面短暂报错通常只是容器正在重启。
          </p>
          <p v-else-if="!systemUpdate.compose_migration_required">
            首次仍需要手动在部署里挂载 <code>/var/run/docker.sock:/var/run/docker.sock</code>。启用后，这个页面才能直接拉取新镜像并重建容器。
          </p>
          <code v-if="!systemUpdate.compose_migration_required" class="system-update-code">{{ manualUpdateCommand }}</code>
        </div>

        <section class="system-update-changelog system-maintenance-card">
          <div class="section-card__header">
            <div>
              <span class="eyebrow">系统维护</span>
              <h2>数据库索引与历史信息补全</h2>
            </div>
            <div class="settings-inline-actions">
              <button class="button button-secondary" type="button" :disabled="profileBackfillLoading || profileBackfillSubmitting" @click="loadProfileBackfillStatus">
                {{ profileBackfillLoading ? "读取中..." : "刷新状态" }}
              </button>
              <button class="button button-primary" type="button" :disabled="profileBackfillSubmitting || profileBackfill.running" @click="triggerProfileBackfill">
                {{ profileBackfill.running ? profileBackfillRunningLabel : profileBackfillSubmitting ? "提交中..." : "手动重建索引" }}
              </button>
            </div>
          </div>
          <p class="archive-form__hint">
            首次连接新数据库时会自动遍历已有本地归档库，把卡片级模型信息写入 Postgres 索引，用于加快模型库、订阅库和本地库读取。索引已完成后无需再处理；这里仅保留手动重建和历史缺失信息补全入口。
          </p>
          <div class="settings-grid settings-grid--four system-update-grid">
            <article class="field-card system-update-stat">
              <span>数据库索引</span>
              <strong>{{ profileBackfillDatabaseLabel }}</strong>
              <small>{{ profileBackfillDatabaseHint }}</small>
            </article>
            <article class="field-card system-update-stat">
              <span>索引进度</span>
              <strong>{{ profileBackfillStats.databaseProcessed }}/{{ profileBackfillStats.databaseTotal }}</strong>
              <small>{{ profileBackfillStats.databaseFailed || profileBackfillStats.jsonStateFailed ? `失败：${profileBackfillStats.databaseFailed + profileBackfillStats.jsonStateFailed}` : profileBackfillDatabaseProgressHint }}</small>
            </article>
            <article class="field-card system-update-stat">
              <span>缺失补全</span>
              <strong>{{ profileBackfillStats.queued }}</strong>
              <small>发现缺失：{{ profileBackfillStats.scanned }}，已在队列：{{ profileBackfillStats.alreadyQueued }}</small>
            </article>
            <article class="field-card system-update-stat">
              <span>失败</span>
              <strong>{{ profileBackfillStats.failed }}</strong>
              <small>{{ profileBackfill.running ? profileBackfillRunningLabel : profileBackfill.finished_at ? `最近结束：${profileBackfill.finished_at}` : "等待执行" }}</small>
            </article>
          </div>
          <div class="form-footer">
            <span class="form-status">{{ profileBackfillStatusText }}</span>
          </div>
        </section>

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

    </div>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { RouterLink, useRoute, useRouter } from "vue-router";

import ThemeSegment from "../components/ThemeSegment.vue";
import { appState, applyConfigPayload, refreshConfig, refreshLightConfig, saveThemePreference } from "../lib/appState";
import { apiRequest } from "../lib/api";
import {
  buildAdvancedPayload,
  buildProxyPayload,
  buildRuntimePayload,
  buildSharingPayload,
  buildThreeMfLimitsPayload,
  normalizeBoundedInt,
  normalizeDailyThreeMfLimit,
} from "../lib/settingsPayloads";
import { createPagePerformanceTracker } from "../lib/performance";
import { createPageRefreshController } from "../lib/usePageRefresh";


const route = useRoute();
const router = useRouter();

const tabs = [
  { key: "system", label: "系统" },
  { key: "accounts", label: "线上账号" },
  { key: "proxy", label: "代理" },
  { key: "organizer", label: "本地整理" },
  { key: "tokens", label: "Token" },
  { key: "sharing", label: "模型分享" },
  { key: "advanced", label: "高级" },
  { key: "user", label: "用户" },
  { key: "notifications", label: "通知" },
];

const activeTab = ref("system");
const themePreference = ref("auto");
const tokenItems = ref([]);
const mobileImportToken = ref("");
const tokenDialogOpen = ref(false);
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
const proxySaving = ref(false);
const accountDialogOpen = ref(false);
let accountCodeTimer = null;
let settingsRefreshController = null;

const proxyForm = reactive({
  enabled: false,
  http_proxy: "",
  https_proxy: "",
});
const accountDialog = reactive({
  platform: "cn",
  username: "",
  password: "",
  verification_code: "",
  consent: false,
  sendingCode: false,
  codeCountdown: 0,
  saving: false,
  error: "",
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
  scraping_engine: "scrapling_first",
  scrapling_browser_fallback: true,
  remote_refresh_model_workers: 2,
  makerworld_request_limit: 2,
  comment_asset_download_limit: 4,
  three_mf_download_limit: 1,
  disk_io_limit: 1,
});
const tokenPermissionOptions = [
  { value: "mobile_import", label: "移动端/本地导入" },
  { value: "archive_write", label: "提交归档任务" },
  { value: "models_read", label: "查看模型库" },
];
const tokenPermissionLabelsMap = [
  ...tokenPermissionOptions,
  { value: "share_manage", label: "接收/管理分享" },
  { value: "system_manage", label: "系统管理" },
  { value: "token_manage", label: "Token 管理" },
];
const tokenForm = reactive({
  name: "我的 iPhone",
  expires_days: 365,
  permissions: ["mobile_import"],
});
const runtimeForm = reactive({
  web_workers: 1,
  worker_concurrency: 2,
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
  accounts: "",
  proxy: "",
  proxy_save: "",
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
const currentWorkerConcurrency = computed(() => (
  normalizeBoundedInt(systemUpdate.value.resources?.worker?.worker_concurrency, 2, 1, 4)
));
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
const onlineAccountItems = computed(() => {
  const cookies = Array.isArray(config.value?.cookies) ? config.value.cookies : [];
  const inventoryByPlatform = config.value?.cookie_source_inventory?.platforms || {};
  const syncStateByPlatform = config.value?.cookie_source_sync_state || {};
  return cookies
    .filter((item) => ["cn", "global"].includes(item.platform) && hasAccountCookie(item))
    .map((item) => {
      const inventoryAccount = inventoryByPlatform[item.platform]?.account || {};
      const mergedItem = {
        ...item,
        display_name: item.display_name || inventoryAccount.name || "",
        account_id: item.account_id || inventoryAccount.uid || "",
        handle: item.handle || inventoryAccount.handle || "",
        avatar_url: item.avatar_url || inventoryAccount.avatar_url || "",
      };
      const displayName = accountDisplayName(mergedItem);
      const status = String(item.status || "").trim();
      const sourceStats = accountSourceStats(
        inventoryByPlatform[item.platform],
        syncStateByPlatform[item.platform],
      );
      return {
        ...mergedItem,
        displayName,
        avatarFallback: accountAvatarFallback(mergedItem, displayName),
        platformLabel: accountPlatformLabel(item.platform),
        statusLabel: accountStatusLabel(mergedItem),
        statusClass: accountStatusClass(mergedItem),
        updatedText: formatAccountDate(item.updated_at || item.last_login_at || item.last_tested_at),
        message: accountMessageText(mergedItem, status),
        ...sourceStats,
      };
    });
});
const isGlobalAccountDialog = computed(() => accountDialog.platform === "global");
const accountLoginLabel = computed(() => isGlobalAccountDialog.value ? "邮箱" : "手机号");
const accountLoginInputType = computed(() => isGlobalAccountDialog.value ? "email" : "tel");
const accountLoginAutocomplete = computed(() => isGlobalAccountDialog.value ? "email" : "tel");
const accountLoginInputMode = computed(() => isGlobalAccountDialog.value ? "email" : "tel");
const accountLoginPlaceholder = computed(() => isGlobalAccountDialog.value ? "请输入邮箱地址" : "请输入手机号");
const accountCodePlaceholder = computed(() => isGlobalAccountDialog.value ? "邮箱验证码" : "短信验证码");
const accountLoginHint = computed(() => isGlobalAccountDialog.value
  ? "提交后会使用邮箱和邮箱验证码登录 MakerWorld 国际站，并自动保存返回的 Cookie。"
  : "提交后会使用手机号和短信验证码登录 MakerWorld 国区，并自动保存返回的 Cookie。");
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
const mobileImportTokenItem = computed(() => (
  tokenItems.value.find((item) => hasTokenPermission(item, "mobile_import") && item.status === "active" && item.token_value) || null
));
const profileBackfillStats = computed(() => {
  const result = profileBackfill.value.last_result || {};
  const databaseIndex = result.database_index || {};
  const jsonState = databaseIndex.json_state || {};
  return {
    jsonStateProcessed: Number(jsonState.processed || 0),
    jsonStateUpdated: Number(jsonState.updated || 0),
    jsonStateFailed: Number(jsonState.failed || 0),
    databaseTotal: Number(databaseIndex.total || 0),
    databaseProcessed: Number(databaseIndex.processed || 0),
    databaseUpdated: Number(databaseIndex.updated || 0),
    databaseFailed: Number(databaseIndex.failed || 0),
    scanned: Number(result.scanned_candidates || 0),
    queued: Number(result.queued_count || 0),
    alreadyQueued: Number(result.already_queued_count || 0),
    failed: Number(result.failed_count || 0),
  };
});
const profileBackfillRunningLabel = computed(() => (
  profileBackfill.value.phase === "database_migration" ? "索引中..." : "扫描中..."
));
const profileBackfillDatabaseLabel = computed(() => {
  const database = profileBackfill.value.database || {};
  if (!database.configured) {
    return "未配置";
  }
  if (!database.driver_available) {
    return "驱动缺失";
  }
  if (profileBackfill.value.running && profileBackfill.value.phase === "database_migration") {
    return "索引中";
  }
  if (database.bootstrapped) {
    return `已完成 ${Number(database.row_count || 0)} 条`;
  }
  if (database.available) {
    return "待初始化";
  }
  return "不可用";
});
const profileBackfillDatabaseHint = computed(() => {
  const database = profileBackfill.value.database || {};
  const marker = database.marker || {};
  if (!database.configured) {
    return "当前未启用 Postgres，页面会继续使用文件扫描。";
  }
  if (database.error) {
    return database.error;
  }
  if (profileBackfill.value.running && profileBackfill.value.phase === "database_migration") {
    return "正在把已有模型信息写入数据库索引。";
  }
  if (database.bootstrapped && marker.completed_at) {
    return `完成于 ${marker.completed_at}`;
  }
  if (database.available) {
    return "启动后会自动初始化索引，也可手动重建。";
  }
  return "等待数据库连接。";
});
const profileBackfillDatabaseProgressHint = computed(() => {
  if (profileBackfill.value.running && profileBackfill.value.phase === "database_migration") {
    return "索引过程中页面会自动刷新。";
  }
  if (profileBackfillStats.value.databaseUpdated) {
    return `模型 ${profileBackfillStats.value.databaseUpdated}，运行状态 ${profileBackfillStats.value.jsonStateUpdated}`;
  }
  if (profileBackfillStats.value.jsonStateUpdated) {
    return `运行状态已写入：${profileBackfillStats.value.jsonStateUpdated}`;
  }
  return "尚未初始化索引。";
});
const profileBackfillStatusText = computed(() => {
  if (statuses.profile_backfill) {
    return statuses.profile_backfill;
  }
  if (profileBackfill.value.last_error) {
    return profileBackfill.value.last_error;
  }
  if (profileBackfill.value.running && profileBackfill.value.phase === "database_migration") {
    return `数据库索引初始化中：${profileBackfillStats.value.databaseProcessed}/${profileBackfillStats.value.databaseTotal}，页面会自动刷新状态。`;
  }
  if (profileBackfill.value.running) {
    return "现有库信息补全正在后台扫描并入队，页面会自动刷新状态。";
  }
  const result = profileBackfill.value.last_result || {};
  if (profileBackfill.value.finished_at && Object.keys(result).length > 0) {
    return `处理完成：模型索引写入 ${profileBackfillStats.value.databaseUpdated} 个，运行状态写入 ${profileBackfillStats.value.jsonStateUpdated} 个，发现 ${profileBackfillStats.value.scanned} 个缺信息模型，新增入队 ${profileBackfillStats.value.queued} 个，已在队列 ${profileBackfillStats.value.alreadyQueued} 个，失败 ${profileBackfillStats.value.failed} 个。`;
  }
  return profileBackfill.value.message || "首次连接新数据库后会自动初始化历史库索引；手动重建会重新扫描归档库并刷新数据库索引。";
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
    runtime_diagnostics: {
      deployment_mode: "",
      docker_socket_mounted: false,
      supported: false,
      support_reason: "",
      roles: [],
    },
    target_version: "",
    current_version: "",
    supported: false,
    support_reason: "",
    compose_migration_required: false,
    compose_migration_reason: "",
    compose_example: "",
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
    phase: "idle",
    database_rebuild_requested: false,
    force_database_rebuild: false,
    auto_database_migration: false,
    started_at: "",
    finished_at: "",
    last_error: "",
    last_result: {},
    database: {},
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

function runtimePayload() {
  return buildRuntimePayload(runtimeForm);
}

function hasTokenPermission(item, permission) {
  return Array.isArray(item?.permissions) && item.permissions.includes(permission);
}

function tokenPermissionLabels(permissions) {
  const values = Array.isArray(permissions) ? permissions : [];
  const labels = values
    .map((value) => tokenPermissionLabelsMap.find((item) => item.value === value)?.label || value)
    .filter(Boolean);
  return labels.length ? labels : ["无权限"];
}

function formatTokenDate(value) {
  const text = String(value || "").trim();
  return text || "永不过期";
}

function hasAccountCookie(item) {
  return Boolean(String(item?.cookie || "").trim());
}

function accountPlatformLabel(platform) {
  return platform === "global" ? "MakerWorld 国际" : "MakerWorld 国区";
}

function accountDisplayName(item) {
  const displayName = String(item?.display_name || "").trim();
  if (displayName) return displayName;
  const handle = String(item?.handle || "").trim().replace(/^@+/, "");
  if (handle) return `@${handle}`;
  const username = String(item?.username || "").trim();
  return username || accountPlatformLabel(item?.platform);
}

function accountAvatarFallback(item, displayName) {
  const source = String(displayName || item?.handle || item?.username || "").trim().replace(/^@+/, "");
  return source.slice(0, 1).toUpperCase() || "U";
}

function accountStatusLabel(item) {
  const status = String(item?.status || "").trim();
  if (status === "ok") return "正常";
  if (status === "auth_required") return "Cookie 失效";
  if (status === "verification_required") return "需要验证";
  if (status === "html_response") return "读取受限";
  if (status === "http_error") return "连接异常";
  if (status) return "需检查";
  return "已保存";
}

function accountStatusClass(item) {
  const status = String(item?.status || "").trim();
  if (!status || status === "ok") return "";
  if (status === "html_response") return "is-warning";
  return "is-expired";
}

function coerceAccountCount(...values) {
  for (const value of values) {
    if (value === null || value === undefined || value === "") {
      continue;
    }
    const parsed = Number(value);
    if (Number.isFinite(parsed) && parsed >= 0) {
      return Math.floor(parsed);
    }
  }
  return null;
}

function accountCountText(value) {
  return value === null || value === undefined ? "未同步" : String(value);
}

function accountMessageText(item, status) {
  const platformLabel = accountPlatformShortLabel(item?.platform);
  const raw = String(item?.message || "").trim();
  if (raw) {
    if (/Cookie\s*部分成功/.test(raw) || /接口可访问/.test(raw) || /\b\d+\s*\/\s*\d+\b/.test(raw)) {
      return `${platformLabel}账号已保存，部分账号信息暂时读取失败；可以点击同步重试。`;
    }
    if (/基础认证可用/.test(raw) || /接口暂时未通过/.test(raw)) {
      return `${platformLabel}账号已保存，部分账号信息暂时读取失败；可以点击同步重试。`;
    }
    if (/认证接口可正常访问/.test(raw)) {
      return `${platformLabel}账号可用，Cookie 已保存。`;
    }
    if (/认证接口返回了登录页或网页页面/.test(raw)) {
      return `${platformLabel}账号已保存，但暂时无法读取账号信息；可以点击同步重试。`;
    }
    return raw;
  }
  return status === "ok" ? `${platformLabel}账号可用，Cookie 已保存。` : "已保存账号，建议测试一次。";
}

function accountPlatformShortLabel(platform) {
  return platform === "global" ? "国际" : "国内";
}

function accountSourceStats(inventory, syncState) {
  const sourceInventory = inventory && typeof inventory === "object" ? inventory : {};
  const sourceSync = syncState && typeof syncState === "object" ? syncState : {};
  const defaultFavorites = sourceInventory.default_favorites && typeof sourceInventory.default_favorites === "object"
    ? sourceInventory.default_favorites
    : {};
  const followedAuthors = Array.isArray(sourceInventory.followed_authors)
    ? sourceInventory.followed_authors
    : [];
  const followedCollections = Array.isArray(sourceInventory.followed_collections)
    ? sourceInventory.followed_collections
    : [];
  const followedAuthorCount = coerceAccountCount(
    sourceSync.followed_author_count,
    followedAuthors.length ? followedAuthors.length : "",
  );
  const followedCollectionCount = coerceAccountCount(
    sourceSync.followed_collection_count,
    sourceInventory.followed_collection_count,
    followedCollections.length ? followedCollections.length : "",
  );
  const defaultFavoritesCount = coerceAccountCount(
    sourceSync.default_favorites_count,
    defaultFavorites.count,
    defaultFavorites.model_count,
    defaultFavorites.remote_model_count,
    defaultFavorites.url || sourceSync.default_favorites_found ? 1 : "",
  );
  const lastSyncAt = sourceSync.last_sync_at || sourceInventory.last_sync_at || "";
  const lastStatus = String(sourceSync.last_status || sourceInventory.last_status || "").trim();
  let sourceSyncText = "";
  if (lastStatus === "pending") {
    sourceSyncText = "账号信息同步已排队，完成后会更新关注作者和收藏夹数量。";
  } else if (lastStatus === "error") {
    sourceSyncText = sourceSync.last_message || sourceInventory.last_message || "来源同步失败。";
  } else if (lastSyncAt) {
    sourceSyncText = `来源同步：${formatAccountDate(lastSyncAt)}`;
  }

  return {
    followedAuthorCountText: accountCountText(followedAuthorCount),
    followedCollectionCountText: accountCountText(followedCollectionCount),
    defaultFavoritesCountText: accountCountText(defaultFavoritesCount),
    sourceSyncText,
  };
}

function formatAccountDate(value) {
  const text = String(value || "").trim();
  if (!text) return "未记录时间";
  const parsed = new Date(text);
  if (Number.isNaN(parsed.getTime())) {
    return text;
  }
  return parsed.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function tokenStatusLabel(item) {
  if (item?.status === "revoked" || item?.disabled) {
    return "已撤销";
  }
  if (item?.status === "expired") {
    return "已过期";
  }
  return "有效";
}

function tokenStatusClass(item) {
  if (item?.status === "revoked" || item?.disabled || item?.status === "expired") {
    return "is-expired";
  }
  return "is-ok";
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
  proxyForm.enabled = Boolean(payload.proxy?.enabled);
  proxyForm.http_proxy = payload.proxy?.http_proxy || "";
  proxyForm.https_proxy = payload.proxy?.https_proxy || "";
  threeMfLimitsForm.cn_daily_limit = normalizeDailyThreeMfLimit(payload.three_mf_limits?.cn_daily_limit);
  threeMfLimitsForm.global_daily_limit = normalizeDailyThreeMfLimit(payload.three_mf_limits?.global_daily_limit);
  advancedForm.scraping_engine = ["legacy", "scrapling_first", "scrapling_only"].includes(payload.advanced?.scraping_engine)
    ? payload.advanced.scraping_engine
    : "scrapling_first";
  advancedForm.scrapling_browser_fallback = payload.advanced?.scrapling_browser_fallback !== false;
  advancedForm.remote_refresh_model_workers = normalizeBoundedInt(payload.advanced?.remote_refresh_model_workers, 2, 1, 4);
  advancedForm.makerworld_request_limit = normalizeBoundedInt(payload.advanced?.makerworld_request_limit, 2, 1, 8);
  advancedForm.comment_asset_download_limit = normalizeBoundedInt(payload.advanced?.comment_asset_download_limit, 4, 1, 16);
  advancedForm.three_mf_download_limit = normalizeBoundedInt(payload.advanced?.three_mf_download_limit, 1, 1, 4);
  advancedForm.disk_io_limit = normalizeBoundedInt(payload.advanced?.disk_io_limit, 1, 1, 4);
  runtimeForm.web_workers = normalizeBoundedInt(payload.runtime?.web_workers, 1, 1, 8);
  runtimeForm.worker_concurrency = normalizeBoundedInt(payload.runtime?.worker_concurrency, 2, 1, 4);
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
  const normalizedTab = tab === "connections" ? "accounts" : tab;
  activeTab.value = tabs.some((item) => item.key === normalizedTab) ? normalizedTab : "system";
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

function refreshSystemPanelFromEvent() {
  if (activeTab.value !== "system") {
    return;
  }
  if (!systemUpdateLoading.value && !systemUpdateSubmitting.value) {
    void loadSystemUpdateStatus({ silent: true });
  }
  if (!profileBackfillLoading.value && !profileBackfillSubmitting.value) {
    void loadProfileBackfillStatus({ silent: true });
  }
}

async function load() {
  const payload = await refreshLightConfig();
  applyConfigToForms(payload);
  setActiveTab(typeof route.query.tab === "string" ? route.query.tab : "system");
  void refreshSettingsDiagnostics();
}

async function refreshSettingsDiagnostics() {
  try {
    await refreshConfig();
  } catch (error) {
    statuses.system_update = error instanceof Error ? error.message : "系统诊断刷新失败。";
  }
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

function clearTimers() {
  clearAccountCodeTimer();
  if (settingsRefreshController) {
    settingsRefreshController.dispose();
    settingsRefreshController = null;
  }
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
    statuses.runtime = "容器进程设置已保存，重启容器后生效。";
  } catch (error) {
    statuses.runtime = error instanceof Error ? error.message : "保存容器进程失败。";
  } finally {
    runtimeResourcesSaving.value = false;
  }
}

async function applyRuntimeResources() {
  await saveRuntimeResources();
  if (statuses.runtime && !statuses.runtime.includes("已保存")) {
    return;
  }
  await runSystemUpdate("这会按当前容器进程设置重建 App / Worker 容器，页面会短暂不可用。确定继续吗？");
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
  }
}

async function triggerProfileBackfill() {
  const shouldProceed = window.confirm("会重新遍历本地归档库，把模型卡片信息写入数据库索引，并把缺少详情媒体或评论回复字段的模型加入后台补整理队列。不会主动下载 3MF。确定手动重建索引吗？");
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
    statuses.profile_backfill = payload.message || "数据库索引重建已提交，后台会继续处理。";
  } catch (error) {
    statuses.profile_backfill = error instanceof Error ? error.message : "提交信息补全失败。";
  } finally {
    profileBackfillSubmitting.value = false;
  }
}

function resetAccountDialog(platform = "cn") {
  accountDialog.platform = platform === "global" ? "global" : "cn";
  accountDialog.username = "";
  accountDialog.password = "";
  accountDialog.verification_code = "";
  accountDialog.consent = false;
  accountDialog.sendingCode = false;
  accountDialog.codeCountdown = 0;
  accountDialog.error = "";
  clearAccountCodeTimer();
}

function openAccountDialog(platform = "cn") {
  resetAccountDialog(platform);
  accountDialogOpen.value = true;
}

function closeAccountDialog() {
  if (accountDialog.saving || accountDialog.sendingCode) {
    return;
  }
  accountDialogOpen.value = false;
  resetAccountDialog(accountDialog.platform);
}

function clearAccountCodeTimer() {
  if (accountCodeTimer) {
    window.clearInterval(accountCodeTimer);
    accountCodeTimer = null;
  }
}

function startAccountCodeCountdown(seconds = 60) {
  clearAccountCodeTimer();
  accountDialog.codeCountdown = seconds;
  accountCodeTimer = window.setInterval(() => {
    accountDialog.codeCountdown = Math.max(0, accountDialog.codeCountdown - 1);
    if (accountDialog.codeCountdown <= 0) {
      clearAccountCodeTimer();
    }
  }, 1000);
}

async function sendAccountSmsCode() {
  if (accountDialog.sendingCode || accountDialog.codeCountdown > 0) {
    return;
  }
  accountDialog.error = "";
  statuses.accounts = "";
  if (!accountDialog.username) {
    accountDialog.error = `请先填写${accountLoginLabel.value}。`;
    return;
  }
  if (!accountDialog.consent) {
    accountDialog.error = "请先勾选用户协议和隐私政策。";
    return;
  }
  accountDialog.sendingCode = true;
  try {
    const payload = await apiRequest("/api/config/online-accounts/sms-code", {
      method: "POST",
      body: {
        platform: accountDialog.platform,
        phone: isGlobalAccountDialog.value ? "" : accountDialog.username,
        email: isGlobalAccountDialog.value ? accountDialog.username : "",
      },
    });
    statuses.accounts = payload?.message || "验证码已发送。";
    startAccountCodeCountdown(60);
  } catch (error) {
    accountDialog.error = error instanceof Error ? error.message : "验证码发送失败。";
  } finally {
    accountDialog.sendingCode = false;
  }
}

async function submitAccountLogin() {
  if (accountDialog.saving) {
    return;
  }
  if (!accountDialog.username) {
    accountDialog.error = `请填写${accountLoginLabel.value}。`;
    return;
  }
  if (!accountDialog.verification_code) {
    accountDialog.error = `请填写${accountCodePlaceholder.value}。`;
    return;
  }
  if (!accountDialog.consent) {
    accountDialog.error = "请先勾选用户协议和隐私政策。";
    return;
  }
  accountDialog.saving = true;
  accountDialog.error = "";
  statuses.accounts = "";
  try {
    const payload = await apiRequest("/api/config/online-accounts/login", {
      method: "POST",
      body: {
        platform: accountDialog.platform,
        username: accountDialog.username,
        verification_code: accountDialog.verification_code,
      },
    });
    applyConfigPayload(payload);
    applyConfigToForms(payload);
    statuses.accounts = "";
    accountDialogOpen.value = false;
    resetAccountDialog(accountDialog.platform);
  } catch (error) {
    accountDialog.error = error instanceof Error ? error.message : "登录失败。";
  } finally {
    accountDialog.saving = false;
  }
}

async function testSavedAccount(platform) {
  const key = platform === "global" ? "cookie_global" : "cookie_cn";
  testing[key] = true;
  statuses.accounts = "";
  try {
    const payload = await apiRequest(`/api/config/online-accounts/${encodeURIComponent(platform)}/test`, {
      method: "POST",
    });
    applyConfigPayload(payload);
    applyConfigToForms(payload);
    statuses.accounts = "";
  } catch (error) {
    statuses.accounts = error instanceof Error ? error.message : "测试失败。";
  } finally {
    testing[key] = false;
  }
}

async function syncSavedAccount(platform) {
  const key = `sync_${platform === "global" ? "global" : "cn"}`;
  testing[key] = true;
  statuses.accounts = "";
  try {
    const payload = await apiRequest(`/api/config/online-accounts/${encodeURIComponent(platform)}/sync`, {
      method: "POST",
    });
    applyConfigPayload(payload);
    applyConfigToForms(payload);
    statuses.accounts = "";
  } catch (error) {
    statuses.accounts = error instanceof Error ? error.message : "提交同步失败。";
  } finally {
    testing[key] = false;
  }
}

async function deleteAccount(platform) {
  const label = accountPlatformLabel(platform);
  if (!window.confirm(`确认删除 ${label} 账号吗？删除后对应站点的归档和订阅会缺少 Cookie。`)) {
    return;
  }
  statuses.accounts = "";
  try {
    const payload = await apiRequest(`/api/config/online-accounts/${encodeURIComponent(platform)}`, {
      method: "DELETE",
    });
    applyConfigPayload(payload);
    applyConfigToForms(payload);
    statuses.accounts = "账号已删除。";
  } catch (error) {
    statuses.accounts = error instanceof Error ? error.message : "删除失败。";
  }
}

async function saveProxySettings() {
  if (proxySaving.value) {
    return;
  }
  proxySaving.value = true;
  statuses.proxy_save = "";
  try {
    const payload = await apiRequest("/api/config/proxy", {
      method: "POST",
      body: buildProxyPayload(proxyForm),
    });
    applyConfigPayload(payload);
    applyConfigToForms(payload);
    statuses.proxy_save = "代理设置已保存。";
  } catch (error) {
    statuses.proxy_save = error instanceof Error ? error.message : "保存失败。";
  } finally {
    proxySaving.value = false;
  }
}

async function saveAdvanced() {
  try {
    await apiRequest("/api/config/three-mf-limits", {
      method: "POST",
      body: buildThreeMfLimitsPayload(threeMfLimitsForm),
    });
    const payload = await apiRequest("/api/config/advanced", {
      method: "POST",
      body: buildAdvancedPayload(advancedForm),
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
  const tokenValue = mobileImportToken.value || mobileImportTokenItem.value?.token_value || "<在 MakerHub 设置里生成后粘贴>";
  const lines = [
    "MakerHub iOS 快捷指令配置",
    `Token: ${tokenValue}`,
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

async function copyShortcutConfig(item = null) {
  statuses.tokens = "";
  const previousToken = mobileImportToken.value;
  try {
    if (item?.token_value) {
      mobileImportToken.value = item.token_value;
    }
    const copied = await copyText(buildShortcutConfigText());
    statuses.tokens = copied ? "快捷指令配置已复制。" : "浏览器阻止复制，请手动复制配置。";
  } catch (error) {
    statuses.tokens = error instanceof Error ? error.message : "复制失败。";
  } finally {
    mobileImportToken.value = previousToken;
  }
}

async function saveSharing() {
  try {
    const payload = await apiRequest("/api/config/sharing", {
      method: "POST",
      body: buildSharingPayload(sharingForm),
    });
    applyConfigPayload(payload);
    statuses.sharing = "分享设置已保存。";
  } catch (error) {
    statuses.sharing = error instanceof Error ? error.message : "保存失败。";
  }
}

async function testProxy() {
  testing.proxy = true;
  statuses.proxy = "";
  try {
    const response = await apiRequest("/api/config/proxy/test", {
      method: "POST",
      body: buildProxyPayload(proxyForm),
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
      body: buildSharingPayload(sharingForm),
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

function resetTokenDialogForm() {
  tokenForm.name = "我的 iPhone";
  tokenForm.expires_days = 365;
  tokenForm.permissions = ["mobile_import"];
}

function openTokenDialog() {
  resetTokenDialogForm();
  tokenDialogOpen.value = true;
}

function closeTokenDialog() {
  tokenDialogOpen.value = false;
}

async function createToken() {
  try {
    const permissions = Array.isArray(tokenForm.permissions) && tokenForm.permissions.length
      ? [...tokenForm.permissions]
      : ["archive_write"];
    const response = await apiRequest("/api/auth/tokens", {
      method: "POST",
      body: {
        name: tokenForm.name,
        permissions,
        expires_days: Number(tokenForm.expires_days || 0),
      },
    });
    mobileImportToken.value = permissions.includes("mobile_import") ? response.token || "" : "";
    tokenItems.value = response.items || [];
    tokenDialogOpen.value = false;
    statuses.tokens = "Token 已生成。";
  } catch (error) {
    statuses.tokens = error instanceof Error ? error.message : "生成失败。";
  }
}

async function revokeToken(tokenId) {
  if (!window.confirm("确认撤销这个 Token 吗？撤销后使用它的脚本或快捷指令会失效。")) {
    return;
  }
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

async function copyTokenValue(item) {
  if (!item?.token_value) {
    statuses.tokens = "这个 Token 没有保存明文，只能重新生成后复制。";
    return;
  }
  try {
    const copied = await copyText(item.token_value);
    statuses.tokens = copied ? "Token 已复制。" : "浏览器阻止复制，请手动复制。";
  } catch (error) {
    statuses.tokens = error instanceof Error ? error.message : "复制失败。";
  }
}

watch(() => route.query.tab, (value) => {
  setActiveTab(typeof value === "string" ? value : "system");
});

onMounted(async () => {
  const perf = createPagePerformanceTracker({ page: "settings", route: () => route.fullPath });
  settingsRefreshController = createPageRefreshController({
    scopes: ["system_update", "archive_profile_backfill_status"],
    refresh: refreshSystemPanelFromEvent,
    delayMs: 450,
  });
  await load();
  void perf.finish();
});
onBeforeUnmount(clearTimers);
</script>
