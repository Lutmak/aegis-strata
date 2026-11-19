"""tools/test_bench_switches.py - the parts of the benchmark harness that can be wrong without a GPU.

A benchmark harness is a measuring instrument, and the failures that matter in one are not crashes: they are the
ways it can print a number that describes a configuration other than the one it claims. Three of those have
already happened in this file's history and are recorded in its comments -- an arm that set the sm_86 switches
itself and so stopped the engine applying them, a prefill rate inflated by prompt-cache reuse, and a "with" arm
whose switch never arrived so the two arms agreed and the switch read as worthless.

What is asserted here is the logic that decides WHAT RUNS: which arms exist, what environment and engine
arguments each one carries, and the guards that refuse a comparison that could not mean anything. Nothing here
starts a server. The speeds themselves are measured, not asserted.

    python3 -m unittest discover -s tools -p 'test_bench_*.py'
"""
from __future__ import annotations

import io
import contextlib
import re
import shutil
import tempfile
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bench_switches as bs


class ArmsTest(unittest.TestCase):
    def test_default_pair_is_the_engines_own_table_against_nothing(self) -> None:
        arms = bs.arms_for(None)
        self.assertEqual(sorted(arms), ["off", "on"])
        # "on" must be EMPTY of environment: the engine applies its own table, and an arm that named the same
        # variables would stop it doing so ("the user's setting wins") and measure a path no operator reaches.
        self.assertEqual(arms["on"].env, {})
        self.assertEqual(arms["off"].env, {"STRATA_SM86_PERF": "0"})
        self.assertEqual(arms["on"].engine_args, [])

    def test_drop_compares_the_table_minus_one_switch_against_the_table(self) -> None:
        arms = bs.arms_for("STRATA_GR_UP75")
        self.assertNotIn("STRATA_GR_UP75", arms["off"].env)
        self.assertIn("STRATA_GR_UP75", arms["on"].env)
        # Everything else is present in both, or the pair would isolate more than the one switch.
        self.assertEqual(set(arms["on"].env) - set(arms["off"].env), {"STRATA_GR_UP75"})
        self.assertEqual(len(arms["off"].env), len(bs.SM86_SWITCHES) - 1)

    def test_drop_refuses_a_switch_outside_the_table(self) -> None:
        with self.assertRaises(SystemExit):
            bs.arms_for("STRATA_NOT_IN_THE_TABLE")

    def test_no_with_arm_unless_one_was_asked_for(self) -> None:
        # So that `--arms on,with` with neither --with nor --with-arg fails as an unknown arm, instead of quietly
        # measuring "on" twice and reporting the difference between a configuration and itself.
        self.assertNotIn("with", bs.arms_for(None))

    def test_with_arm_is_the_default_path_plus_the_setting(self) -> None:
        arms = bs.arms_for(None, {"STRATA_IQ4_W4A8": "2"})
        self.assertIn("with", arms)
        self.assertEqual(arms["with"].env, {"STRATA_IQ4_W4A8": "2"})

    def test_with_arg_reaches_the_engine_command_line(self) -> None:
        arms = bs.arms_for(None, None, bs.parse_with_args(["--tail-skip 7"]), "route tail skip: ON at rank 7")
        self.assertEqual(arms["with"].engine_args, ["--tail-skip", "7"])
        self.assertEqual(arms["with"].expect, "route tail skip: ON at rank 7")
        # The other arm must NOT carry it, or both sides would skip and the pair would measure nothing.
        self.assertEqual(arms["on"].engine_args, [])


class ServerArgvTest(unittest.TestCase):
    def test_it_is_the_shipped_command_line(self) -> None:
        argv = bs.server_argv()
        # The three that make this the README's line rather than a convenient one. A decode figure measured at
        # `--prefill auto` is not the decode figure of a line that borrows 2.49 GiB instead of 4.00 GiB, and the
        # expert cache is sized against the reserve at load.
        self.assertIn("4096", argv[argv.index("--prefill") + 1])
        self.assertEqual(argv[argv.index("--max-context") + 1], "131072")
        self.assertEqual(argv[argv.index("--vram-reserve-mib") + 1], "1152")

    def test_an_arms_engine_arguments_go_after_the_separator(self) -> None:
        argv = bs.server_argv(["--tail-skip", "7"])
        # Everything after the bare `--` belongs to the engine; an argument that landed before it would be read
        # by the server, which does not have this flag, and the server would refuse to start.
        self.assertIn("--", argv)
        self.assertGreater(argv.index("--tail-skip"), argv.index("--"))
        self.assertEqual(argv[argv.index("--tail-skip") + 1], "7")

    def test_the_engine_is_asked_for_one_gpu_explicitly(self) -> None:
        # Not a style point: a second card driving a display would otherwise be chosen some of the time, and two
        # arms that ran on different cards are not a comparison.
        self.assertEqual(bs.server_argv()[-1], "0")
        self.assertEqual(bs.server_argv()[-2], "--gpu")


