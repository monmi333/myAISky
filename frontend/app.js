const $ = (id) => document.getElementById(id);
const REFRESH_MS = 60 * 1000;

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error("HTTP " + res.status);
  return res.json();
}

// 날씨 상태 → 아이콘 / 배경 테마
function conditionStyle(condition, source) {
  const night = (() => { const h = new Date().getHours(); return h < 6 || h >= 19; })();
  if (source !== "kma") return { icon: "🧪", theme: "mock" };
  if (/눈/.test(condition) && /비|빗/.test(condition)) return { icon: "🌨️", theme: "snow" };
  if (/눈/.test(condition)) return { icon: "❄️", theme: "snow" };
  if (/비|빗/.test(condition)) return { icon: "🌧️", theme: "rain" };
  if (condition === "흐림") return { icon: "☁️", theme: "overcast" };
  if (condition === "구름많음") return { icon: night ? "☁️" : "⛅", theme: "cloudy" };
  return { icon: night ? "🌙" : "☀️", theme: "clear" };
}

function renderWeather(w) {
  const { icon, theme } = conditionStyle(w.condition, w.source);
  const hero = $("hero");
  const prev = $("h-temp").textContent;
  hero.dataset.cond = theme;
  $("h-icon").textContent = icon;
  $("h-loc").textContent = "📍 " + w.location;
  $("h-temp").textContent = w.temperature.toFixed(1);
  $("h-cond").textContent = w.condition;
  $("h-obs").textContent = w.observed_at;
  $("h-ts").textContent = w.timestamp.slice(11, 16);
  $("h-src").textContent = w.source === "kma" ? "기상청 실데이터" : "시연용 mock";
  if (prev !== $("h-temp").textContent) {
    hero.classList.remove("updated"); void hero.offsetWidth; hero.classList.add("updated");
  }

  $("s-hum").textContent = Math.round(w.humidity) + "%";
  $("s-wind").textContent = w.wind_speed.toFixed(1) + " m/s";

  const badge = $("badge");
  badge.className = "badge " + (w.source === "kma" ? "live" : "mock");
  $("badge-text").textContent = w.source === "kma" ? "기상청 실시간 연동" : "mock 데이터 (API 실패)";

  $("a-summary").textContent = w.source === "kma"
    ? `${w.location} 현재 ${w.temperature.toFixed(1)}℃, ${w.condition}. 기상청 초단기실황(${w.observed_at}) 기준이며 weather_logs에 저장됩니다.`
    : `기상청 호출 실패로 mock 데이터를 표시 중입니다 (DB 미저장). 사유: ${w.error || "알 수 없음"}`;
}

function renderTrend(points) {
  const box = $("trend");
  if (!points || points.length < 2) {
    box.innerHTML = '<div class="trend-empty">기록이 2건 이상 쌓이면 트렌드가 표시됩니다.</div>';
    return;
  }
  const W = 600, H = 210, P = 30;
  const temps = points.map(p => p.temperature);
  const min = Math.min(...temps) - 1, max = Math.max(...temps) + 1;
  const xs = (i) => P + (i * (W - 2 * P)) / (points.length - 1);
  const ys = (t) => H - P - ((t - min) / (max - min)) * (H - 2 * P);
  const pts = points.map((p, i) => [xs(i), ys(p.temperature)]);
  const line = pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const area = `M${pts[0][0]},${H - P} L${line.replace(/ /g, " L")} L${pts[pts.length - 1][0]},${H - P} Z`;
  const dots = pts.map(([x, y], i) =>
    `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="3.5" fill="#C9A96E"><title>${points[i].time} · ${points[i].temperature.toFixed(1)}℃</title></circle>`).join("");
  const first = points[0].time, last = points[points.length - 1].time;
  box.innerHTML = `
    <svg class="trend" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="온도 트렌드">
      <defs><linearGradient id="fill" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stop-color="#C9A96E" stop-opacity=".35"/><stop offset="1" stop-color="#C9A96E" stop-opacity="0"/>
      </linearGradient></defs>
      <line x1="${P}" y1="${H - P}" x2="${W - P}" y2="${H - P}" stroke="#d6d0c2"/>
      <path d="${area}" fill="url(#fill)"/>
      <polyline points="${line}" fill="none" stroke="#2B2A27" stroke-width="2.5" stroke-linejoin="round"/>
      ${dots}
      <text x="${P}" y="${P - 10}" font-size="12" fill="#7A766C">${max.toFixed(1)}℃</text>
      <text x="${P}" y="${H - 8}" font-size="12" fill="#7A766C">${first}</text>
      <text x="${W - P}" y="${H - 8}" font-size="12" fill="#7A766C" text-anchor="end">${last}</text>
    </svg>`;
}

function renderDbLogs(rows, total) {
  const body = $("db-rows");
  body.textContent = "";
  $("s-count").textContent = (total ?? rows.length) + "건";

  const today = new Date().toLocaleDateString("sv-SE", { timeZone: "Asia/Seoul" });
  const todays = rows.filter(r => r.timestamp.startsWith(today)).map(r => r.temperature);
  $("s-range").textContent = todays.length
    ? `${Math.max(...todays).toFixed(1)}° / ${Math.min(...todays).toFixed(1)}°` : "--";

  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="6" class="empty">아직 저장된 기록이 없습니다.</td></tr>';
    return;
  }
  rows.slice().reverse().forEach(r => {
    const tr = document.createElement("tr");
    const { icon } = conditionStyle(r.condition, "kma");
    [r.id, r.timestamp.slice(5, 16), r.temperature.toFixed(1) + "℃", icon + " " + r.condition,
     r.humidity == null ? "-" : Math.round(r.humidity) + "%",
     r.wind_speed == null ? "-" : r.wind_speed.toFixed(1) + " m/s"].forEach(v => {
      const td = document.createElement("td"); td.textContent = v; tr.append(td);
    });
    body.append(tr);
  });
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
    const [hist, db, logs] = await Promise.all([
      api("/api/history"), api("/api/weather/logs?limit=24"), api("/api/logs"),
    ]);
    renderTrend(hist.points);
    renderDbLogs(db.logs, db.total);
    renderLogs(logs.logs);
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

$("btn-sync").addEventListener("click", syncData);
$("btn-report").addEventListener("click", makeReport);
refresh();
setInterval(refresh, REFRESH_MS);
