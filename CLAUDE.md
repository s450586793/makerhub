# MakerHub Agent Notes

For frontend work, follow [docs/UI_DESIGN_SYSTEM.md](docs/UI_DESIGN_SYSTEM.md) first.

Important current UI direction:

- MakerHub is a dark, compact data workstation, not a marketing page.
- Use existing tokens in `app/static/css/app.css` and `frontend/src/style.css`.
- Keep page top toolbars consistent across 首页、模型库、订阅库、本地库、源端刷新、归档任务、设置、日志.
- Preserve `light` / `dark` / `auto` theme support.
- Do not reintroduce the old white-only SaaS card style.
- Avoid blue-purple gradients, decorative blobs, glassmorphism, emoji icons, and oversized hero sections.
- Use 6px or 8px radii for normal business UI; keep nested cards and tiny repeated blocks under control.
- Check dark mode whenever adding surfaces, borders, generated images, file lists, or overlays.
