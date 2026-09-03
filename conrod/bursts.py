"""Which camera took it, and which burst it belongs to.

Two shooters at one event produce one folder of interleaved filenames, and
IMG_0451 from each of them sorts next to nothing in particular. Worse, they
both fire at the same car seconds apart, so "frames close together in time"
is not a burst unless it is also "frames from the same body".

The camera is taken from the serial number where the file carries one, which
is the only thing that distinguishes two identical bodies at the same event.
Where it does not, the model plus the lens is a reasonable stand-in, and
where there is nothing at all every frame falls back to one camera named for
the folder, which is what a single-shooter job looks like anyway.

A burst is then consecutive frames from one camera with no gap longer than a
threshold. That threshold is not a frame rate: a photographer holding the
shutter down at 10fps and one squeezing off singles as a car comes past are
both shooting one car, and the gap that separates them from the *next* car is
seconds long. So the default is generous, and the point is the gap between
cars rather than the interval between frames.

Bursts matter twice over. They are a strong prior for grouping -- twelve
frames of one pass are almost always one vehicle -- and they are the unit the
vision model should be asked about, because eight views of a car answer a
question that one blurred three-quarter view cannot.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Longer than this between two frames of the same camera and they belong to
# different bursts. Generous on purpose: the gap being detected is the one
# between two cars going past, not the one between two shutter actuations.
BURST_GAP_SECONDS = 4.0

# The tags worth asking exiftool for. Kept here so the caller does not have to
# know which of them identifies a body.
TAGS = ("SerialNumber", "InternalSerialNumber", "Model", "Make",
        "LensModel", "LensID", "DateTimeOriginal", "SubSecTimeOriginal",
        "SubSecDateTimeOriginal")


@dataclass
class Frame:
    path: str
    camera: str
    taken: float | None      # seconds; None when the file carries no time
    burst: int = 0


@dataclass
class Burst:
    key: int
    camera: str
    frames: list[str] = field(default_factory=list)
    started: float | None = None
    ended: float | None = None

    def __len__(self) -> int:
        return len(self.frames)


def camera_of(tags: dict, fallback: str = "camera") -> str:
    """A stable name for the body that took this frame.

    The serial is what actually separates two shooters carrying the same
    model, so it wins wherever it exists. Everything below it is a
    best-effort stand-in rather than an identity, and is labelled as one so a
    second body is at least visibly a different camera in the UI.
    """
    serial = _first(tags, "SerialNumber", "InternalSerialNumber")
    model = _first(tags, "Model")
    if serial:
        return f"{model} {serial}".strip() if model else str(serial)
    if model:
        # No serial. Two identical bodies collapse into one camera here, and
        # nothing in the file can prevent that -- but the lens often differs
        # between shooters and costs nothing to include.
        lens = _first(tags, "LensModel", "LensID")
        return f"{model} + {lens}".strip() if lens else str(model)
    return fallback


def taken_at(tags: dict) -> float | None:
    """When the shutter fired, in seconds, sub-second precision if recorded.

    Sub-seconds are what make bursts separable at all: a 10fps camera stamps
    ten frames with the same whole second, so without them a burst is one
    indivisible lump and the gap between two cars is invisible.
    """
    stamp = _first(tags, "SubSecDateTimeOriginal", "DateTimeOriginal")
    if not stamp:
        return None
    seconds = _parse_stamp(str(stamp))
    if seconds is None:
        return None
    if "SubSecDateTimeOriginal" not in tags:
        sub = _first(tags, "SubSecTimeOriginal")
        if sub is not None:
            try:
                seconds += float(f"0.{str(sub).strip()}")
            except ValueError:
                pass
    return seconds


def _parse_stamp(stamp: str) -> float | None:
    """EXIF times are "YYYY:MM:DD HH:MM:SS", optionally .sss, optionally a zone.

    Parsed by hand rather than with strptime because the variants are few and
    the failures are many: a camera with a flat clock battery writes
    "0000:00:00 00:00:00", which strptime raises on and which means only
    "unknown".
    """
    text = stamp.strip().replace("T", " ")
    for cut in ("+", "-"):
        head = text.split(" ")[-1]
        if cut in head:
            text = text[:text.rindex(cut)]
    parts = text.replace("/", ":").replace("-", ":").split(" ")
    if len(parts) < 2:
        return None
    date_bits = parts[0].split(":")
    time_bits = parts[1].split(":")
    if len(date_bits) != 3 or len(time_bits) < 3:
        return None
    try:
        year, month, day = (int(b) for b in date_bits)
        hour, minute = int(time_bits[0]), int(time_bits[1])
        second = float(time_bits[2])
    except ValueError:
        return None
    if year < 1970 or not 1 <= month <= 12 or not 1 <= day <= 31:
        return None

    import calendar
    try:
        base = calendar.timegm((year, month, day, hour, minute, 0, 0, 0, 0))
    except (ValueError, OverflowError):
        return None
    return float(base) + second


def _first(tags: dict, *names: str):
    for name in names:
        value = tags.get(name)
        if value not in (None, "", "-"):
            return value
    return None


def describe(rows: list[dict], *, fallback: str = "camera",
             gap: float = BURST_GAP_SECONDS) -> list[Frame]:
    """Turn exiftool rows into frames tagged with camera and burst.

    Rows need a "SourceFile" and whatever of TAGS the file carried.
    """
    frames = [
        Frame(path=str(row.get("SourceFile") or ""),
              camera=camera_of(row, fallback),
              taken=taken_at(row))
        for row in rows
    ]
    return assign_bursts(frames, gap=gap)


def assign_bursts(frames: list[Frame], *,
                  gap: float = BURST_GAP_SECONDS) -> list[Frame]:
    """Number the bursts, per camera, in time order.

    Frames with no timestamp cannot join a burst on evidence, so each gets
    one of its own rather than being swept into whichever burst happens to be
    open. A file with no clock is not proof of anything, and a burst is only
    useful if belonging to it means something.
    """
    by_camera: dict[str, list[Frame]] = {}
    for frame in frames:
        by_camera.setdefault(frame.camera, []).append(frame)

    key = 0
    for camera in sorted(by_camera):
        timed = [f for f in by_camera[camera] if f.taken is not None]
        untimed = [f for f in by_camera[camera] if f.taken is None]
        timed.sort(key=lambda f: (f.taken, f.path))

        previous: float | None = None
        for frame in timed:
            if previous is None or frame.taken - previous > gap:
                key += 1
            frame.burst = key
            previous = frame.taken

        for frame in untimed:
            key += 1
            frame.burst = key
    return frames


def collect(frames: list[Frame]) -> list[Burst]:
    """The bursts themselves, in the order they were shot."""
    bursts: dict[int, Burst] = {}
    for frame in frames:
        burst = bursts.get(frame.burst)
        if burst is None:
            burst = bursts[frame.burst] = Burst(key=frame.burst,
                                                camera=frame.camera)
        burst.frames.append(frame.path)
        if frame.taken is not None:
            burst.started = (frame.taken if burst.started is None
                             else min(burst.started, frame.taken))
            burst.ended = (frame.taken if burst.ended is None
                           else max(burst.ended, frame.taken))
    return sorted(bursts.values(),
                  key=lambda b: (b.started if b.started is not None else 0.0,
                                 b.key))
