from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evox_plugin import VoicePlugin

plugin = VoicePlugin()
print(plugin.start())
print(plugin.wake_detected("小沃小沃", 0.91))
print(plugin.submit_text("你好"))
print(plugin.status())
