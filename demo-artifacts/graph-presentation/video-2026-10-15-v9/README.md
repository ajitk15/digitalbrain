# CarePath video v8: one rule, five files, one story

About six minutes, ten chapters, 1920 × 1080 at 24 fps, Indian English narration.
It follows the review in `../v7-review-and-v8-script.md`.

**The example.** "Every list API must cap its page size on the server" runs through five
real CarePath files: the non-functional requirements spreadsheet, the coding standard, the
API specification, the traceability matrix and the outage postmortem. The test review found
the gap on 28 May, the release review waived it on 2 June, and on 14 August a search for
one lakh records took the service down for 45 minutes. On screen, plain names replace short
identifiers. The stored records keep them.

**What is real.** File types and content, the file's SHA-256 hash, the Markdown shown
(trimmed, with the ID column hidden), all six source-check scores (100 for every file and
60 for the raw export, from the platform's own rubric), the shared "Blocks release" value
node, the dates and outage figures, the ontology's 8 types and 12 relationships, and the
benchmark's 15 of 16 (scorecard baseline).

**What is illustrative.** Footnoted on screen: the AI proposals and the rejected one, the
theme names, the index terms, the vector values and the answer wording. Before showing the
film to customers, run these five files through a real AI build and replace the proposals
and the answer with what it actually produces.

## How it is made

| Step | Command |
|---|---|
| Spoken text | `python timeline.py spoken` |
| Narration, one WAV per sentence | `powershell.exe -ExecutionPolicy Bypass -File narrate.ps1` |
| Timeline, captions, chapters | `python timeline.py assemble` |
| Still frames | `.venv/Scripts/python.exe render.py preview 60 150 245` |
| Film | `.venv/Scripts/python.exe render.py film` |
| Checks and contact sheet | `.venv/Scripts/python.exe verify_video.py` |

`scene.html` and `scene.js` are one page in which every visual is a pure function of time.
Playwright drives the installed Chrome, which seeks to each frame and captures it. Animation
cues hang off the start of each recorded sentence, so a script change re-times everything.

**Voice.** Only the older Windows voices (Heera, Ravi) are installed. Heera speaks at engine
rate 1.2, which is the voice's own setting and not a time-stretch of finished audio, with
pauses between sentences. A neural en-IN voice (Azure `en-IN-NeerjaNeural` or
`en-IN-PrabhatNeural`) or a human read would sound noticeably more natural. Re-record into
`speech/` with the same file names, then rerun the timeline and the film.
