import unittest

from hub import schema


class SchemaTests(unittest.TestCase):
    def test_required_and_types(self):
        spec = {
            "type": "object",
            "required": ["comments"],
            "properties": {
                "comments": {
                    "type": "array",
                    "items": {"type": "object", "required": ["id", "text"], "properties": {"id": {"type": "string"}}},
                }
            },
        }
        self.assertEqual(schema.validate({"comments": [{"id": "a", "text": "b"}]}, spec), [])
        self.assertTrue(schema.validate({}, spec))
        self.assertTrue(schema.validate({"comments": [{"id": 1, "text": "b"}]}, spec))
        self.assertTrue(schema.validate({"comments": "nope"}, spec))

    def test_enum_and_bounds(self):
        spec = {"type": "object", "properties": {"label": {"enum": ["ok", "spam"]}, "n": {"type": "integer", "minimum": 0, "maximum": 1}}}
        self.assertEqual(schema.validate({"label": "ok", "n": 1}, spec), [])
        self.assertTrue(schema.validate({"label": "other"}, spec))
        self.assertTrue(schema.validate({"n": 2}, spec))
        self.assertTrue(schema.validate({"n": True}, spec))

    def test_defaults_filled(self):
        spec = {"type": "object", "properties": {"name": {"type": "string", "default": "world"}, "flag": {"type": "boolean", "default": False}}}
        self.assertEqual(schema.apply_defaults({}, spec), {"name": "world", "flag": False})
        self.assertEqual(schema.apply_defaults({"name": "x"}, spec)["name"], "x")

    def test_example_generation(self):
        spec = {
            "type": "object",
            "properties": {
                "results": {
                    "type": "array",
                    "items": {"type": "object", "properties": {"id": {"type": "string"}, "label": {"enum": ["ok", "spam"]}}},
                }
            },
        }
        example = schema.example_from_schema(spec)
        self.assertEqual(example["results"][0]["label"], "ok")
        self.assertEqual(schema.validate(example, spec), [])


if __name__ == "__main__":
    unittest.main()