class DecodePromptsTest(unittest.TestCase):
    def test_there_are_three_kinds_of_answer(self) -> None:
        # Upstream reports its tail-skip figures as a story, a code answer and an answer after a long document,
        # and a gap measured on one continuation is a gap for that continuation only.
        self.assertEqual(sorted(bs.DECODE_PROMPTS), ["code", "prose", "story"])

    def test_the_old_single_prompt_is_still_one_of_them(self) -> None:
        # So the figures published before --prompt existed can still be reproduced by name.
        self.assertEqual(bs.DECODE_PROMPT, bs.DECODE_PROMPTS["prose"])

    def test_they_ask_for_different_work(self) -> None:
        self.assertNotEqual(bs.DECODE_PROMPTS["code"], bs.DECODE_PROMPTS["prose"])
        self.assertIn("C++", bs.DECODE_PROMPTS["code"])


class ParseWithArgsTest(unittest.TestCase):
    def test_a_flag_and_its_value_travel_together(self) -> None:
        self.assertEqual(bs.parse_with_args(["--tail-skip 7"]), ["--tail-skip", "7"])

    def test_several_options_accumulate_in_order(self) -> None:
        self.assertEqual(bs.parse_with_args(["--tail-skip 7", "--adapt-every 0"]),
                         ["--tail-skip", "7", "--adapt-every", "0"])

    def test_a_quoted_value_with_a_space_survives(self) -> None:
        self.assertEqual(bs.parse_with_args(['--expert-profile "/a path/p.bin"']),
                         ["--expert-profile", "/a path/p.bin"])

    def test_something_that_is_not_an_option_is_refused(self) -> None:
        # Catches the shape that silently does nothing: a bare value with no flag in front of it.
        with self.assertRaises(SystemExit):
            bs.parse_with_args(["7"])

    def test_empty_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            bs.parse_with_args(["   "])

    def test_none_is_empty(self) -> None:
        self.assertEqual(bs.parse_with_args(None), [])


class ParseWithTest(unittest.TestCase):
    def test_a_value_is_required(self) -> None:
        # `--with STRATA_X=` is how a shell turns most of these off, so it would make the "with" arm equal to the
        # "on" arm and report the agreement as a measurement of STRATA_X.
        with self.assertRaises(SystemExit):
            bs.parse_with(["STRATA_SIDE_MIXER="])

    def test_the_name_must_look_like_one_of_the_engines(self) -> None:
        with self.assertRaises(SystemExit):
            bs.parse_with(["SIDE_MIXER=2"])

    def test_it_refuses_the_same_key_twice(self) -> None:
        with self.assertRaises(SystemExit):
            bs.parse_with(["STRATA_SIDE_MIXER=2", "STRATA_SIDE_MIXER=1"])

    def test_it_accepts_a_pair(self) -> None:
        self.assertEqual(bs.parse_with(["STRATA_SIDE_MIXER=2"]), {"STRATA_SIDE_MIXER": "2"})

    def test_none_is_empty(self) -> None:
        self.assertEqual(bs.parse_with(None), {})


class ArmFactsTest(unittest.TestCase):
    """What a row says about itself. A pair of tok/s figures is uninterpretable later without the cache state
    they were taken at, and `skipped=` is the only thing that distinguishes "the skip ran and cost nothing" from
    "the skip never ran"."""

    class FakeServer:
        def __init__(self, text: str) -> None:
            self._text = text

        @property
        def log(self):  # noqa: ANN202 - a stand-in for Path, with only the one method arm_facts uses
            text = self._text

            class _Log:
                @staticmethod
                def read_text(errors: str = "strict") -> str:
                    return text

            return _Log()

    def test_it_reads_the_engines_own_numbers(self) -> None:
        log = (
            "strata generate: expert cache 2973 slots, 4.86 GiB of VRAM; policy is\n"
            "strata serve: route tail skip: ON at rank 7 - a missed expert ...\n"
            "strata serve: decode expert cache hit rate: 74.6% (214534 hits / 287643 lookups)\n"
            "strata serve: route tail skip: 39571 missed experts skipped (42134 entries) since the start\n"
        )
        facts = bs.arm_facts(self.FakeServer(log))
        self.assertIn("slots=2973", facts)
        self.assertIn("hit=74.6%", facts)
        self.assertIn("skipped=42134 entries", facts)
        self.assertIn("tail_skip=ON at rank 7", facts)

    def test_no_skip_line_reads_as_zero_and_not_as_missing(self) -> None:
        # The engine prints the cumulative skip line only when something was skipped, so its absence is a real
        # measurement -- nothing was dropped -- and must be reported as such rather than left blank.
        log = ("strata generate: expert cache 2973 slots, 4.86 GiB of VRAM; policy is\n"
               "strata serve: route tail skip: OFF - every routed expert is copied or computed ...\n"
               "strata serve: decode expert cache hit rate: 68.9% (1 hits / 2 lookups)\n")
        facts = bs.arm_facts(self.FakeServer(log))
        self.assertIn("skipped=0 entries", facts)
        self.assertIn("tail_skip=OFF", facts)

    def test_it_takes_the_last_hit_rate_not_the_first(self) -> None:
        # The engine prints one per request, and the first is the discarded warm-up.
        log = ("strata serve: decode expert cache hit rate: 57.0% (1 hits / 2 lookups)\n"
               "strata serve: decode expert cache hit rate: 74.6% (3 hits / 4 lookups)\n")
        self.assertIn("hit=74.6%", bs.arm_facts(self.FakeServer(log)))


