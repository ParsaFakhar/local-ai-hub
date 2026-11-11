import io
import unittest

from hub import protocol


class FramingTests(unittest.TestCase):
    def test_roundtrip(self):
        message = {"type": "ping", "id": "x", "input": {"text": "héllo 🌍"}}
        stream = io.BytesIO(protocol.encode_native_message(message))
        self.assertEqual(protocol.read_native_message(stream), message)

    def test_eof_returns_none(self):
        self.assertIsNone(protocol.read_native_message(io.BytesIO(b"")))

    def test_two_frames_in_a_row(self):
        buffer = io.BytesIO(
            protocol.encode_native_message({"id": "1"}) + protocol.encode_native_message({"id": "2"})
        )
        self.assertEqual(protocol.read_native_message(buffer)["id"], "1")
        self.assertEqual(protocol.read_native_message(buffer)["id"], "2")

    def test_outgoing_limit_enforced(self):
        huge = {"type": "result", "id": "x", "output": {"blob": "a" * (protocol.MAX_OUTGOING_BYTES + 10)}}
        with self.assertRaises(protocol.ProtocolError):
            protocol.encode_native_message(huge)


class ValidationTests(unittest.TestCase):
    def test_task_requires_plugin(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate_request({"type": "task"})

    def test_infers_task_type(self):
        request = protocol.validate_request({"plugin": "hello.python", "input": {}})
        self.assertEqual(request["type"], "task")
        self.assertTrue(request["id"])

    def test_rejects_future_major_version(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate_request({"type": "ping", "protocol": "2.0"})

    def test_rejects_non_object(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate_request([1, 2, 3])

    def test_error_envelope(self):
        error = protocol.make_error("abc", protocol.ErrorCode.UNKNOWN_PLUGIN, "nope")
        self.assertFalse(error["ok"])
        self.assertEqual(error["error"]["code"], "unknown_plugin")
        self.assertEqual(error["id"], "abc")


if __name__ == "__main__":
    unittest.main()
