"""Tests for the task runner and the string lookup layer."""

from __future__ import annotations

import threading
import unittest

from soundweaver.core.cancel import CancellationToken
from soundweaver.core.i18n import get_language, set_language, t
from soundweaver.core.runner import Outcome, run_steps
from soundweaver.core.task import Step


def noop(_token, _report) -> None:
    return None


class RunnerTests(unittest.TestCase):
    def test_sequential_steps_run_in_order(self) -> None:
        order: list[int] = []
        token = CancellationToken()
        outcome = run_steps(
            [Step(f"s{i}", noop, parallel=False) for i in range(4)],
            token,
            started=lambda i: order.append(i),
            progress=lambda *_: None,
            finished=lambda *_: None,
        )
        self.assertEqual(outcome, Outcome.DONE)
        self.assertEqual(order, [0, 1, 2, 3])

    def test_parallel_steps_overlap_within_the_bound(self) -> None:
        release = threading.Event()
        all_entered = threading.Event()
        entered: list[int] = []
        lock = threading.Lock()

        def wait_step(_token, _report) -> None:
            with lock:
                entered.append(1)
                if len(entered) == 3:
                    all_entered.set()
            release.wait(5)

        token = CancellationToken()
        outcome: dict[str, Outcome] = {}

        def finish(index, _outcome, _note, _seconds) -> None:
            if index == 2:  # the last of three only finishes once all three overlapped
                outcome["result"] = _outcome

        runner = threading.Thread(
            target=lambda: outcome.setdefault(
                "result",
                run_steps(
                    [Step(f"p{i}", wait_step, parallel=True) for i in range(3)],
                    token,
                    started=lambda *_: None,
                    progress=lambda *_: None,
                    finished=finish,
                ),
            )
        )
        runner.start()
        self.assertTrue(all_entered.wait(5), "three parallel steps never ran side by side")
        release.set()
        runner.join(10)
        self.assertEqual(outcome["result"], Outcome.DONE)

    def test_failure_keeps_running_and_reports_failed(self) -> None:
        ran: list[int] = []

        def boom(_token, _report) -> None:
            raise ValueError("boom")

        token = CancellationToken()
        failures: dict[int, str] = {}
        outcome = run_steps(
            [
                Step("bad", boom, parallel=False),
                Step("good", lambda _t, _r: ran.append(1), parallel=False),
            ],
            token,
            started=lambda *_: None,
            progress=lambda *_: None,
            finished=lambda i, oc, note, _s: failures.setdefault(i, note),
        )
        self.assertEqual(outcome, Outcome.FAILED)
        self.assertEqual(ran, [1])
        self.assertEqual(failures[0], "boom")

    def test_cancellation_skips_remaining_steps(self) -> None:
        ran: list[int] = []
        token = CancellationToken()

        def cancel_and_pass(_token, _report) -> None:
            token.cancel()

        outcome = run_steps(
            [
                Step("stopper", cancel_and_pass, parallel=False),
                Step("after", lambda _t, _r: ran.append(1), parallel=False),
            ],
            token,
            started=lambda *_: None,
            progress=lambda *_: None,
            finished=lambda *_: None,
        )
        self.assertEqual(outcome, Outcome.CANCELLED)
        self.assertEqual(ran, [])

    def test_cancelled_step_raises_processing_cancelled(self) -> None:
        def cancellable(token, _report) -> None:
            token.raise_if_cancelled()

        token = CancellationToken()
        token.cancel()
        notes: dict[int, tuple[Outcome, str]] = {}
        outcome = run_steps(
            [Step("c", cancellable, parallel=False)],
            token,
            started=lambda *_: None,
            progress=lambda *_: None,
            finished=lambda i, oc, note, _s: notes.setdefault(i, (oc, note)),
        )
        self.assertEqual(outcome, Outcome.CANCELLED)
        self.assertEqual(notes[0][0], Outcome.SKIPPED)


class I18nTests(unittest.TestCase):
    def tearDown(self) -> None:
        set_language("zh_cn")

    def test_fields_fill_in_every_language(self) -> None:
        for lang in ("zh_cn", "zh_tw", "en"):
            set_language(lang)
            text = t("log.step_done", title="X", seconds=1.5)
            self.assertIn("X", text)
            self.assertIn("1.5", text)

    def test_fallback_returns_the_key_when_missing(self) -> None:
        set_language("en")
        self.assertEqual(t("no.such_key.anywhere"), "no.such_key.anywhere")

    def test_unknown_language_falls_back_to_chinese(self) -> None:
        set_language("fr")
        self.assertEqual(get_language(), "zh_cn")


if __name__ == "__main__":
    unittest.main()
