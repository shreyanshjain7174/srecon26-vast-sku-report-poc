# Google Slides import QA

## Final local artifact

- PPTX: `artifacts/presentation/srecon26-llm-hpa-evidence-poc.pptx`
- SHA-256: `f6b027b3a3f45b8f1fc9713616dec2e796489426abe37d0b332eeaadd7cb26f9`
- Layout: 16:9, 16 slides, 16 speaker-note sections
- PDF render: 16 pages, unencrypted, regenerated after the final PPTX
- PDF SHA-256: `4aad79bb58b1f7a6afe2992960a6a9308848f580c8e1f2d02a779ac9479405b5`

The PPTX ZIP and every OOXML part parse successfully. It contains ordinary
text shapes, three PNG images, and one native clustered-column chart on slide
8 backed by an embedded workbook. It contains no external relationships,
linked media, animation, transitions, SmartArt, grouped shapes, or tables.
Arial and Cambria are the only declared fonts.

The final PDF was rendered into all 16 slide images under
`artifacts/presentation/qa-google-import-ready-20260923/`. A full contact-sheet
review plus full-size inspection of slides 8, 12, and 16 found no local
clipping, overlap, reversed flow, missing image, or chart-label regression.
This proves the final local binary renders coherently; it does not substitute
for Google's conversion render.

## Import risk

Slide 8's native chart is the only material Google Slides conversion risk.
Text uses PowerPoint auto-fit, so browser rendering must still be checked for
line reflow or clipping even though the local render passes.

## Browser release gate

Google Slides was reached in an authenticated Chrome session for
`007ssancheti@gmail.com` on 2026-09-23. The upload was not performed because
uploading the local PPTX is an external file-transfer action requiring
action-time confirmation.

After confirmed upload, verify all 16 slides in order, slide 8's chart, the
three embedded PNGs, title/body fonts, clipping, and speaker notes. Record the
Google Slides URL, UTC import time, uploaded PPTX hash, slide count, and any
conversion replacements. Do not mark the release gate complete merely because
the file appears in Drive.
