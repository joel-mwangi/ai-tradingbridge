
from fastapi import FastAPI
from pydantic import BaseModel, Field
from app.analysis import analyze_digits

app = FastAPI(
    title="TradeBridge AI",
    description="Research and evaluation service for trading strategies",
    version="0.1.0",
)

class DigitRequest(BaseModel):
    digits: list[int] = Field(min_length=1, max_length=100000)

@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "tradebridge-ai",
        "mode": "research",
    }


@app.post("/analysis/digits")
def digit_analysis(request: DigitRequest):
    return analyze_digits(request.digits)