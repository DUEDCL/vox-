import json
from datetime import datetime
from pathlib import Path

from evox_plugin import VoicePlugin


def test_generated_events_match_required_schema_shape():
    schema = json.loads(Path("contracts/voice-events.schema.json").read_text(encoding="utf-8"))
    plugin = VoicePlugin()
    event = plugin.start()
    for key in schema["required"]:
        assert key in event
    assert event["version"] == "1"
    assert event["type"] in schema["properties"]["type"]["enum"]
    datetime.fromisoformat(event["timestamp"])
