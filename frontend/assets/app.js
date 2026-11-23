// 프론트 ↔ 백엔드 공유 헬퍼 (소유자: 박태준)
(function (root) {
  "use strict";
  const RESULT_KEY = "dr-router.results.v1";
  const RESULT_TTL = 30 * 60 * 1000;
  const KINDS = ["병원", "약국", "응급실"];

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
  function filterHospitals(items, kind = "전체", onlyOpen = false) {
    return items.filter(item => item && typeof item === "object")
      .filter(item => (kind === "전체" || kindOf(item) === kind) && (!onlyOpen || item.is_open === true))
      .sort((a, b) => distanceValue(a.distance_m) - distanceValue(b.distance_m));
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
  function bedStatus(item) {
    // 응급실 실시간 가용병상(er_beds). 0 이하는 포화. 값이 없으면 표시하지 않는다(구버전 응답·정보 없는 기관).
    if (!item || kindOf(item) !== "응급실" || typeof item.er_beds !== "number" || !Number.isFinite(item.er_beds)) return null;
    const matched = typeof item.er_beds_at === "string" ? item.er_beds_at.match(/T(\d\d:\d\d)$/) : null;
    const time = matched ? matched[1] : null;
    const count = Math.trunc(item.er_beds);
    const full = count <= 0;
    return {
      text: full ? "병상 포화" : `병상 ${count}`, className: full ? "full" : "free", time,
      detail: `실시간 병상: ${full ? "포화" : `${count}개`}${time ? ` (${time} 기관 입력 기준)` : ""}. 실제와 다를 수 있으니 이동 전에 전화로 확인하세요.`,
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
