# CloakBrowser Verification Hardening Design

## Purpose

MakerHub already uses CloakBrowser for the built-in 3MF browser verification flow. The next change should harden that flow so MakerWorld verification is less noisy for the user: keep the real browser profile stable, show only the verification box when Cloudflare or MakerWorld asks for a human action, and continue the missing 3MF retry automatically after the user finishes the check.

This design does not attempt to solve, bypass, fake, or automate Cloudflare Turnstile or other human verification. The user remains responsible for the human check.

## Scope

In scope:

- Keep using CloakBrowser persistent browser contexts for browser verification sessions.
- Preserve platform-specific browser profiles so successful verification state can be reused for later downloads.
- Improve screenshot clipping for Cloudflare Turnstile and MakerWorld verification boxes.
- Keep the MakerHub popup chrome-free during normal verification.
- Keep automatic proof capture from the resulting `/f3mf` request.
- Automatically retry missing 3MF downloads after proof capture.
- Add diagnostics that distinguish browser startup, page load, download trigger, visible verification, proof capture, retry submission, timeout, and failure.
- Keep sensitive values such as Cookie, token, `cf_clearance`, and `x-bbl-captcha-result` out of logs and state responses.

Out of scope:

- Automatically clicking or solving Cloudflare Turnstile.
- Integrating third-party captcha solving services.
- Faking MakerWorld verification proof.
- Replacing the existing ArchiveTaskManager retry path.
- Changing source-refresh batching or concurrency.
- Reworking account login.

## Current State

`BrowserVerificationRuntime._launch_context()` already starts CloakBrowser with a persistent profile directory per platform, `headless=False`, and `humanize=True`. The runtime injects platform Cookie values, routes Bambu API requests with token headers, opens a start URL, screenshots the remote browser, forwards user input, captures `x-bbl-captcha-result` from `/f3mf` requests, and retries missing 3MF tasks after capture.

The remaining UX problem is that Cloudflare can still show a large verification page. MakerHub should make that page feel like a focused verification popup by cropping to the actual Turnstile box when possible and by continuing automatically after the user's click.

## User Experience

Normal state:

- The popup shows only the remote browser image.
- If a Cloudflare Turnstile or MakerWorld verification widget is visible, the screenshot is cropped to that widget plus small padding.
- The user clicks the verification checkbox or performs the required human action inside the MakerHub popup.
- MakerHub forwards the click to the real CloakBrowser page.
- After the resulting 3MF request carries a proof, MakerHub retries missing 3MF downloads automatically.
- The popup marks completion with a minimal message or can close after completion if the existing window opener supports it.

Fallback state:

- If no verification widget is detected, MakerHub shows the full remote browser screenshot so the user can still operate the model page.
- If the page is a raw API denial, the existing fallback to the MakerWorld model page remains in place.
- If automatic download triggering fails, the user can still click the download control manually inside the same popup.

## Backend Design

### Browser Context

Continue using:

- `launch_persistent_context()`
- a platform-specific profile path under `STATE_DIR / "browser_verification" / "profiles" / platform`
- `headless=False`
- `humanize=True`
- the existing selected proxy policy

Do not rotate profiles by session because that would discard useful verification and site state. Keep one active verification session per platform profile to avoid profile locking and conflicting browser state.

### Cookie And Header Handling

Keep the current cookie injection model:

- MakerWorld site domain cookies
- Bambu API domain cookies
- token-derived headers only for Bambu API requests

Do not log sensitive request headers. Any new diagnostic event must redact Cookie, Authorization, token, `cf_clearance`, `__cf_bm`, and `x-bbl-captcha-result`.

### Verification Clipping

Extend `_verification_clip()` with selectors and text-based fallback for the observed Cloudflare page:

- `.cf-turnstile`
- `[data-sitekey]`
- `iframe[src*="challenges.cloudflare.com"]`
- elements containing `Verify you are human`
- containers near text such as `Performing security verification`

The clipping function should prefer the smallest visible actionable verification widget. If only the broader Cloudflare page is detectable, crop to the card or section that contains the widget and explanatory text. If no reliable candidate exists, return no clip and keep the full screenshot.

Click coordinate mapping must keep using the screenshot viewport offset so cropped screenshots remain interactive.

### State And Diagnostics

Keep frequent verification screenshots because the user is interacting with a live remote page. Add only low-volume session milestone logs:

- session accepted by worker
- CloakBrowser context launched
- start URL loaded
- API denial fallback applied
- download trigger attempted
- verification surface detected
- proof captured
- retry submitted
- session completed, expired, cancelled, or failed

Do not log every screenshot refresh or every input command.

### Completion

After proof capture:

1. Store only the in-memory proof id.
2. Submit the existing missing 3MF retry flow.
3. Mark the browser verification session completed.
4. Keep the proof secret out of database JSON state and API responses.

## Frontend Design

Keep `BrowserVerificationPage.vue` as a single-purpose verification viewport:

- no MakerHub header
- no model metadata
- no task metadata
- no refresh/cancel/return controls during normal operation
- only the screenshot surface plus terminal loading/error/completion text

No additional user instructions should be shown in the normal state. The visible verification provider page already tells the user what action is required.

## Error Handling

- Missing Cookie: fail with the existing concise account-cookie message.
- Browser launch failure: fail the session and log a redacted milestone.
- Raw API denial: route to the model page and try the existing download trigger path.
- Verification widget not detected: keep full screenshot and manual input.
- Proof not captured: expire after the existing timeout.
- Retry submission failure: keep the session failed or completed with retry error details that do not expose secrets.

## Testing

Backend tests:

- `_verification_clip()` recognizes a Cloudflare Turnstile widget candidate.
- `_verification_clip()` prefers a small widget over a broad page container.
- `_write_screenshot()` preserves crop offsets for Cloudflare clips.
- `_apply_input()` maps cropped Cloudflare coordinates back to the real page.
- Diagnostics redact sensitive headers and proof values.
- Existing proof capture and retry tests continue to pass.

Frontend tests:

- The browser verification popup still does not contain model metadata or action chrome.
- Completion and error messages remain concise.

Verification commands:

- `.venv/bin/python -m pytest tests/test_browser_verification.py tests/test_browser_verification_api.py -q`
- Node browser verification popup tests if frontend code changes.
- Frontend production build if frontend code changes.

## Acceptance Criteria

- MakerHub continues using CloakBrowser for browser verification.
- Cloudflare and MakerWorld verification widgets are cropped when visible.
- The user can manually complete the Cloudflare check inside the MakerHub popup.
- MakerHub does not auto-solve, bypass, fake, or outsource the human verification.
- After user verification, proof capture and automatic missing 3MF retry still work.
- Sensitive Cookie, token, `cf_clearance`, and proof values are not logged or exposed.
