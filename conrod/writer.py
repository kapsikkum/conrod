"""Writing keywords back out as XMP.

RAW files get a sidecar (.xmp beside the frame), which is what Lightroom and
Bridge expect and which leaves the original bytes untouched. JPEGs get the
keywords embedded, plus legacy IPTC so Photo Mechanic sees them too.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .config import JPEG_SUFFIXES, Settings
from .exif import ExifTool


@dataclass
class WriteResult:
    path: Path
    target: Path
    keywords: list[str]
    ok: bool
    message: str = ""


def sidecar_for(image: Path) -> Path:
    """Lightroom's convention is IMG_1234.xmp, not IMG_1234.CR3.xmp."""
    return image.with_suffix(".xmp")


def write_keywords(tool: ExifTool, image: Path, keywords: Sequence[str],
                   settings: Settings, caption: str | None = None,
                   rating: int | None = None,
                   label: str | None = None) -> WriteResult:
    """Write one frame's keywords, and how good the frame is.

    ``rating`` and ``label`` carry the cull's verdict into the catalogue,
    where it can actually be acted on: stars to sort by and a colour to
    filter on, rather than a number in a database only Conrod can read.
    """
    keywords = [k for k in dict.fromkeys(keywords) if k]
    if not keywords and rating is None and label is None:
        return WriteResult(image, image, [], True, "no keywords")

    is_jpeg = image.suffix.lower() in JPEG_SUFFIXES

    if is_jpeg or not settings.write_sidecar_for_raw:
        target = image
    else:
        target = sidecar_for(image)
        if not target.exists():
            created = _create_sidecar(tool, image, target)
            if not created:
                return WriteResult(image, target, keywords, False,
                                   "could not create XMP sidecar")

    if not keywords:
        # Nothing to keyword, but there is still a verdict to record: a frame
        # the cull dropped has no vehicle worth naming and is exactly the one
        # that needs to arrive in the catalogue marked red. Running the
        # keyword command with no keywords in it reported failure for every
        # such frame while the label was in fact written.
        ok = _write_verdict(tool, target, rating, label, is_jpeg, settings)
        return WriteResult(image, target, [], ok, "rating only")

    tags = ["XMP-dc:Subject", "XMP-lr:HierarchicalSubject"]
    if is_jpeg:
        # Photo Mechanic and older catalogues still read legacy IPTC.
        tags.append("IPTC:Keywords")

    args = ["-overwrite_original", "-charset", "filename=utf8"]
    # Delete each keyword before adding it. exiftool's '+=' appends
    # unconditionally — '-api nodups' does not suppress that — so writing a
    # shoot twice would otherwise stack every keyword a second time. Removing
    # first makes the write idempotent while leaving keywords this tool did
    # not add untouched.
    for tag in tags:
        args += [f"-{tag}-={kw}" for kw in keywords]
    for tag in tags:
        args += [f"-{tag}+={kw}" for kw in keywords]
    args.append(str(target))

    output = tool.execute(*args)
    ok = "1 image files updated" in output or "1 output files created" in output

    if caption:
        _write_caption(tool, target, caption, is_jpeg, settings)
    if rating is not None or label is not None:
        _write_verdict(tool, target, rating, label, is_jpeg, settings)
    return WriteResult(image, target, keywords, ok, output.strip())


def _write_verdict(tool: ExifTool, target: Path, rating: int | None,
                   label: str | None, is_jpeg: bool, settings: Settings) -> bool:
    """Put the cull's judgement where a photographer already looks for it.

    Separate from the keyword write for the same reason the caption is: these
    are single values rather than lists, so writing one replaces what is
    there. That matters more here than anywhere else in this file -- a rating
    is often the photographer's own first pass, and overwriting it would
    destroy an afternoon's work without saying so.

    So unless told otherwise Conrod only fills in what is missing. The rating
    cannot use exiftool's create-only mode to decide that, because a camera
    writes ``Rating=0`` meaning *unrated*: the tag is present, create-only
    skips it, and the star rating silently never appears. Zero is therefore
    treated as absent, which is what every catalogue means by it.
    """
    keep_rating = not getattr(settings, "overwrite_rating", False)
    keep_label = not getattr(settings, "overwrite_label", False)
    wrote = False
    base = ["-overwrite_original", "-charset", "filename=utf8"]

    if rating is not None and getattr(settings, "write_rating", True):
        args = list(base)
        if keep_rating:
            # Write only where the frame is unrated. "0" is unrated.
            args += ["-if", 'not $Rating or $Rating eq "0"']
        args.append(f"-XMP:Rating={rating}")
        if is_jpeg:
            args.append(f"-EXIF:Rating={rating}")
        args.append(str(target))
        out = tool.execute(*args)
        wrote = wrote or "1 image files updated" in out

    if label is not None and getattr(settings, "write_label", True):
        args = list(base)
        if keep_label:
            args += ["-wm", "cg"]      # a label has no "unset but present" value
        args += [f"-XMP:Label={label}", str(target)]
        out = tool.execute(*args)
        wrote = wrote or "1 image files updated" in out

    return wrote


def _write_caption(tool: ExifTool, target: Path, caption: str, is_jpeg: bool,
                   settings: Settings) -> None:
    """Fill in a caption without destroying one the photographer wrote.

    This is a separate exiftool call because it needs a different write mode.
    Keywords are merged -- ours are added alongside whatever is already
    there -- but a description is a single value, so writing one replaces it.
    An earlier version did exactly that, and a caption typed in Lightroom was
    silently lost the first time a shoot was keyworded.

    '-wm cg' is create-only: exiftool writes the tag when it is absent and
    leaves it alone when it is not.
    """
    args = ["-overwrite_original", "-charset", "filename=utf8"]
    if not settings.overwrite_caption:
        args.append("-wm")
        args.append("cg")
    args.append(f"-XMP-dc:Description={caption}")
    if is_jpeg:
        args.append(f"-IPTC:Caption-Abstract={caption}")
    args.append(str(target))
    tool.execute(*args)


def _create_sidecar(tool: ExifTool, image: Path, sidecar: Path) -> bool:
    """Seed a new sidecar from the RAW file's own XMP block."""
    tool.execute(
        "-charset", "filename=utf8",
        "-o", str(sidecar),
        "-XMP:all",
        str(image),
    )
    if sidecar.exists():
        return True
    # Some RAWs carry no XMP at all, so there is nothing to copy out; write a
    # bare sidecar and let the keyword pass fill it.
    sidecar.write_text(_EMPTY_XMP, encoding="utf-8")
    return sidecar.exists()


_EMPTY_XMP = """<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""/>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>
"""
