# Bundled glyph fonts

`render.py` passes this directory to FFmpeg's `ass` filter as `fontsdir`.

libass does not use `fontsdir` for glyph fallback on macOS, so the title text
switches font explicitly for emoji runs (`_title_text` in `render.py`) instead
of relying on the system emoji font. Apple Color Emoji is a bitmap font that
libass cannot draw, which is why an outline emoji font is bundled.

| File | Purpose | Source |
| --- | --- | --- |
| `NotoEmoji.ttf` | Monochrome emoji glyphs in opening titles | [google/fonts `ofl/notoemoji`](https://github.com/google/fonts/tree/main/ofl/notoemoji), variable weight |
| `OFL.txt` | License for the font above | Same repository |

The font is licensed under the SIL Open Font License 1.1; see `OFL.txt`.
The license permits redistribution with this repository.
