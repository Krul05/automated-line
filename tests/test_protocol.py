import unittest

from montrac.protocol import COMMAND_PRESENCE, COMMAND_START_FORWARD, build_frame, extract_messages, parse_frame


class ProtocolTests(unittest.TestCase):
    def test_build_start_forward_frame_uses_xor_checksum(self):
        self.assertEqual(build_frame(1, 255, COMMAND_START_FORWARD).hex().upper(), "010100FF33CD03")

    def test_parse_presence_frame(self):
        frame = build_frame(1, 513, COMMAND_PRESENCE)
        message = parse_frame(frame)
        self.assertEqual(message.group, 1)
        self.assertEqual(message.shuttle_id, 513)
        self.assertEqual(message.command, COMMAND_PRESENCE)
        self.assertTrue(message.is_presence)

    def test_extract_messages_ignores_garbage_and_keeps_tail(self):
        first = build_frame(1, 10, COMMAND_PRESENCE)
        second = build_frame(1, 11, COMMAND_PRESENCE)
        buffer = bytearray(b"noise" + first + second[:3])
        messages = extract_messages(buffer)

        self.assertEqual([item.shuttle_id for item in messages], [10])
        self.assertEqual(bytes(buffer), second[:3])


if __name__ == "__main__":
    unittest.main()
