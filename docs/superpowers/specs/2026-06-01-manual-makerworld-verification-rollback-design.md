# Manual MakerWorld Verification Rollback Design

## Goal

MakerHub should stop trying to complete MakerWorld verification inside its own popup. When MakerWorld CN or Global requires human verification, MakerHub should show the platform status on the dashboard and provide an external visit button so the user can verify directly on MakerWorld, then return to MakerHub and retry the affected task.

## Scope

- Restore dashboard source status cards as the primary verification signal for MakerWorld CN and Global.
- Replace built-in browser verification actions with external MakerWorld navigation.
- Remove the MakerHub browser verification popup, route, API, runtime, proof handling, and CloakBrowser dependency.
- Keep browser pieces that are still needed by unrelated features, especially local preview rendering and Scrapling-based fetching.

## User Experience

The dashboard continues to show separate status cards for CN and Global. Verification, Cloudflare, or auth-required states should not create a MakerHub verification session. The card action should open the relevant MakerWorld home page in a new tab with a concise label such as `访问主页`.

The task page should also stop showing a MakerHub `去验证` popup action. For missing 3MF items blocked by verification, the action should open the best available external URL:

- the model page when the task has a MakerWorld model URL
- otherwise the platform home page inferred from the task

After completing verification on MakerWorld, the user retries or resumes the task in MakerHub.

## Architecture

Remove the browser verification product surface:

- frontend route `/browser-verification/:sessionId`
- `BrowserVerificationPage.vue`
- browser verification popup/window helpers
- dashboard and task-page session creation calls
- backend `/api/browser-verification/*` endpoints
- browser verification worker polling
- proof storage and automatic proof injection into retry tasks

Preserve status classification. Existing `verification_required`, `cloudflare`, and `auth_required` states remain useful because they explain why downloads are blocked. The behavior change is only the action that MakerHub offers.

## Dependency Cleanup

Remove `cloakbrowser` from Python requirements and remove its binary preinstall step from the Docker image.

Do not remove `scrapling`, `RUN scrapling install`, `chromium`, or Chromium system libraries in this rollback. Current source inspection shows:

- Scrapling is used by MakerWorld fetching and source health checks.
- Chromium is used by local preview rendering through `puppeteer-core`.

Those dependencies can be reviewed separately if a later image-size cleanup proves they are no longer required.

## Testing

Update tests so they assert the new manual verification behavior:

- dashboard verification states produce external actions, not `browser-verification` actions
- task-page verification actions no longer call `/api/browser-verification/sessions`
- backend no longer exposes browser verification API routes
- worker startup no longer polls browser verification sessions
- missing 3MF retry metadata does not depend on a browser verification proof

Remove tests whose only purpose was validating the deleted popup, input mapping, screenshot polling, or CloakBrowser runtime.

## Release Notes

When this implementation is released, bump the patch version and add a README note that MakerWorld verification now returns to manual external verification from dashboard/task actions, while the unused CloakBrowser verification stack has been removed.
