---
name: hyperframes-authoring
description: Write or fix a HyperFrames composition (HTML, CSS, SVG, GSAP, Three.js) for the factory's headless, picture-only render pipeline. Use for every scene-code and fix stage.
---

# HyperFrames authoring (headless, picture-only)

You write composition files into the project folder. The harness lints, checks and renders them. Files are your whole output.

## The contract

Every composition meets all of these:

- **Seekable.** Every visual change is a pure function of time: a paused GSAP timeline, or `render(t)` driven by `hf-seek`. The same `t` always draws the same frame.
- **Seeded.** Randomness comes from a seeded generator (mulberry32 with the brief's seed), named `rng()`.
- **Guardrail.** These never appear in a composition: `Date.now`, `performance.now`, `requestAnimationFrame`, CSS `@keyframes` or `animation:`, Web Animations (`element.animate`), unseeded `Math.random`.
- **Picture only.** No `<audio>`, and no `<video>` with sound. Narration and music are mixed later, so a `film_silent` warning is expected.
- **Local.**
  - GSAP loads from `./vendor/gsap.min.js` and three.js from `./vendor/three.module.js`, both inside the project. The harness copies them in, and a CDN `<script>` tag leaks a network call.
  - Fonts are local files.
  - Every URL resolves inside the project or to 127.0.0.1.
- **Declared.**
  - Every text element carries `data-text-box`.
  - Every timed event carries `data-event="<name>"` and `data-at="<seconds>"`.

  The quality gate reads these declarations.
- **Framed.**
  - The subject fills at least a third of the frame height at key beats.
  - All text sits above 65% of the frame height, because the bottom 35% belongs to the platform's buttons.

## Steps

1. Read the brief and `timing.json`, which holds word and sentence times in seconds. **Done when** every sentence has a planned beat.
2. Read `references/spec.md`, `references/runtimes.md` and `references/format.md`, plus the type files for this piece from the table below. **Done when** you know the composition structure and the data attributes you will use.
3. Write `index.html`, plus any scene files, with each beat timed from `timing.json`. **Done when** the timeline covers the full narration duration.
4. Self-check: search your files for each guardrail term, and confirm every text box and timed event is declared. **Done when** the search finds nothing and every declaration is present.
5. **Fix stage only:** read the findings JSON and fix each finding at its source file and selector. A finding below has its fix in a reference, so read that reference first. **Done when** every finding has an edit.

   | Finding | Its fix |
   |---|---|
   | `line_unread` on a caption group | `references/captions.md`, Structure: the group sits outside the one `data-track-kind="captions"` element. Move it in. A caption held longer talks over the next one |
   | `frame_out_of_frame` on drawn art a camera moves | `references/motion.md`, a camera on drawn art: move a `<g>` inside the `<svg>` |
   | `container_overflow` or `escaped_container` on what a camera moves | `references/motion.md`, a camera on drawn art: `data-layout-allow-overflow` on the moved element, text kept outside it |
   | `contrast_aa_failure` | Recolour the text to the finding's `suggestedColor`, or set it on its plate; the scene playbook's palette says which colours read where |
   | `rotation_pivot_drift` on a part that turns about one end | `references/types/character.md`, Build: `svgOrigin` at the hinge and `data-layout-allow-orbit` |
   | `text_below_floor` | Set the type at 28 px or more on screen, or cut the text. Never `data-layout-ignore`, which the factory fails |
   | `gsap_timeline_set_initial_hide` | `references/runtimes.md`: hide it before the timeline, reveal it with a tween |

## References

These come from hyperframes 0.9.9 (Apache-2.0; see `LICENSE`). The factory has changed some of them, and each changed file says so at its end.

| Read | When |
|---|---|
| `references/spec.md`, `references/runtimes.md`, `references/format.md` | Always |
| `references/motion.md` | Camera moves, easing, transitions |
| `references/captions.md` | Captions: karaoke groups for explainers, one word at a time for the story |
| `references/types/explainer.md`, `references/types/vertical.md` | Explainer pieces |
| `references/story.md`, `references/types/3d.md`, `references/types/character.md` | The story piece |
| `references/look.md`, `references/review.md` | Polish and self-review |

The references were written for an interactive session, but this session is headless:
- Wherever a reference offers a person a choice, take your own recommendation and build it. In `story.md`, that is the "no stops" path.
- Wherever a reference mentions Studio or a preview, the harness's lint, check and render are the preview.
