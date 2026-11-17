import math
import tempfile
import unittest
from datetime import date
from pathlib import Path

from crop_following_inlets import (
    InletRule,
    InletSchedule,
    apply_to_umep_input,
    held_crop_height,
    inlet_setting,
)
from generate_umep_footprint_input import RecurringCropHeightPoint

SCHEDULE = [
    RecurringCropHeightPoint(1, 1, 0.0),
    RecurringCropHeightPoint(5, 1, 0.0),
    RecurringCropHeightPoint(7, 1, 0.8),
    RecurringCropHeightPoint(8, 15, 0.8),
    RecurringCropHeightPoint(9, 1, 0.4),
    RecurringCropHeightPoint(9, 10, 0.0),
    RecurringCropHeightPoint(12, 31, 0.0),
]


class InletSettingTests(unittest.TestCase):
    def test_bare_soil_uses_minimum_heights(self):
        s = inlet_setting(0.0, 0.0, InletRule())
        self.assertEqual((s.lower, s.upper), (0.5, 1.0))
        self.assertAlmostEqual(s.effective_height, math.sqrt(0.5 * 1.0), places=3)

    def test_lower_inlet_is_factor_times_crop_rounded_up(self):
        s = inlet_setting(0.77, 0.77, InletRule())  # 2.2 x 0.77 = 1.694 -> 1.7
        self.assertEqual((s.lower, s.upper), (1.7, 2.2))

    def test_exact_step_is_not_rounded_up(self):
        s = inlet_setting(0.5, 0.5, InletRule(factor=2.0))  # exactly 1.0
        self.assertEqual(s.lower, 1.0)

    def test_effective_height_is_geometric_mean_above_displacement(self):
        s = inlet_setting(0.8, 0.8, InletRule())
        d = 0.8 * 2 / 3
        self.assertAlmostEqual(s.effective_height, d + math.sqrt((s.lower - d) * (s.upper - d)), places=3)

    def test_custom_rule(self):
        s = inlet_setting(0.3, 0.3, InletRule(factor=1.5, minimum_lower=0.3, separation=0.4, step=0.05))
        self.assertEqual((s.lower, s.upper), (0.45, 0.85))


class HeldHeightTests(unittest.TestCase):
    def test_inlets_stay_up_while_crop_ripens(self):
        self.assertAlmostEqual(held_crop_height(SCHEDULE, date(2025, 9, 1)), 0.8)

    def test_inlets_come_down_after_harvest(self):
        self.assertAlmostEqual(held_crop_height(SCHEDULE, date(2025, 9, 10)), 0.0)

    def test_growing_crop_is_followed(self):
        self.assertLess(held_crop_height(SCHEDULE, date(2025, 6, 1)), 0.8)
        self.assertGreater(held_crop_height(SCHEDULE, date(2025, 6, 1)), 0.0)


class ApplyTests(unittest.TestCase):
    def test_rewrites_measurement_height_column_only(self):
        header = "iy id it imin z_0_input z_d_input z_m_input sigv Obukhov ustar dir h por"
        rows = ["2025 1 0 0 0.0100 0.0000 10.000 1.0 500.0 0.5 180.0 800.0 60.0",
                "2025 213 12 0 0.0980 0.5300 10.000 1.0 -50.0 0.4 200.0 1200.0 60.0"]
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / "in.txt", Path(folder) / "out.txt"
            source.write_text("\n".join([header] + rows) + "\n", encoding="utf-8")
            count = apply_to_umep_input(InletSchedule(SCHEDULE, InletRule()), source, target)
            lines = target.read_text(encoding="utf-8").splitlines()
        self.assertEqual(count, 2)
        self.assertEqual(lines[0], header)
        winter, summer = lines[1].split(), lines[2].split()
        self.assertAlmostEqual(float(winter[6]), math.sqrt(0.5), places=3)
        self.assertGreater(float(summer[6]), 1.9)  # day 213 = 1 Aug, inlets at 1.8 / 2.3 m
        for original, new in zip(rows, lines[1:]):
            a, b = original.split(), new.split()
            self.assertEqual(a[:6] + a[7:], b[:6] + b[7:])


if __name__ == "__main__":
    unittest.main()
