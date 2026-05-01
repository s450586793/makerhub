# Frontend Design System Rules

Use these rules for all future frontend changes in MakerHub.

## Color System

- Define and use CSS variables: `--color-primary`, `--color-secondary`, `--color-neutral-*`, `--color-success`, `--color-warning`, `--color-error`.
- Backgrounds must be white (`#ffffff`) or light gray (`#f9fafb`, `#f3f4f6`) only.
- Do not use gradients on backgrounds or buttons.
- Do not use blue-purple gradients anywhere.
- Do not use neon colors or rainbow palettes.
- Use no more than 3 brand colors in any single view.
- Text colors: `#111827` primary, `#6b7280` secondary, `#9ca3af` tertiary.

## Typography

- Font scale: `12px`, `14px`, `16px`, `20px`, `24px`, `32px`.
- Use CSS variables: `--text-xs`, `--text-sm`, `--text-base`, `--text-lg`, `--text-xl`, `--text-2xl`.
- Body text: `font-weight: 400`, `line-height: 1.5`.
- Headings: `font-weight: 600`, `line-height: 1.25`.
- Use `px` as the unit system.
- Do not use arbitrary font sizes outside the defined scale.

## Spacing

- Use a 4px base grid.
- Use CSS variables `--space-1` through `--space-16`.
- Avoid magic numbers such as `13px`, `7px`, or `23px`.
- Keep padding consistent within component families.

## Components

Cards:
- Use either a border (`1px solid #e5e7eb`) or a shadow, not both.
- Shadow level 1: `0 1px 3px rgba(0,0,0,0.08)`.
- Shadow level 2: `0 4px 12px rgba(0,0,0,0.1)`.
- Border radius must be `6px` or `8px`; do not use `16px+`.

Buttons:
- Primary buttons use a solid fill, no gradient.
- Secondary buttons use outline or ghost styling.
- Hover darkens by about 10%; do not switch to unrelated colors.
- Do not use rounded-full styling on rectangular buttons.

Inputs:
- Border: `1px solid #d1d5db`.
- Border radius: `6px`.
- Focus uses border-color change plus outline, not glow.

## Icons

- Use one icon set consistently: Lucide, Heroicons, or Phosphor.
- Size: `16px` inline, `20px` standalone.
- Do not use emoji as functional icons.

## Forbidden Patterns

- No blue-purple gradients.
- No glassmorphism unless explicitly requested.
- No emoji icons.
- No excessive shadows on every element.
- No inline styles for color, spacing, or typography.
- No magic numbers; every value should reference a design token where practical.
- No more than 2 shadow depth levels per page.
