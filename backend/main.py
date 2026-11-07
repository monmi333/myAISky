"""
myAISky — AI Weather & Agent Monitoring Dashboard (FastAPI 백엔드)

실행 (프로젝트 루트에서):
    uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000

환경변수 (프로젝트 루트의 .env 파일에서 자동 로드):
    KMA_API_KEY      : 공공데이터포털(data.go.kr) 기상청 단기예보 인증키 → 요청의 serviceKey
    KMA_NX / KMA_NY  : 격자 좌표 (기본 55, 127 — 서울)
    WEATHER_LOCATION : 화면에 표시할 지역명 (기본 "서울")
    WEATHER_DB_PATH  : SQLite 파일 경로 (기본 backend/weather_logs.db)
    KMA_CACHE_SECONDS: 기상청 재호출 최소 간격(초, 기본 600)

데이터 흐름:
    기상청 초단기실황(getUltraSrtNcst)  → 기온(T1H)·습도·풍속·강수형태(PTY)
    기상청 초단기예보(getUltraSrtFcst)  → 하늘상태(SKY: 맑음/구름많음/흐림)
    → condition 결정(강수가 있으면 PTY, 없으면 SKY) → weather_logs 테이블 INSERT → /api/weather JSON

기상청 호출이 실패하면 대시보드가 멈추지 않도록 mock 데이터로 대체하고 source="mock"으로 표시합니다.
mock 데이터는 DB에 저장하지 않습니다.
"""

import asyncio
import math
import os
import sqlite3
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BACKEND_DIR = Path(__file__).resolve().parent
ROOT_DIR = BACKEND_DIR.parent
FRONTEND_DIR = ROOT_DIR / "frontend"
IMAGE_DIR = ROOT_DIR / "images"

load_dotenv(ROOT_DIR / ".env")

# ---------------------------------------------------------------- 설정
KMA_ENDPOINT = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0"
KMA_API_KEY = os.getenv("KMA_API_KEY", "")
KMA_NX = int(os.getenv("KMA_NX", "55"))
KMA_NY = int(os.getenv("KMA_NY", "127"))
LOCATION_NAME = os.getenv("WEATHER_LOCATION", "서울")
DB_PATH = Path(os.getenv("WEATHER_DB_PATH", str(BACKEND_DIR / "weather_logs.db")))
CACHE_SECONDS = int(os.getenv("KMA_CACHE_SECONDS", "600"))
REQUEST_TIMEOUT = 8  # seconds
KST = ZoneInfo("Asia/Seoul")
SOURCE_NAME = "Korea Meteorological Administration (KMA)"
MOCK_SOURCE_NAME = "Mock (demo data)"

PTY_LABELS = {0: "강수 없음", 1: "비", 2: "비/눈", 3: "눈", 5: "빗방울", 6: "빗방울/눈날림", 7: "눈날림"}
SKY_LABELS = {1: "맑음", 3: "구름많음", 4: "흐림"}

agent_logs: Deque[Dict] = deque(maxlen=100)  # AI 에이전트 로그 (인메모리)


class WeatherResponse(BaseModel):
    source: str                      # "kma" | "mock"
    source_name: str                 # 화면/로그에 표시할 데이터 출처
    location: str
    temperature: float               # ℃ (T1H)
    condition: str                   # 맑음 / 구름많음 / 흐림 / 비 / 눈 ...
    humidity: float                  # % (REH)
    wind_speed: float                # m/s (WSD)
    precipitation_type: int          # PTY 코드
    sky: Optional[int] = None        # SKY 코드
    observed_at: str                 # 관측 기준 시각 (KST)
    timestamp: str                   # 서버 조회 시각 (KST) — DB에 저장되는 시각
    error: Optional[str] = None


def now_kst() -> datetime:
    return datetime.now(KST)


def add_log(level: str, message: str) -> None:
    agent_logs.append({"time": now_kst().strftime("%H:%M:%S"), "level": level, "message": message})


