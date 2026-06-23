import json
import tempfile
import time
import unittest
from pathlib import Path

from montrac.controller import LineConfig, MontracController, StationConfig, load_or_create_config
from montrac.protocol import COMMAND_PRESENCE, build_frame, parse_frame


def make_controller() -> MontracController:
    config = LineConfig(
        stations=[StationConfig(index=i, port=f"COM{i}", mock=True) for i in range(1, 4)],
        loop=True,
        hold_seconds=3,
    )
    return MontracController(config)


def presence(shuttle_id: int):
    return parse_frame(build_frame(1, shuttle_id, COMMAND_PRESENCE))


class ControllerTests(unittest.TestCase):
    def test_release_station_marks_segment_busy(self):
        controller = make_controller()
        controller.on_message(1, presence(101))
        controller.on_message(3, presence(303))

        state = controller.release_station(1)

        self.assertTrue(state["lastAction"]["released"])
        self.assertEqual(state["lastAction"]["station"], "COM1")
        self.assertEqual(controller.segments[(1, 2)].occupied_by, 101)
        self.assertFalse(controller.stations[1].occupied)

    def test_station_release_is_allowed_when_next_station_is_occupied(self):
        controller = make_controller()
        controller.on_message(1, presence(101))
        controller.on_message(2, presence(202))

        state = controller.release_station(1)

        self.assertTrue(state["lastAction"]["released"])
        self.assertEqual(state["lastAction"]["station"], "COM1")
        self.assertFalse(controller.stations[1].occupied)
        self.assertEqual(controller.segments[(1, 2)].shuttle_count(), 1)
        self.assertEqual(controller.segments[(1, 2)].occupied_by, 101)

    def test_station_release_is_blocked_when_segment_has_one_shuttle(self):
        controller = make_controller()
        controller.on_message(1, presence(101))
        controller.segments[(1, 2)].add_shuttle(909, time.time())

        state = controller.release_station(1)

        self.assertFalse(state["lastAction"]["released"])
        self.assertIn("already has 1 shuttles", state["lastAction"]["reason"])
        self.assertTrue(controller.stations[1].occupied)
        self.assertEqual(controller.segments[(1, 2)].shuttle_count(), 1)
        self.assertEqual(controller.segments[(1, 2)].shuttle_ids, [909])

    def test_station_release_is_blocked_when_segment_has_two_shuttles(self):
        controller = make_controller()
        controller.on_message(1, presence(101))
        controller.segments[(1, 2)].add_shuttle(901, time.time())
        controller.segments[(1, 2)].add_shuttle(902, time.time())

        state = controller.release_station(1)

        self.assertFalse(state["lastAction"]["released"])
        self.assertIn("already has 2 shuttles", state["lastAction"]["reason"])
        self.assertTrue(controller.stations[1].occupied)
        self.assertEqual(controller.segments[(1, 2)].shuttle_count(), 2)

    def test_arrival_clears_busy_segment(self):
        controller = make_controller()
        controller.on_message(1, presence(101))
        controller.release_station(1)

        controller.on_message(2, presence(101))

        self.assertIsNone(controller.segments[(1, 2)].occupied_by)
        self.assertTrue(controller.stations[2].occupied)
        self.assertEqual(controller.stations[2].shuttle_id, 101)

    def test_mock_controller_can_start_and_stop_threads(self):
        controller = make_controller()
        controller.start()
        controller.stop()

        self.assertTrue(all(station.connected for station in controller.stations.values()))

    def test_update_station_config_saves_active_config_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            controller = MontracController(make_controller().config, config_path=path, force_mock=True)

            state = controller.update_station_config(
                [
                    {"index": 1, "name": "Load", "port": "COM7"},
                    {"index": 2, "name": "Unload", "port": "COM8"},
                ]
            )

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(state["saved"])
            self.assertEqual([station["name"] for station in state["stations"]], ["Load", "Unload"])
            self.assertEqual(saved["stations"][0]["port"], "COM7")
            self.assertEqual(set(controller.stations), {1, 2})

    def test_update_station_config_is_blocked_when_line_is_not_idle(self):
        controller = make_controller()
        controller.on_message(1, presence(101))

        with self.assertRaises(ValueError):
            controller.update_station_config([{"index": 1, "name": "Only", "port": "COM1"}])

    def test_default_modes_are_available_for_existing_configs(self):
        controller = make_controller()
        state = controller.snapshot()

        self.assertEqual([mode["id"] for mode in state["modes"]], ["stop_2_4", "stop_all"])

    def test_custom_mode_releases_station_after_configured_delay(self):
        controller = make_controller()
        controller.update_modes_config(
            [
                {
                    "id": "custom",
                    "name": "Custom",
                    "stationDelays": {"1": 0, "2": 5, "3": 0},
                }
            ]
        )
        controller.on_message(1, presence(101))

        state = controller.set_mode("custom")

        self.assertEqual(state["activeModeId"], "custom")
        self.assertFalse(controller.stations[1].occupied)
        self.assertEqual(controller.segments[(1, 2)].occupied_by, 101)

    def test_update_modes_config_saves_modes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            controller = MontracController(make_controller().config, config_path=path, force_mock=True)

            state = controller.update_modes_config(
                [{"id": "slow", "name": "Slow", "stationDelays": {"1": 1.5, "2": 0, "3": 2}}]
            )

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(state["saved"])
            self.assertEqual(saved["modes"][0]["id"], "slow")
            self.assertEqual(saved["modes"][0]["stationDelays"]["1"], 1.5)

    def test_load_or_create_config_copies_example_on_first_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            example_path = temp_path / "config.example.json"
            config_path = temp_path / "config.json"
            example_path.write_text(
                json.dumps(
                    {
                        "hold_seconds": 3,
                        "loop": True,
                        "stations": [{"index": 1, "name": "Base", "port": "COM9"}],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            config, active_path = load_or_create_config(config_path)

            self.assertEqual(active_path, config_path)
            self.assertTrue(config_path.exists())
            self.assertEqual(config.stations[0].name, "Base")
            self.assertEqual(json.loads(example_path.read_text(encoding="utf-8"))["stations"][0]["name"], "Base")


if __name__ == "__main__":
    unittest.main()
