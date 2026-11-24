from dataclasses import dataclass, field


@dataclass
class LLMResponse:
    kind: str                      # "tool_call" 或 "final"
    tool: str = ""
    args: dict = field(default_factory=dict)
    final: dict = field(default_factory=dict)
    tokens: int = 0


class FakeLLM:
    """按脚本依次返回预设回答，测试全程离线。真实模型只需实现同样的 next()。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def next(self, messages) -> LLMResponse:
        self.calls += 1
        if not self.script:
            raise RuntimeError("script exhausted")
        return self.script.pop(0)
