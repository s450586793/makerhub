# MakerHub Intro Video Design

Date: 2026-06-05

## Purpose

Create a 45-second Chinese promotional video for MakerHub using real visuals from the user's online MakerHub instance. The video should introduce MakerHub as a private MakerWorld archive workstation and quickly walk through the main product areas: model library, dashboard status, online account sync, subscription library, source refresh, source-deleted status, local upload, model sharing, and manual MakerWorld verification recovery.

HyperFrames will be used for video composition, motion, subtitles, highlight overlays, layout variants, and final rendering. Online instance material collection is a separate automation step using browser capture.

## Goals

- Produce a fast promotional video, not a step-by-step tutorial.
- Use real MakerHub interface material from the online instance.
- Start with the model library grid to make the first seconds visually attractive.
- Generate a 16:9 primary version and a 9:16 derived vertical version.
- Include Chinese voiceover and synchronized subtitles, using replaceable TTS audio for the first version.
- Avoid exposing the online instance address, login credentials, tokens, share codes, public base URLs, proxy values, server paths, and raw sensitive logs.
- Keep the workflow repeatable so captured material can be refreshed and the video can be rendered again.

## Non-Goals

- Do not build a full tutorial video in this pass.
- Do not expose the browser address bar or browser chrome in captured video.
- Do not store real account credentials, the online URL, tokens, cookies, or share codes in the repository.
- Do not require fake product UI when real online material is available.
- Do not change MakerHub product behavior or frontend UI as part of the video workflow.

## Selected Approach

Use real page captures from the online MakerHub instance, then package them with HyperFrames as a polished promotional video.

The capture step should use Playwright to log in and navigate the app. It must capture only the MakerHub app content area, not the browser address bar. Credentials and the base URL must be supplied through local runtime configuration such as environment variables. The rendered video should use real screenshots or short captures, plus title cards, zooms, green highlights, subtitles, and TTS voiceover.

This approach is preferred over mock-only visuals because the video should show the real MakerHub product and its actual workflows. It is preferred over a pure animation because the requested feature-by-feature introduction depends on recognizable UI.

## Storyboard

| Time | Visual | Screen Title | Voiceover |
| --- | --- | --- | --- |
| 0-4s | Model library cover grid with fast movement and card detail emphasis. | 私有模型库 | 把 MakerWorld 模型，整理成你自己的私有模型库。 |
| 4-8s | Dashboard status cards sweep across archive, subscriptions, local import, tasks, and source status. | 一个工作台看全局 | 归档、订阅、导入、任务和源站状态，都集中在一个工作台。 |
| 8-14s | Online account area followed by synced subscription source cards. | 登录后自动同步 | 登录线上账号，关注作者、收藏夹和合集会自动进入订阅库。 |
| 14-19s | Subscription/source library cards and source detail model aggregation. | 持续发现新模型 | MakerHub 会按订阅来源持续检查，把新模型加入归档流程。 |
| 19-25s | Source refresh page showing progress and result summary. | 保持源端信息新鲜 | 源端刷新会更新评论、附件、打印配置和模型状态。 |
| 25-30s | Model card or detail page with source-deleted/remote status marker. | 识别源端删除 | 如果源站模型已经消失，本地资料库也能清楚标记。 |
| 30-35s | Local upload entry and organizer progress. | 本地模型也能整理 | 3MF、STL、STEP、OBJ 和压缩包，可以从网页、手机或本地文件夹导入。 |
| 35-40s | Share dialog and receive-share entry, with sensitive codes hidden or avoided. | 模型安全分享 | 生成分享码，把模型发送给另一台 MakerHub，并自动检查重复。 |
| 40-45s | Dashboard verification warning and retry action. | 验证后继续归档 | 遇到 MakerWorld 验证，去源站完成验证，回到 MakerHub 重试即可。 |

## Capture Requirements

Capture material should cover these app areas:

- Model library: cover grid, search or filter area, model count, and a card detail emphasis.
- Dashboard: archive count, missing 3MF, subscriptions, source refresh, local organizer, worker/task status, and MakerWorld status cards.
- Settings or online accounts: account list, login entry, and sync status, while avoiding cookies, tokens, and raw credentials.
- Subscription/source library: author, favorite, and collection source cards plus source-level model aggregation.
- Source refresh: run state, progress, recent result summary, and error summary if useful.
- Source-deleted status: model card or detail marker showing remote deletion/source status.
- Local upload/local organizer: upload entry, accepted file type context, and organizer progress.
- Model sharing: share dialog, share records, or receive-share entry, while hiding or avoiding share codes and public URLs.
- MakerWorld verification recovery: MakerHub-side warning and retry entry only.

The capture implementation should wait for data to load before taking each capture and should record material-to-storyboard mapping in a manifest.

## Privacy And Redaction Rules

Must not appear in final videos, generated subtitles, voiceover text, filenames, manifests, or logs:

- Online instance address and full URLs.
- Browser address bar or browser chrome.
- Account password and saved login credentials.
- Cookies, API tokens, mobile-import tokens, share access codes, and share codes.
- Public sharing base URL.
- Proxy credentials or proxy host values.
- Real server paths and mounted storage paths.
- Raw stack traces, raw upstream HTML, and unredacted business logs.

May appear by default:

- Public model names.
- Public author names.
- Public collection or favorite names.
- Public model cover images.

The first workflow should include a text scan over generated files and metadata to reject obvious leaks such as the online URL, configured credential values, token-like strings, or public share-code patterns.

## Video Composition

The 16:9 version is the primary composition. It should use full-width UI captures, short zooms, quick cuts, green highlight boxes, concise section titles, and synchronized Chinese subtitles. It should preserve MakerHub's dark compact workstation feel and avoid marketing-page visual tropes such as blue-purple gradients, decorative blobs, oversized hero layouts, and glassy panels.

The 9:16 version should be derived from the same material but recomposed rather than hard-cropped. It should place the title near the top, key UI crop in the center, and subtitles near the bottom. Priority crops are model cards, dashboard cards, source cards, dialogs, and status actions.

## Proposed Artifacts

- `videos/makerhub-intro/storyboard.md`: approved storyboard, voiceover, and subtitle text.
- `videos/makerhub-intro/assets/`: captured images or short clips.
- `videos/makerhub-intro/assets/manifest.json`: mapping from assets to storyboard segments and redaction notes.
- `videos/makerhub-intro/scripts/`: capture, redaction check, and render helper scripts.
- `videos/makerhub-intro/scenes/`: HyperFrames scene code or the structure required by the HyperFrames plugin.
- `videos/makerhub-intro/output/`: rendered 16:9 and 9:16 exports.
- `.env.example`: variable names only, with no real URL or credentials.

## Verification

Before calling the video complete:

- Confirm no captured frame contains browser chrome or the browser address bar.
- Confirm text scan does not find the online URL, configured credential values, cookies, tokens, share codes, or public sharing addresses.
- Manually inspect representative frames for every storyboard segment.
- Confirm subtitles do not cover the key UI in both 16:9 and 9:16.
- Confirm the 9:16 export uses focused crops and is not a destructive center crop of the desktop page.
- Confirm voiceover audio is replaceable without changing storyboard timing.

## Open Implementation Notes

The user approved using the real online instance with username and password login. The actual online base URL and credentials must be passed only at runtime and should not be written into this document or committed files. The initial audio may use any available TTS voice as a replaceable placeholder.
