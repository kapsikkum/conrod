# Conrod

Vehicle keywording for motorsport and car photography. Point it at a folder of
frames; it finds cars and bikes, reads competition numbers, plates and livery
text, works out make, model and colour, and writes it all into XMP for
Lightroom, Bridge, Photo Mechanic and Capture One.

![Scanning a folder of frames](docs/screenshots/scan.png)

![Reviewing what was found](docs/screenshots/review.png)

## Run it

Download the latest zip from [Releases](../../releases), unpack it, run
`Conrod.exe`.

Needs [ExifTool](https://exiftool.org/) on `PATH`, and
[Ollama](https://ollama.com/) + `ollama pull qwen2.5vl:7b` for make/model/colour
(optional — without it, Conrod still reads plates, numbers and livery text).
The Setup screen checks both and links to what's missing.

From source:

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python main.py
```

`main.py --browser` opens in a browser tab instead of a native window.
`main.py --cli run <folder>` / `review` / `write` / `jobs` gives a command
line. `main.py --selftest` runs a real detector, OCR and plate-detector pass —
the first thing to try if a build misbehaves.

## The entry list

Optional. A CSV with a `number` column — every other column becomes a
keyword, so a bare two-column grid and a full entry list both work with no
configuration:

```csv
number,driver,team,class,sponsor
88,Broc Feeney,Triple Eight Race Engineering,Supercars,Red Bull
```

A frame with car 88 gets keyworded `88`, `#88`, `Car 88`, `Broc Feeney`,
`Triple Eight Race Engineering`, `Supercars`, `Red Bull`. A cell can hold
several values separated by `;` or `,`. Where the entry list names a number,
it beats anything read off the car's own panels.

## Good to know

- **Non-destructive.** RAW frames get an `.xmp` sidecar; the original file is
  never touched. Only JPEGs are written to directly. Keywords are deleted then
  re-added on every write, so scanning the same shoot twice doesn't stack
  duplicates.
- **Culling-aware.** Keywording happens after the cull — rejected frames are
  skipped by default, and a minimum star rating or colour label can be
  required. Ratings are read from the `.xmp` sidecar first and the file
  second, the order Lightroom and Bridge write them.
- **Canon-first.** Tested throughout on `.cr3`/`.cr2`. `.jpg`/`.jpeg` are
  fully supported. Other RAW (`.crw`, `.arw`, `.raf`, `.orf`, `.rw2`, `.dng`)
  is accepted but unverified; `.nef` is not supported. Open an issue with a
  few sample frames if you shoot something else.
- **Data lives in `%USERPROFILE%\.conrod`** — models, previews, the job
  database, and `conrod.log` if something goes wrong before the window opens.

## Models

Each stage uses the model that's actually good at it, rather than asking one
model to do everything:

| Task | Model | Why |
|---|---|---|
| Vehicle detection | YOLO11s | Small and fast, runs on CPU |
| Plate detection | [open-image-models](https://github.com/ankandrew/open-image-models) | 7.5 MB, ~60ms/frame; also boxes competition-number roundels, which reads far better than OCR across the whole car |
| Plate OCR | fast-plate-ocr | Trained on plates specifically — 18/18 test crops correct vs 5/18 for general OCR, and ~40x faster |
| Number & livery text | RapidOCR | General-purpose, for anything that isn't a plate |
| Grouping one car across a burst | dinov2-small (quantized) | Cheap visual similarity to merge crops of the same vehicle |
| Make, model, colour, team | qwen2.5vl:7b via Ollama | See below |

### Why qwen2.5vl:7b

Every vision-language model that fits in 8 GB VRAM, same 13 real crops, same
prompt:

| model | sharp crops correct | per crop |
|---|---|---|
| **qwen2.5vl:7b** | **11 / 11** | 2.7 s |
| gemma3:4b | 4 / 11 | 5.6 s |
| minicpm-v:8b | 3 / 11 | 6.4 s |
| qwen3-vl:8b | worse | 2.6 s |

Not close. qwen3-vl is newer and worse here, and puts its answer in a
`thinking` field a normal reader sees as empty. The vision model also cannot
read plate characters at any resolution tried, which is why plate reading is
a separate detector + OCR pair rather than one more thing asked of the VLM.

## Test

```bash
.venv/Scripts/python -m unittest discover -s tests
```

## Contribute

```
scan -> preview -> detect -> plate/number/text -> identify (VLM) -> merge -> review -> write
```

Everything runs per-vehicle, not per-frame — that's what stops a trackside
banner being keyworded onto every car that passes it. `conrod/pipeline.py` is
the orchestration; each reader (`plates.py`, `vlm.py`, `normalise.py`, ...) is
independent and swappable.

Issues and PRs welcome.

### Releasing

Push a tag and CI builds the Windows app, self-tests the frozen exe, and
attaches it to a GitHub Release:

```bash
git tag v0.9.0 && git push origin v0.9.0
```

## Licensing

Detection uses Ultralytics YOLO, which is **AGPL-3.0** — anyone distributing a
build must make source available on the same terms. The plate detector
([open-image-models](https://github.com/ankandrew/open-image-models)) is MIT.