# ---------------------------------------------------------------- SQLite
_db_lock = threading.Lock()


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _db_lock, db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS weather_logs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp    TEXT    NOT NULL,   -- 조회 시각 (KST, YYYY-MM-DD HH:MM:SS)
                temperature  REAL    NOT NULL,   -- ℃
                condition    TEXT    NOT NULL,   -- 날씨 상태
                humidity     REAL,
                wind_speed   REAL,
                observed_at  TEXT,               -- 기상청 관측 기준 시각
                location     TEXT,
                nx           INTEGER,
                ny           INTEGER
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_weather_logs_ts ON weather_logs(timestamp)")


def insert_weather_log(w: WeatherResponse) -> int:
    with _db_lock, db_connect() as conn:
        cur = conn.execute(
            """INSERT INTO weather_logs
               (timestamp, temperature, condition, humidity, wind_speed, observed_at, location, nx, ny)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (w.timestamp, w.temperature, w.condition, w.humidity, w.wind_speed,
             w.observed_at, w.location, KMA_NX, KMA_NY),
        )
        return cur.lastrowid


def recent_weather_logs(limit: int = 24) -> List[Dict]:
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT id, timestamp, temperature, condition, humidity, wind_speed, observed_at "
            "FROM weather_logs ORDER BY id DESC LIMIT ?", (limit,),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]  # 오래된 → 최신 순


def today_weather_logs() -> List[Dict]:
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT timestamp, temperature, condition FROM weather_logs "
            "WHERE timestamp LIKE ? ORDER BY id", (now_kst().strftime("%Y-%m-%d") + "%",),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 기상청 API
class KMAError(RuntimeError):
    def __init__(self, code: str, msg: str):
        super().__init__(f"기상청 API 오류 {code}: {msg}")
        self.code = code


def _kma_call(operation: str, base: datetime, base_time: str, rows: int) -> List[Dict]:
    params = {
        "serviceKey": KMA_API_KEY,
        "pageNo": 1,
        "numOfRows": rows,
        "dataType": "JSON",
        "base_date": base.strftime("%Y%m%d"),
        "base_time": base_time,
        "nx": KMA_NX,
        "ny": KMA_NY,
    }
    resp = requests.get(f"{KMA_ENDPOINT}/{operation}", params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    try:
        body = resp.json()["response"]
    except (ValueError, KeyError):
        # 인증키 오류 등은 JSON이 아닌 XML로 내려오는 경우가 있음
        raise RuntimeError(f"기상청 응답 해석 실패 (인증키 확인 필요): {resp.text[:120]}")
    header = body["header"]
    if header["resultCode"] != "00":
        raise KMAError(header["resultCode"], header.get("resultMsg", ""))
    return body["body"]["items"]["item"]


def _with_fallback(operation: str, candidates: List[Tuple[datetime, str]], rows: int):
    """발표 직후엔 데이터가 아직 없을 수 있어(resultCode 03 NO_DATA) 직전 발표시각으로 재시도."""
    last_exc: Optional[Exception] = None
    for base, base_time in candidates:
        try:
            return _kma_call(operation, base, base_time, rows), base, base_time
        except KMAError as exc:
            last_exc = exc
            if exc.code != "03":
                raise
    raise last_exc  # type: ignore[misc]


def fetch_ultra_srt_ncst(now: datetime) -> Dict:
    """초단기실황: 매시 정각 발표 (보통 HH:10 전후 제공)."""
    this_hour = now.replace(minute=0, second=0, microsecond=0)
    prev_hour = this_hour - timedelta(hours=1)
    candidates = [(prev_hour, prev_hour.strftime("%H00"))]
    if now.minute >= 10:
        candidates.insert(0, (this_hour, this_hour.strftime("%H00")))
    items, base, base_time = _with_fallback("getUltraSrtNcst", candidates, rows=10)
    values = {it["category"]: it["obsrValue"] for it in items}
    return {
        "temperature": float(values["T1H"]),
        "humidity": float(values.get("REH", 0)),
        "wind_speed": float(values.get("WSD", 0)),
        "precipitation_type": int(float(values.get("PTY", 0))),
        "observed_at": f"{base.strftime('%Y-%m-%d')} {base_time[:2]}:00",
    }


def fetch_ultra_srt_sky(now: datetime) -> Optional[int]:
    """초단기예보: HH:30 발표 (HH:45 이후 제공). 가장 가까운 예보 시각의 SKY 코드를 반환."""
    this_hour = now.replace(minute=0, second=0, microsecond=0)
    prev_hour = this_hour - timedelta(hours=1)
    candidates = [(prev_hour, prev_hour.strftime("%H30"))]
    if now.minute >= 45:
        candidates.insert(0, (this_hour, this_hour.strftime("%H30")))
    items, _, _ = _with_fallback("getUltraSrtFcst", candidates, rows=60)
    sky = sorted((it["fcstDate"] + it["fcstTime"], it["fcstValue"]) for it in items if it["category"] == "SKY")
    return int(sky[0][1]) if sky else None


def decide_condition(pty: int, sky: Optional[int]) -> str:
    if pty:
        return PTY_LABELS.get(pty, "강수")
    if sky is not None:
        return SKY_LABELS.get(sky, "알 수 없음")
    return PTY_LABELS[0]


def fetch_kma_current(now: Optional[datetime] = None) -> Dict:
    """오늘 날짜·서울 격자(nx, ny) 기준 실시간 기온과 날씨 상태를 기상청에서 조회."""
    if not KMA_API_KEY:
        raise RuntimeError("KMA_API_KEY 환경변수가 설정되지 않았습니다.")
    now = now or now_kst()
    data = fetch_ultra_srt_ncst(now)
    try:
        sky: Optional[int] = fetch_ultra_srt_sky(now)
    except Exception as exc:  # 하늘상태만 실패하면 강수형태로 대체
        sky = None
        add_log("WARN", f"초단기예보(SKY) 조회 실패, 강수형태로 상태 표시: {exc}")
    data["sky"] = sky
    data["condition"] = decide_condition(data["precipitation_type"], sky)
    return data


def mock_current(now: Optional[datetime] = None) -> Dict:
    """API 키가 없거나 호출 실패 시 쓰는 시연용 데이터."""
    now = now or now_kst()
    minutes = now.hour * 60 + now.minute
    temperature = round(12.0 + 4.0 * math.sin((minutes / 1440.0) * 2 * math.pi - math.pi / 2), 1)
    return {"temperature": temperature, "humidity": 55.0, "wind_speed": 2.1, "precipitation_type": 0,
            "sky": 1, "condition": "맑음", "observed_at": now.strftime("%Y-%m-%d %H:00")}


# ---------------------------------------------------------------- 수집 (캐시 + DB 적재)
_cache: Dict[str, object] = {"weather": None, "at": 0.0}
_cache_lock = threading.Lock()


def collect_weather(force: bool = False) -> WeatherResponse:
    """기상청 실데이터를 조회해 weather_logs에 INSERT하고 반환.

    대시보드가 1분마다 폴링해도 기상청 호출(일일 트래픽 제한)과 DB 행이 폭증하지 않도록
    CACHE_SECONDS 동안은 마지막 결과를 재사용한다.
    """
    with _cache_lock:
        cached: Optional[WeatherResponse] = _cache["weather"]  # type: ignore[assignment]
        if (not force and cached is not None and cached.source == "kma"
                and time.monotonic() - float(_cache["at"]) < CACHE_SECONDS):
            return cached

        timestamp = now_kst().strftime("%Y-%m-%d %H:%M:%S")
        try:
            data = fetch_kma_current()
            weather = WeatherResponse(source="kma", source_name=SOURCE_NAME, location=LOCATION_NAME, timestamp=timestamp, **data)
            row_id = insert_weather_log(weather)
            add_log("INFO", f"[{SOURCE_NAME}] 초단기실황 수신 {weather.temperature:.1f}℃ / {weather.condition} → weather_logs #{row_id}")
            _cache.update(weather=weather, at=time.monotonic())
        except Exception as exc:  # 네트워크/키/파싱 오류여도 대시보드는 계속 동작
            weather = WeatherResponse(source="mock", source_name=MOCK_SOURCE_NAME, location=LOCATION_NAME, timestamp=timestamp,
                                      error=str(exc), **mock_current())
            add_log("WARN", f"[{SOURCE_NAME}] 호출 실패 → mock 사용 (DB 미저장): {exc}")
        return weather


async def _collector_loop() -> None:
    """대시보드를 열어두지 않아도 CACHE_SECONDS 주기로 자동 수집·적재."""
    while True:
        await asyncio.to_thread(collect_weather)
        await asyncio.sleep(CACHE_SECONDS)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    add_log("INFO", f"weather_logs DB 준비: {DB_PATH.name} · 데이터 소스 {SOURCE_NAME} (nx={KMA_NX}, ny={KMA_NY})")
    task = asyncio.create_task(_collector_loop())
    try:
        yield
    finally:
        task.cancel()


# ---------------------------------------------------------------- App
app = FastAPI(title="myAISky", version="0.2.0", lifespan=lifespan)
app.mount("/images", StaticFiles(directory=IMAGE_DIR), name="images")
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/api/weather", response_model=WeatherResponse)
def api_weather():
    """기상청 실시간 기온·날씨 상태 (캐시 만료 시 새로 조회하고 DB에 적재)."""
    return collect_weather()


@app.get("/api/weather/logs")
def api_weather_logs(limit: int = 24):
    """weather_logs 테이블의 최근 기록."""
    with db_connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM weather_logs").fetchone()[0]
    return {"total": total, "logs": recent_weather_logs(max(1, min(limit, 500)))}


@app.get("/api/history")
def api_history():
    """온도 트렌드용 포인트 (DB 기반)."""
    return {"points": [{"time": r["timestamp"][11:16], "temperature": r["temperature"]}
                       for r in recent_weather_logs(24)]}


@app.get("/api/logs")
def api_logs():
    return {"logs": list(agent_logs)}


@app.post("/api/sync")
def api_sync():
    """[수동 제어] 캐시를 무시하고 기상청에서 즉시 재조회·적재."""
    weather = collect_weather(force=True)
    add_log("SYNC", f"수동 동기화 완료 ({weather.source_name}, {weather.temperature:.1f}℃, {weather.condition})")
    return {"synced": weather.source == "kma", "source": weather.source, "source_name": weather.source_name,
            "temperature": weather.temperature, "condition": weather.condition, "error": weather.error}


@app.get("/api/report")
def api_report():
    """[수동 제어] 오늘 DB 기록 기반 리포트."""
    rows = today_weather_logs()
    if not rows:
        report = "오늘 저장된 기록이 없습니다. 먼저 '데이터 동기화'를 실행하세요."
    else:
        temps = [r["temperature"] for r in rows]
        conditions = sorted({r["condition"] for r in rows})
        report = "\n".join([
            f"[myAISky 리포트] {LOCATION_NAME} · {now_kst().strftime('%Y-%m-%d')}",
            f"- 데이터 소스: {SOURCE_NAME}",
            f"- 기록 수: {len(temps)} (weather_logs)",
            f"- 최고 기온: {max(temps):.1f}℃",
            f"- 최저 기온: {min(temps):.1f}℃",
            f"- 평균 기온: {sum(temps) / len(temps):.1f}℃",
            f"- 최신 기온: {temps[-1]:.1f}℃ ({rows[-1]['condition']}, {rows[-1]['timestamp'][11:16]})",
            f"- 관측된 날씨: {', '.join(conditions)}",
        ])
    add_log("REPORT", "리포트 생성 완료")
    return {"report": report}
