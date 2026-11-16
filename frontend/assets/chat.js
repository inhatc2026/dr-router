"use strict";
const chatApi = window.DrRouter;
const chatForm = document.querySelector("#chat-form");
const messageInput = document.querySelector("#message");
const regionInput = document.querySelector("#region");
const chatStatus = document.querySelector("#chat-status");
const conversation = document.querySelector("#conversation");
const recommendations = document.querySelector("#recommendations");
const resultsLink = document.querySelector("#results-link");
const locationStatus = document.querySelector("#location-status");
const locationButton = document.querySelector("#get-location");
let history = [];
let point = null;
let locationRequest = 0;
let busy = false;
let endedWithEmergency = false;
const emergencyPlaces = document.querySelector("#emergency-places");
const charCount = document.querySelector("#char-count");
function updateLength() {
  const count = Array.from(messageInput.value).length;
  charCount.textContent = `${count} / 500자${count > 500 ? " · 500자 이내로 줄여 주세요." : ""}`;
  messageInput.setAttribute("aria-invalid", String(count > 500));
}
messageInput.addEventListener("input", updateLength);

function appendMessage(role, content) {
  const row = chatApi.element("div", `msg ${role === "user" ? "me" : "ai"}`);
  row.append(chatApi.element("span", "av", role === "user" ? "나" : "AI"), chatApi.element("div", "bubble", content));
  conversation.append(row);
  row.scrollIntoView({ block: "nearest" });
  return row;
}
function clearLocation() {
  locationRequest += 1;
  locationButton.disabled = false;
  locationStatus.textContent = regionInput.value.trim() ? "입력한 지역을 우선 확인합니다. 모호한 지역은 추가 확인이 필요합니다."
    : point ? "현재 위치를 기준으로 찾습니다." : "위치를 지정하면 주변 의료기관도 안내받을 수 있습니다.";
}
regionInput.addEventListener("input", clearLocation);
locationButton.addEventListener("click", () => {
  if (!navigator.geolocation) {
    locationStatus.textContent = "현재 위치를 사용할 수 없습니다. 시·군·구를 입력해 주세요."; return;
  }
  const requestId = ++locationRequest;
  locationButton.disabled = true;
  locationStatus.textContent = "현재 위치를 확인하고 있습니다…";
  navigator.geolocation.getCurrentPosition(position => {
    if (requestId !== locationRequest) return;
    point = { lat: position.coords.latitude, lng: position.coords.longitude };
    locationButton.disabled = false;
    locationStatus.textContent = regionInput.value.trim() ? "입력한 지역을 우선 확인하며, 현재 위치는 지역 후보 확인에 사용합니다." : "현재 위치를 기준으로 찾습니다.";
  }, () => {
    if (requestId !== locationRequest) return;
    point = null;
    locationButton.disabled = false;
    locationStatus.textContent = "위치를 확인하지 못했습니다. 시·군·구를 입력하거나 문진을 계속해 주세요.";
  }, { timeout: 10000, maximumAge: 60000 });
});

document.querySelector("#new-chat").addEventListener("click", () => {
  if (busy) return;
  history = [];
  endedWithEmergency = false;
  emergencyPlaces.replaceChildren();
  conversation.replaceChildren();
  recommendations.replaceChildren();
  recommendations.hidden = true;
  resultsLink.hidden = true;
  document.querySelector("#emergency-result").hidden = true;
  chatApi.clearResults();
  messageInput.value = "";
  updateLength();
  chatStatus.textContent = "새 문진을 시작합니다.";
  messageInput.focus();
});

