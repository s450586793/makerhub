# Browser Verification Drag Accuracy Design

## Purpose

MakerHub already maps the remote MakerWorld verification page into a MakerHub popup by showing browser screenshots and forwarding user input back to the real CloakBrowser page. This design improves that mapping for slider and drag-based verification, where users reported two concrete problems:

- The slider can fail to move or lose the drag.
- Cropped verification screenshots can feel coordinate-shifted or inaccurate.

The feature remains a manual verification flow. MakerHub must not auto-solve, bypass, fake, or outsource MakerWorld or Cloudflare verification.

## Scope

In scope:

- Replace the popup's mouse-only pointer handling with Pointer Events.
- Capture the active pointer during drag so `move` and `up` events are not lost when the cursor leaves the screenshot frame.
- Send denser movement samples only while dragging.
- Avoid sending a synthetic click after a real drag.
- Pause or slow screenshot refresh while dragging, then refresh once after release.
- Render the screenshot frame using the backend-reported viewport aspect ratio so the displayed image matches the coordinate space more closely.
- Keep keyboard, paste, wheel, and click support.
- Keep the backend input command contract compatible with existing `mousedown`, `mousemove`, `mouseup`, `click`, `wheel`, `key`, and `text` commands.
- Add focused frontend tests for coordinate scaling, drag-state behavior, click suppression after drag, and dynamic aspect ratio.
- Keep backend tests for cropped offset mapping.

Out of scope:

- Automatically dragging sliders.
- Generating human-like tracks without user input.
- Solving Cloudflare Turnstile, GeeTest, or any other challenge.
- Changing proof capture, retry policy, account login, source refresh, or 3MF queue behavior.
- Replacing the screenshot polling architecture with live streaming.

## Current State

`BrowserVerificationPage.vue` currently listens to `click`, `mousemove`, `mousedown`, `mouseup`, `wheel`, `keydown`, and `paste`. Coordinates are calculated from the rendered frame size and the session `viewport.width` / `viewport.height`. The backend then adds `viewport.offset_x` and `viewport.offset_y` before applying the command to the real browser page.

This is correct for simple clicks. It is weaker for sliders because:

- Browser `mouseup` can be lost if the cursor leaves the frame.
- `mousemove` is throttled to about 180ms, which is too sparse for a smooth slider drag.
- A drag can also produce a final `click`, adding an unwanted extra command.
- The CSS frame uses a fixed `520 / 640` aspect ratio while screenshots can be different sizes after cropping.
- Screenshot refresh during drag can make the visible target move while the user is still holding the slider.

## Recommended Design

Use Pointer Events as the primary interaction layer for the verification screenshot.

Frontend behavior:

1. `pointerdown`
   - Focus the screenshot frame.
   - Set `isPointerDown = true`.
   - Record the pointer id and start coordinates.
   - Call `setPointerCapture(pointerId)` when available.
   - Send `mousedown` to the backend.
   - Temporarily slow screenshot refresh while dragging.

2. `pointermove`
   - Ignore unrelated pointer ids.
   - If dragging, send `mousemove` at a tighter interval such as 35-50ms.
   - Track total movement distance from the down point.
   - Mark the gesture as `dragged` once movement exceeds a small threshold, such as 4 CSS pixels.
   - If not dragging, keep the existing low-frequency hover movement behavior or skip hover movement entirely.

3. `pointerup`
   - Send one final `mousemove` if the pointer moved since the last sent sample.
   - Send `mouseup`.
   - Release pointer capture when available.
   - If the gesture moved beyond the drag threshold, suppress the follow-up `click`.
   - Resume normal screenshot refresh and immediately request a fresh screenshot.

4. `pointercancel` / lost capture
   - Send `mouseup` if MakerHub believes the pointer is down.
   - Clear drag state.
   - Resume screenshot refresh.

5. `click`
   - Keep click support for checkbox-style verification.
   - Do not send click when the previous pointer gesture was a drag.

Dynamic screenshot ratio:

- Compute a CSS aspect-ratio from `session.viewport.width / session.viewport.height`.
- Apply it to the frame via an inline style or CSS variable.
- Keep stable min-height constraints for small windows.
- Continue using `object-fit: fill` only when the frame ratio matches the screenshot ratio; otherwise the dynamic frame ratio should remove most visible distortion.

Backend behavior:

- Keep `_apply_input()` unchanged unless tests reveal ordering or offset bugs.
- Continue adding cropped offsets in backend coordinates.
- Continue consuming commands in stored order.
- Do not log every movement command.

## Data Flow

1. Worker captures a full or cropped screenshot and stores `viewport.width`, `viewport.height`, and optional `offset_x` / `offset_y`.
2. Frontend renders the screenshot frame at the same aspect ratio as that viewport.
3. User presses and drags inside the frame.
4. Frontend maps rendered coordinates into screenshot coordinates.
5. Frontend sends `mousedown`, denser `mousemove`, and `mouseup` commands.
6. Backend adds cropped offsets and applies the commands to the real CloakBrowser page.
7. Verification provider sees a continuous user-driven drag.
8. When verification produces a 3MF proof request, existing proof capture and retry logic continues.

## Error Handling

- If pointer capture is unavailable, keep sending pointer events from the frame and fall back to existing behavior.
- If a `pointercancel` happens, send a best-effort `mouseup` to avoid leaving the remote browser in a pressed state.
- If screenshot refresh fails during drag, keep the current screenshot visible and retry after release.
- If a command API call fails, keep the current console warning behavior and allow the next command or refresh to continue.
- If viewport dimensions are missing or invalid, fall back to the existing fixed frame sizing.

## Testing

Frontend tests should cover:

- Coordinate scaling uses `session.viewport.width` and `session.viewport.height`.
- Cropped viewport coordinates continue to map correctly when backend offset exists.
- Drag sends `mousedown`, multiple `mousemove` commands, and `mouseup` in order.
- Drag movement uses a tighter throttle than hover movement.
- A drag suppresses the extra `click`.
- A simple click still sends a `click`.
- `pointercancel` sends a best-effort `mouseup` when dragging.
- Frame aspect ratio reflects the current viewport dimensions.

Backend tests should keep covering:

- `_apply_input()` adds cropped `offset_x` / `offset_y`.
- Input commands are rejected before the browser is running.
- Input commands are consumed in order.
- Sensitive values are not persisted or logged.

Manual acceptance checks:

- User can hold and drag a slider inside the MakerHub popup without losing the drag.
- Slider remains under the pointer when the screenshot is cropped.
- Releasing outside the visible frame still releases the remote mouse when pointer capture is supported.
- Checkbox-style Cloudflare verification still works.
- Proof capture and automatic 3MF retry still work after manual verification.

## Acceptance Criteria

- Slider verification can be manually dragged from MakerHub with no obvious coordinate shift.
- Drag gestures send continuous pointer-derived movement while preserving command order.
- Click-based verification still works.
- No automatic verification solving is introduced.
- The popup remains a pure verification viewport with no extra MakerHub controls in normal state.
- Version and README release notes are updated only when the user explicitly asks to push.