class WithTookEffectTest(unittest.TestCase):
    def test_an_engine_argument_that_never_arrived_is_a_failure(self) -> None:
        arm = bs.Arm("with", engine_args=["--tail-skip", "7"], expect="route tail skip: ON at rank 7")
        srv = ArmFactsTest.FakeServer("strata serve: route tail skip: OFF - every routed expert is computed\n")
        with self.assertRaises(RuntimeError):
            bs.assert_with_took_effect(srv, arm)

    def test_the_expected_line_passes(self) -> None:
        arm = bs.Arm("with", engine_args=["--tail-skip", "7"], expect="route tail skip: ON at rank 7")
        srv = ArmFactsTest.FakeServer("strata serve: route tail skip: ON at rank 7 - a missed expert ...\n")
        bs.assert_with_took_effect(srv, arm)   # no raise

    def test_an_environment_switch_the_engine_never_mentions_is_a_failure(self) -> None:
        arm = bs.Arm("with", env={"STRATA_IQ4_W4A8": "2"})
        srv = ArmFactsTest.FakeServer("nothing about that switch at all\n")
        with self.assertRaises(RuntimeError):
            bs.assert_with_took_effect(srv, arm)

    def test_arms_that_are_not_the_with_arm_are_not_checked(self) -> None:
        bs.assert_with_took_effect(ArmFactsTest.FakeServer(""), bs.Arm("on"))


class MainGuardsTest(unittest.TestCase):
    """The guards run BEFORE the machine checks, so a typo is reported the same way on every box rather than
    only on one that already has an idle GPU and the model on disk."""

    def run_main(self, argv: list[str]) -> int:
        old = sys.argv
        sys.argv = ["bench_switches.py", *argv]
        try:
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                return bs.main()
        finally:
            sys.argv = old

    def test_with_arg_without_expect_is_refused(self) -> None:
        self.assertEqual(self.run_main(["decode", "--with-arg", "--tail-skip 7"]), 2)

    def test_expect_without_with_arg_is_refused(self) -> None:
        self.assertEqual(self.run_main(["decode", "--expect", "something"]), 2)

    def test_an_unknown_arm_is_refused(self) -> None:
        self.assertEqual(self.run_main(["decode", "--arms", "on,nonesuch"]), 2)

    def test_naming_the_with_arm_without_defining_it_is_refused(self) -> None:
        self.assertEqual(self.run_main(["decode", "--arms", "on,with"]), 2)


if __name__ == "__main__":
    unittest.main()


class TookEffectIsNotSubstringTest(unittest.TestCase):
    """A gauntlet run passed this guard three ways with switches the engine never reads.

    The test was `key not in text`, bare containment, so anything that is a prefix of a name the engine does
    mention satisfied it -- including the one-letter typo the function's own docstring says it catches. Each of
    these sets a variable nothing reads, which is an arm that measured the default path a second time and then
    reported "the switch is worth nothing".
    """

    LOG = "strata: sm_86: 7 speed switches on by default: STRATA_SM86_PERF=1 STRATA_S2_GROUPED=1\n"

    def test_a_one_letter_typo_is_caught(self) -> None:
        arm = bs.Arm("with", env={"STRATA_SM86_PER": "1"})      # the real name with the F dropped
        with self.assertRaises(RuntimeError):
            bs.assert_with_took_effect(ArmFactsTest.FakeServer(self.LOG), arm)

    def test_a_one_character_suffix_is_caught(self) -> None:
        with self.assertRaises(RuntimeError):
            bs.assert_with_took_effect(ArmFactsTest.FakeServer(self.LOG), bs.Arm("with", env={"STRATA_S": "1"}))

    def test_the_empty_suffix_is_caught(self) -> None:
        # Two holes here, both found by writing this test. `"" in text` is true of every log, and the proof
        # falls back to the name without its prefix -- so for the key `STRATA_` the fallback became `"=" in
        # text`, which any log with a switch in it satisfies. Caught now at the proof...
        with self.assertRaises(RuntimeError):
            bs.assert_with_took_effect(ArmFactsTest.FakeServer(self.LOG), bs.Arm("with", env={"STRATA_": "1"}))

    def test_the_empty_suffix_cannot_be_asked_for_in_the_first_place(self) -> None:
        # ... and at the boundary, where a name with nothing after the prefix is not a switch name at all.
        with self.assertRaises(SystemExit):
            bs.parse_with(["STRATA_=1"])

    def test_a_switch_the_engine_really_named_still_passes(self) -> None:
        bs.assert_with_took_effect(ArmFactsTest.FakeServer(self.LOG), bs.Arm("with", env={"STRATA_SM86_PERF": "1"}))


