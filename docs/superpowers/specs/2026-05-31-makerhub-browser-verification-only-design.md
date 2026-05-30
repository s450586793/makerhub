# MakerHub Pure Browser Verification View Design

## Purpose

MakerHub should let the user manually complete MakerWorld verification inside a MakerHub popup without showing unrelated task metadata, model metadata, navigation, or status controls. The popup should feel like a dedicated verification window, not a reduced task detail page.

This design keeps the existing real-browser approach: the Worker opens MakerWorld in Chromium, MakerHub displays a cropped screenshot of the verification surface, user input is replayed into the real browser, and the backend captures the verification proof from the resulting 3MF request.

## Scope

In scope:

- Show only the verification surface in the MakerHub popup during normal verification.
- Prefer the cropped captcha or verification panel when it exists.
- Hide model title, platform, status, screenshot count, model ID, configuration ID, captcha ID, retry result, and task navigation from the verification popup.
- Remove visible action chrome such as return, refresh, and cancel buttons from the normal verification surface.
- Keep minimal fallback text only when no screenshot is available, the session fails, expires, or completes.
- Automatically continue the missing 3MF retry after proof capture.

Out of scope:

- Automatically solving or bypassing MakerWorld verification.
- Embedding MakerWorld directly with an iframe.
- Changing the missing 3MF queue, archive retry policy, or account login settings.
- Reworking the main task page or global navigation.

## User Experience

Normal state:

- The popup opens as a compact window.
- The content area contains only the remote verification image.
- If a captcha panel is detected, the screenshot is cropped to that panel plus a small padding.
- User clicks, drags, scrolls, types, or pastes directly on the image; MakerHub maps those inputs back to the real browser coordinates.
- When verification succeeds, MakerHub captures the proof and the backend retries downloads automatically.
- The popup may show a minimal completion state or close automatically after completion.

Loading and error states:

- Before the first screenshot, show one centered message such as `正在加载验证页面`.
- If the Worker cannot start, Cookie is missing, or the session expires, show one concise error message.
- No model or task metadata should be introduced in these states.

## Architecture

Backend:

- Continue using `BrowserVerificationRuntime` as the verification coordinator.
- Continue using `cloakbrowser` persistent Chromium contexts so MakerWorld sees a real browser environment.
- Continue injecting the selected platform Cookie into both MakerWorld and Bambu API domains.
- Continue adding token headers only for `api.bambulab.com` and `api.bambulab.cn` requests.
- Continue preferring `api.bambulab.* /v1/design-service/instance/{id}/f3mf?type=download&fileType=3mf` as the direct verification start URL when an instance ID exists.
- Keep screenshot cropping in `_verification_clip`; improve selectors only if the visible captcha panel is not being detected reliably.

Frontend:

- Treat `BrowserVerificationPage.vue` as a single-purpose verification viewport.
- Remove the header row that contains heading and action buttons from the normal surface.
- Keep the screenshot frame as the primary and only normal visible element.
- Preserve pointer, keyboard, wheel, and paste forwarding.
- Keep polling and screenshot refresh logic unchanged unless it causes visible UI noise.
- Add source-level regression tests to ensure removed metadata and controls do not reappear.

## Data Flow

1. User starts browser verification from a missing 3MF item.
2. Backend creates a verification session and opens the compact MakerHub popup.
3. Worker starts Chromium with the matching platform Cookie and API request auth handling.
4. Worker opens the preferred 3MF verification URL or fallback model page.
5. Backend screenshots the visible verification page and crops to the captcha panel when detectable.
6. Frontend renders only that screenshot and forwards user input to the session input API.
7. Worker applies input to Chromium using the current screenshot crop offsets.
8. Worker captures `x-bbl-captcha-result` from a subsequent `/f3mf` request.
9. Worker stores a proof ID, retries missing 3MF downloads, and marks the session completed.

## Error Handling

- Missing Cookie: fail the session with a concise message that the account Cookie is required.
- Browser launch failure: mark failed and show one concise message.
- No screenshot yet: show loading text, then keep polling.
- No captcha crop detected: show the full remote browser screenshot rather than blocking the user.
- Session timeout: mark expired and show a concise timeout message.
- Input API failure: keep the current behavior of ignoring the transient failure and allowing the next input or refresh.

## Testing

Backend tests:

- Start URL uses Bambu API direct 3MF endpoint when an instance ID exists.
- Legacy MakerWorld `/api/.../f3mf` URLs are rewritten to Bambu API endpoints.
- Instance ID can be parsed from a legacy `/instance/{id}/f3mf` URL.
- Cookies are injected for both MakerWorld and Bambu API domains.
- Token headers are applied only to Bambu API request domains.
- Cropped screenshot viewport offsets still map user input back to real browser coordinates.

Frontend tests:

- `BrowserVerificationPage.vue` does not contain model/platform/status/screenshot metadata.
- It does not render task navigation or action controls in the normal verification surface.
- The route remains a standalone popup route outside the main shell.

Build verification:

- Python unit tests for browser verification and web routes.
- Node tests for popup route and verification page shape.
- Frontend production build.

## Acceptance Criteria

- In normal verification, the MakerHub popup shows only the remote verification image.
- No model title, platform, status, screenshot count, model ID, configuration ID, captcha ID, retry result, return button, refresh button, or cancel button is visible.
- The user can complete the captcha by clicking inside the MakerHub popup.
- Click coordinates remain accurate when the backend crops the screenshot.
- Verification completion still triggers automatic missing 3MF retry.
- The implementation does not attempt to auto-solve or bypass MakerWorld verification.
