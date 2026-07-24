"""Unit tests for the pure liveness/ack-window helpers in dimos_cli.

Run:  cd backend && python3 -m unittest test_liveness -v

Stdlib-only: nothing here spawns the dimos binary or needs the robot, so this
runs on any machine (including the macOS dev box).
"""
import unittest

import dimos_cli


ROWS = {
    "/odom": {"type": "geometry_msgs.PoseStamped", "freq_hz": 14.9,
              "bandwidth": "12.1 KB/s"},
    "/camera_info": {"type": "sensor_msgs.CameraInfo", "freq_hz": 0.0,
                     "bandwidth": "0 B/s"},
}
# /lidar appears only as partially-rendered TUI text (no parseable row);
# /odom_raw exists to prove the word-boundary guard.
RAW = ("lcm /odom geometry_msgs.PoseStamped 14.9 12.1 KB/s\n"
       "/lidar\n"
       "lcm /odom_raw x 3.0 1 B/s\n")


class ClassifyTopicTest(unittest.TestCase):
    def test_parsed_row_with_rate_is_alive_and_definite(self):
        info = dimos_cli._classify_topic(ROWS, RAW, "/odom")
        self.assertTrue(info["alive"])
        self.assertFalse(info["indeterminate"])
        self.assertIn("14.9 Hz", info["sample"])

    def test_parsed_zero_hz_row_is_dead_not_indeterminate(self):
        # A listed-but-silent topic (robot off, publisher still registered)
        # must NOT read as alive — the "robot off but frontend shows alive" bug.
        info = dimos_cli._classify_topic(ROWS, RAW, "/camera_info")
        self.assertFalse(info["alive"])
        self.assertFalse(info["indeterminate"])

    def test_name_only_in_raw_text_is_indeterminate(self):
        # Rendering-timing fallback: name seen but no parseable rate. Still
        # reported alive (avoids false-dead flapping on tiles) but flagged so
        # aggregates/UI treat it as "maybe", never as proof of a live robot.
        info = dimos_cli._classify_topic(ROWS, RAW, "/lidar")
        self.assertTrue(info["alive"])
        self.assertTrue(info["indeterminate"])

    def test_absent_topic_is_dead(self):
        info = dimos_cli._classify_topic(ROWS, RAW, "/color_image")
        self.assertFalse(info["alive"])
        self.assertFalse(info["indeterminate"])

    def test_word_boundary_no_substring_false_positive(self):
        info = dimos_cli._classify_topic({}, "lcm /odom_raw x 3.0 1 B/s", "/odom")
        self.assertFalse(info["alive"])


class StrictAggregateTest(unittest.TestCase):
    def test_indeterminate_alone_does_not_make_the_set_alive(self):
        per = {"/lidar": {"alive": True, "indeterminate": True, "sample": None}}
        self.assertFalse(dimos_cli._strict_alive(per))

    def test_one_confirmed_rate_makes_the_set_alive(self):
        per = {
            "/lidar": {"alive": True, "indeterminate": True, "sample": None},
            "/odom": {"alive": True, "indeterminate": False, "sample": "x"},
        }
        self.assertTrue(dimos_cli._strict_alive(per))

    def test_empty_set_is_dead(self):
        self.assertFalse(dimos_cli._strict_alive({}))


class SportAckWindowTest(unittest.TestCase):
    def test_default_window(self):
        self.assertEqual(dimos_cli._sport_ack_window({}), 20.0)

    def test_long_rpc_timeout_stretches_window(self):
        # /api/sport passes timeout=25 for sport_command animations; the old
        # fixed 20s read window killed a healthy daemon mid-Dance and reported
        # failure for a command the robot actually performed.
        self.assertEqual(dimos_cli._sport_ack_window({"timeout": 25}), 30.0)

    def test_short_rpc_timeout_keeps_floor(self):
        self.assertEqual(dimos_cli._sport_ack_window({"timeout": 5}), 20.0)

    def test_garbage_timeout_keeps_floor(self):
        self.assertEqual(dimos_cli._sport_ack_window({"timeout": "abc"}), 20.0)


if __name__ == "__main__":
    unittest.main()
