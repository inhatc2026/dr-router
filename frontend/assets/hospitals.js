"use strict";
const placesApi = window.DrRouter;
const snapshot = placesApi.loadResults();
const hospitalList = document.querySelector("#hospital-list");
const resultCount = document.querySelector("#result-count");
const mapNode = document.querySelector("#places-map");
const mapPanel = document.querySelector("#map-panel");
const markerStyles = { "병원": ["hospital", "병"], "약국": ["pharmacy", "약"], "응급실": ["er", "응"], "내 위치": ["me", "나"] };
let activeKind = snapshot?.emergency ? "응급실" : "전체";
let map;
let markers;
let markersByItem = new Map();

function showMap(item) {
  const links = placesApi.mapLinks(item);
  if (!links) return;
  document.querySelector("#map-name").textContent = item.name;
  document.querySelector("#map-address").textContent = item.address || "주소 정보 없음";
  const external = document.querySelector("#external-map");
  external.href = links.external;
  external.hidden = false;
  if (map) { map.setView([item.lat, item.lng], 15); markersByItem.get(item)?.openPopup(); }
  mapPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
}
function renderMap(items) {
  document.querySelector("#map-name").textContent = "의료기관 위치";
  document.querySelector("#map-address").textContent = "색상과 글자로 기관 종류를 구분합니다.";
  document.querySelector("#external-map").hidden = true;
  const placeholder = document.querySelector("#map-placeholder");
  const points = items.filter(item => placesApi.mapLinks(item));
  const userPoint = snapshot?.position && placesApi.mapLinks(snapshot.position) ? snapshot.position : null;
  mapNode.hidden = !window.L || !(points.length || userPoint);
  placeholder.hidden = !mapNode.hidden;
  placeholder.textContent = window.L ? "표시할 좌표가 없습니다. 주소와 전화번호를 확인해 주세요." : "지도를 불러오지 못했습니다. 기관의 지도 보기 링크를 이용해 주세요.";
  if (mapNode.hidden) { markers?.clearLayers(); return; }
  if (!map) {
    map = L.map(mapNode, { scrollWheelZoom: false });
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    }).on("tileerror", () => { document.querySelector("#map-status").textContent = "지도 배경을 불러오지 못했습니다. 주소 또는 큰 지도 링크를 확인해 주세요."; }).addTo(map);
    markers = L.featureGroup().addTo(map);
  }
  map.invalidateSize();
  markers.clearLayers();
  markersByItem = new Map();
  function addMarker(item, kind, label) {
    const [style, letter] = markerStyles[kind];
    const icon = L.divIcon({ className: `place-marker ${style}`, html: letter, iconSize: [44, 44], iconAnchor: [22, 22] });
    const marker = L.marker([item.lat, item.lng], { icon, title: label, alt: label });
    marker.bindPopup(placesApi.element("p", "", label));
    marker.addTo(markers);
    markersByItem.set(item, marker);
  }
  points.forEach(item => addMarker(item, placesApi.kindOf(item), `${placesApi.kindOf(item)} · ${item.name}`));
  if (userPoint) addMarker(userPoint, "내 위치", "내 위치 (조회 당시 GPS)");
  map.fitBounds(markers.getBounds(), { padding: [35, 35], maxZoom: 15 });
}
function render() {
  hospitalList.replaceChildren();
  const source = snapshot?.emergency ? placesApi.filterHospitals(snapshot.hospitals, "응급실").slice(0, 3) : snapshot?.hospitals || [];
  const items = placesApi.filterHospitals(source, activeKind, document.querySelector("#only-open").checked);
  resultCount.textContent = `${items.length}곳`;
  renderMap(items);
  if (!items.length) {
    hospitalList.append(placesApi.element("p", "empty-state", !snapshot
      ? "표시할 결과가 없거나 조회 후 30분이 지났습니다. AI 문진에서 새로 안내받아 주세요."
      : source.length ? "선택한 조건에 맞는 의료기관이 없습니다. 필터를 바꿔 보세요."
      : snapshot.emergency ? "이번 응답에 응급실 정보가 없습니다. 목록을 기다리지 말고 119에 연락하세요."
      : "이번 응답에 의료기관이 없습니다. 위치나 지역을 지정하고 문진을 이어가 주세요."));
    return;
  }
  items.forEach(item => hospitalList.append(placesApi.placeCard(item, showMap)));
}

document.querySelectorAll("[data-kind]").forEach(button => {
  button.setAttribute("aria-pressed", String(button.dataset.kind === activeKind));
  if (snapshot?.emergency) button.disabled = button.dataset.kind !== "응급실";
  button.addEventListener("click", () => {
    activeKind = button.dataset.kind;
    document.querySelectorAll("[data-kind]").forEach(chip => chip.setAttribute("aria-pressed", String(chip === button)));
    render();
  });
});
document.querySelector("#only-open").addEventListener("change", render);
if (snapshot) {
  const depts = snapshot.departments.map(dept => dept.name).filter(Boolean).join(", ");
  document.querySelector("#search-context").textContent = `${snapshot.locationLabel || "검색 위치"}${depts ? " · 참고 진료과 후보: " + depts : ""}`;
  document.querySelector("#search-time").textContent = `${new Date(snapshot.savedAt).toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" })} 조회 · 상태는 조회 시점 기준이며 방문 전 전화로 확인해 주세요.`;
  document.querySelector("#emergency-result").hidden = !snapshot.emergency;
  if (snapshot.emergency) document.querySelector(".open-filter").hidden = true;
}
render();
