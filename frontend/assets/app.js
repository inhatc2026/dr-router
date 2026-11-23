// 프론트 ↔ 백엔드 공유 헬퍼 (소유자: 박태준)
(function (root) {
  "use strict";
  const RESULT_KEY = "dr-router.results.v1";
  const RESULT_TTL = 30 * 60 * 1000;
  const KINDS = ["병원", "약국", "응급실"];
  // 응급실 실시간 병상: 서버 backend/tools/egen.py의 BEDS_ROW_MAX_AGE·PROMOTE_RANGE_M과 같은 값
  const BEDS_MAX_AGE = 60 * 60 * 1000;      // 기관 입력 후 60분이 지나면 표시하지 않는다
  const BEDS_FUTURE_SKEW = 5 * 60 * 1000;   // 기기 시계 차이 허용
  const ER_PROMOTE_RANGE_M = 10000;         // 가장 가까운 응급실보다 이만큼 넘게 먼 곳은 앞당기지 않는다

  async function sendChat(message, { lat = null, lng = null, region = null, history = [] } = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 45000);
    try {
      const res = await fetch("/api/chat", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message, lat, lng, region, history }), signal: controller.signal,
      });
      if (!res.ok) throw new Error("응답을 받지 못했습니다. 잠시 후 다시 전송해 주세요.");
      let data;
      try { data = await res.json(); }
      catch (error) {
        if (error.name === "AbortError") throw error;
        throw new Error("응답 형식을 확인할 수 없습니다. 다시 시도해 주세요.");
      }
      if (!data || typeof data.reply !== "string" || !Array.isArray(data.hospitals) || !Array.isArray(data.departments) ||
          !data.hospitals.every(item => item && typeof item.name === "string") ||
          !data.departments.every(item => item && typeof item.name === "string")) {
        throw new Error("응답 형식을 확인할 수 없습니다. 다시 시도해 주세요.");
      }
      return data;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("응답이 지연되고 있습니다. 잠시 후 다시 시도해 주세요.");
      throw error;
    } finally { clearTimeout(timer); }
  }

  function kindOf(item) {
    if (KINDS.includes(item.kind)) return item.kind;
    return item.dept === "약국" ? "약국" : item.dept === "응급의학과" ? "응급실" : "병원";
  }
  function distanceValue(value) { return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : Infinity; }
  function formatDistance(value) {
    if (distanceValue(value) === Infinity) return "거리 정보 없음";
    return value < 1000 ? `${Math.round(value).toLocaleString("ko-KR")} m` : `${(value / 1000).toLocaleString("ko-KR", { maximumFractionDigits: 1 })} km`;
  }
  function openStatus(value, kind) {
    if (value === true) return { text: kind === "응급실" ? "24시간" : "진료중", className: "open" };
    if (value === false) return { text: "진료중 아님", className: "closed" };
    return { text: "확인 필요", className: "unknown" };
  }
  function filterHospitals(items, kind = "전체", onlyOpen = false, now = Date.now()) {
    const list = items.filter(item => item && typeof item === "object")
      .filter(item => (kind === "전체" || kindOf(item) === kind) && (!onlyOpen || item.is_open === true))
      .sort((a, b) => distanceValue(a.distance_m) - distanceValue(b.distance_m));
    // 응급실만 있는 목록(응급 안내·응급실 필터)은 포화인 곳을 뒤로. 여러 종류가 섞인 목록은 거리순 그대로(FR-20)
    return list.length && list.every(item => kindOf(item) === "응급실") ? orderEmergency(list, now) : list;
  }
  function orderEmergency(list, now) {
    // 거리순 응급실 → 가장 가까운 곳 기준 ER_PROMOTE_RANGE_M 안에서 포화(유효한 병상 0 이하)인 곳만 뒤로. 서버 egen._pick과 같은 규칙
    const limit = distanceValue(list[0].distance_m) + ER_PROMOTE_RANGE_M;
    const full = item => bedStatus(item, now)?.className === "full";
    const near = list.filter(item => distanceValue(item.distance_m) <= limit);
    return [...near.filter(item => !full(item)), ...near.filter(full), ...list.filter(item => !near.includes(item))];
  }
  function mapLinks(item) {
    const { lat, lng } = item;
    if (![lat, lng].every(v => typeof v === "number" && Number.isFinite(v)) || Math.abs(lat) > 90 || Math.abs(lng) > 180) return null;
    const params = new URLSearchParams({
      bbox: [Math.max(-180, lng - .006), Math.max(-90, lat - .004), Math.min(180, lng + .006), Math.min(90, lat + .004)].join(","),
      layer: "mapnik", marker: `${lat},${lng}`,
    });
    return {
      embed: `https://www.openstreetmap.org/export/embed.html?${params}`,
      external: `https://www.openstreetmap.org/?mlat=${lat}&mlon=${lng}#map=16/${lat}/${lng}`,
      // 출발지는 지도에서 확인한다. 조회 당시 GPS를 현재 위치로 재사용하지 않는다.
      directions: `https://www.google.com/maps/dir/?${new URLSearchParams({ api: "1", destination: `${lat},${lng}` })}`,
    };
  }
  function phoneLink(phone) {
    return typeof phone === "string" && /^\+?[\d ()-]+$/.test(phone) && /\d/.test(phone)
      ? "tel:" + phone.replace(/[^\d+]/g, "") : null;
  }
  function saveResults(data, locationLabel = "", position = null) {
    // 증상·대화 이력은 저장하지 않는다. 의료기관 결과만 현재 탭에 임시 보관한다.
    try {
      root.sessionStorage.setItem(RESULT_KEY, JSON.stringify({
        hospitals: data.hospitals, departments: data.departments.map(dept => ({ name: dept.name })),
        emergency: data.emergency === true, locationLabel,
        position: position && mapLinks(position) ? { lat: position.lat, lng: position.lng } : null, savedAt: Date.now(),
      }));
      return true;
    } catch { return false; }
  }
  function clearResults() { try { root.sessionStorage.removeItem(RESULT_KEY); } catch { /* 저장 불가 환경 */ } }
  function loadResults() {
    try {
      const data = JSON.parse(root.sessionStorage.getItem(RESULT_KEY));
      if (!data || !Array.isArray(data.hospitals) || !Array.isArray(data.departments) ||
          !Number.isFinite(data.savedAt) || Date.now() - data.savedAt > RESULT_TTL || data.savedAt > Date.now()) {
        clearResults(); return null;
      }
      return data;
    } catch { clearResults(); return null; }
  }
  function bedsTime(value) {
    // "YYYY-MM-DDTHH:MM"(한국 시간) → epoch ms. 형식이 다르면 null
    const m = typeof value === "string" ? value.match(/^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d)$/) : null;
    return m ? Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4] - 9, +m[5]) : null;
  }
  function bedStatus(item, now = Date.now()) {
    // 응급실 실시간 가용병상(er_beds). 0 이하는 포화. 값이 없거나, 입력 시각을 모르거나, 60분이 지났으면 표시하지 않는다
    // (구버전 응답·정보 없는 기관·저장된 결과를 나중에 다시 연 경우)
    if (!item || kindOf(item) !== "응급실" || typeof item.er_beds !== "number" || !Number.isFinite(item.er_beds)) return null;
    const at = bedsTime(item.er_beds_at);
    if (at === null || now - at > BEDS_MAX_AGE || at - now > BEDS_FUTURE_SKEW) return null;
    const time = item.er_beds_at.slice(11);
    const count = Math.trunc(item.er_beds);
    const full = count <= 0;
    return {
      text: full ? "병상 포화" : `병상 ${count}`, className: full ? "full" : "free", time,
      detail: `실시간 병상: ${full ? "포화" : `${count}개`} (${time} 기관 입력 기준)`,
      notice: "병상 수는 기관이 입력한 값이라 실제와 다를 수 있습니다. 이동 전에 전화로 확인하세요.",
    };
  }
  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  const api = { sendChat, KINDS, kindOf, formatDistance, openStatus, bedStatus, filterHospitals, mapLinks, phoneLink, saveResults, loadResults, clearResults, element };
  root.DrRouter = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(globalThis);