class ExpectDiscriminatesTest(unittest.TestCase):
    """`--expect` proves an argument arrived only if the other arm does not print the same string."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="expect-test-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def _log(self, text: str) -> Path:
        p = self.dir / "baseline.log"
        p.write_text(text)
        return p

    def test_a_string_the_baseline_arm_also_prints_is_refused(self) -> None:
        # The engine announces the route tail skip in every outcome, and its OFF line quotes the flag's own name
        # in the explanatory prose. So --expect "--tail-skip 7" -- what an operator types first, because it
        # echoes what they passed -- matched an arm where the skip was OFF, and both arms ran the whole model.
        off = ("strata serve: route tail skip: OFF - every routed expert is copied or computed; --tail-skip 7 "
               "drops the last three of the ten when the GPU cache misses them\n")
        with self.assertRaises(RuntimeError):
            bs.assert_expect_discriminates("--tail-skip 7", self._log(off))

    def test_the_discriminating_string_passes(self) -> None:
        off = "strata serve: route tail skip: OFF - every routed expert is copied or computed; --tail-skip 7 ...\n"
        bs.assert_expect_discriminates("route tail skip: ON at rank 7", self._log(off))   # no raise

    def test_an_empty_expect_is_not_checked(self) -> None:
        bs.assert_expect_discriminates("", self._log("anything at all\n"))


class WithValueOffTest(unittest.TestCase):
    def test_zero_is_refused_like_the_empty_value(self) -> None:
        # "0" is the other way these switches are turned off, and the "with" arm is built as the default table
        # overridden by --with, so this produced "the default path MINUS one switch" labelled as plus one.
        with self.assertRaises(SystemExit):
            bs.parse_with(["STRATA_SM86_PERF=0"])

    def test_a_real_value_is_still_accepted(self) -> None:
        self.assertEqual(bs.parse_with(["STRATA_IQ4_W4A8=2"]), {"STRATA_IQ4_W4A8": "2"})


class NoBackslashInFStringsTest(unittest.TestCase):
    def test_the_module_parses_as_python_3_11(self) -> None:
        """A backslash inside an f-string replacement field is a SyntaxError before 3.12 (PEP 701).

        It matters because this test module imports bench_switches, so `make test` cannot even collect on a
        pre-3.12 interpreter -- the gate, not just the tool, on any box with the Python that Debian 12 and
        Ubuntu 22.04 ship. Checked as a text property because ast.parse's feature_version does not re-apply the
        old f-string restriction.
        """
        src = Path(bs.__file__).read_text()
        offenders = [
            n for n, line in enumerate(src.splitlines(), 1)
            if re.search(r'f"[^"]*\{[^}]*\\', line) or re.search(r"f'[^']*\{[^}]*\\", line)
        ]
        self.assertEqual(offenders, [], f"backslash inside an f-string replacement field on line(s) {offenders}")


class DecodeArmsCheckedBeforeTheMachineTest(unittest.TestCase):
    def test_decode_refuses_one_arm_without_looking_at_the_gpu(self) -> None:
        # This check used to live in cmd_decode, which runs after gpu_is_idle(), so `decode --arms off` on a box
        # with a busy GPU reported "the GPU is not idle" and never mentioned the arms.
        called: list[bool] = []
        real = bs.gpu_is_idle
        bs.gpu_is_idle = lambda: (called.append(True), True)[1]
        try:
            self.assertEqual(MainGuardsTest().run_main(["decode", "--arms", "off"]), 2)
        finally:
            bs.gpu_is_idle = real
        self.assertEqual(called, [], "the GPU was inspected before the arms were checked")

    def test_two_different_arms_get_past_the_check(self) -> None:
        real = bs.gpu_is_idle
        bs.gpu_is_idle = lambda: False      # stop right after the arms check, without touching the card
        try:
            self.assertEqual(MainGuardsTest().run_main(["decode", "--arms", "off,on"]), 2)
        finally:
            bs.gpu_is_idle = real
