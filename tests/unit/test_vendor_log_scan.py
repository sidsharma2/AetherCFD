"""Anchored solver-log scanning (lesson from les_pilot_hybrid_00)."""

from __future__ import annotations

import unittest

from aethercfd._vendor.log_scan import fatal_lines, is_benign, strip_benign_lines

BANNER = "trapFpe: Floating point exception trapping enabled (FOAM_SIGFPE)."
CRASH = (
    "#0  Foam::error::printStack(Foam::Ostream&) at ??:?\n"
    "#1  Foam::sigFpe::sigHandler(int) in /usr/lib/openfoam/openfoam2306/lib/libOpenFOAM.so\n"
    "Floating point exception (core dumped)\n"
)


class TestLogScan(unittest.TestCase):
    def test_trapfpe_banner_is_benign_not_fatal(self):
        self.assertTrue(is_benign(BANNER))
        self.assertEqual(fatal_lines(f"header\n{BANNER}\nTime = 1\n"), [])
        self.assertNotIn("Floating point", strip_benign_lines(f"a\n{BANNER}\nb"))

    def test_real_crash_is_fatal_even_after_banner_stripping(self):
        text = strip_benign_lines(f"{BANNER}\nTime = 1\n{CRASH}")
        self.assertEqual([n for n, _ in fatal_lines(text)], [3, 4])

    def test_foam_fatal_error_is_anchored(self):
        self.assertEqual(len(fatal_lines("--> FOAM FATAL ERROR: \nkeyword div(phi,U) is undefined")), 1)
        self.assertEqual(fatal_lines("note: we grep for '--> FOAM FATAL ERROR' later"), [])
