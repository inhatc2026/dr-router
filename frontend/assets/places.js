"use strict";
// 응급 안내와 전체 목록에서 같은 필드와 안전한 텍스트 렌더링을 사용한다.
window.DrRouter.placeCard = function (item, onMap) {
  const api = window.DrRouter;
  const card = api.element("article", "hospital-card");
  const heading = api.element("div", "card-heading");
  const status = api.openStatus(item.is_open, api.kindOf(item));
  heading.append(api.element("h2", "", item.name || "이름 정보 없음"), api.element("span", `tag ${status.className}`, status.text));
  const labels = api.element("div", "labels");
  labels.append(api.element("span", "kind", api.kindOf(item)));
  if (item.dept && item.dept !== api.kindOf(item)) labels.append(api.element("span", "dept", item.dept));
  const beds = api.bedStatus(item);
  if (beds) labels.append(api.element("span", `beds ${beds.className}`, beds.time ? `${beds.text} · ${beds.time} 기준` : beds.text));
  const distance = api.element("p", "distance", api.formatDistance(item.distance_m));
  if (typeof item.distance_m === "number" && item.distance_m > 5000) distance.append(api.element("span", "far-note", " · 5 km 밖"));
  const actions = api.element("div", "actions");
  const phone = api.phoneLink(item.phone);
  if (phone) {
    const call = api.element("a", "btn secondary", `전화 ${item.phone}`);
    call.href = phone;
    actions.append(call);
  } else actions.append(api.element("span", "muted", "전화번호 정보 없음"));
  const links = api.mapLinks(item);
  if (onMap) {
    const button = api.element("button", "btn secondary", links ? "지도 보기" : "좌표 정보 없음");
    button.type = "button";
    button.disabled = !links;
    button.addEventListener("click", () => onMap(item));
    actions.append(button);
  } else if (links) {
    const link = api.element("a", "btn secondary", "지도 보기 ↗");
    link.href = links.external; link.target = "_blank"; link.rel = "noopener noreferrer";
    actions.append(link);
  }
  if (links) {
    const directions = api.element("a", "btn secondary", "길찾기 ↗");
    directions.href = links.directions;
    directions.target = "_blank";
    directions.rel = "noopener noreferrer";
    directions.setAttribute("aria-label", `${item.name || "의료기관"} 길찾기 (구글맵, 새 창)`);
    actions.append(directions);
  }
  const details = api.element("details", "place-details");
  details.append(api.element("summary", "", "상세 정보"),
    api.element("p", "", `진료과: ${item.dept || "정보 없음"}`),
    api.element("p", "", `주소: ${item.address || "정보 없음"}`),
    api.element("p", "", `전화: ${item.phone || "정보 없음"}`),
    ...(beds ? [api.element("p", "", beds.detail)] : []),
    api.element("p", "note", "진료 상태는 조회 시점 기준입니다. 방문 전 전화로 확인해 주세요."));
  card.append(heading, labels, api.element("p", "address", item.address || "주소 정보 없음"), distance, actions, details);
  return card;
};
