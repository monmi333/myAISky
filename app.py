"""
myAISky — AI Weather & Agent Monitoring Dashboard (FastAPI 미니 백엔드)

실행 (프로젝트 루트에서):
    uvicorn app:app --reload --host 0.0.0.0 --port 8000

환경변수:
    KMA_API_KEY : 공공데이터포털(data.go.kr)에서 발급받은 기상청 단기예보 인증키 (Decoding 키)
    KMA_NX      : 격자 X 좌표 (기본 60, 서울 종로)
    KMA_NY      : 격자 Y 좌표 (기본 127, 서울 종로)

KMA_API_KEY가 없거나 호출이 실패하면 대시보드가 멈추지 않도록 mock 데이터로 대체하고
응답의 source 필드를 "mock"으로, error 필드에 사유를 담아 반환합니다.
"""

import math
import os
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Deque, Dict, List, Optional

import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
IMAGE_DIR = BASE_DIR / "images"

# 기상청 단기예보 서비스 - 초단기실황조회 (T1H 기온, REH 습도, WSD 풍속, PTY 강수형태)
KMA_URL = "http://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getUltraSrtNcst"
KMA_API_KEY = os.getenv("KMA_API_KEY", "")
KMA_NX = int(os.getenv("KMA_NX", "60"))
KMA_NY = int(os.getenv("KMA_NY", "127"))
LOCATION_NAME = os.getenv("WEATHER_LOCATION", "서울 종로")
REQUEST_TIMEOUT = 5  # seconds

# PTY(강수형태) 코드 → 상태 라벨
PTY_LABELS = {
    0: "강수 없음",
    1: "비",
    2: "비/눈",
    3: "눈",
    5: "빗방울",
    6: "빗방울/눈날림",
    7: "눈날림",
}

# 인메모리 상태 (1교시 미니 백엔드용; 재시작 시 초기화됨)
history: Deque[Dict] = deque(maxlen=48)   # 온도 트렌드용 기록
agent_logs: Deque[Dict] = deque(maxlen=100)  # AI 에이전트 로그


class WeatherResponse(BaseModel):
    source: str                      # "kma" | "mock"
    location: str
    temperature: float               # ℃
    humidity: float                  # %
    wind_speed: float                # m/s
    precipitation_type: int          # PTY 코드
    status: str                      # 강수 상태 라벨
    observed_at: str                 # 관측 기준 시각
    fetched_at: str                  # 서버 조회 시각
    error: Optional[str] = None


def add_log(level: str, message: str) -> None:
    agent_logs.append({
        "time": datetime.now().strftime("%H:%M:%S"),
        "level": level,
        "message": message,
    })


def _kma_base_datetime(now: datetime):
    """초단기실황은 매시 정각 기준이며 발표 후 약 40분 뒤부터 조회 가능하므로 45분 이전이면 직전 시각을 사용."""
    base = now if now.minute >= 45 else now - timedelta(hours=1)
    return base.strftime("%Y%m%d"), base.strftime("%H") + "00"


