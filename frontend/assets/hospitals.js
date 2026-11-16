"use strict";
const placesApi = window.DrRouter;
const snapshot = placesApi.loadResults();
const hospitalList = document.querySelector("#hospital-list");
const resultCount = document.querySelector("#result-count");
const mapFrame = document.querySelector("#map-frame");
const mapPanel = document.querySelector("#map-panel");
let activeKind = "전체";

function clearMap() {
  mapFrame.hidden = true;
  mapFrame.removeAttribute("src");
  document.querySelector("#map-placeholder").hidden = false;
  document.querySelector("#map-name").textContent = "의료기관 위치";
  document.querySelector("#map-address").textContent = "목록에서 ‘지도 보기’를 선택해 주세요.";
  document.querySelector("#external-map").hidden = true;
}
function showMap(item) {
  const links = placesApi.mapLinks(item);
  if (!links) return;
  mapFrame.src = links.embed;
  mapFrame.title = `${item.name} 위치 지도`;
  mapFrame.hidden = false;
  document.querySelector("#map-placeholder").hidden = true;
  document.querySelector("#map-name").textContent = item.name;
  document.querySelector("#map-address").textContent = item.address || "주소 정보 없음";
  const external = document.querySelector("#external-map");
  external.href = links.external;
  external.hidden = false;
  mapPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
}
function hospitalCard(item) {
  const card = placesApi.element("article", "hospital-card");
  const heading = placesApi.element("div", "card-heading");
  const status = placesApi.openStatus(item.is_open);
  heading.append(placesApi.element("h2", "", item.name || "이름 정보 없음"), placesApi.element("span", `tag ${status.className}`, status.text));
  const labels = placesApi.element("div", "labels");
  labels.append(placesApi.element("span", "kind", placesApi.kindOf(item)));
  if (item.dept && item.dept !== placesApi.kindOf(item)) labels.append(placesApi.element("span", "dept", item.dept));
  const address = placesApi.element("p", "address", item.address || "주소 정보 없음");
  const distance = placesApi.element("p", "distance", placesApi.formatDistance(item.distance_m));
  if (typeof item.distance_m === "number" && item.distance_m > 5000) distance.append(placesApi.element("span", "far-note", " · 5 km 밖"));
  const actions = placesApi.element("div", "actions");
  const phone = placesApi.phoneLink(item.phone);
  if (phone) {
    const call = placesApi.element("a", "btn secondary", `전화 ${item.phone}`);
    call.href = phone;
    actions.append(call);
  } else actions.append(placesApi.element("span", "muted", "전화번호 정보 없음"));
  const mapButton = placesApi.element("button", "btn secondary", "지도 보기");
  mapButton.type = "button";
  mapButton.disabled = !placesApi.mapLinks(item);
  if (mapButton.disabled) mapButton.textContent = "좌표 정보 없음";
  mapButton.addEventListener("click", () => showMap(item));
  actions.append(mapButton);
  card.append(heading, labels, address, distance, actions);
  return card;
}
function render() {
  clearMap();
  hospitalList.replaceChildren();
  const items = placesApi.filterHospitals(snapshot?.hospitals || [], activeKind, document.querySelector("#only-open").checked);
  resultCount.textContent = `${items.length}곳`;
  if (!items.length) {
    hospitalList.append(placesApi.element("p", "empty-state", !snapshot
      ? "표시할 결과가 없거나 조회 후 30분이 지났습니다. AI 문진에서 새로 안내받아 주세요."
      : snapshot.hospitals.length ? "선택한 조건에 맞는 의료기관이 없습니다. 필터를 바꿔 보세요."
      : "이번 응답에 의료기관이 없습니다. 위치나 지역을 지정하고 문진을 이어가 주세요."));
    return;
  }
  items.forEach(item => hospitalList.append(hospitalCard(item)));
}
document.querySelectorAll("[data-kind]").forEach(button => {
  button.addEventListener("click", () => {
    activeKind = button.dataset.kind;
    document.querySelectorAll("[data-kind]").forEach(chip => chip.setAttribute("aria-pressed", String(chip === button)));
    render();
  });
});
document.querySelector("#only-open").addEventListener("change", render);
if (snapshot) {
  const depts = snapshot.departments.map(dept => dept.name).filter(Boolean).join(", ");
  document.querySelector("#search-context").textContent = `${snapshot.locationLabel || "검색 위치"}${depts ? " · 추천 진료과: " + depts : ""}`;
  document.querySelector("#search-time").textContent = `${new Date(snapshot.savedAt).toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" })} 조회 · 상태는 조회 시점 기준이며 방문 전 전화로 확인해 주세요.`;
  document.querySelector("#emergency-result").hidden = !snapshot.emergency;
}
render();
