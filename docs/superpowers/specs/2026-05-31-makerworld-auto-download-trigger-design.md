# MakerWorld Auto Download Trigger Verification Design

## Purpose

MakerHub should make the browser verification popup reach the MakerWorld captcha as quickly as possible. The current pure popup UI is correct, but the backend can open a direct 3MF API URL that returns raw JSON `403` instead of an interactive verification page. The next change should actively drive the real browser through the MakerWorld model download flow so the user sees the captcha surface, not an API error.

## Scope

In scope:

- Detect direct API denial pages such as JSON `403` permission errors and stop showing them as the verification surface.
- Navigate the verification browser to the MakerWorld model page when the direct API path is not interactive.
- Try to select the target print profile when the instance ID or profile information is available.
- Try to click the MakerWorld `Download 3MF` control automatically after the model page loads.
- Keep the popup UI chrome-free: no MakerHub header, task metadata, model metadata, or extra action buttons.
- Crop screenshots to the captcha or verification panel once it appears.
- Keep manual user input forwarding so the user completes the captcha inside MakerHub.
- Keep proof capture from the resulting `/f3mf` request and automatic retry after proof capture.

Out of scope:

- Solving or bypassing MakerWorld captcha.
- Depending on an iframe embed of MakerWorld.
- Reworking missing 3MF task storage, retry policy, quota logic, or account login.
- Guaranteeing that MakerWorld will always expose the captcha without showing any model-page state first. If MakerWorld requires a model-page click before it creates a captcha, MakerHub can only minimize that stage.

## User Experience

The popup remains a compact, pure remote-browser viewport. During normal operation the user should see one of these surfaces:

1. A cropped captcha or verification panel, when MakerWorld has shown one.
2. A MakerWorld download-related modal or page section, if it appears before the captcha.
3. The model page only as a fallback while MakerHub is trying to trigger the download control.

Raw JSON API errors must not remain visible as the verification surface. If the browser lands on a JSON permission error, the worker should immediately route to the model page and begin the automatic trigger flow.

When the automatic click succeeds, the user only needs to complete the captcha. If MakerWorld changes the button text or layout and the automatic click fails, the user can still click the visible download control inside the same MakerHub popup.

## Backend Design

`BrowserVerificationRuntime` remains the coordinator.

Navigation flow:

1. Compute the same direct API candidate URL when an instance ID exists.
2. Open that URL only as a quick interactive check.
3. If the page body looks like JSON/API denial, navigate to `target.model_url`.
4. Wait for the model page to reach a usable DOM state.
5. Try a bounded automatic trigger sequence:
   - Prefer controls whose accessible name or text contains `Download 3MF`, `下载 3MF`, `下载`, or `Download`.
   - Prefer buttons, links, menu items, and elements with role `button` or `menuitem`.
   - Avoid clicking unrelated browser chrome or MakerHub UI because the click happens only inside the remote MakerWorld page.
   - Limit attempts and timeouts so the worker does not stall the verification session.
6. Continue the existing screenshot loop, input replay, proof capture, and retry flow.

The automatic trigger should be best effort, not a hard dependency. Failure to find or click a button should update the session message for logs, then keep showing the page so the user can click manually.

API denial detection:

- Treat a visible document body that parses like JSON and contains `code: 403`, `error`, `access rights`, `permission`, or equivalent denial text as non-interactive.
- Also treat plain text pages with the same denial wording as non-interactive.
- Do not classify normal captcha HTML, MakerWorld pages, or Cloudflare-style verification HTML as JSON denial.

Screenshot cropping:

- Reuse `_verification_clip`.
- Add selectors only if needed for MakerWorld's current captcha/modal surfaces.
- When no captcha/modal selector matches, keep the full remote screenshot so the user can still interact with the model page.

## Frontend Design

No visible frontend chrome is added.

`BrowserVerificationPage.vue` should continue to:

- Render only the screenshot surface during normal verification.
- Forward pointer, wheel, keyboard, and paste input.
- Show concise fallback text only before first screenshot or after terminal failure/completion.
- Avoid model metadata, task metadata, header buttons, refresh controls, and cancel controls.

## Error Handling

- Missing cookie: fail with the existing concise account-cookie message.
- Direct API JSON `403`: do not fail immediately; reroute to the model page and try the automatic trigger.
- Model page load failure: keep the session failed with a concise browser navigation message.
- Automatic trigger failure: keep the session running and allow manual click in the popup.
- Captcha never appears: keep polling until the existing timeout.
- Proof not captured before timeout: expire the session with the existing timeout behavior.

## Testing

Backend tests:

- JSON `403` page detection recognizes the observed MakerWorld/Bambu response.
- JSON `403` detection does not classify normal HTML/captcha content as API denial.
- Runtime navigation falls back from direct API URL to `model_url` after API denial.
- Automatic trigger tries expected download selectors/text without requiring an exact single language.
- Trigger failure does not fail the session.
- Existing Bambu API auth header routing and proof capture behavior remain unchanged.

Frontend tests:

- Existing pure-popup assertions remain unchanged.
- No task metadata or action buttons reappear.

Verification commands:

- `pytest tests/test_browser_verification.py tests/test_web_routes.py`
- Existing node tests that cover browser verification route and popup shape.
- Frontend production build if frontend files change.

## Acceptance Criteria

- The MakerHub popup no longer stays on the raw JSON `403` page shown in the screenshot.
- When the direct API route is non-interactive, the worker opens the MakerWorld model page.
- The worker attempts to click the `Download 3MF` flow automatically.
- If MakerWorld shows a captcha, the popup crops to the captcha or verification panel.
- If automatic clicking fails, the user can still click the download control manually inside the same popup.
- Verification proof capture and automatic missing 3MF retry continue to work.
- The implementation does not auto-solve, bypass, or fake MakerWorld verification.
