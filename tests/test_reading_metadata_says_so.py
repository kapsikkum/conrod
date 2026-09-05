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

import inspect
import unittest
import unittest.mock
from pathlib import Path

from conrod import pipeline


class TheReadReportsItself(unittest.TestCase):
    """About the whole-album pass specifically.

    Bounded with inspect rather than by slicing between two names in the
    file: functions got added between those names and the slice quietly
    started covering them, which failed the assertion for a reason that had
    nothing to do with what it is checking. fill_frames next door reads in
    one call on purpose -- it is handed a chunk of thirty-two and is the
    thing doing the chunking.
    """

    def body(self) -> str:
        return inspect.getsource(pipeline._record_origins)

    def test_it_reads_in_chunks_rather_than_one_call(self) -> None:
        self.assertIn("culling.CULL_CHUNK", self.body())
        self.assertNotIn("rows = tool.read_tags(files, wanted)", self.body())

    def test_it_says_how_far_through_it_is(self) -> None:
        """Behaviour, not the exact words: the read reports a count and a
        total as it goes. Pinning the literal broke the moment the read
        moved to several exiftool processes and started reporting as
        batches landed rather than as they were handed out."""
        called = []
        rows = pipeline.read_tags_many(
            [Path(f"nowhere/{n}.CR3") for n in range(5)], ["Model"],
            workers=2, chunk=2,
            on_progress=lambda done, of: called.append((done, of)))
        self.assertTrue(called, "nothing reported progress")
        for done, of in called:
            self.assertLessEqual(done, of)
        self.assertEqual(called[-1][1], 5)

    def test_the_pass_hands_the_reader_a_progress_callback(self) -> None:
        self.assertIn("on_progress=tick", self.body())

    def test_a_stop_is_heard_between_chunks(self) -> None:
        self.assertIn("should_stop()", self.body())


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
