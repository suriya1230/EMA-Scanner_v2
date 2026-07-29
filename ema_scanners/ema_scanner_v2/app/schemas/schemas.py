from datetime import datetime
from pydantic import BaseModel, ConfigDict


# ─── Signal ───────────────────────────────────────────────────────────────────

class SignalBase(BaseModel):
    symbol: str
    market: str
    interval: str
    signal_type: str
    cross_price: float
    cross_time: datetime
    ema_7: float
    ema_25: float
    ema_99: float


class SignalCreate(SignalBase):
    pass


class SignalOut(SignalBase):
    id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
