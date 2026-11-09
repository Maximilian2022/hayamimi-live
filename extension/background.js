// =============================================================================
// Hayamimi OBS Sync - Background Service Worker (Manifest V3)
// OBS WebSocket v5 (ws://127.0.0.1:4455) と接続し、マイクミュート状態を監視・配信
// =============================================================================

let obsWs = null;
let isConnected = false;
let isMuted = false;
let activeMicName = "Mic/Aux";
let reconnectTimer = null;
let retryInterval = 4000;

let config = {
  url: "ws://127.0.0.1:4455",
  password: "",
  targetSource: "__auto__"
};

// 保存された設定の読み込み
chrome.storage.local.get(["obsConfig"], (res) => {
  if (res && res.obsConfig) {
    config = { ...config, ...res.obsConfig };
  }
  connectObs();
});

// OBS WebSocket v5 SHA-256 認証ハッシュ計算
async function computeAuth(password, salt, challenge) {
  const enc = new TextEncoder();
  const s1 = await crypto.subtle.digest("SHA-256", enc.encode(password + salt));
  const s1Base64 = btoa(String.fromCharCode(...new Uint8Array(s1)));
  const s2 = await crypto.subtle.digest("SHA-256", enc.encode(s1Base64 + challenge));
  return btoa(String.fromCharCode(...new Uint8Array(s2)));
}

// 全タブ（早耳マイク画面）へ状態をブロードキャスト
function broadcastStatus(targetTabId = null) {
  const payload = {
    type: "OBS_STATUS_UPDATE",
    isConnected,
    isMuted,
    activeMicName,
    config
  };

  if (targetTabId) {
    chrome.tabs.sendMessage(targetTabId, payload).catch(() => {});
  } else {
    chrome.tabs.query({}, (tabs) => {
      for (const tab of tabs) {
        if (tab.id) {
          chrome.tabs.sendMessage(tab.id, payload).catch(() => {});
        }
      }
    });
  }
}

function connectObs() {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }

  if (obsWs) {
    try { obsWs.close(); } catch (_) {}
    obsWs = null;
  }

  try {
    obsWs = new WebSocket(config.url);
  } catch (err) {
    isConnected = false;
    broadcastStatus();
    scheduleReconnect();
    return;
  }

  obsWs.onopen = () => {
    // Hello メッセージ (op 0) の待機
  };

  obsWs.onmessage = async (evt) => {
    try {
      const msg = JSON.parse(evt.data);
      const op = msg.op;
      const d = msg.d;

      if (op === 0) { // Hello
        const identifyPayload = {
          rpcVersion: 1,
          eventSubscriptions: 8 // EventSubscription::Inputs (1 << 3)
        };

        if (d.authentication) {
          if (!config.password) {
            console.warn("[Hayamimi Ext] OBS password required but empty");
            isConnected = false;
            broadcastStatus();
            return;
          }
          identifyPayload.authentication = await computeAuth(config.password, d.authentication.salt, d.authentication.challenge);
        }

        obsWs.send(JSON.stringify({ op: 1, d: identifyPayload }));
      } else if (op === 2) { // Identified (接続完了)
        isConnected = true;
        // マイク名と初期ミュート状態の問い合わせ
        obsWs.send(JSON.stringify({
          op: 6,
          d: { requestType: "GetSpecialInputs", requestId: "req-special-inputs" }
        }));
        broadcastStatus();
      } else if (op === 5) { // Event
        if (d.eventType === "InputMuteStateChanged") {
          const evData = d.eventData;
          if (evData && (!activeMicName || evData.inputName === activeMicName)) {
            isMuted = Boolean(evData.inputMuted);
            broadcastStatus();
          }
        }
      } else if (op === 7) { // RequestResponse
        if (d.requestId === "req-special-inputs" && d.responseData) {
          activeMicName = d.responseData.mic1 || d.responseData.mic2 || "Mic/Aux";
          obsWs.send(JSON.stringify({
            op: 6,
            d: {
              requestType: "GetInputMute",
              requestId: "req-get-mute",
              requestData: { inputName: activeMicName }
            }
          }));
        } else if (d.requestId === "req-get-mute" && d.responseData) {
          isMuted = Boolean(d.responseData.inputMuted);
          broadcastStatus();
        }
      }
    } catch (e) {
      console.error("[Hayamimi Ext] Message parse error:", e);
    }
  };

  obsWs.onerror = () => {
    isConnected = false;
    broadcastStatus();
  };

  obsWs.onclose = () => {
    isConnected = false;
    isMuted = false;
    broadcastStatus();
    scheduleReconnect();
  };
}

function scheduleReconnect() {
  if (reconnectTimer) clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(connectObs, retryInterval);
}

// Content script からの受信ハンドラ
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.type === "GET_OBS_STATUS") {
    sendResponse({
      isConnected,
      isMuted,
      activeMicName,
      config
    });
  } else if (request.type === "UPDATE_CONFIG") {
    config = { ...config, ...request.config };
    chrome.storage.local.set({ obsConfig: config });
    connectObs();
    sendResponse({ ok: true });
  } else if (request.type === "TRIGGER_RECONNECT") {
    connectObs();
    sendResponse({ ok: true });
  }
  return true;
});
