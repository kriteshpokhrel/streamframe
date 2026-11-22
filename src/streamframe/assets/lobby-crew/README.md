# Lobby Crew export preset

Bundled artwork for the branding that every editor export uses. The renderer
picks this directory up automatically; see `render.py` for the layout and
`jobs.py` (`brand_preset`) for how a machine-specific override wins.

| File | Used for | Notes |
| --- | --- | --- |
| `outro.png` | Title-card badge and the animated outro logo | The full square logo, opaque. Scaled to 900 px inside the outro card and to 124 px in the title badge. |
| `watermark.png` | The static wordmark on the webcam/gameplay divider | Transparent PNG authored 172 px wide at 1080 × 1920 output scale, so `render.py` draws it 1:1 and centers it on the divider. Artwork only — no stream overlay graphics. |

Do not raster-edit these files in place; replace them from the original
artwork instead. The export layout (violet title card, lavender divider,
1.5-second outro over a blurred replay) is code, not artwork, and the social
handles live in `render.py`.

The original vector artwork for `outro.png` was not available when the preset
was committed, so the files above are the shipped artwork at working
resolution. `watermark.png` was reconstructed from an approved export because
the wordmark source file was lost; replacing it with a clean high-resolution
export of the same lockup is an improvement, not a behavior change.
