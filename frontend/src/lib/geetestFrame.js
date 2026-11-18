const CHANNEL = "makerhub-geetest";
const SDK_URL = "https://static.geetest.com/v4/gt4.js";
const PROOF_FIELDS = ["captcha_id", "lot_number", "pass_token", "gen_time", "captcha_output"];
const PROVIDER_DOMAINS = ["geetest.com", "geevisit.com", "gsensebot.com"];

function sdkConfig(challenge = {}) {
  if (!/^[A-Za-z0-9_-]{1,160}$/.test(challenge.challenge_id || "") || !/^[0-9a-fA-F]{32}$/.test(challenge.captcha_id || "")) {
    throw new Error("验证配置无效，请重新打开验证。");
  }
  const config = { captchaId: challenge.captcha_id, product: "bind", language: "zho", protocol: "https://" };
  for (const [field, name] of [["api_servers", "apiServers"], ["static_servers", "staticServers"]]) {
    const servers = challenge[field];
    if (servers === undefined) continue;
    if (!Array.isArray(servers) || !servers.length || servers.length > 5 || servers.some((value) => typeof value !== "string"
      || !/^[a-z0-9.-]+$/i.test(value) || !PROVIDER_DOMAINS.some((domain) => value === domain || value.endsWith(`.${domain}`)))) {
      throw new Error("验证配置无效，请重新打开验证。");
    }
    config[name] = servers;
  }
  return config;
}

function frameDocument(config, nonce) {
  const settings = JSON.stringify({ nonce, config }).replace(/</g, "\\u003c");
  const providerSources = PROVIDER_DOMAINS.map((domain) => `https://${domain} https://*.${domain}`).join(" ");
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval' ${providerSources}; style-src 'unsafe-inline' ${providerSources}; img-src data: blob: ${providerSources}; connect-src ${providerSources}; font-src data: ${providerSources}; frame-src ${providerSources}; worker-src blob:; base-uri 'none'; form-action 'none'">
<style>html,body{margin:0;width:100%;height:100%;overflow:hidden;background:transparent}body{font:13px system-ui,sans-serif}#captcha{width:100%;height:100%}</style></head>
<body><div id="captcha"></div><script>
(() => {
  const settings = ${settings};
  const send = (type, extra = {}) => parent.postMessage({ channel: "${CHANNEL}", nonce: settings.nonce, type, ...extra }, "*");
  const script = document.createElement("script");
  script.src = "${SDK_URL}";
  script.referrerPolicy = "no-referrer";
  script.onerror = () => send("error");
  script.onload = () => {
    try {
      // 使用 SDK 官方的整体缩放参数，使窄屏仍能完整展示题框。
      const rem = Math.min(1, Math.max(0.1, (window.innerWidth - 16) / 340));
      window.initGeetest4({ ...settings.config, rem, onError: () => send("error") }, (captcha) => {
        captcha.onReady(() => { send("ready"); captcha.showCaptcha(); });
        captcha.onSuccess(() => send("success", { proof: captcha.getValidate() }));
        captcha.onError(() => send("error"));
        captcha.onClose(() => send("close"));
      });
    } catch { send("error"); }
  };
  document.head.appendChild(script);
})();
</script></body></html>`;
}

function validationProof(value, captchaId) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const proof = {};
  for (const key of PROOF_FIELDS) {
    if (typeof value[key] !== "string" || !value[key] || value[key].length > 8192) return null;
    proof[key] = value[key];
  }
  return proof.captcha_id === captchaId && JSON.stringify(proof).length <= 32768 ? proof : null;
}

export function createGeetestFrame({ container, challenge, onReady = () => {}, onSuccess = () => {}, onError = () => {}, onClose = () => {},
  windowObject = window, documentObject = document, cryptoObject = globalThis.crypto, schedule = setTimeout, unschedule = clearTimeout, timeout = 30000 } = {}) {
  const config = sdkConfig(challenge);
  const bytes = cryptoObject.getRandomValues(new Uint8Array(24));
  const nonce = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  const frame = documentObject.createElement("iframe");
  frame.setAttribute("title", "MakerWorld 验证码");
  frame.setAttribute("sandbox", "allow-scripts");
  frame.setAttribute("referrerpolicy", "no-referrer");
  frame.setAttribute("class", "account-verification-dialog__frame");
  frame.srcdoc = frameDocument(config, nonce);
  let closed = false;
  let delivered = false;
  let ready = false;
  let timer = null;

  function clearTimer() {
    if (timer !== null) unschedule(timer);
    timer = null;
  }

  function destroy() {
    if (closed) return;
    closed = true;
    clearTimer();
    windowObject.removeEventListener("message", receive);
    frame.remove();
  }

  function fail(message = "验证码加载失败，请重试。") {
    destroy();
    onError(message);
  }

  function receive(event) {
    if (closed || delivered || event.source !== frame.contentWindow || event.origin !== "null") return;
    const data = event.data;
    if (!data || typeof data !== "object" || data.channel !== CHANNEL || data.nonce !== nonce) return;
    if (data.type === "ready") {
      clearTimer();
      if (!ready) { ready = true; onReady(); }
    } else if (data.type === "success") {
      const proof = validationProof(data.proof, challenge.captcha_id);
      if (!proof) { fail("验证结果无效，请重试。"); return; }
      delivered = true;
      clearTimer();
      onSuccess(proof);
    } else if (data.type === "error") fail();
    else if (data.type === "close") { destroy(); onClose(); }
  }

  windowObject.addEventListener("message", receive);
  timer = schedule(() => fail("验证码加载超时，请重试。"), timeout);
  try { container.appendChild(frame); } catch (error) { destroy(); throw error; }
  return { destroy };
}
