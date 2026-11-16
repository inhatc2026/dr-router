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

function appendMessage(role, content) {
  const row = chatApi.element("div", `msg ${role === "user" ? "me" : "ai"}`);
  row.append(chatApi.element("span", "av", role === "user" ? "나" : "AI"), chatApi.element("div", "bubble", content));
  conversation.append(row);
  row.scrollIntoView({ block: "nearest" });
}
function clearLocation() {
  point = null;
  locationRequest += 1;
  locationButton.disabled = false;
  locationStatus.textContent = regionInput.value.trim() ? "입력한 지역을 기준으로 찾습니다." : "위치를 지정하면 주변 의료기관도 안내받을 수 있습니다.";
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
    regionInput.value = "";
    locationButton.disabled = false;
    locationStatus.textContent = "현재 위치를 기준으로 찾습니다.";
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
  conversation.replaceChildren();
  recommendations.replaceChildren();
  recommendations.hidden = true;
  resultsLink.hidden = true;
  document.querySelector("#emergency-result").hidden = true;
  chatApi.clearResults();
  messageInput.value = "";
  chatStatus.textContent = "새 문진을 시작합니다.";
  messageInput.focus();
});

chatForm.addEventListener("submit", async event => {
  event.preventDefault();
  const message = messageInput.value.trim();
  if (!message || busy) return;
  busy = true;
  const sendButton = document.querySelector("#send");
  sendButton.disabled = true;
  messageInput.disabled = true;
  document.querySelector("#new-chat").disabled = true;
  recommendations.hidden = true;
  resultsLink.hidden = true;
  chatApi.clearResults();
  const region = regionInput.value.trim() || null;
  const requestPoint = point;
  chatStatus.textContent = "답변을 기다리고 있습니다…";
  appendMessage("user", message);
  try {
    const response = await chatApi.sendChat(message, { ...requestPoint, region, history });
    history.push({ role: "user", content: message }, { role: "assistant", content: response.reply });
    appendMessage("assistant", response.reply);
    messageInput.value = "";
    document.querySelector("#emergency-result").hidden = !response.emergency;
    recommendations.replaceChildren();
    if (!response.emergency && response.departments.length) {
      recommendations.append(chatApi.element("h2", "", "추천 진료과"));
      for (const dept of response.departments) {
        const row = chatApi.element("div", "department");
        row.append(chatApi.element("h3", "", dept.name), chatApi.element("p", "muted", dept.reason));
        recommendations.append(row);
      }
      recommendations.hidden = false;
    }
    const saved = chatApi.saveResults(response, requestPoint ? "현재 위치" : region || "위치 미지정");
    resultsLink.hidden = !saved;
    resultsLink.textContent = response.hospitals.length
      ? `의료기관 ${response.hospitals.length}곳 보기 →` : "의료기관 검색 결과 보기 →";
    chatStatus.textContent = !saved ? "이 탭에 목록을 보관할 수 없습니다. 브라우저 저장 설정을 확인해 주세요."
      : response.need_more_info ? "추가 질문에 답해 주세요." : "답변을 확인해 주세요.";
  } catch (error) {
    chatStatus.textContent = error instanceof TypeError ? "연결하지 못했습니다. 입력 내용은 유지됩니다. 다시 전송해 주세요." : error.message;
  } finally {
    busy = false;
    sendButton.disabled = false;
    messageInput.disabled = false;
    document.querySelector("#new-chat").disabled = false;
    messageInput.focus();
  }
});