chatForm.addEventListener("submit", async event => {
  event.preventDefault();
  const message = messageInput.value.trim();
  if (busy) return;
  if (!message) { chatStatus.textContent = "증상을 입력해 주세요."; messageInput.focus(); return; }
  if (Array.from(messageInput.value).length > 500) { chatStatus.textContent = "500자 이내로 입력해 주세요."; messageInput.focus(); return; }
  if (endedWithEmergency) {
    history = [];
    conversation.replaceChildren();
    endedWithEmergency = false;
  }
  busy = true;
  const sendButton = document.querySelector("#send");
  sendButton.disabled = true;
  messageInput.disabled = true;
  document.querySelector("#new-chat").disabled = true;
  recommendations.hidden = true;
  resultsLink.hidden = true;
  emergencyPlaces.replaceChildren();
  document.querySelector("#emergency-result").hidden = true;
  chatApi.clearResults();
  const region = regionInput.value.trim() || null;
  const requestPoint = point;
  chatStatus.textContent = "답변을 기다리고 있습니다…";
  const pendingMessage = appendMessage("user", message);
  try {
    const response = await chatApi.sendChat(message, { ...requestPoint, region, history });
    history.push({ role: "user", content: message }, { role: "assistant", content: response.reply });
    appendMessage("assistant", response.reply);
    messageInput.value = "";
    updateLength();
    endedWithEmergency = response.emergency === true;
    document.querySelector("#emergency-result").hidden = !response.emergency;
    recommendations.replaceChildren();
    if (response.emergency) {
      response.hospitals = chatApi.filterHospitals(response.hospitals, "응급실").slice(0, 3);
      response.departments = [];
      for (const place of response.hospitals) emergencyPlaces.append(chatApi.placeCard(place));
      if (!response.hospitals.length) emergencyPlaces.append(chatApi.element("p", "", "응급실 정보를 받지 못했습니다. 목록을 기다리지 말고 119에 연락하세요."));
    } else if (response.need_more_info) {
      chatStatus.textContent = "추가 질문에 답해 주세요.";
      return;
    }
    if (!response.emergency && response.departments.length) {
      const score = dept => typeof dept.score === "number" && Number.isFinite(dept.score) && dept.score >= 0 && dept.score <= 1 ? dept.score : -1;
      response.departments = [...response.departments].sort((a, b) => score(b) - score(a)).slice(0, 3);
      // 현재 공유 계약에 출처 필드가 없다. 문서가 있다는 전제로 추천을 확정하지 않는다.
      recommendations.append(chatApi.element("h2", "", "참고 진료과 후보"));
      recommendations.append(chatApi.element("p", "", score(response.departments[0]) < .5
        ? "분류 확신이 낮고 근거 문서를 확인할 수 없어 진료과를 확정하기 어렵습니다. 의료기관을 방문해 상담해 주세요."
        : "근거 문서를 확인할 수 없어 추천을 확정하지 않습니다. 의료기관을 방문해 상담해 주세요."));
      for (const dept of response.departments) {
        const row = chatApi.element("div", "department");
        row.append(chatApi.element("h3", "", dept.name), chatApi.element("p", "muted", dept.reason));
        row.append(chatApi.element("p", "note", `분류 점수: ${score(dept) < 0 ? "정보 없음" : score(dept).toFixed(2)} · 근거 문서: 확인 필요`));
        recommendations.append(row);
      }
      recommendations.hidden = false;
    } else if (!response.emergency) {
      recommendations.append(chatApi.element("p", "", "진료과를 판단할 정보가 부족합니다. 의료기관을 방문해 상담해 주세요."));
      recommendations.hidden = false;
    }
    // 응답 계약에 실제 검색 중심점이 없으므로 입력값을 검색 위치라고 단정하지 않는다.
    const locationLabel = region ? `요청 지역: ${region}` : requestPoint ? "요청 위치: 현재 GPS" : "위치 미지정";
    const saved = chatApi.saveResults(response, locationLabel, requestPoint);
    resultsLink.hidden = !saved;
    resultsLink.textContent = response.hospitals.length
      ? `의료기관 ${response.hospitals.length}곳 보기 →` : "의료기관 검색 결과 보기 →";
    chatStatus.textContent = !saved ? "이 탭에 목록을 보관할 수 없습니다. 브라우저 저장 설정을 확인해 주세요."
      : response.need_more_info ? "추가 질문에 답해 주세요." : "답변을 확인해 주세요.";
  } catch (error) {
    pendingMessage.remove();
    chatStatus.textContent = error instanceof TypeError ? "연결하지 못했습니다. 입력 내용은 유지됩니다. 다시 전송해 주세요." : error.message;
  } finally {
    busy = false;
    sendButton.disabled = false;
    messageInput.disabled = false;
    document.querySelector("#new-chat").disabled = false;
    messageInput.focus();
  }
});
