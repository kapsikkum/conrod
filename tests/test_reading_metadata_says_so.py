"""A long read has to look like a long read.

Opening six thousand RAW files takes minutes. Doing it in one exiftool call
with no progress showed a full progress bar, "about 0s left" and nothing
else for the whole time, which reads as a hang rather than as work -- and
the app was then doing it twice, once for capture times and once for
ratings.

culling.read_culls already chunked and reported for exactly this reason. The
burst pass did not, and the ratings pass added beside it did not either.
"""

from __future__ import annotations

import unittest
import unittest.mock
from pathlib import Path

from conrod import pipeline


class TheReadReportsItself(unittest.TestCase):
    def test_it_reads_in_chunks_rather_than_one_call(self) -> None:
        source = Path(pipeline.__file__).read_text(encoding="utf-8")
        body = source[source.index("def _record_origins"):
                      source.index("def _existing_marks")]
        self.assertIn("culling.CULL_CHUNK", body)
        self.assertNotIn("rows = tool.read_tags(files, wanted)", body)

    def test_it_says_how_far_through_it_is(self) -> None:
        source = Path(pipeline.__file__).read_text(encoding="utf-8")
        body = source[source.index("def _record_origins"):
                      source.index("def _existing_marks")]
        self.assertIn('"done": done, "total": total', body)

    def test_a_stop_is_heard_between_chunks(self) -> None:
        source = Path(pipeline.__file__).read_text(encoding="utf-8")
        body = source[source.index("def _record_origins"):
                      source.index("def _existing_marks")]
        self.assertIn("should_stop()", body)


class TheFilesAreNotOpenedTwice(unittest.TestCase):
    """The ratings pass rides on the burst pass wherever it can.

    Both want tags off the same files and the burst call already asks for
    the mark tags, so a frame with no sidecar is answered already. Only the
    ones that actually have a sidecar are worth a second look, and those are
    small text files rather than RAWs.
    """

    def test_a_shoot_with_no_sidecars_needs_no_second_pass(self) -> None:
        rows = [{"SourceFile": "C:/shoot/a.CR3", "Rating": 3, "Label": "Green"},
                {"SourceFile": "C:/shoot/b.CR3", "Rating": 0, "Label": ""}]
        files = [Path("C:/shoot/a.CR3"), Path("C:/shoot/b.CR3")]

        def explode(*_args, **_kwargs):        # a second pass would call this
            raise AssertionError("opened the files again for no reason")

        with unittest.mock.patch.object(pipeline.culling, "read_culls", explode):
            out = pipeline._existing_marks(files, rows)
        self.assertEqual(out[str(files[0])], (3, "Green"))
        self.assertEqual(out[str(files[1])], (0, ""))

    def test_what_the_file_said_is_still_read(self) -> None:
        """A JPEG carries its rating internally and has no sidecar at all."""
        rows = [{"SourceFile": "C:/shoot/c.jpg", "Rating": 5, "Label": "Blue"}]
        out = pipeline._existing_marks([Path("C:/shoot/c.jpg")], rows)
        self.assertEqual(out[str(Path("C:/shoot/c.jpg"))], (5, "Blue"))


if __name__ == "__main__":
    unittest.main()
