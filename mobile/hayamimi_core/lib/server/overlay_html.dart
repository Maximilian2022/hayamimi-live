/// Transparent-background overlay page served at `/`, for use as an OBS
/// browser source. Ported from `OVERLAY_HTML` in `scripts/subtitle_server.py`
/// so both the desktop and mobile subtitle servers look identical to OBS.
const String overlayHtml = '''
<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>Hayamimi Live - OBS 字幕オーバーレイ</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Black+Han+Sans&family=Bungee&family=Dela+Gothic+One&family=Do+Hyeon&family=DotGothic16&family=Fredoka:wght@500;700&family=Hachi+Maru+Pop&family=IBM+Plex+Sans+JP:wght@500;700&family=IBM+Plex+Sans+KR:wght@500;700&family=IBM+Plex+Sans:wght@500;700&family=Jua&family=Kaisei+Decol:wght@700&family=Kiwi+Maru:wght@500&family=Kosugi+Maru&family=LINE+Seed+JP:wght@400;700;800&family=M+PLUS+1p:wght@500;700&family=M+PLUS+Rounded+1c:wght@500;700;800&family=Ma+Shan+Zheng&family=Montserrat:wght@500;700;800&family=Noto+Sans+JP:wght@500;700&family=Noto+Sans+KR:wght@500;700&family=Noto+Sans+SC:wght@500;700&family=Noto+Sans+TC:wght@500;700&family=Oswald:wght@500;700&family=Rampart+One&family=Reggae+One&family=Righteous&family=Shippori+Mincho+B1:wght@600;800&family=Yuji+Boku&family=ZCOOL+KuaiLe&family=Zen+Kaku+Gothic+New:wght@500;700&display=swap" rel="stylesheet">
<style>
  @font-face {
    font-family: 'LINE Seed KR';
    src: local('LINE Seed KR Bold'), local('LINE Seed KR-Bold'), local('LINESeedKR-Bd'),
         url('https://cdn.jsdelivr.net/gh/projectnoonnu/noonfonts_11-01@1.0/LINESeedKR-Bd.woff2') format('woff2');
    font-weight: 700;
    font-display: swap;
  }
  @font-face {
    font-family: 'LINE Seed KR';
    src: local('LINE Seed KR Regular'), local('LINE Seed KR-Regular'), local('LINESeedKR-Rg'),
         url('https://cdn.jsdelivr.net/gh/projectnoonnu/noonfonts_11-01@1.0/LINESeedKR-Rg.woff2') format('woff2');
    font-weight: 400;
    font-display: swap;
  }

  html, body { margin: 0; background: transparent; overflow: hidden; width: 100%; height: 100%; }
  #box {
    position: absolute; left: 0; right: 0; bottom: 4vh;
    text-align: center;
    font-family: 'Zen Kaku Gothic New', sans-serif;
    font-size: 5vh; line-height: 1.4; color: #fff;
    text-shadow: 0 0 8px #000, 0 0 4px #000, 2px 2px 2px #000;
    transition: all 0.2s ease;
    box-sizing: border-box;
    padding: 0 2vw;
  }
  #final-line, #partial-line { display: block; min-height: 1.4em; }
  #partial-line { opacity: 0.75; font-style: italic; font-size: 0.85em; }
  #trans-line { display: block; opacity: 0.85; font-size: 0.75em; margin-top: 0.2em; min-height: 1.2em; }
</style>
</head>
<body>
<div id="box">
  <span id="final-line"></span>
  <span id="partial-line"></span>
  <span id="trans-line"></span>
</div>
<script>
  const mode = new URLSearchParams(location.search).get("show") || "both";
  const box = document.getElementById("box");
  const fin = document.getElementById("final-line");
  const par = document.getElementById("partial-line");
  const trans = document.getElementById("trans-line");
  if (mode === "final") par.style.display = "none";
  if (mode === "partial") { fin.style.display = "none"; trans.style.display = "none"; }

  function hexToRgba(hex, alphaPercent) {
    hex = (hex || "#000000").replace("#", "");
    if (hex.length === 3) hex = hex.split("").map(c => c+c).join("");
    const r = parseInt(hex.substring(0, 2), 16) || 0;
    const g = parseInt(hex.substring(2, 4), 16) || 0;
    const b = parseInt(hex.substring(4, 6), 16) || 0;
    return `rgba(${r}, ${g}, ${b}, ${(alphaPercent || 0) / 100})`;
  }

  function generateTextShadow(color, width, effect) {
    if (width <= 0) return "none";
    if (effect === "glow") {
      return `0 0 ${width * 2}px ${color}, 0 0 ${width * 4}px ${color}, 0 0 ${width * 7}px ${color}`;
    }
    if (effect === "double") {
      let s = [];
      for (let x = -width; x <= width; x++) {
        for (let y = -width; y <= width; y++) {
          if (x !== 0 || y !== 0) s.push(`${x}px ${y}px 0 #000000`);
        }
      }
      const w2 = width + 2;
      for (let x = -w2; x <= w2; x += 2) {
        for (let y = -w2; y <= w2; y += 2) {
          s.push(`${x}px ${y}px 0 ${color}`);
        }
      }
      s.push(`0 0 ${w2 * 2}px ${color}`);
      return s.join(", ");
    }
    let s = [];
    for (let x = -width; x <= width; x++) {
      for (let y = -width; y <= width; y++) {
        if (x !== 0 || y !== 0) s.push(`${x}px ${y}px 0 ${color}`);
      }
    }
    s.push(`0 0 ${width * 2}px ${color}`);
    return s.join(", ");
  }

  let currentConfig = null;

  function applyStyle(s) {
    if (!s) return;
    currentConfig = s;

    // 全体コンテナ配置 & 背景
    if (s.align) box.style.textAlign = s.align;
    if (s.bottom != null) box.style.bottom = s.bottom + "vh";
    if (s.bgColor && s.bgOpacity != null) {
      box.style.backgroundColor = hexToRgba(s.bgColor, parseInt(s.bgOpacity));
      box.style.padding = parseInt(s.bgOpacity) > 0 ? "0.6rem 1.2rem" : "0 2vw";
    }
    if (s.radius != null) box.style.borderRadius = s.radius + "px";
    if (s.showTrans != null && mode !== "partial") {
      trans.style.display = s.showTrans ? "block" : "none";
    }

    const isSplit = s.mode === "split";
    const mainCfg = isSplit && s.main ? s.main : s;
    const transCfg = isSplit && s.trans ? s.trans : s;

    // メイン字幕 (確定 & ドラフト)
    fin.style.fontFamily = mainCfg.font || s.font || "'Zen Kaku Gothic New', sans-serif";
    fin.style.fontSize = (mainCfg.size || s.size || 42) + "px";
    fin.style.color = mainCfg.color || s.color || "#ffffff";
    fin.style.textShadow = generateTextShadow(
      mainCfg.strokeColor || s.strokeColor || "#000000",
      parseInt(mainCfg.strokeWidth != null ? mainCfg.strokeWidth : (s.strokeWidth != null ? s.strokeWidth : 3)),
      mainCfg.effect || s.effect || "stroke"
    );

    par.style.fontFamily = fin.style.fontFamily;
    par.style.fontSize = ((mainCfg.size || s.size || 42) * 0.85) + "px";
    par.style.color = fin.style.color;
    par.style.textShadow = fin.style.textShadow;

    // 翻訳字幕コンテナの基本スタイル
    trans.style.fontFamily = transCfg.font || s.font || "'Zen Kaku Gothic New', sans-serif";
    trans.style.fontSize = (isSplit ? (transCfg.size || 32) : ((s.size || 42) * 0.75)) + "px";
    trans.style.color = transCfg.color || (isSplit ? "#ffd166" : s.color);
    trans.style.textShadow = generateTextShadow(
      transCfg.strokeColor || s.strokeColor || "#000000",
      parseInt(transCfg.strokeWidth != null ? transCfg.strokeWidth : (s.strokeWidth != null ? s.strokeWidth : 2.5)),
      transCfg.effect || s.effect || "stroke"
    );

    // 既存翻訳行のスタイル更新
    for (const child of trans.children) {
      applyTransLineStyle(child);
    }
  }

  function applyTransLineStyle(el) {
    if (!currentConfig) return;
    const lang = el.dataset.lang || "";
    const isSplit = currentConfig.mode === "split";
    const transCfg = isSplit && currentConfig.trans ? currentConfig.trans : currentConfig;
    const overrides = (isSplit && currentConfig.langOverrides && currentConfig.langOverrides[lang]) ? currentConfig.langOverrides[lang] : {};

    el.style.fontFamily = overrides.font || transCfg.font || currentConfig.font || "'Zen Kaku Gothic New', sans-serif";
    if (overrides.size != null) el.style.fontSize = overrides.size + "px";
    el.style.color = overrides.color || transCfg.color || (isSplit ? "#ffd166" : currentConfig.color);
    const strokeColor = overrides.strokeColor || transCfg.strokeColor || currentConfig.strokeColor || "#000000";
    const strokeWidth = overrides.strokeWidth != null ? overrides.strokeWidth : (transCfg.strokeWidth != null ? transCfg.strokeWidth : currentConfig.strokeWidth);
    const effect = overrides.effect || transCfg.effect || currentConfig.effect || "stroke";
    el.style.textShadow = generateTextShadow(strokeColor, parseInt(strokeWidth != null ? strokeWidth : 2.5), effect);
  }

  let clearTimer = null;
  let es = null;
  let heartbeatTimer = null;
  let reconnectTimer = null;
  let retryCount = 0;

  function resetHeartbeat() {
    if (heartbeatTimer) clearTimeout(heartbeatTimer);
    // サーバーは15秒ごとに keepalive を送るため、25秒間無音・無通信なら再接続
    heartbeatTimer = setTimeout(() => {
      connectEventSource();
    }, 25000);
  }

  function connectEventSource() {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    if (es) {
      try { es.close(); } catch (_) {}
      es = null;
    }
    resetHeartbeat();

    try {
      es = new EventSource("/events?_t=" + Date.now());
    } catch (_) {
      scheduleReconnect();
      return;
    }

    es.onopen = () => {
      retryCount = 0;
      resetHeartbeat();
      fetch("/overlay-style?_t=" + Date.now()).then(r => r.json()).then(applyStyle).catch(() => {});
    };

    es.onmessage = (e) => {
      resetHeartbeat();
      let ev;
      try {
        ev = JSON.parse(e.data);
      } catch (_) {
        return;
      }
      if (ev.type === "style_update") {
        applyStyle(ev.style);
        return;
      }
      if (clearTimer) { clearTimeout(clearTimer); clearTimer = null; }
      if (ev.type === "partial") {
        par.textContent = ev.text;
      } else if (ev.type === "final") {
        fin.textContent = ev.text;
        par.textContent = "";
        trans.innerHTML = "";
      } else if (ev.type === "translation") {
        const line = document.createElement("div");
        line.dataset.lang = ev.lang;
        line.textContent = `→ [${ev.lang}] ${ev.text}`;
        applyTransLineStyle(line);
        trans.appendChild(line);
      }
      clearTimer = setTimeout(() => {
        fin.textContent = "";
        trans.innerHTML = "";
      }, 6000);
    };

    es.onerror = () => {
      scheduleReconnect();
    };
  }

  function scheduleReconnect() {
    if (reconnectTimer) return;
    if (es) {
      try { es.close(); } catch (_) {}
      es = null;
    }
    retryCount++;
    const delay = Math.min(1000 * Math.pow(1.5, retryCount - 1), 5000);
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connectEventSource();
    }, delay);
  }

  // シーン復帰時（OBSでシーンがアクティブになった時）の即時ヘルスチェック
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      if (!es || es.readyState !== 1) { // 1 = OPEN
        connectEventSource();
      }
    }
  });

  // 初回スタイル取得 & SSE接続
  fetch("/overlay-style?_t=" + Date.now()).then(r => r.json()).then(applyStyle).catch(() => {});
  connectEventSource();
</script>
</body>
</html>




''';
