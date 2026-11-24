from pydantic import BaseModel


class Chaos(BaseModel):
    duplicate_rate: float = 0.0
    delay_ms: int = 0
    drop_rate: float = 0.0


chaos = Chaos()
