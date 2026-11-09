// =============================================================================
// Hayamimi OBS Sync - Content Script
// 早耳 Web ページと Background Service Worker を window.postMessage で橋渡し
// =============================================================================

// 早耳マイク画面へ拡張機能が存在することを通知
function announceReady() {
  window.postMessage({
    source: "hayamimi-extension",
    type: "EXTENSION_READY"
  }, "*");
}

announceReady();
// ページのロード完了後にも再通知
document.addEventListener("DOMContentLoaded", announceReady);
window.addEventListener("load", announceReady);

// ページからのメッセージ受信 (早耳マイク画面 ➔ 拡張機能)
window.addEventListener("message", (event) => {
  if (!event.data || event.data.source !== "hayamimi-page") return;

  if (event.data.type === "GET_OBS_STATUS") {
    chrome.runtime.sendMessage({ type: "GET_OBS_STATUS" }, (resp) => {
      if (resp) {
        window.postMessage({
          source: "hayamimi-extension",
          type: "OBS_STATUS_UPDATE",
          ...resp
        }, "*");
      }
    });
  } else if (event.data.type === "UPDATE_CONFIG") {
    chrome.runtime.sendMessage({
      type: "UPDATE_CONFIG",
      config: event.data.config
    });
  } else if (event.data.type === "TRIGGER_RECONNECT") {
    chrome.runtime.sendMessage({ type: "TRIGGER_RECONNECT" });
  }
});

// Background Service Worker からのメッセージ受信 (拡張機能 ➔ 早耳マイク画面)
chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.type === "OBS_STATUS_UPDATE") {
    window.postMessage({
      source: "hayamimi-extension",
      type: "OBS_STATUS_UPDATE",
      ...msg
    }, "*");
  }
});

// 初回ステータスを即時問い合わせ
setTimeout(() => {
  chrome.runtime.sendMessage({ type: "GET_OBS_STATUS" }, (resp) => {
    if (resp) {
      window.postMessage({
        source: "hayamimi-extension",
        type: "OBS_STATUS_UPDATE",
        ...resp
      }, "*");
    }
  });
}, 300);