def fetch_kma_current(now: Optional[datetime] = None) -> Dict:
    """기상청 초단기실황 API를 호출해 현재 기온/습도/풍속/강수 상태를 반환한다."""
    if not KMA_API_KEY:
        raise RuntimeError("KMA_API_KEY 환경변수가 설정되지 않았습니다.")

    base_date, base_time = _kma_base_datetime(now or datetime.now())
    params = {
        "serviceKey": KMA_API_KEY,
        "pageNo": 1,
        "numOfRows": 10,
        "dataType": "JSON",
        "base_date": base_date,
        "base_time": base_time,
        "nx": KMA_NX,
        "ny": KMA_NY,
    }

    resp = requests.get(KMA_URL, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    try:
        body = resp.json()["response"]
    except (ValueError, KeyError):
        # 인증키 오류 등은 JSON이 아닌 XML 에러로 내려오는 경우가 있음
        raise RuntimeError("기상청 API 응답을 해석할 수 없습니다 (인증키/요청 파라미터 확인 필요).")

    header = body["header"]
    if header["resultCode"] != "00":
        raise RuntimeError(f"기상청 API 오류 {header['resultCode']}: {header.get('resultMsg', '')}")

    items: List[Dict] = body["body"]["items"]["item"]
    values = {item["category"]: item["obsrValue"] for item in items}
    pty = int(float(values.get("PTY", 0)))

    return {
        "temperature": float(values["T1H"]),
        "humidity": float(values.get("REH", 0)),
        "wind_speed": float(values.get("WSD", 0)),
        "precipitation_type": pty,
        "status": PTY_LABELS.get(pty, "알 수 없음"),
        "observed_at": f"{base_date[:4]}-{base_date[4:6]}-{base_date[6:]} {base_time[:2]}:00",
    }


def mock_current(now: Optional[datetime] = None) -> Dict:
    """API 키가 없거나 호출 실패 시 쓰는 시연용 데이터 (시간에 따라 완만하게 변동)."""
    now = now or datetime.now()
    minutes = now.hour * 60 + now.minute
    temperature = round(18.0 + 4.0 * math.sin((minutes / 1440.0) * 2 * math.pi - math.pi / 2), 1)
    return {
        "temperature": temperature,
        "humidity": 55.0,
        "wind_speed": 2.1,
        "precipitation_type": 0,
        "status": PTY_LABELS[0],
        "observed_at": now.strftime("%Y-%m-%d %H:00"),
    }


def get_weather() -> WeatherResponse:
    """실데이터를 우선 시도하고, 실패하면 mock으로 대체."""
    fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        data = fetch_kma_current()
        add_log("INFO", f"KMA 초단기실황 수신: {data['temperature']}℃ / {data['status']}")
        return WeatherResponse(source="kma", location=LOCATION_NAME, fetched_at=fetched_at, **data)
    except Exception as exc:  # 네트워크/키/파싱 오류 모두 대시보드는 계속 동작
        data = mock_current()
        add_log("WARN", f"실데이터 실패 → mock 사용: {exc}")
        return WeatherResponse(source="mock", location=LOCATION_NAME, fetched_at=fetched_at,
                               error=str(exc), **data)


def record(weather: WeatherResponse) -> None:
    history.append({"time": weather.fetched_at[11:16], "temperature": weather.temperature})


# ---------------------------------------------------------------- App
app = FastAPI(title="myAISky", version="0.1.0")
app.mount("/images", StaticFiles(directory=IMAGE_DIR), name="images")


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return DASHBOARD_HTML


@app.get("/api/weather", response_model=WeatherResponse)
def api_weather():
    weather = get_weather()
    record(weather)
    return weather


@app.get("/api/history")
def api_history():
    return {"points": list(history)}


@app.get("/api/logs")
def api_logs():
    return {"logs": list(agent_logs)}


@app.post("/api/sync")
def api_sync():
    """[수동 제어] 기상 데이터 동기화."""
    weather = get_weather()
    record(weather)
    add_log("SYNC", f"수동 동기화 완료 ({weather.source}, {weather.temperature}℃)")
    return {"synced": True, "source": weather.source, "temperature": weather.temperature,
            "history_size": len(history)}


@app.get("/api/report")
def api_report():
    """[수동 제어] 누적 기록 기반 간단 리포트 생성."""
    temps = [p["temperature"] for p in history]
    if not temps:
        report = "기록이 없습니다. 먼저 '데이터 동기화'를 실행하세요."
    else:
        report = "\n".join([
            f"[myAISky 리포트] {LOCATION_NAME}",
            f"- 기록 수: {len(temps)}",
            f"- 최고 기온: {max(temps):.1f}℃",
            f"- 최저 기온: {min(temps):.1f}℃",
            f"- 평균 기온: {sum(temps) / len(temps):.1f}℃",
            f"- 최신 기온: {temps[-1]:.1f}℃",
        ])
    add_log("REPORT", "리포트 생성 완료")
    return {"report": report}


# ---------------------------------------------------------------- Dashboard HTML
DASHBOARD_HTML = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>myAISky · AI Weather & Agent Monitoring</title>
<style>
  :root {
    --cream: #F8F5EE;
    --ink: #2B2A27;
    --muted: #7A766C;
    --accent: #C9A96E;
    --charcoal: #1F2226;
    --charcoal-line: #2E333A;
    --ok: #3FA66B;
    --radius: 22px;
    --shadow: 0 18px 40px rgba(20, 30, 45, 0.25);
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; min-height: 100%; }
  body {
    background-color: #9fb8cc;
    background-image: url('/images/sky.jpg');
    background-size: cover;
    background-position: center;
    background-attachment: fixed;
    background-repeat: no-repeat;
    font-family: -apple-system, BlinkMacSystemFont, "Apple SD Gothic Neo", "Pretendard", "Segoe UI", sans-serif;
    color: var(--ink);
    padding: 24px 16px;
  }
  .container {
    max-width: 1200px;
    margin: 0 auto;
    background: var(--cream);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    padding: 28px;
  }
  header {
    display: flex; justify-content: space-between; align-items: flex-start;
    flex-wrap: wrap; gap: 12px; margin-bottom: 24px;
  }
  h1 { margin: 0; font-size: 2rem; letter-spacing: -0.02em; }
  .subtitle { margin: 4px 0 0; color: var(--muted); font-size: 0.95rem; }
  .badge {
    background: #fff; border-radius: 999px; padding: 8px 14px;
    font-size: 0.85rem; font-weight: 600; box-shadow: 0 2px 8px rgba(0,0,0,0.08);
    white-space: nowrap;
  }
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 14px; margin-bottom: 20px; }
  .card {
    background: #fff; border-radius: 16px; padding: 18px;
    box-shadow: 0 4px 14px rgba(0,0,0,0.05);
  }
  .card .label { color: var(--muted); font-size: 0.8rem; margin-bottom: 6px; }
  .card .value { font-size: 1.8rem; font-weight: 700; }
  .card .hint { color: var(--muted); font-size: 0.8rem; margin-top: 4px; }
  .grid { display: grid; grid-template-columns: 1.2fr 1fr; gap: 18px; margin-bottom: 20px; }
  @media (max-width: 860px) { .grid { grid-template-columns: 1fr; } }
  .panel-title { font-weight: 700; margin: 0 0 12px; font-size: 1.05rem; }
  .analysis p { margin: 6px 0; line-height: 1.55; }
  .trend { width: 100%; height: 200px; margin-top: 10px; }
  .trend-empty { color: var(--muted); font-size: 0.9rem; padding: 40px 0; text-align: center; }
  .agent {
    background: var(--charcoal); color: #E6E3DC; border-radius: 16px;
    padding: 18px; display: flex; flex-direction: column; min-height: 320px;
  }
  .agent .panel-title { color: #fff; }
  .log {
    flex: 1; overflow-y: auto; max-height: 300px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.82rem; line-height: 1.6;
    border-top: 1px solid var(--charcoal-line); padding-top: 10px;
  }
  .log .row { white-space: pre-wrap; word-break: break-word; }
  .log .t { color: #7d8590; }
  .lv-INFO { color: #7fd1a0; } .lv-WARN { color: #f2c46d; }
  .lv-SYNC { color: #8ab4f8; } .lv-REPORT { color: #d7a8ff; }
  .control {
    background: #fff; border-radius: 16px; padding: 18px;
    box-shadow: 0 4px 14px rgba(0,0,0,0.05);
  }
  .buttons { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 4px; }
  button {
    border: none; border-radius: 12px; padding: 11px 18px; font-size: 0.95rem; font-weight: 600;
    background: var(--ink); color: var(--cream); cursor: pointer; transition: transform .1s, opacity .2s;
  }
  button.secondary { background: var(--accent); color: #1c1a16; }
  button:hover { transform: translateY(-1px); }
  button:disabled { opacity: .5; cursor: wait; transform: none; }
  pre#report {
    background: #F1EDE3; border-radius: 12px; padding: 14px; margin: 14px 0 0;
    white-space: pre-wrap; font-size: 0.88rem; min-height: 48px;
  }
  footer { text-align: center; color: var(--muted); font-size: 0.8rem; margin-top: 18px; }
  @media (max-width: 480px) { h1 { font-size: 1.6rem; } .container { padding: 20px; } }
</style>
</head>
<body>
<div class="container">

  <header>
    <div>
      <h1>myAISky</h1>
      <p class="subtitle">AI Weather &amp; Agent Monitoring Dashboard</p>
    </div>
    <div class="badge">🟢 24/7 Sandbox Active</div>
  </header>

  <section class="stats">
    <div class="card"><div class="label">현재 기온</div><div class="value" id="s-temp">--℃</div><div class="hint" id="s-loc">-</div></div>
    <div class="card"><div class="label">습도</div><div class="value" id="s-hum">--%</div><div class="hint">REH</div></div>
    <div class="card"><div class="label">풍속</div><div class="value" id="s-wind">-- m/s</div><div class="hint">WSD</div></div>
    <div class="card"><div class="label">강수 상태</div><div class="value" id="s-status">--</div><div class="hint" id="s-source">데이터 소스 확인 중</div></div>
  </section>

  <section class="grid">
    <div class="card analysis">
      <h2 class="panel-title">실시간 기상 데이터 분석 · 온도 트렌드</h2>
      <p id="a-summary">데이터를 불러오는 중입니다...</p>
      <p id="a-time" class="subtitle"></p>
      <div id="trend"><div class="trend-empty">데이터 동기화 후 트렌드가 표시됩니다.</div></div>
    </div>

    <div class="agent">
      <h2 class="panel-title">AI 에이전트 실시간 로그</h2>
      <div class="log" id="log"></div>
    </div>
  </section>

  <section class="control">
    <h2 class="panel-title">Manual Control Panel</h2>
    <div class="buttons">
      <button id="btn-sync" onclick="syncData()">데이터 동기화</button>
      <button id="btn-report" class="secondary" onclick="makeReport()">리포트 생성</button>
    </div>
    <pre id="report">리포트는 여기에 표시됩니다.</pre>
  </section>

  <footer>myAISky · FastAPI backend · /api/weather</footer>
</div>

<script>
  const $ = (id) => document.getElementById(id);

  async function api(path, opts) {
    const res = await fetch(path, opts);
    if (!res.ok) throw new Error("HTTP " + res.status);
    return res.json();
  }

  function renderWeather(w) {
    $("s-temp").textContent = w.temperature.toFixed(1) + "℃";
    $("s-hum").textContent = Math.round(w.humidity) + "%";
    $("s-wind").textContent = w.wind_speed.toFixed(1) + " m/s";
    $("s-status").textContent = w.status;
    $("s-loc").textContent = w.location;
    $("s-source").textContent = w.source === "kma" ? "기상청 실데이터" : "시연용 mock 데이터";
    $("a-summary").textContent =
      `${w.location} 현재 ${w.temperature.toFixed(1)}℃, ${w.status}. ` +
      (w.source === "kma" ? "기상청 초단기실황 기준입니다." : "API 키 미설정 또는 호출 실패로 mock 데이터를 표시 중입니다.");
    $("a-time").textContent = "관측 기준 " + w.observed_at + " · 조회 " + w.fetched_at;
  }

  function renderTrend(points) {
    const box = $("trend");
    if (!points || points.length < 2) {
      box.innerHTML = '<div class="trend-empty">트렌드 데이터가 2개 이상 쌓이면 표시됩니다.</div>';
      return;
    }
    const W = 600, H = 200, P = 28;
    const temps = points.map(p => p.temperature);
    const min = Math.min(...temps) - 1, max = Math.max(...temps) + 1;
    const xs = (i) => P + (i * (W - 2 * P)) / (points.length - 1);
    const ys = (t) => H - P - ((t - min) / (max - min)) * (H - 2 * P);
    const line = points.map((p, i) => `${xs(i).toFixed(1)},${ys(p.temperature).toFixed(1)}`).join(" ");
    const dots = points.map((p, i) =>
      `<circle cx="${xs(i).toFixed(1)}" cy="${ys(p.temperature).toFixed(1)}" r="3.5" fill="#C9A96E"/>`).join("");
    box.innerHTML = `
      <svg class="trend" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="온도 트렌드">
        <line x1="${P}" y1="${H - P}" x2="${W - P}" y2="${H - P}" stroke="#d6d0c2"/>
        <polyline points="${line}" fill="none" stroke="#2B2A27" stroke-width="2.5" stroke-linejoin="round"/>
        ${dots}
        <text x="${P}" y="${P - 8}" font-size="12" fill="#7A766C">${max.toFixed(1)}℃</text>
        <text x="${P}" y="${H - 6}" font-size="12" fill="#7A766C">${min.toFixed(1)}℃</text>
      </svg>`;
  }

  function renderLogs(logs) {
    const box = $("log");
    box.textContent = "";
    logs.slice().reverse().forEach(l => {
      const row = document.createElement("div");
      row.className = "row";
      const t = document.createElement("span");
      t.className = "t"; t.textContent = l.time + " ";
      const lv = document.createElement("span");
      lv.className = "lv-" + l.level; lv.textContent = "[" + l.level + "] ";
      row.append(t, lv, document.createTextNode(l.message));
      box.append(row);
    });
    if (!logs.length) box.textContent = "로그가 없습니다.";
  }

  async function refresh() {
    try {
      renderWeather(await api("/api/weather"));
      renderTrend((await api("/api/history")).points);
      renderLogs((await api("/api/logs")).logs);
    } catch (e) {
      $("a-summary").textContent = "데이터 로드 실패: " + e.message;
    }
  }

  async function syncData() {
    const btn = $("btn-sync");
    btn.disabled = true; btn.textContent = "동기화 중...";
    try {
      await api("/api/sync", { method: "POST" });
      await refresh();
    } catch (e) {
      $("a-summary").textContent = "동기화 실패: " + e.message;
    } finally {
      btn.disabled = false; btn.textContent = "데이터 동기화";
    }
  }

  async function makeReport() {
    try {
      const r = await api("/api/report");
      $("report").textContent = r.report;
      renderLogs((await api("/api/logs")).logs);
    } catch (e) {
      $("report").textContent = "리포트 생성 실패: " + e.message;
    }
  }

  refresh();
  setInterval(refresh, 60 * 1000);
</script>
</body>
</html>
"""
