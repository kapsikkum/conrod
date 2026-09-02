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
                   settings: Settings, caption: str | None = None) -> WriteResult:
    """Write one frame's keywords. Returns a result rather than raising."""
    keywords = [k for k in dict.fromkeys(keywords) if k]
    if not keywords:
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
    if caption:
        args += [f"-XMP-dc:Description={caption}"]
        if is_jpeg:
            args += [f"-IPTC:Caption-Abstract={caption}"]
    args.append(str(target))

    output = tool.execute(*args)
    ok = "1 image files updated" in output or "1 output files created" in output
    return WriteResult(image, target, keywords, ok, output.strip())


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
