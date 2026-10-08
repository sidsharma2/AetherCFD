"""Tests for the vendored cfd_agents dat_parser — fixture content is REAL output
captured this round from a scratch icoFoam cavity-tutorial run (never
round-jet), not synthesized guesses at the format."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from aethercfd._vendor.dat_parser import (
    field_min_max_series,
    parse_dat_file,
    parse_dat_text,
    solver_info_final_residual_series,
    unique_field_names,
)

_REAL_FIELD_MIN_MAX_DAT = (
    "# Field minima and maxima\n"
    "# Time        \tfield         \tmin           \tlocation(min) \tmax           \tlocation(max) \n"
    "0.005         \tmag(U)        \t0.000000e+00\t(0.000000e+00 2.500000e-03 5.000000e-03)"
    "\t1.000000e+00\t(2.500000e-03 1.000000e-01 5.000000e-03)\n"
    "0.005         \tp             \t-5.149569e+00\t(2.500000e-03 9.750000e-02 5.000000e-03)"
    "\t6.514309e+00\t(9.750000e-02 9.750000e-02 5.000000e-03)\n"
    "0.01          \tmag(U)        \t0.000000e+00\t(0.000000e+00 2.500000e-03 5.000000e-03)"
    "\t1.000000e+00\t(2.500000e-03 1.000000e-01 5.000000e-03)\n"
    "0.01          \tp             \t-4.576365e+00\t(2.500000e-03 9.750000e-02 5.000000e-03)"
    "\t4.829782e+00\t(9.750000e-02 9.750000e-02 5.000000e-03)\n"
)

_REAL_COMPONENT_MODE_FIELD_MIN_MAX_DAT = (
    "# Field minima and maxima\n"
    "# Time        \tfield         \tmin           \tlocation(min) \tmax           \tlocation(max) \n"
    "0.005         \tU             \t(1.099590e-03 2.403061e-03 0.000000e+00)\t(2.500000e-03 2.500000e-03 5.000000e-03)"
    "\t(1.099590e-03 2.403061e-03 0.000000e+00)\t(2.500000e-03 2.500000e-03 5.000000e-03)\n"
)

_REAL_SOLVER_INFO_DAT = (
    "# Solver information\n"
    "# Time        \tU_solver      \tUx_initial    \tUx_final      \tUx_iters      \tUy_initial    \tUy_final      "
    "\tUy_iters      \tU_converged   \tp_solver      \tp_initial     \tp_final       \tp_iters       \tp_converged   \n"
    "0.005         \tsmoothSolver\t1.000000e+00\t8.905110e-06\t19\t0.000000e+00\t0.000000e+00\t0\tfalse\tDICPCG"
    "\t1.000000e+00\t4.928540e-02\t12\ttrue\n"
    "0.01          \tsmoothSolver\t1.606860e-01\t6.830310e-06\t19\t2.608280e-01\t9.659390e-06\t18\tfalse\tDICPCG"
    "\t4.289250e-01\t1.037390e-02\t22\ttrue\n"
)

# A restart re-prints the identical header partway through the file —
# confirmed live in a real round-jet massFlowOutlet.dat.
_REAL_DAT_WITH_RESTART_HEADER = (
    "# Field minima and maxima\n"
    "# Time        \tfield         \tmin           \tlocation(min) \tmax           \tlocation(max) \n"
    "1             \tp             \t-1.0\t(0 0 0)\t1.0\t(0 0 0)\n"
    "# Field minima and maxima\n"
    "# Time        \tfield         \tmin           \tlocation(min) \tmax           \tlocation(max) \n"
    "2             \tp             \t-2.0\t(0 0 0)\t2.0\t(0 0 0)\n"
)


def _write(text: str) -> Path:
    tmp = TemporaryDirectory()
    path = Path(tmp.name) / "data.dat"
    path.write_text(text, encoding="utf-8")
    return path, tmp


class TestParseDatFile(unittest.TestCase):
    def test_parses_real_field_min_max_header_and_rows(self) -> None:
        path, tmp = _write(_REAL_FIELD_MIN_MAX_DAT)
        self.addCleanup(tmp.cleanup)
        table = parse_dat_file(path)
        self.assertEqual(table.columns, ["Time", "field", "min", "location(min)", "max", "location(max)"])
        self.assertEqual(len(table.rows), 4)

    def test_parses_real_solver_info_header_and_rows(self) -> None:
        path, tmp = _write(_REAL_SOLVER_INFO_DAT)
        self.addCleanup(tmp.cleanup)
        table = parse_dat_file(path)
        self.assertIn("Ux_final", table.columns)
        self.assertIn("p_final", table.columns)
        self.assertEqual(len(table.rows), 2)

    def test_restart_reprinted_header_does_not_duplicate_or_break_columns(self) -> None:
        path, tmp = _write(_REAL_DAT_WITH_RESTART_HEADER)
        self.addCleanup(tmp.cleanup)
        table = parse_dat_file(path)
        self.assertEqual(table.columns, ["Time", "field", "min", "location(min)", "max", "location(max)"])
        self.assertEqual(len(table.rows), 2)


class TestFieldMinMaxSeries(unittest.TestCase):
    def test_extracts_signed_scalar_series_for_p(self) -> None:
        path, tmp = _write(_REAL_FIELD_MIN_MAX_DAT)
        self.addCleanup(tmp.cleanup)
        table = parse_dat_file(path)
        series = field_min_max_series(table, "p")
        self.assertEqual(series, [(0.005, -5.149569, 6.514309), (0.01, -4.576365, 4.829782)])

    def test_extracts_magnitude_field(self) -> None:
        path, tmp = _write(_REAL_FIELD_MIN_MAX_DAT)
        self.addCleanup(tmp.cleanup)
        table = parse_dat_file(path)
        series = field_min_max_series(table, "mag(U)")
        self.assertEqual(len(series), 2)
        self.assertEqual(series[0], (0.005, 0.0, 1.0))

    def test_component_mode_vector_value_reduced_to_magnitude(self) -> None:
        path, tmp = _write(_REAL_COMPONENT_MODE_FIELD_MIN_MAX_DAT)
        self.addCleanup(tmp.cleanup)
        table = parse_dat_file(path)
        series = field_min_max_series(table, "U")
        self.assertEqual(len(series), 1)
        t, lo, hi = series[0]
        expected_mag = (1.099590e-03**2 + 2.403061e-03**2 + 0.0**2) ** 0.5
        self.assertAlmostEqual(lo, expected_mag, places=9)
        self.assertAlmostEqual(hi, expected_mag, places=9)

    def test_unique_field_names_preserves_first_seen_order(self) -> None:
        path, tmp = _write(_REAL_FIELD_MIN_MAX_DAT)
        self.addCleanup(tmp.cleanup)
        table = parse_dat_file(path)
        self.assertEqual(unique_field_names(table), ["mag(U)", "p"])

    def test_missing_field_returns_empty_series(self) -> None:
        path, tmp = _write(_REAL_FIELD_MIN_MAX_DAT)
        self.addCleanup(tmp.cleanup)
        table = parse_dat_file(path)
        self.assertEqual(field_min_max_series(table, "omega"), [])


class TestSolverInfoFinalResidualSeries(unittest.TestCase):
    def test_extracts_scalar_field_final_residual(self) -> None:
        path, tmp = _write(_REAL_SOLVER_INFO_DAT)
        self.addCleanup(tmp.cleanup)
        table = parse_dat_file(path)
        series = solver_info_final_residual_series(table, "p_final")
        self.assertEqual(series, [(0.005, 0.0492854), (0.01, 0.0103739)])

    def test_extracts_vector_component_final_residual(self) -> None:
        path, tmp = _write(_REAL_SOLVER_INFO_DAT)
        self.addCleanup(tmp.cleanup)
        table = parse_dat_file(path)
        series = solver_info_final_residual_series(table, "Ux_final")
        self.assertEqual(series, [(0.005, 8.90511e-06), (0.01, 6.83031e-06)])

    def test_unknown_column_returns_empty(self) -> None:
        path, tmp = _write(_REAL_SOLVER_INFO_DAT)
        self.addCleanup(tmp.cleanup)
        table = parse_dat_file(path)
        self.assertEqual(solver_info_final_residual_series(table, "nonexistent_final"), [])


# One complete row from les_pilot_hybrid_00_retry_02's fieldMinMax.dat.
_NUT_ROW = ("6.912e-07         \tnut               \t4.1539979846e-11\t"
            "(2.4507860887e-02 1.1963680775e-02 4.5134786783e-03)\t2.5886332626e-03\t"
            "(8.1387980630e-02 6.7284512650e-03 -4.0601883232e-03)")
_HEADER = ("# Field minima and maxima\n# Time            \tfield             \tmin               \t"
           "location(min)     \tmax               \tlocation(max)     \n")


class TestGrowingFiles(unittest.TestCase):
    """Guard-race lessons from les_pilot_hybrid_00_retry_01/02."""

    def test_unterminated_last_row_is_incomplete_not_data(self) -> None:
        cut = _NUT_ROW[: _NUT_ROW.index("2.5886332626e-03") + len("2.58863")]
        table = parse_dat_text(_HEADER + _NUT_ROW + "\n" + cut)
        self.assertEqual(field_min_max_series(table, "nut"), [(6.912e-07, 4.1539979846e-11, 2.5886332626e-03)])
        self.assertEqual(table.incomplete_rows, [cut])
        self.assertEqual(table.malformed_rows, [])

    def test_partial_head_drops_leading_fragment_only_when_asked(self) -> None:
        text = _NUT_ROW[40:] + "\n" + _NUT_ROW + "\n"
        self.assertEqual(len(parse_dat_text(text, partial_head=True).rows), 1)
        self.assertEqual(parse_dat_text(text, partial_head=True).incomplete_rows, [_NUT_ROW[40:]])

    def test_complete_row_with_missing_columns_is_malformed(self) -> None:
        short = "\t".join(_NUT_ROW.split("\t")[:4])
        table = parse_dat_text(_HEADER + _NUT_ROW + "\n" + short + "\n")
        self.assertEqual(table.malformed_rows, [short])
        self.assertEqual(len(table.rows), 1)

    def test_crlf_text_parses_like_lf(self) -> None:
        table = parse_dat_text((_HEADER + _NUT_ROW + "\n").replace("\n", "\r\n"))
        self.assertEqual((len(table.rows), table.incomplete_rows, table.malformed_rows), (1, [], []))


if __name__ == "__main__":
    unittest.main()
