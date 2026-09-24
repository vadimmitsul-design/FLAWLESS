# FLAWLESS client workspace

The Next.js cabinet uses a cyberpunk direction requested by the user. The user
works in a focused, dark personal AI workspace; readable controls and financial
information take priority over ambient effects.

## Palette

- Canvas: `#0b0b10`; sidebar: `#101017`; panel: `#13131c`.
- Text: `#f1f0f7`; secondary text: `#a6a5b9`.
- Main action and selection: acid lime `#d8fc71`.
- AI illustration and chart: violet `#a494fc`.
- Model identification: muted mint, peach, blue and cyan.
- Error: pale red `#ffa4b3`; pending: amber.

## Typography

Self-hosted Onest for Russian interface text; JetBrains Mono for IDs and small
technical labels. Body and controls use normal casing. Headings use restrained
negative tracking, never below -0.04em. The display treatment belongs only to the
overview and sign-in screen.

## Structure

Fixed sidebar and compact top bar; desktop overview uses an asymmetric hero and
wallet row, a single metrics strip, chart and recent-dialog list. Secondary views
are task-focused: chat, model selection, usage, keys, wallet and account details.
Under 700px the sidebar becomes a dismissible drawer. Tables keep their own
horizontal scroll, while page content fits the viewport.

## Components and motion

Panels use a thin border, 8–12px radii and no soft drop shadow. Primary actions use
lime with dark ink; secondary actions use neutral surfaces. Focus stays visible.
Native dialogs handle search and help. Loading, error, empty, pending and success
states carry text, not color alone.

The canvas torus is a code-generated abstract AI illustration; it pauses outside
the viewport, in background tabs and under reduced-motion preferences. No WebGL
or image-generation dependency is needed. UI transitions last 170–200ms and
respect reduced motion.

The former Jinja pages keep their existing visual identity during migration.
