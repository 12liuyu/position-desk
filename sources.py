"""Only explicitly selected local JSON is read. Demo never reads an account."""
from pathlib import Path

from engine import read_bundle
from planner import execution_card


class FileSource:
    def __init__(self, data_dir=None):
        self.demo = data_dir is None
        self.path = (Path(__file__).parent / "examples" / "demo.json" if self.demo
                     else Path(data_dir).expanduser().resolve() / "account.json")
        self.label = "虚构演示" if self.demo else "本地导入"

    def read(self, now=None):
        return read_bundle(self.path, demo=self.demo, now=now)

    def analyze(self, snapshot):
        if not snapshot["valid"]:
            return {}, []
        return {row["code"]: execution_card(row, snapshot["bundle"]) for row in snapshot["rows"]}, []
