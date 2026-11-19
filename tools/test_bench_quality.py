"""tools/test_bench_quality.py - the parts of the quality harness that can be wrong without a GPU.

What this instrument can get wrong is not crashing: it is producing a number for a comparison that was never
made. Three ways that happens, all of them cheap to test and all of them invisible in the output:

  - the two arms read different text, and the rows are paired anyway;
  - the skip arm never skipped anything, so the complete model is compared against itself and the agreement comes
    out at 100%, which reads as "the skip costs nothing";
  - the engine wrote no scores at all -- the usual cause being the containment refusing the write -- and an empty
    file is treated as an empty result rather than a failed run.

Each of those is a refusal in bench_quality.py, so each gets a test here. The arithmetic gets tests against
hand-worked values, because a metric that is wrong by a constant factor still looks like a plausible metric.

    python3 -m unittest discover -s tools -p 'test_bench_*.py'
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bench_quality as bq


def row(pos: int, target: int, tlp: float, top: int, toplp: float = 0.0) -> bq.Row:
    return bq.Row(pos=pos, target=target, target_logprob=tlp, top=top, top_logprob=toplp)


class ParseLogposTest(unittest.TestCase):
    def test_it_reads_the_five_columns_it_uses(self) -> None:
        # One line as src/core/verify.cpp#window_logprobs writes it: eight tab-separated fields.
        line = "41\t9707\t-0.250000000\t9707\t-0.250000000\t1\t-12.5\t-0.249\n"
        rows = bq.parse_logpos(line)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].pos, rows[0].target, rows[0].top), (41, 9707, 9707))
        self.assertAlmostEqual(rows[0].target_logprob, -0.25)

    def test_blank_lines_are_skipped(self) -> None:
        text = "1\t2\t-0.5\t2\t-0.5\t1\t-9\t-0.4\n\n2\t3\t-0.5\t4\t-0.1\t0\t-9\t-0.4\n"
        self.assertEqual(len(bq.parse_logpos(text)), 2)

    def test_a_short_line_is_an_error_not_a_skip(self) -> None:
        # A truncated file means the engine died mid-write, which must not quietly become a smaller sample.
        with self.assertRaises(ValueError):
            bq.parse_logpos("1\t2\t-0.5\n")

    def test_an_empty_file_yields_nothing(self) -> None:
        self.assertEqual(bq.parse_logpos(""), [])


class CompareTest(unittest.TestCase):
    def test_identical_arms_agree_completely_and_cost_nothing(self) -> None:
        rows = [row(0, 10, -0.5, 10), row(1, 11, -1.0, 11)]
        c = bq.compare(rows, list(rows))
        self.assertEqual(c.n, 2)
        self.assertEqual(c.top1_agreement, 1.0)
        self.assertAlmostEqual(c.perplexity_ratio, 1.0)
        self.assertEqual(c.max_abs_delta, 0.0)

    def test_top1_agreement_counts_positions_whose_argmax_moved(self) -> None:
        ref = [row(0, 10, -0.5, 10), row(1, 11, -0.5, 11), row(2, 12, -0.5, 12), row(3, 13, -0.5, 13)]
        arm = [row(0, 10, -0.5, 10), row(1, 11, -0.5, 99), row(2, 12, -0.5, 12), row(3, 13, -0.5, 13)]
        self.assertAlmostEqual(bq.compare(ref, arm).top1_agreement, 0.75)

    def test_the_perplexity_ratio_is_the_exponent_of_the_mean_nll_difference(self) -> None:
        # Worked by hand: the text's log-probabilities are -1 and -1 under the reference (mean nll 1.0) and
        # -1.5 and -2.5 under the arm (mean nll 2.0), so the ratio is exp(2.0 - 1.0) = e.
        ref = [row(0, 10, -1.0, 10), row(1, 11, -1.0, 11)]
        arm = [row(0, 10, -1.5, 10), row(1, 11, -2.5, 11)]
        c = bq.compare(ref, arm)
        self.assertAlmostEqual(c.nll_ref, 1.0)
        self.assertAlmostEqual(c.nll_arm, 2.0)
        self.assertAlmostEqual(c.perplexity_ratio, math.e)
        # And the direction is the one a reader assumes: a ratio above 1 means the text got LESS likely.
        self.assertGreater(c.perplexity_ratio, 1.0)

    def test_a_skip_that_helps_one_position_still_reports_the_mean(self) -> None:
        # Not every position has to get worse, and a mean that silently took absolute values would hide that.
        ref = [row(0, 10, -1.0, 10), row(1, 11, -1.0, 11)]
        arm = [row(0, 10, -0.5, 10), row(1, 11, -1.5, 11)]
        c = bq.compare(ref, arm)
        self.assertAlmostEqual(c.perplexity_ratio, 1.0)        # the two cancel
        self.assertAlmostEqual(c.mean_abs_delta, 0.5)          # but the movement is still reported

    def test_the_deltas_are_on_the_texts_own_tokens(self) -> None:
        ref = [row(0, 10, -1.0, 10), row(1, 11, -2.0, 11), row(2, 12, -3.0, 12)]
        arm = [row(0, 10, -1.25, 10), row(1, 11, -2.0, 11), row(2, 12, -3.75, 12)]
        c = bq.compare(ref, arm)
        self.assertAlmostEqual(c.mean_abs_delta, (0.25 + 0.0 + 0.75) / 3)
        self.assertAlmostEqual(c.max_abs_delta, 0.75)

    def test_different_lengths_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            bq.compare([row(0, 10, -1.0, 10)], [row(0, 10, -1.0, 10), row(1, 11, -1.0, 11)])

    def test_different_positions_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            bq.compare([row(0, 10, -1.0, 10)], [row(5, 10, -1.0, 10)])

    def test_different_text_at_the_same_position_is_refused(self) -> None:
        # The positions lining up is not enough: two different prompts of the same length would pair happily and
        # the perplexity ratio would be a comparison of two unrelated texts.
        with self.assertRaises(ValueError):
            bq.compare([row(0, 10, -1.0, 10)], [row(0, 999, -1.0, 10)])

    def test_nothing_to_compare_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            bq.compare([], [])


class SkippedEntriesTest(unittest.TestCase):
    def test_it_reads_the_engines_cumulative_count(self) -> None:
        log = "strata serve: route tail skip: 39571 missed experts skipped (42134 entries) since the start\n"
        self.assertEqual(bq.skipped_entries(log), 42134)

    def test_the_last_line_wins(self) -> None:
        # The engine prints it per request, and the first is the warm-up.
        log = ("strata serve: route tail skip: 1 missed experts skipped (2 entries) since the start\n"
               "strata serve: route tail skip: 10 missed experts skipped (20 entries) since the start\n")
        self.assertEqual(bq.skipped_entries(log), 20)

    def test_no_line_is_a_real_zero(self) -> None:
        # The engine prints the line only when it skipped something, so absence means nothing was dropped --
        # which is exactly what the reference arm has to be able to prove about itself.
        self.assertEqual(bq.skipped_entries("strata serve: route tail skip: OFF - every routed expert ...\n"), 0)


class SourcesTest(unittest.TestCase):
    def test_there_is_a_code_source_and_a_prose_one(self) -> None:
        # Upstream measured the cost as uneven across prompt kinds, so a single figure averaged over one kind
        # would be the wrong number for a machine whose traffic is coding work.
        self.assertIn("code", bq.SOURCES)
        self.assertIn("prose", bq.SOURCES)

    def test_every_source_file_is_in_this_repository(self) -> None:
        # The text is read from the tree rather than embedded, so a renamed or deleted file has to fail here
        # rather than at the end of a twenty-minute run on a busy GPU.
        for name, (rel, _) in bq.SOURCES.items():
            self.assertTrue((bq.PROJECT / rel).exists(), f"{name}: {rel}")

    def test_the_prompt_carries_an_instruction_and_then_the_text(self) -> None:
        text = bq.prompt_text("code", 64)
        rel, instruction = bq.SOURCES["code"]
        self.assertTrue(text.startswith(instruction))
        self.assertGreater(len(text), len(instruction))

    def test_the_length_tracks_the_token_estimate(self) -> None:
        # Not an exact claim -- four characters per token is a rough average -- but a --tokens that did not
        # change the amount of text would silently score the same tiny sample however it was called, which is
        # how the first run of this harness came back with 78 positions.
        short = bq.prompt_text("prose", 64)
        long_ = bq.prompt_text("prose", 1024)
        self.assertGreater(len(long_), len(short) * 4)

    def test_asking_for_nothing_yields_the_instruction_alone(self) -> None:
        self.assertEqual(bq.prompt_text("prose", 0), bq.SOURCES["prose"][1])


if __name__ == "__main__":
    unittest.main()


class NonFiniteTest(unittest.TestCase):
    """A gauntlet run found `compare` reporting `max |delta log p| 0.00000` on data containing a NaN.

    `sorted()` over a sequence with a NaN in it is not an order, so the order statistics came out of whichever
    end the partial comparisons happened to leave there -- and that end read as "no position moved". The cure is
    that a `Row` with a non-finite log-probability cannot be constructed, so no caller can reach the sort with
    one: not the parse, not a test, not a later caller.
    """

    def test_a_nan_logprob_cannot_become_a_row(self) -> None:
        with self.assertRaises(ValueError):
            row(0, 10, float("nan"), 10)

    def test_an_infinite_logprob_cannot_become_a_row(self) -> None:
        # +inf made the perplexity ratio 0.0, which reads as "the skip made the text MORE likely".
        for bad in (float("inf"), float("-inf")):
            with self.assertRaises(ValueError):
                row(0, 10, bad, 10)

    def test_a_non_finite_top_logprob_is_refused_too(self) -> None:
        with self.assertRaises(ValueError):
            bq.Row(pos=0, target=10, target_logprob=-1.0, top=10, top_logprob=float("nan"))

    def test_the_parse_names_the_line_it_refused(self) -> None:
        with self.assertRaises(ValueError) as caught:
            bq.parse_logpos("0\t10\tnan\t10\t-0.5\t1\t-9\t-0.4\n")
        self.assertIn("non-finite", str(caught.exception))


class ColumnCountTest(unittest.TestCase):
    """The format has eight columns; accepting five read whatever sat in column 5 as `top_logprob`."""

    def test_a_five_field_line_is_refused(self) -> None:
        # Enough fields for the five values this reads, but not the format the docstring names -- so the engine
        # writing something else there would have produced a plausible wrong number instead of an error.
        with self.assertRaises(ValueError):
            bq.parse_logpos("0\t10\t-0.5\t10\t-0.5\n")

    def test_a_seven_field_line_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            bq.parse_logpos("0\t10\t-0.5\t10\t-0.5\t1\t-9\n")

    def test_the_eight_column_format_is_accepted(self) -> None:
        self.assertEqual(len(bq.parse_logpos("0\t10\t-0.5\t10\t-0.5\t1\t-9\t-0.4\n")), 1)

    def test_topk_pairs_after_the_eight_are_still_accepted(self) -> None:
        # STRATA_LOGPOS_TOPK appends `id:logprob` pairs AFTER the eight, so longer lines must still parse.
        rows = bq.parse_logpos("0\t10\t-0.5\t10\t-0.5\t1\t-9\t-0.4\t10:-0.5\t11:-1.5\n")
        self.assertEqual((rows[0].pos, rows[0].top), (0, 10))


class DuplicatePositionTest(unittest.TestCase):
    def test_a_position_scored_twice_is_refused(self) -> None:
        # The engine can replay a window after a state restore. Counting the position twice would weight it
        # twice in every mean -- and would defeat run_arm's coverage check, which only catches too FEW rows.
        dup = [row(0, 10, -1.0, 10), row(0, 10, -1.0, 10)]
        with self.assertRaises(ValueError):
            bq.compare(dup, list(dup))

    def test_distinct_positions_are_still_fine(self) -> None:
        ok = [row(0, 10, -1.0, 10), row(1, 11, -1.0, 11)]
        self.assertEqual(bq.compare(ok, list(ok)).n, 2)


class PromptTokensReadTest(unittest.TestCase):
    """The coverage check's input. Absence is a failed run, not a run without a check."""

    def test_it_reads_the_prompt_count(self) -> None:
        self.assertEqual(bq.prompt_tokens_read("DONE produced=1 prompt=2441\n"), (2441, 0))

    def test_it_reads_the_reused_count_separately(self) -> None:
        # Only the fresh tokens are scored, so a reused prefix must not look like missing coverage.
        self.assertEqual(bq.prompt_tokens_read("DONE produced=1 prompt=2441 reused=400\n"), (2441, 400))

    def test_no_done_line_is_an_error_not_a_zero(self) -> None:
        with self.assertRaises(ValueError):
            bq.prompt_tokens_read("strata serve: READY\n")

    def test_the_last_request_wins(self) -> None:
        log = "DONE produced=1 prompt=10\nDONE produced=1 prompt=2441 reused=7\n"
        self.assertEqual(bq.prompt_tokens_read(log), (2441, 7))


class SkippedEntriesWordingTest(unittest.TestCase):
    def test_the_one_shot_summary_wording_is_matched_too(self) -> None:
        # Two wordings of the same fact exist in this engine. Matching only the serve one made the reference
        # arm's "it must have skipped nothing" check pass whenever the other was printed.
        log = "route tail skip          rank 7, 6622 missed experts skipped (7021 routed entries)\n"
        self.assertEqual(bq.skipped_entries(log), 7021)

    def test_the_serve_wording_still_wins_when_both_appear(self) -> None:
        log = ("route tail skip          rank 7, 1 missed experts skipped (2 routed entries)\n"
               "strata serve: route tail skip: 39571 missed experts skipped (42134 entries) since the start\n")
        self.assertEqual(bq.skipped_entries(log), 42134)


class StableSourceTest(unittest.TestCase):
    def test_the_prose_source_is_not_the_most_edited_file(self) -> None:
        # The scored slice is a file's opening. The README's opening is the part a change to this project is
        # most likely to rewrite, and a figure taken before such an edit is not reproducible by the command
        # printed next to it -- which happened, and is why this is pinned.
        self.assertNotEqual(bq.SOURCES["prose"][0], "README.md")
