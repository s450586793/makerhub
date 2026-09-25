import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import { test } from "node:test";

import {
  attemptAutomaticVerification,
  buildDragTrajectory,
  detectVerificationChallenge,
  runVisionRequest,
  sanitizeVerificationResult,
} from "../../../app/services/cloakbrowser_verification.mjs";
import {
  coordinateThreeMfAuthorization,
  readAuthorizationResponse,
  threeMfDownloadActionScore,
} from "../../../app/services/cloakbrowser_bridge.mjs";


function fakeHandle({
  box = { x: 10, y: 20, width: 40, height: 40 },
  visible = true,
  value = "",
  screenshot = pngBuffer(40, 40),
  fingerprint = null,
  one = () => null,
  many = () => [],
  click = async () => {},
  backendNodeId = 1,
  contains = () => true,
  viewportOffset = { x: 0, y: 0 },
  selectionRegion = "selection-a",
  evaluate,
} = {}) {
  const handle = {
    selectionRegion,
    boundingBox: async () => box,
    backendNodeId: async () => backendNodeId,
    evaluate: evaluate || (async (_callback, argument, ...relatedHandles) => {
      if (argument === "read-value") return value;
      if (argument === "fingerprint") return fingerprint;
      if (argument === "contains") return contains(relatedHandles[0]);
      if (argument === "shared-selection-region") {
        const candidates = relatedHandles.slice(1);
        const region = candidates[0]?.selectionRegion;
        return Boolean(region) && candidates.every((candidate) => candidate.selectionRegion === region);
      }
      if (argument === "viewport-offset") return viewportOffset;
      if (argument?.action === "click") {
        if (Date.now() >= argument.deadline) return false;
        await click();
        return true;
      }
      if (argument?.action === "hide") {
        return { applied: true, id: argument.id, value: "", priority: "" };
      }
      if (argument?.action === "restore") return true;
      return typeof visible === "function" ? visible() : visible;
    }),
    screenshot: async () => (typeof screenshot === "function" ? screenshot() : screenshot),
    $: async (selector) => one(selector),
    $$: async (selector) => many(selector),
    click,
  };
  return handle;
}

function pngBuffer(width, height = 40, marker = 0) {
  const buffer = Buffer.alloc(32);
  Buffer.from("89504e470d0a1a0a", "hex").copy(buffer, 0);
  buffer.writeUInt32BE(13, 8);
  buffer.write("IHDR", 12, "ascii");
  buffer.writeUInt32BE(width, 16);
  buffer.writeUInt32BE(height, 20);
  buffer.writeUInt32BE(marker, 24);
  return buffer;
}

test("3MF download action scoring accepts localized and contextual primary actions", () => {
  assert.equal(threeMfDownloadActionScore({
    visible: true,
    disabled: false,
    text: "下载 3MF",
  }), 100);
  assert.equal(threeMfDownloadActionScore({
    visible: true,
    disabled: false,
    text: "下载",
    className: "primaryButton",
    contextText: "主配置 0.2mm 层高 下载 3MF",
  }), 80);
  assert.equal(threeMfDownloadActionScore({
    visible: true,
    disabled: false,
    ariaLabel: "Download printing profile",
  }), 100);
});

test("3MF download action scoring rejects disabled and unrelated download actions", () => {
  assert.equal(threeMfDownloadActionScore({
    visible: true,
    disabled: true,
    text: "Download 3MF",
  }), 0);
  assert.equal(threeMfDownloadActionScore({
    visible: true,
    disabled: false,
    text: "Download image",
    className: "asset-download",
    contextText: "Model gallery",
  }), 0);
  assert.equal(threeMfDownloadActionScore({
    visible: false,
    disabled: false,
    text: "下载 3MF",
  }), 0);
});

test("3MF download discovery ignores model links in descriptions and selects the actual action", async () => {
  const clicked = [];
  const candidates = [
    { text: "Download 3MF", className: "primaryButton", visible: false },
    { text: "Download", href: "https://makerworld.com/fr/models/2684142#profileId-2973114", target: "_blank" },
    { text: "Download 3MF", className: "primaryButton", inDescription: true },
    { text: "Download 3MF", className: "primaryButton" },
  ];
  const page = fakeAuthorizationPage([fakeAuthorizationResponse()]);
  page.mouse = { click: async (index) => { clicked.push(index); } };
  page.$$ = async () => candidates.map((candidate, index) => ({
    evaluate: async (_fn, operation) => operation === "download-click-point"
      ? { x: index, y: 30 } : { visible: true, disabled: false, ...candidate },
    dispose: async () => {},
  }));
  await coordinateThreeMfAuthorization(page, { authorizationTimeout: 1000 });
  assert.deepEqual(clicked, [3]);
});

test("3MF action scoring excludes navigation and non-3MF assets", () => {
  for (const candidate of [
    { text: "Download", href: "/en/models/2684142#profileId-1" },
    { text: "Download model image", className: "primaryButton" },
    { text: "Download STL", contextText: "Download 3MF", className: "primaryButton" },
    { text: "Download 3MF", inDescription: true },
    { text: "Download 3MF", href: "https://example.org/models/1", target: "_blank" },
  ]) assert.equal(threeMfDownloadActionScore({ visible: true, ...candidate }), 0);
});

test("3MF discovery rejects tag search links even when their labels describe model downloads", () => {
  for (const href of [
    "/zh/search/models?keyword=tag:download",
    "/en/collections/123",
    "https://example.test/3mf",
  ]) {
    assert.equal(threeMfDownloadActionScore({
      visible: true, text: "无 AMS 3D打印模型下载", href,
    }), 0);
  }
});

test("3MF download avoids Puppeteer isolated-world handle clicks", async () => {
  const page = fakeAuthorizationPage([fakeAuthorizationResponse()]);
  const clicks = [];
  let disposed = false;
  page.$$ = async (_selector, options) => {
    if (options?.isolate !== false) {
      throw new Error("Protocol error (Runtime.callFunctionOn): Argument should belong to the same JavaScript world as target object");
    }
    return [{
      evaluate: async (_fn, operation) => operation === "download-click-point"
        ? { x: 40, y: 30 }
        : { visible: true, text: "Download 3MF", className: "primaryButton" },
      click: async () => {
        throw new Error("Protocol error (Runtime.callFunctionOn): Argument should belong to the same JavaScript world as target object");
      },
      dispose: async () => { disposed = true; },
    }];
  };
  page.mouse = { click: async (...args) => clicks.push(args) };
  const result = await coordinateThreeMfAuthorization(page, { authorizationTimeout: 1000 });
  assert.equal(result.payload.name, "part.3mf");
  assert.deepEqual(clicks, [[40, 30, { delay: 20 }]]);
  assert.equal(disposed, true);
});

test("3MF coordinator reacquires a stale download control before any click is dispatched", async () => {
  const page = fakeAuthorizationPage([fakeAuthorizationResponse()]);
  let lookups = 0;
  let clicks = 0;
  let disposed = 0;
  const result = await coordinateThreeMfAuthorization(page, {
    authorizationTimeout: 1000,
    findButton: async () => {
      const attempt = ++lookups;
      return {
        click: async () => {
          if (attempt === 1) throw new Error("Execution context was destroyed, most likely because of a navigation.");
          clicks++;
        },
        dispose: async () => { disposed++; },
      };
    },
  });
  assert.equal(result.payload.name, "part.3mf");
  assert.equal(lookups, 2);
  assert.equal(clicks, 1);
  assert.equal(disposed, 2);
});

test("3MF coordinator retries a cross-world query failure but bounds repeated failures", async () => {
  const page = fakeAuthorizationPage([fakeAuthorizationResponse()]);
  let lookups = 0;
  await assert.rejects(coordinateThreeMfAuthorization(page, {
    authorizationTimeout: 1000,
    findButton: async () => {
      lookups++;
      throw new Error("Protocol error (Runtime.callFunctionOn): Argument should belong to the same JavaScript world as target object");
    },
  }), /same JavaScript world/);
  assert.equal(lookups, 3);
});

test("3MF coordinator never repeats an uncertain mouse dispatch", async () => {
  const page = fakeAuthorizationPage([fakeAuthorizationResponse()]);
  let clicks = 0;
  await assert.rejects(coordinateThreeMfAuthorization(page, {
    findButton: async () => ({
      click: async () => {
        clicks++;
        throw new Error("Protocol error (Input.dispatchMouseEvent): Target closed");
      },
      dispose: async () => {},
    }),
  }), /Target closed/);
  assert.equal(clicks, 1);
});

test("3MF coordinator does not click after its response listener has already timed out", async () => {
  const page = fakeAuthorizationPage([]);
  let clicks = 0;
  let disposed = 0;
  await assert.rejects(coordinateThreeMfAuthorization(page, {
    authorizationTimeout: 5,
    findButton: async () => {
      await new Promise(resolve => setTimeout(resolve, 20));
      return { click: async () => { clicks++; }, dispose: async () => { disposed++; } };
    },
  }), /authorization response timed out/);
  assert.equal(clicks, 0);
  assert.equal(disposed, 1);
});

test("crowdfunding project entry skips authorization without clicking", async () => {
  const lifecycle = [];
  const page = fakeAuthorizationPage([], lifecycle);
  page.url = () => "https://makerworld.com/en/models/2888275";
  page.$$ = async () => [{
    evaluate: async () => ({ visible: true, disabled: false, text: "View the project", href: "/en/crowdfunding/272-demo" }),
    click: async () => { assert.fail("crowdfunding must not be clicked"); },
    dispose: async () => {},
  }];
  const result = await coordinateThreeMfAuthorization(page, { authorizationTimeout: 1000 });
  assert.equal(result.payload.code, "MAKERHUB_CROWDFUNDING");
  assert.equal(result.payload.url, undefined);
});

test("a project recommendation does not suppress an available 3MF action", async () => {
  const clicked = [];
  const page = fakeAuthorizationPage([fakeAuthorizationResponse()]);
  page.url = () => "https://makerworld.com/en/models/1";
  page.mouse = { click: async (index) => { clicked.push(index); } };
  page.$$ = async () => [
    { text: "View the project", href: "/en/crowdfunding/272-demo" },
    { text: "Download 3MF", className: "primaryButton" },
  ].map((candidate, index) => ({
    evaluate: async (_fn, operation) => operation === "download-click-point"
      ? { x: index, y: 30 } : { visible: true, ...candidate },
    dispose: async () => {},
  }));
  const result = await coordinateThreeMfAuthorization(page, { authorizationTimeout: 1000 });
  assert.equal(result.payload.name, "part.3mf");
  assert.deepEqual(clicked, [1]);
});

function fakeFrame({ one = () => null, many = () => [], label = "frame" } = {}) {
  return {
    label,
    $: async (selector) => one(selector),
    $$: async (selector) => many(selector),
  };
}

function fakeClickDiscoveryPage({
  containerBox = { x: 0, y: 0, width: 220, height: 220 },
  targetBox = { x: 80, y: 12, width: 40, height: 40 },
  candidateBoxes = [
    { x: 30, y: 80, width: 40, height: 40 },
    { x: 100, y: 80, width: 40, height: 40 },
    { x: 30, y: 140, width: 40, height: 40 },
    { x: 100, y: 140, width: 40, height: 40 },
  ],
} = {}) {
  const target = fakeHandle({ box: targetBox });
  const candidates = candidateBoxes.map((box) => fakeHandle({ box }));
  const container = fakeHandle({
    box: containerBox,
    one: (selector) => (/ques|tip|target/i.test(selector) ? target : null),
    many: (selector) => (/item|icon|candidate/i.test(selector) ? candidates : []),
  });
  const frame = fakeFrame({ one: (selector) => (selector.includes("geetest") ? container : null) });
  return {
    page: { mainFrame: () => frame, frames: () => [frame] },
    target,
    candidates,
  };
}

function coordinateDiscoveryContainer({ targets, background, confirm, sliderHandle }) {
  const sliderPiece = fakeHandle({ box: { x: 60, y: 90, width: 36, height: 36 } });
  return fakeHandle({
    box: { x: 0, y: 0, width: 340, height: 330 },
    one: (selector) => {
      if (/ques_tips|ques_back|question/.test(selector)) return targets;
      if (/commit_tip|submit/.test(selector)) return confirm;
      if (/geetest_bg|canvas_bg|geetest_window/.test(selector)) return background;
      if (/slider_button|geetest_btn|slider.*handle/.test(selector)) return sliderHandle;
      if (/geetest_slice|canvas_slice|slider_piece/.test(selector)) return sliderPiece;
      return null;
    },
  });
}

function fakeChild(onInput) {
  const child = new EventEmitter();
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  child.stdin = new PassThrough();
  const input = [];
  child.stdin.on("data", (chunk) => input.push(chunk));
  child.stdin.on("finish", () => onInput(child, Buffer.concat(input).toString("utf8")));
  child.killed = false;
  child.kill = () => {
    child.killed = true;
    queueMicrotask(() => child.emit("close", null, "SIGKILL"));
    return true;
  };
  return child;
}

function fakeAuthorizationResponse({
  status = 200,
  payload = { name: "part.3mf", url: "https://download.example.test/part.3mf" },
  rawText,
  textError,
  url = "https://api.bambulab.cn/v1/design-service/instance/123/f3mf",
} = {}) {
  return {
    status: () => status,
    text: async () => {
      if (textError) throw textError;
      return rawText ?? JSON.stringify(payload);
    },
    url: () => url,
  };
}

function fakeAuthorizationPage(responses, lifecycle = []) {
  const queue = [...responses];
  const forbidden = (name) => async () => {
    throw new Error(`${name} must not be called`);
  };
  return {
    fetch: forbidden("fetch"),
    goto: forbidden("goto"),
    reload: forbidden("reload"),
    evaluate: async () => false,
    waitForResponse: (matcher, options = {}) => {
      lifecycle.push("waiter");
      const response = queue.shift();
      assert.equal(typeof matcher, "function");
      if (!response) {
        return new Promise((resolve, reject) => {
          const timer = setTimeout(() => reject(new Error("response timeout")), 5);
          options.signal?.addEventListener("abort", () => {
            clearTimeout(timer);
            reject(options.signal.reason);
          }, { once: true });
        });
      }
      assert.equal(matcher(response), true);
      return Promise.resolve(response);
    },
  };
}

async function runAuthorizationCoordinator({
  first = fakeAuthorizationResponse(),
  second,
  inputAutoVerify = true,
  lifecycle = [],
  verificationAdapter = async () => ({
    attempted: true,
    completed: true,
    provider: "geetest4",
    challenge_type: "slider",
    attempts: 1,
    reason: "completed",
  }),
} = {}) {
  let clicks = 0;
  let disposals = 0;
  const page = fakeAuthorizationPage([first, second].filter(Boolean), lifecycle);
  const result = await coordinateThreeMfAuthorization(page, {
    instanceId: "123",
    authorizationTimeout: 100,
    inputAutoVerify,
    findButton: async () => {
      lifecycle.push("find-button");
      return {
        click: async () => {
          clicks += 1;
          lifecycle.push("click");
        },
        dispose: async () => {
          disposals += 1;
          lifecycle.push("dispose");
        },
      };
    },
    verificationAdapter: async (...args) => {
      lifecycle.push("verification-input");
      return verificationAdapter(...args);
    },
  });
  return { result, clicks, disposals };
}

test("3MF coordinator returns a signed HTTP 200 response without verification", async () => {
  let verificationCalls = 0;
  const { result, clicks, disposals } = await runAuthorizationCoordinator({
    verificationAdapter: async () => {
      verificationCalls += 1;
      return { completed: true };
    },
  });

  assert.equal(result.status_code, 200);
  assert.equal(result.payload.url, "https://download.example.test/part.3mf");
  assert.equal(verificationCalls, 0);
  assert.equal(clicks, 1);
  assert.equal(disposals, 1);
});

test("3MF coordinator installs the second waiter before verifying an HTTP 418 response", async () => {
  const lifecycle = [];
  const first = fakeAuthorizationResponse({
    status: 418,
    payload: { captchaId: "captcha-123", message: "verify" },
  });
  const second = fakeAuthorizationResponse({
    payload: { name: "verified.3mf", url: "https://download.example.test/verified.3mf" },
  });

  const { result, clicks } = await runAuthorizationCoordinator({ first, second, lifecycle });

  assert.equal(clicks, 1);
  assert.deepEqual(lifecycle, [
    "waiter",
    "find-button",
    "click",
    "dispose",
    "waiter",
    "verification-input",
  ]);
  assert.equal(result.status_code, 200);
  assert.equal(result.payload.url, "https://download.example.test/verified.3mf");
  assert.equal(result.verification.completed, true);
});

test("3MF coordinator arms the second waiter before parsing the first response body", async () => {
  const lifecycle = [];
  const waiters = [];
  const second = fakeAuthorizationResponse({
    payload: { name: "verified.3mf", url: "https://download.example.test/verified.3mf" },
  });
  const first = fakeAuthorizationResponse({
    status: 418,
    payload: { captchaId: "captcha-123" },
  });
  first.text = async () => {
    lifecycle.push("first-body-start");
    assert.equal(waiters.length, 2);
    waiters[1].resolve(second);
    await new Promise((resolve) => setImmediate(resolve));
    lifecycle.push("first-body-end");
    return JSON.stringify({ captchaId: "captcha-123" });
  };
  const page = {
    waitForResponse: (matcher, options = {}) => new Promise((resolve, reject) => {
      const waiter = {
        resolve: (response) => {
          assert.equal(matcher(response), true);
          resolve(response);
        },
      };
      waiters.push(waiter);
      lifecycle.push(`waiter-${waiters.length}`);
      options.signal?.addEventListener("abort", () => reject(options.signal.reason), { once: true });
    }),
  };

  const result = await coordinateThreeMfAuthorization(page, {
    instanceId: "123",
    inputAutoVerify: true,
    findButton: async () => ({
      click: async () => waiters[0].resolve(first),
      dispose: async () => {},
    }),
    verificationAdapter: async () => ({
      attempted: true,
      completed: true,
      provider: "geetest4",
      challenge_type: "slider",
      attempts: 1,
      reason: "completed",
    }),
  });

  assert.ok(lifecycle.indexOf("waiter-2") < lifecycle.indexOf("first-body-start"));
  assert.equal(result.payload.url, "https://download.example.test/verified.3mf");
});

test("3MF coordinator consumes a passive page success and cancels verification", async () => {
  const waiters = [];
  const first = fakeAuthorizationResponse({
    status: 418,
    payload: { captchaId: "captcha-123" },
  });
  const second = fakeAuthorizationResponse({
    payload: { name: "verified.3mf", url: "https://download.example.test/passive.3mf" },
  });
  let clicks = 0;
  let adapterAborted = false;
  const page = {
    waitForResponse: (matcher, options = {}) => new Promise((resolve, reject) => {
      waiters.push({
        resolve: (response) => {
          assert.equal(matcher(response), true);
          resolve(response);
        },
      });
      options.signal?.addEventListener("abort", () => reject(options.signal.reason), { once: true });
    }),
  };

  const result = await coordinateThreeMfAuthorization(page, {
    instanceId: "123",
    inputAutoVerify: true,
    findButton: async () => ({
      click: async () => {
        clicks += 1;
        waiters[0].resolve(first);
      },
      dispose: async () => {},
    }),
    verificationAdapter: async (_page, { signal }) => new Promise((resolve) => {
      signal.addEventListener("abort", () => {
        adapterAborted = true;
        resolve({
          attempted: false,
          completed: false,
          provider: "turnstile",
          challenge_type: "checkbox",
          attempts: 0,
          reason: "aborted",
        });
      }, { once: true });
      waiters[1].resolve(second);
    }),
  });

  assert.equal(clicks, 1);
  assert.equal(adapterAborted, true);
  assert.equal(result.status_code, 200);
  assert.equal(result.payload.url, "https://download.example.test/passive.3mf");
  assert.deepEqual(result.verification, {
    attempted: false,
    completed: true,
    provider: "turnstile",
    challenge_type: "checkbox",
    attempts: 0,
    reason: "completed",
  });
});

test("3MF coordinator aborts and settles the speculative waiter for a non-verification response", async () => {
  const first = fakeAuthorizationResponse();
  let waiterCalls = 0;
  let speculativeAborted = false;
  let speculativeSettled = false;
  const page = {
    waitForResponse: (_matcher, options = {}) => {
      waiterCalls += 1;
      if (waiterCalls === 1) return Promise.resolve(first);
      return new Promise((resolve, reject) => {
        options.signal?.addEventListener("abort", () => {
          speculativeAborted = true;
          reject(options.signal.reason);
        }, { once: true });
      }).finally(() => {
        speculativeSettled = true;
      });
    },
  };

  const result = await coordinateThreeMfAuthorization(page, {
    instanceId: "123",
    inputAutoVerify: true,
    findButton: async () => ({ click: async () => {}, dispose: async () => {} }),
  });

  assert.equal(result.status_code, 200);
  assert.equal(waiterCalls, 2);
  assert.equal(speculativeAborted, true);
  assert.equal(speculativeSettled, true);
});

test("3MF coordinator verifies a captcha payload even when its status is not 418", async () => {
  let verificationCalls = 0;
  const first = fakeAuthorizationResponse({
    status: 400,
    payload: { captchaId: "captcha-from-payload" },
  });
  const second = fakeAuthorizationResponse();

  await runAuthorizationCoordinator({
    first,
    second,
    verificationAdapter: async () => {
      verificationCalls += 1;
      return { attempted: true, completed: true, provider: "geetest4" };
    },
  });

  assert.equal(verificationCalls, 1);
});

test("3MF coordinator settles an unused waiter and sanitizes adapter failure", async () => {
  const first = fakeAuthorizationResponse({ status: 418, payload: { captchaId: "captcha-123" } });
  const { result, clicks } = await runAuthorizationCoordinator({
    first,
    verificationAdapter: async () => {
      throw new Error("secret-cookie-and-token");
    },
  });

  assert.equal(clicks, 1);
  assert.equal(result.status_code, 418);
  assert.equal(result.payload.captchaId, "captcha-123");
  assert.deepEqual(result.verification, {
    attempted: true,
    completed: false,
    provider: "unknown",
    challenge_type: "unknown",
    attempts: 0,
    reason: "verification_failed",
  });
});

test("3MF coordinator returns the original response when verification times out", async () => {
  const first = fakeAuthorizationResponse({ status: 418, payload: { captchaId: "captcha-123" } });
  const { result } = await runAuthorizationCoordinator({
    first,
    verificationAdapter: async () => ({
      attempted: true,
      completed: false,
      provider: "geetest4",
      challenge_type: "slider",
      attempts: 1,
      reason: "timeout",
      confidence: 0.4,
      token: "must-not-leak",
    }),
  });

  assert.equal(result.status_code, 418);
  assert.equal(result.payload.captchaId, "captcha-123");
  assert.deepEqual(result.verification, {
    attempted: true,
    completed: false,
    provider: "geetest4",
    challenge_type: "slider",
    attempts: 1,
    reason: "timeout",
    confidence: 0.4,
  });
});

test("3MF coordinator settles the first waiter when button lookup outlives its timeout", async () => {
  let waiterSignal;
  let waiterSettled = false;
  const page = {
    waitForResponse: (_matcher, options = {}) => {
      waiterSignal = options.signal;
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          waiterSettled = true;
          reject(new Error("first response timeout"));
        }, 5);
        options.signal?.addEventListener("abort", () => {
          clearTimeout(timer);
          waiterSettled = true;
          reject(options.signal.reason);
        }, { once: true });
      });
    },
  };

  await assert.rejects(
    coordinateThreeMfAuthorization(page, {
      instanceId: "123",
      authorizationTimeout: 5,
      findButton: async () => {
        await new Promise((resolve) => setTimeout(resolve, 10));
        throw new Error("button lookup failed");
      },
    }),
    /button lookup failed/,
  );
  assert.equal(waiterSignal?.aborted, true);
  assert.equal(waiterSettled, true);
});

test("3MF coordinator preserves a click failure when dispose also fails", async () => {
  const first = fakeAuthorizationResponse();
  const page = fakeAuthorizationPage([first]);
  let clicks = 0;
  let disposals = 0;

  await assert.rejects(
    coordinateThreeMfAuthorization(page, {
      instanceId: "123",
      findButton: async () => ({
        click: async () => {
          clicks += 1;
          throw new Error("click failed");
        },
        dispose: async () => {
          disposals += 1;
          throw new Error("dispose failed");
        },
      }),
    }),
    /click failed/,
  );
  assert.equal(clicks, 1);
  assert.equal(disposals, 1);
});

test("3MF coordinator ignores dispose failure after a successful click", async () => {
  const first = fakeAuthorizationResponse();
  const page = fakeAuthorizationPage([first]);

  const result = await coordinateThreeMfAuthorization(page, {
    instanceId: "123",
    findButton: async () => ({
      click: async () => {},
      dispose: async () => {
        throw new Error("dispose failed with secret-token");
      },
    }),
  });

  assert.equal(result.status_code, 200);
  assert.equal(result.payload.url, "https://download.example.test/part.3mf");
});

test("3MF coordinator returns the first response when a completed verification has no second response", async () => {
  const first = fakeAuthorizationResponse({ status: 418, payload: { captchaId: "captcha-123" } });

  const { result } = await runAuthorizationCoordinator({ first });

  assert.equal(result.status_code, 418);
  assert.equal(result.payload.captchaId, "captcha-123");
  assert.equal(result.verification.completed, true);
});

test("authorization response parsing bounds invalid JSON", async () => {
  const result = await readAuthorizationResponse(fakeAuthorizationResponse({
    status: 502,
    rawText: `<html>${"x".repeat(2000)}`,
  }));

  assert.equal(result.status_code, 502);
  assert.equal(result.text.length, 1024);
  assert.equal(result.payload.message.length, 1024);
  assert.equal(result.payload.code, "");
  assert.equal(result.payload.captchaId, "");
});

test("authorization response parsing does not expose text rejection details", async () => {
  await assert.rejects(
    readAuthorizationResponse(fakeAuthorizationResponse({
      textError: new Error("secret-cookie-from-response"),
    })),
    (error) => error.message === "authorization response body unavailable",
  );
});

test("authorization response forwards only Retry-After for download pacing", async () => {
  const response = fakeAuthorizationResponse({ status: 429, payload: { message: "Too Many Requests" } });
  response.headers = () => ({ "retry-after": "120", "set-cookie": "secret-cookie", authorization: "secret-token" });
  const result = await readAuthorizationResponse(response);
  assert.deepEqual(result.headers, { "retry-after": "120" });
  assert.equal(JSON.stringify(result).includes("secret-"), false);
});

test("authorization header failure preserves an already granted download", async () => {
  const response = fakeAuthorizationResponse();
  response.headers = () => { throw new Error("header metadata unavailable"); };
  const result = await readAuthorizationResponse(response);
  assert.equal(result.payload.url, "https://download.example.test/part.3mf");
});

test("3MF coordinator falls back when the second response cannot be parsed", async () => {
  const first = fakeAuthorizationResponse({ status: 418, payload: { captchaId: "captcha-123" } });
  const second = fakeAuthorizationResponse({
    textError: new Error("second-response-secret"),
  });

  const { result } = await runAuthorizationCoordinator({ first, second });

  assert.equal(result.status_code, 418);
  assert.equal(result.payload.captchaId, "captcha-123");
  assert.equal(result.verification.completed, true);
});

function fakeInputPage({
  dispatch = async () => {},
  protocol,
  detach = async () => {},
} = {}) {
  let style = null;
  let nextSessionId = 0;
  const sendProtocol = protocol || (async (method, params) => {
    if (method === "DOM.getDocument") return { root: { nodeId: 1 } };
    if (method === "DOM.pushNodesByBackendIdsToFrontend") return { nodeIds: [1] };
    if (method === "DOM.getAttributes") {
      return { attributes: style === null ? [] : ["style", style] };
    }
    if (method === "DOM.setAttributeValue") {
      style = params.value;
      return {};
    }
    if (method === "DOM.removeAttribute") {
      style = null;
      return {};
    }
    throw new Error(`unexpected protocol method: ${method}`);
  });
  return {
    mouse: {
      move: async (x, y) => dispatch({ type: "mouseMoved", x, y }),
      down: async () => dispatch({ type: "mousePressed" }),
      up: async () => dispatch({ type: "mouseReleased" }),
    },
    createCDPSession: async () => {
      const sessionId = nextSessionId;
      nextSessionId += 1;
      return {
        send: async (method, params, options) => {
          if (method === "Input.dispatchMouseEvent") return dispatch(params, options);
          return sendProtocol(method, params, options);
        },
        detach: () => detach(sessionId),
      };
    },
  };
}

function clickChallenge(click, id = "challenge-a") {
  return {
    provider: "geetest4",
    challenge_type: "icon_click",
    container: fakeHandle({ box: { x: 0, y: 0, width: 220, height: 220 } }),
    target: fakeHandle({
      box: { x: 80, y: 12, width: 40, height: 40 },
      fingerprint: `${id}:target`,
    }),
    candidates: [
      fakeHandle({ box: { x: 30, y: 80, width: 40, height: 40 }, fingerprint: `${id}:candidate-0` }),
      fakeHandle({ box: { x: 100, y: 80, width: 40, height: 40 }, click, fingerprint: `${id}:candidate-1` }),
      fakeHandle({ box: { x: 30, y: 140, width: 40, height: 40 }, fingerprint: `${id}:candidate-2` }),
      fakeHandle({ box: { x: 100, y: 140, width: 40, height: 40 }, fingerprint: `${id}:candidate-3` }),
    ],
  };
}

function coordinateChallenge({
  container = fakeHandle({ box: { x: 0, y: 0, width: 340, height: 330 } }),
  targets = fakeHandle({ box: { x: 70, y: 18, width: 150, height: 42 } }),
  background = fakeHandle({ box: { x: 20, y: 72, width: 300, height: 200 } }),
  confirm = fakeHandle({ box: { x: 136, y: 282, width: 68, height: 30 } }),
} = {}) {
  return {
    provider: "geetest4",
    challenge_type: "coordinate_click",
    container,
    targets,
    background,
    confirm,
  };
}

function pressedCenters(events) {
  return events
    .filter(({ type }) => type === "mousePressed")
    .map(({ x, y }) => [x, y]);
}

test("drag trajectory reaches the target with bounded overshoot", () => {
  const points = buildDragTrajectory(180, () => 0.5);

  assert.ok(points.length >= 12);
  assert.equal(points.at(-1).x, 180);
  assert.ok(Math.max(...points.map((point) => point.x)) <= 186);
  assert.ok(points.every((point) => Math.abs(point.y) <= 3));
  assert.ok(points.slice(0, -3).every((point, index, values) => (
    index === 0 || point.x >= values[index - 1].x
  )));
});

test("verification diagnostics keep only canonical bounded fields", () => {
  assert.deepEqual(
    sanitizeVerificationResult({
      attempted: true,
      completed: false,
      provider: "geetest4",
      challenge_type: "slider",
      attempts: 9,
      reason: "confidence_too_low",
      confidence: 0.61,
      token: "secret",
      screenshot: "secret-image",
    }),
    {
      attempted: true,
      completed: false,
      provider: "geetest4",
      challenge_type: "slider",
      attempts: 2,
      reason: "confidence_too_low",
      confidence: 0.61,
    },
  );
  assert.equal(sanitizeVerificationResult({ reason: "token=secret" }).reason, "unknown");
});

test("verification diagnostics preserve coordinate-click safe reasons", () => {
  for (const reason of [
    "challenge_changed",
    "confirmation_unavailable",
    "coordinate_ambiguous",
    "coordinate_invalid",
    "coordinate_not_found",
    "image_depth_invalid",
    "target_count_invalid",
    "target_segmentation_failed",
  ]) {
    const result = sanitizeVerificationResult({
      provider: "geetest4",
      challenge_type: "coordinate_click",
      reason,
    });

    assert.equal(result.challenge_type, "coordinate_click");
    assert.equal(result.reason, reason);
  }
});

test("vision request passes only the strict mode payload to Python", async () => {
  let spawnCall;
  let inputPayload;
  const spawnFn = (...args) => {
    spawnCall = args;
    return fakeChild((child, input) => {
      inputPayload = JSON.parse(input);
      child.stdout.end('{"ok":true,"candidate_index":1,"confidence":0.9}');
      child.emit("close", 0, null);
    });
  };

  const result = await runVisionRequest({
    mode: "click",
    target_png: "target",
    candidate_pngs: ["left", "right"],
    url: "https://example.com/private",
    cookie: "secret-cookie",
    token: "secret-token",
    browser_credentials: "secret-browser",
  }, { spawnFn });

  assert.equal(spawnCall[0], process.env.PYTHON || "python");
  assert.deepEqual(spawnCall[1], ["-m", "app.services.makerworld_captcha_vision"]);
  assert.deepEqual(spawnCall[2], { stdio: ["pipe", "pipe", "pipe"] });
  assert.deepEqual(inputPayload, {
    mode: "click",
    target_png: "target",
    candidate_pngs: ["left", "right"],
  });
  assert.equal(result.candidate_index, 1);
});

test("slider vision request forwards only the exact composited-piece geometry", async () => {
  let inputPayload;
  const geometry = {
    image_width: 600,
    image_height: 240,
    track_width: 300,
    track_height: 120,
    handle_width: 40,
    piece_offset_x: 30,
    piece_offset_y: 10,
  };

  await runVisionRequest({
    mode: "slider",
    background_png: "background",
    piece_png: "piece",
    geometry: { ...geometry, token: "secret-token" },
    cookie: "secret-cookie",
  }, {
    spawnFn: () => fakeChild((child, input) => {
      inputPayload = JSON.parse(input);
      child.stdout.end('{"ok":false,"reason":"gap_not_found"}');
      child.emit("close", 0, null);
    }),
  });

  assert.deepEqual(inputPayload, {
    mode: "slider",
    background_png: "background",
    piece_png: "piece",
    geometry,
  });
  assert.doesNotMatch(JSON.stringify(inputPayload), /secret/);
});

test("coordinate-click vision request forwards only the two PNG inputs", async () => {
  let inputPayload;

  await runVisionRequest({
    mode: "coordinate_click",
    targets_png: "targets",
    background_png: "background",
    geometry: { width: 300, height: 200 },
    url: "https://example.com/private",
    cookie: "secret-cookie",
    token: "secret-token",
  }, {
    spawnFn: () => fakeChild((child, input) => {
      inputPayload = JSON.parse(input);
      child.stdout.end('{"ok":false,"reason":"coordinate_not_found"}');
      child.emit("close", 0, null);
    }),
  });

  assert.deepEqual(inputPayload, {
    mode: "coordinate_click",
    targets_png: "targets",
    background_png: "background",
  });
  assert.doesNotMatch(JSON.stringify(inputPayload), /secret|url|geometry/);
});

test("vision request kills a timed out child", async () => {
  let child;
  const spawnFn = () => {
    child = fakeChild(() => {});
    return child;
  };

  await assert.rejects(
    runVisionRequest({ mode: "click", target_png: "a", candidate_pngs: ["b", "c"] }, {
      spawnFn,
      timeoutMs: 5,
    }),
    /timed out/,
  );
  assert.equal(child.killed, true);
});

test("vision request caps stdout and stderr independently", async () => {
  let stdoutOverflowChild;
  await assert.rejects(
    runVisionRequest({ mode: "click", target_png: "a", candidate_pngs: ["b", "c"] }, {
      spawnFn: () => {
        stdoutOverflowChild = fakeChild((child) => child.stdout.write(Buffer.alloc(64 * 1024 + 1)));
        return stdoutOverflowChild;
      },
    }),
    /output limit/,
  );
  assert.equal(stdoutOverflowChild.killed, true);

  let stderrOverflowChild;
  await assert.rejects(
    runVisionRequest({ mode: "click", target_png: "a", candidate_pngs: ["b", "c"] }, {
      spawnFn: () => {
        stderrOverflowChild = fakeChild((child) => child.stderr.write(Buffer.alloc(64 * 1024 + 1)));
        return stderrOverflowChild;
      },
    }),
    /output limit/,
  );
  assert.equal(stderrOverflowChild.killed, true);

  const result = await runVisionRequest(
    { mode: "click", target_png: "a", candidate_pngs: ["b", "c"] },
    {
      spawnFn: () => fakeChild((child) => {
        child.stderr.write(Buffer.alloc(64 * 1024));
        child.stdout.end('{"ok":true}');
        child.emit("close", 0, null);
      }),
    },
  );
  assert.equal(result.ok, true);
});

test("vision request does not expose stderr", async () => {

  await assert.rejects(
    runVisionRequest({ mode: "click", target_png: "a", candidate_pngs: ["b", "c"] }, {
      spawnFn: () => fakeChild((child) => {
        child.stderr.end("secret-cookie-from-python");
        child.emit("close", 1, null);
      }),
    }),
    (error) => error.message === "vision process failed",
  );
});

test("vision request rejects non-JSON output", async () => {
  await assert.rejects(
    runVisionRequest({ mode: "click", target_png: "a", candidate_pngs: ["b", "c"] }, {
      spawnFn: () => fakeChild((child) => {
        child.stdout.end("not json");
        child.emit("close", 0, null);
      }),
    }),
    /invalid JSON/,
  );
});

test("challenge discovery checks main frame first, deduplicates frames, and finds GeeTest clicks", async () => {
  const visited = [];
  const seen = new Set();
  const hiddenContainer = fakeHandle({ box: { x: 0, y: 0, width: 0, height: 0 } });
  const target = fakeHandle({ box: { x: 80, y: 12, width: 40, height: 40 } });
  const candidates = [
    fakeHandle({ box: { x: 30, y: 80, width: 40, height: 40 } }),
    fakeHandle({ box: { x: 100, y: 80, width: 40, height: 40 } }),
    fakeHandle({ box: { x: 30, y: 140, width: 40, height: 40 } }),
    fakeHandle({ box: { x: 100, y: 140, width: 40, height: 40 } }),
  ];
  const visibleContainer = fakeHandle({
    box: { x: 0, y: 0, width: 220, height: 220 },
    one: (selector) => (/ques|tip|target/i.test(selector) ? target : null),
    many: (selector) => (/item|icon|candidate/i.test(selector) ? candidates : []),
  });
  const main = fakeFrame({
    label: "main",
    one: (selector) => {
      if (!seen.has("main")) visited.push("main");
      seen.add("main");
      return selector.includes("geetest") ? hiddenContainer : null;
    },
  });
  const child = fakeFrame({
    label: "child",
    one: (selector) => {
      if (!seen.has("child")) visited.push("child");
      seen.add("child");
      return selector.includes("geetest") ? visibleContainer : null;
    },
  });
  const page = { mainFrame: () => main, frames: () => [main, child, main] };

  const challenge = await detectVerificationChallenge(page);

  assert.equal(challenge.provider, "geetest4");
  assert.equal(challenge.challenge_type, "icon_click");
  assert.equal(challenge.frame, child);
  assert.equal(challenge.target, target);
  assert.deepEqual(challenge.candidates, candidates);
  assert.deepEqual(visited, ["main", "child"]);
});

test("coordinate-click discovery wins over slider-like generated classes", async () => {
  const targets = fakeHandle({ box: { x: 70, y: 18, width: 150, height: 42 } });
  const background = fakeHandle({ box: { x: 20, y: 72, width: 300, height: 200 } });
  const confirm = fakeHandle({ box: { x: 136, y: 282, width: 68, height: 30 } });
  const sliderHandle = fakeHandle({ box: { x: 20, y: 282, width: 40, height: 30 } });
  const container = coordinateDiscoveryContainer({ targets, background, confirm, sliderHandle });
  const frame = fakeFrame({ one: (selector) => (selector.includes("geetest") ? container : null) });

  const challenge = await detectVerificationChallenge({ mainFrame: () => frame, frames: () => [frame] });

  assert.equal(challenge.challenge_type, "coordinate_click");
  assert.equal(challenge.targets, targets);
  assert.equal(challenge.background, background);
  assert.equal(challenge.confirm, confirm);
});

test("click discovery rejects candidates that do not form a coherent grid", async () => {
  const { page } = fakeClickDiscoveryPage({
    candidateBoxes: [
      { x: 30, y: 80, width: 40, height: 40 },
      { x: 100, y: 80, width: 40, height: 40 },
      { x: 30, y: 140, width: 40, height: 40 },
      { x: 175, y: 140, width: 40, height: 40 },
    ],
  });

  assert.equal(await detectVerificationChallenge(page), null);
});

test("click discovery rejects uneven three-row candidate grids", async () => {
  for (const candidateBoxes of [
    [
      { x: 65, y: 80, width: 40, height: 40 },
      { x: 30, y: 140, width: 40, height: 40 },
      { x: 100, y: 140, width: 40, height: 40 },
      { x: 65, y: 200, width: 40, height: 40 },
    ],
    [
      { x: 30, y: 80, width: 40, height: 40 },
      { x: 100, y: 80, width: 40, height: 40 },
      { x: 65, y: 140, width: 40, height: 40 },
      { x: 30, y: 200, width: 40, height: 40 },
      { x: 100, y: 200, width: 40, height: 40 },
    ],
  ]) {
    const { page } = fakeClickDiscoveryPage({
      containerBox: { x: 0, y: 0, width: 220, height: 270 },
      candidateBoxes,
    });

    assert.equal(await detectVerificationChallenge(page), null);
  }
});

test("click discovery rejects an uneven two-row grid whose columns do not align", async () => {
  const { page } = fakeClickDiscoveryPage({
    containerBox: { x: 0, y: 0, width: 240, height: 220 },
    candidateBoxes: [
      { x: 30, y: 80, width: 40, height: 40 },
      { x: 100, y: 80, width: 40, height: 40 },
      { x: 170, y: 80, width: 40, height: 40 },
      { x: 65, y: 140, width: 40, height: 40 },
      { x: 135, y: 140, width: 40, height: 40 },
    ],
  });

  assert.equal(await detectVerificationChallenge(page), null);
});

test("click discovery rejects aligned but non-rectangular two-row grids", async () => {
  for (const candidateBoxes of [
    [
      { x: 30, y: 80, width: 40, height: 40 },
      { x: 100, y: 80, width: 40, height: 40 },
      { x: 170, y: 80, width: 40, height: 40 },
      { x: 30, y: 140, width: 40, height: 40 },
      { x: 100, y: 140, width: 40, height: 40 },
    ],
    [
      { x: 30, y: 80, width: 40, height: 40 },
      { x: 100, y: 80, width: 40, height: 40 },
      { x: 30, y: 140, width: 40, height: 40 },
    ],
  ]) {
    const { page } = fakeClickDiscoveryPage({
      containerBox: { x: 0, y: 0, width: 240, height: 220 },
      candidateBoxes,
    });

    assert.equal(await detectVerificationChallenge(page), null);
  }
});

test("click discovery requires one-to-one column alignment for an uneven two-row grid", async () => {
  const { page } = fakeClickDiscoveryPage({
    containerBox: { x: 0, y: 0, width: 240, height: 220 },
    candidateBoxes: [
      { x: 30, y: 80, width: 40, height: 40 },
      { x: 100, y: 80, width: 40, height: 40 },
      { x: 170, y: 80, width: 40, height: 40 },
      { x: 80, y: 140, width: 40, height: 40 },
      { x: 120, y: 140, width: 40, height: 40 },
    ],
  });

  assert.equal(await detectVerificationChallenge(page), null);
});

test("click discovery rejects candidates collected from sibling selection regions", async () => {
  const { page, candidates } = fakeClickDiscoveryPage();
  candidates[0].selectionRegion = "selection-a";
  candidates[1].selectionRegion = "selection-a";
  candidates[2].selectionRegion = "selection-b";
  candidates[3].selectionRegion = "selection-b";

  assert.equal(await detectVerificationChallenge(page), null);
});

test("click discovery rejects candidates with incompatible dimensions", async () => {
  const { page } = fakeClickDiscoveryPage({
    candidateBoxes: [
      { x: 30, y: 80, width: 40, height: 40 },
      { x: 100, y: 80, width: 40, height: 40 },
      { x: 30, y: 140, width: 82, height: 40 },
      { x: 130, y: 140, width: 40, height: 40 },
    ],
  });

  assert.equal(await detectVerificationChallenge(page), null);
});

test("click discovery rejects a target that overlaps the selection region", async () => {
  const { page } = fakeClickDiscoveryPage({
    targetBox: { x: 35, y: 85, width: 40, height: 40 },
  });

  assert.equal(await detectVerificationChallenge(page), null);
});

test("click discovery rejects more than six visible controls instead of truncating them", async () => {
  const { page } = fakeClickDiscoveryPage({
    containerBox: { x: 0, y: 0, width: 360, height: 220 },
    candidateBoxes: Array.from({ length: 7 }, (_value, index) => ({
      x: 20 + (index * 45),
      y: 100,
      width: 36,
      height: 36,
    })),
  });

  assert.equal(await detectVerificationChallenge(page), null);
});

test("challenge discovery finds bounded slider fallbacks and ignores hidden controls", async () => {
  const hiddenHandle = fakeHandle({ visible: false });
  const sliderHandle = fakeHandle();
  const sliderBackground = fakeHandle({ box: { x: 20, y: 20, width: 300, height: 120 } });
  const sliderPiece = fakeHandle();
  const container = fakeHandle({
    one: (selector) => {
      if (selector.includes("slider_button") || selector.includes("geetest_btn")) return hiddenHandle;
      if (selector.includes("[class*='slider'][class*='handle']")) return sliderHandle;
      if (selector.includes("geetest_bg")) return sliderBackground;
      if (selector.includes("geetest_slice")) return sliderPiece;
      return null;
    },
  });
  const frame = fakeFrame({ one: (selector) => (selector.includes("geetest") ? container : null) });

  const challenge = await detectVerificationChallenge({ mainFrame: () => frame, frames: () => [frame] });

  assert.equal(challenge.challenge_type, "slider");
  assert.equal(challenge.handle, sliderHandle);
  assert.equal(challenge.background, sliderBackground);
  assert.equal(challenge.piece, sliderPiece);
});

test("challenge discovery rejects a generic slider container as the drag handle", async () => {
  const sliderContainer = fakeHandle({ box: { x: 10, y: 10, width: 300, height: 120 } });
  const sliderBackground = fakeHandle({ box: { x: 10, y: 10, width: 300, height: 120 } });
  const sliderPiece = fakeHandle();
  const container = fakeHandle({
    one: (selector) => {
      if (selector === "[class*='slider']") return sliderContainer;
      if (selector.includes("geetest_bg")) return sliderBackground;
      if (selector.includes("geetest_slice")) return sliderPiece;
      return null;
    },
  });
  const frame = fakeFrame({ one: (selector) => (selector.includes("geetest") ? container : null) });

  const challenge = await detectVerificationChallenge({ mainFrame: () => frame, frames: () => [frame] });

  assert.equal(challenge, null);
});

test("challenge discovery recognizes visible Cloudflare iframe or a populated response", async () => {
  const iframe = fakeHandle();
  const iframeFrame = fakeFrame({
    one: (selector) => (selector.includes("challenges.cloudflare.com") ? iframe : null),
  });
  const iframeChallenge = await detectVerificationChallenge({
    mainFrame: () => iframeFrame,
    frames: () => [iframeFrame],
  });
  assert.equal(iframeChallenge.provider, "turnstile");
  assert.equal(iframeChallenge.challenge_type, "checkbox");

  const response = fakeHandle({ value: "completed-token", box: null });
  const responseFrame = fakeFrame({
    one: (selector) => (selector.includes("cf-turnstile-response") ? response : null),
  });
  const responseChallenge = await detectVerificationChallenge({
    mainFrame: () => responseFrame,
    frames: () => [responseFrame],
  });
  assert.equal(responseChallenge.provider, "turnstile");
  assert.equal(responseChallenge.response, response);
});

test("Turnstile discovery does not select an unrelated page checkbox", async () => {
  const iframe = fakeHandle();
  const unrelatedCheckbox = fakeHandle();
  const turnstileCheckbox = fakeHandle();
  const main = fakeFrame({
    one: (selector) => {
      if (selector.includes("challenges.cloudflare.com")) return iframe;
      if (selector.includes("checkbox")) return unrelatedCheckbox;
      return null;
    },
  });
  main.url = () => "https://makerworld.com/zh";
  const child = fakeFrame({
    one: (selector) => (selector.includes("checkbox") ? turnstileCheckbox : null),
  });
  child.url = () => "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/turnstile";

  const challenge = await detectVerificationChallenge({
    mainFrame: () => main,
    frames: () => [main, child],
  });

  assert.equal(challenge.checkbox, turnstileCheckbox);
});

test("coordinate click dispatches every point in target order before confirmation", async () => {
  const events = [];
  const challenge = coordinateChallenge();
  const result = await attemptAutomaticVerification(fakeInputPage({
    dispatch: async (event) => events.push(event),
  }), {
    detectChallenge: async () => challenge,
    fingerprintChallenge: async () => "coordinate-a",
    visionRequest: async () => ({
      ok: true,
      points: [
        { x: 0.75, y: 0.25, confidence: 0.90 },
        { x: 0.20, y: 0.80, confidence: 0.86 },
      ],
      confidence: 0.86,
      margin: 0.12,
    }),
    isChallengeComplete: async () => true,
    sleep: async () => {},
  });

  assert.equal(result.completed, true);
  assert.deepEqual(events.map(({ type }) => type), [
    "mouseMoved", "mousePressed", "mouseReleased",
    "mouseMoved", "mousePressed", "mouseReleased",
    "mouseMoved", "mousePressed", "mouseReleased",
  ]);
  assert.deepEqual(pressedCenters(events), [[245, 122], [80, 232], [170, 297]]);
});

test("coordinate click rejects one point without mouse input", async () => {
  const events = [];
  const result = await attemptAutomaticVerification(fakeInputPage({
    dispatch: async (event) => events.push(event),
  }), {
    detectChallenge: async () => coordinateChallenge(),
    fingerprintChallenge: async () => "coordinate-one",
    visionRequest: async () => ({
      ok: true,
      points: [{ x: 0.25, y: 0.25, confidence: 0.9 }],
      confidence: 0.9,
      margin: 0.1,
    }),
  });

  assert.deepEqual(events, []);
  assert.equal(result.reason, "coordinate_invalid");
});

test("coordinate click rejects six points without mouse input", async () => {
  const events = [];
  const result = await attemptAutomaticVerification(fakeInputPage({
    dispatch: async (event) => events.push(event),
  }), {
    detectChallenge: async () => coordinateChallenge(),
    fingerprintChallenge: async () => "coordinate-six",
    visionRequest: async () => ({
      ok: true,
      points: [0.1, 0.2, 0.3, 0.4, 0.5, 0.6].map((x) => ({ x, y: 0.5, confidence: 0.9 })),
      confidence: 0.9,
      margin: 0.1,
    }),
  });

  assert.deepEqual(events, []);
  assert.equal(result.reason, "coordinate_invalid");
});

test("coordinate click rejects non-finite and out-of-range coordinates without mouse input", async () => {
  for (const invalidPoint of [
    { x: Number.NaN, y: 0.5, confidence: 0.9 },
    { x: 0.5, y: Number.POSITIVE_INFINITY, confidence: 0.9 },
    { x: -0.01, y: 0.5, confidence: 0.9 },
    { x: 0.5, y: 1.01, confidence: 0.9 },
  ]) {
    const events = [];
    const result = await attemptAutomaticVerification(fakeInputPage({
      dispatch: async (event) => events.push(event),
    }), {
      detectChallenge: async () => coordinateChallenge(),
      fingerprintChallenge: async () => "coordinate-invalid",
      visionRequest: async () => ({
        ok: true,
        points: [
          { x: 0.2, y: 0.2, confidence: 0.9 },
          invalidPoint,
        ],
        confidence: 0.9,
        margin: 0.1,
      }),
    });

    assert.deepEqual(events, []);
    assert.equal(result.reason, "coordinate_invalid");
  }
});

test("coordinate click rejects non-numeric vision fields without mouse input", async () => {
  const validResult = {
    ok: true,
    points: [
      { x: 0.2, y: 0.2, confidence: 0.9 },
      { x: 0.8, y: 0.8, confidence: 0.85 },
    ],
    confidence: 0.85,
    margin: 0.1,
  };
  const invalidResults = [
    ["ok is a truthy string", { ...validResult, ok: "false" }],
    ["point x is null", {
      ...validResult,
      points: [{ ...validResult.points[0], x: null }, validResult.points[1]],
    }],
    ["point y is boolean", {
      ...validResult,
      points: [{ ...validResult.points[0], y: true }, validResult.points[1]],
    }],
    ["point x is a numeric string", {
      ...validResult,
      points: [{ ...validResult.points[0], x: "0.2" }, validResult.points[1]],
    }],
    ["point y is a numeric string", {
      ...validResult,
      points: [{ ...validResult.points[0], y: "0.2" }, validResult.points[1]],
    }],
    ["point confidence is a numeric string", {
      ...validResult,
      points: [{ ...validResult.points[0], confidence: "0.9" }, validResult.points[1]],
    }],
    ["aggregate confidence is a string", { ...validResult, confidence: "0.85" }],
    ["aggregate confidence is boolean", { ...validResult, confidence: true }],
    ["aggregate margin is a string", { ...validResult, margin: "0.1" }],
    ["aggregate margin is boolean", { ...validResult, margin: false }],
  ];
  const { confidence: _confidence, ...missingConfidence } = validResult;
  const { margin: _margin, ...missingMargin } = validResult;
  invalidResults.push(
    ["aggregate confidence is missing", missingConfidence],
    ["aggregate confidence is NaN", { ...validResult, confidence: Number.NaN }],
    ["aggregate confidence is infinite", { ...validResult, confidence: Number.POSITIVE_INFINITY }],
    ["aggregate confidence is below zero", { ...validResult, confidence: -0.01 }],
    ["aggregate confidence is above one", { ...validResult, confidence: 1.01 }],
    ["aggregate margin is missing", missingMargin],
    ["aggregate margin is NaN", { ...validResult, margin: Number.NaN }],
    ["aggregate margin is infinite", { ...validResult, margin: Number.POSITIVE_INFINITY }],
    ["aggregate margin is below zero", { ...validResult, margin: -0.01 }],
    ["aggregate margin is above one", { ...validResult, margin: 1.01 }],
  );

  for (const [name, visionResult] of invalidResults) {
    const events = [];
    const result = await attemptAutomaticVerification(fakeInputPage({
      dispatch: async (event) => events.push(event),
    }), {
      detectChallenge: async () => coordinateChallenge(),
      fingerprintChallenge: async () => `coordinate-invalid-${name}`,
      isChallengeComplete: async () => true,
      visionRequest: async () => visionResult,
      sleep: async () => {},
    });

    assert.deepEqual(events, [], name);
    assert.equal(result.reason, "coordinate_invalid", name);
  }
});

test("coordinate click rejects duplicate coordinates without mouse input", async () => {
  const events = [];
  const result = await attemptAutomaticVerification(fakeInputPage({
    dispatch: async (event) => events.push(event),
  }), {
    detectChallenge: async () => coordinateChallenge(),
    fingerprintChallenge: async () => "coordinate-duplicate",
    visionRequest: async () => ({
      ok: true,
      points: [
        { x: 0.4, y: 0.6, confidence: 0.9 },
        { x: 0.4, y: 0.6, confidence: 0.85 },
      ],
      confidence: 0.85,
      margin: 0.1,
    }),
  });

  assert.deepEqual(events, []);
  assert.equal(result.reason, "coordinate_invalid");
});

test("coordinate click rejects layout changes after recognition without mouse input", async () => {
  const events = [];
  let layoutChanged = false;
  const challenge = coordinateChallenge();
  challenge.background.boundingBox = async () => (
    layoutChanged
      ? { x: 21, y: 72, width: 300, height: 200 }
      : { x: 20, y: 72, width: 300, height: 200 }
  );
  const result = await attemptAutomaticVerification(fakeInputPage({
    dispatch: async (event) => events.push(event),
  }), {
    detectChallenge: async () => challenge,
    fingerprintChallenge: async () => "coordinate-layout",
    visionRequest: async () => {
      layoutChanged = true;
      return {
        ok: true,
        points: [
          { x: 0.2, y: 0.2, confidence: 0.9 },
          { x: 0.8, y: 0.8, confidence: 0.9 },
        ],
        confidence: 0.9,
        margin: 0.1,
      };
    },
  });

  assert.deepEqual(events, []);
  assert.equal(result.reason, "challenge_changed");
});

test("coordinate click rejects a changed fingerprint without mouse input", async () => {
  const events = [];
  let fingerprintCalls = 0;
  const result = await attemptAutomaticVerification(fakeInputPage({
    dispatch: async (event) => events.push(event),
  }), {
    detectChallenge: async () => coordinateChallenge(),
    fingerprintChallenge: async () => {
      fingerprintCalls += 1;
      return fingerprintCalls === 1 ? "coordinate-a" : "coordinate-b";
    },
    visionRequest: async () => ({
      ok: true,
      points: [
        { x: 0.2, y: 0.2, confidence: 0.9 },
        { x: 0.8, y: 0.8, confidence: 0.9 },
      ],
      confidence: 0.9,
      margin: 0.1,
    }),
  });

  assert.deepEqual(events, []);
  assert.equal(result.reason, "challenge_changed");
});

test("coordinate click rejects a missing confirmation control without mouse input", async () => {
  const events = [];
  const result = await attemptAutomaticVerification(fakeInputPage({
    dispatch: async (event) => events.push(event),
  }), {
    detectChallenge: async () => coordinateChallenge({ confirm: null }),
    fingerprintChallenge: async () => "coordinate-no-confirm",
    visionRequest: async () => ({
      ok: true,
      points: [
        { x: 0.2, y: 0.2, confidence: 0.9 },
        { x: 0.8, y: 0.8, confidence: 0.9 },
      ],
      confidence: 0.9,
      margin: 0.1,
    }),
  });

  assert.deepEqual(events, []);
  assert.equal(result.reason, "confirmation_unavailable");
});

test("coordinate click vision timeout emits no mouse input", async () => {
  const events = [];
  const result = await attemptAutomaticVerification(fakeInputPage({
    dispatch: async (event) => events.push(event),
  }), {
    detectChallenge: async () => coordinateChallenge(),
    fingerprintChallenge: async () => "coordinate-timeout",
    visionRequest: async () => new Promise(() => {}),
    timeoutMs: 10,
  });

  assert.deepEqual(events, []);
  assert.equal(result.reason, "timeout");
});

test("coordinate click abort between points prevents remaining input and confirmation", async () => {
  const controller = new AbortController();
  const events = [];
  const lifecycle = [];
  const result = await attemptAutomaticVerification(fakeInputPage({
    dispatch: async (event) => {
      events.push(event);
      if (event.type === "mouseReleased") controller.abort();
    },
    detach: async () => lifecycle.push("detach"),
  }), {
    signal: controller.signal,
    detectChallenge: async () => coordinateChallenge(),
    fingerprintChallenge: async () => "coordinate-abort",
    visionRequest: async () => ({
      ok: true,
      points: [
        { x: 0.2, y: 0.2, confidence: 0.9 },
        { x: 0.8, y: 0.8, confidence: 0.9 },
      ],
      confidence: 0.9,
      margin: 0.1,
    }),
    sleep: async () => {},
  });

  assert.deepEqual(events.map(({ type }) => type), [
    "mouseMoved", "mousePressed", "mouseReleased",
    "mouseReleased",
  ]);
  assert.deepEqual(lifecycle, ["detach"]);
  assert.equal(result.reason, "aborted");
});

test("coordinate click retries release with the hard deadline when abort races mouse up", async () => {
  const controller = new AbortController();
  const lifecycle = [];
  let releaseAttempts = 0;
  let cleanupReleaseSawAbort = false;
  const result = await attemptAutomaticVerification(fakeInputPage({
    dispatch: async (event) => {
      lifecycle.push(event.type);
      if (event.type !== "mouseReleased") return;
      releaseAttempts += 1;
      if (releaseAttempts === 1) {
        controller.abort();
        await new Promise((resolve) => setTimeout(resolve, 20));
      } else {
        cleanupReleaseSawAbort = controller.signal.aborted;
      }
    },
    detach: async () => lifecycle.push("detach"),
  }), {
    signal: controller.signal,
    detectChallenge: async () => coordinateChallenge(),
    fingerprintChallenge: async () => "coordinate-release-race",
    visionRequest: async () => ({
      ok: true,
      points: [
        { x: 0.2, y: 0.2, confidence: 0.9 },
        { x: 0.8, y: 0.8, confidence: 0.9 },
      ],
      confidence: 0.9,
      margin: 0.1,
    }),
    sleep: async () => {},
    timeoutMs: 100,
  });

  assert.equal(result.reason, "aborted");
  assert.equal(cleanupReleaseSawAbort, true);
  assert.deepEqual(lifecycle, [
    "mouseMoved",
    "mousePressed",
    "mouseReleased",
    "mouseReleased",
    "detach",
  ]);
});

test("coordinate click stops after the first complete click when the challenge changes", async () => {
  const events = [];
  let fingerprint = "coordinate-before-first-point";
  const result = await attemptAutomaticVerification(fakeInputPage({
    dispatch: async (event) => {
      events.push(event);
      if (event.type === "mouseReleased") fingerprint = "coordinate-replaced";
    },
  }), {
    detectChallenge: async () => coordinateChallenge(),
    fingerprintChallenge: async () => fingerprint,
    isChallengeComplete: async () => true,
    visionRequest: async () => ({
      ok: true,
      points: [
        { x: 0.2, y: 0.2, confidence: 0.9 },
        { x: 0.8, y: 0.8, confidence: 0.9 },
      ],
      confidence: 0.9,
      margin: 0.1,
    }),
    sleep: async () => {},
  });

  assert.deepEqual(events.map(({ type }) => type), [
    "mouseMoved", "mousePressed", "mouseReleased",
  ]);
  assert.equal(result.completed, false);
  assert.equal(result.reason, "challenge_changed");
});

test("coordinate click treats selection markers and confirmation styling as unchanged", async () => {
  const events = [];
  let confirmationChanged = false;
  let selectionMarkerAdded = false;
  const confirm = fakeHandle({ box: { x: 136, y: 282, width: 68, height: 30 } });
  confirm.evaluate = async (_callback, argument) => {
    if (argument === "fingerprint") {
      return confirmationChanged ? "confirmation-after-click" : "confirmation-before-click";
    }
    return true;
  };
  const challenge = coordinateChallenge({
    targets: fakeHandle({
      box: { x: 70, y: 18, width: 150, height: 42 },
      fingerprint: "coordinate-targets",
    }),
    background: fakeHandle({
      box: { x: 20, y: 72, width: 300, height: 200 },
      fingerprint: "coordinate-background",
      screenshot: () => pngBuffer(300, 200, selectionMarkerAdded ? 2 : 1),
    }),
    confirm,
  });
  const result = await attemptAutomaticVerification(fakeInputPage({
    dispatch: async (event) => {
      events.push(event);
      if (event.type === "mousePressed" && event.x === 170 && event.y === 297) {
        confirmationChanged = true;
      }
      if (event.type === "mouseReleased" && event.x !== 170 && event.y !== 297) {
        selectionMarkerAdded = true;
      }
    },
  }), {
    detectChallenge: async () => challenge,
    isChallengeComplete: async () => false,
    visionRequest: async () => ({
      ok: true,
      points: [
        { x: 0.2, y: 0.2, confidence: 0.9 },
        { x: 0.8, y: 0.8, confidence: 0.9 },
      ],
      confidence: 0.9,
      margin: 0.1,
    }),
    sleep: async () => {},
    timeoutMs: 100,
  });

  assert.equal(pressedCenters(events).filter(([x, y]) => x === 170 && y === 297).length, 1);
  assert.equal(result.attempts, 1);
  assert.equal(result.reason, "challenge_unchanged");
});

test("coordinate click performs at most one second interaction for a replaced challenge", async () => {
  const events = [];
  let activeChallenge;
  let confirmations = 0;
  const challengeA = coordinateChallenge();
  const challengeB = coordinateChallenge();
  activeChallenge = challengeA;
  const result = await attemptAutomaticVerification(fakeInputPage({
    dispatch: async (event) => {
      events.push(event);
      if (event.type === "mousePressed" && event.x === 170 && event.y === 297) {
        confirmations += 1;
        if (confirmations === 1) activeChallenge = challengeB;
      }
    },
  }), {
    detectChallenge: async () => activeChallenge,
    fingerprintChallenge: async (challenge) => (
      challenge === challengeA ? "coordinate-a" : "coordinate-b"
    ),
    isChallengeComplete: async () => false,
    visionRequest: async () => ({
      ok: true,
      points: [
        { x: 0.2, y: 0.2, confidence: 0.9 },
        { x: 0.8, y: 0.8, confidence: 0.9 },
      ],
      confidence: 0.9,
      margin: 0.1,
    }),
    sleep: async () => {},
    timeoutMs: 100,
  });

  assert.equal(confirmations, 2);
  assert.equal(result.attempts, 2);
  assert.equal(result.reason, "challenge_unchanged");
});

test("click verification clicks only the candidate selected by vision", async () => {
  let syntheticClicks = 0;
  const inputEvents = [];
  const page = fakeInputPage({ dispatch: async (event) => { inputEvents.push(event); } });
  const challenge = clickChallenge(async () => { syntheticClicks += 1; });

  const result = await attemptAutomaticVerification(page, {
    detectChallenge: async () => challenge,
    fingerprintChallenge: async () => "fingerprint-a",
    isChallengeComplete: async () => true,
    visionRequest: async () => ({ ok: true, candidate_index: 1, confidence: 0.91 }),
  });

  assert.equal(syntheticClicks, 0);
  assert.deepEqual(inputEvents.map(({ type }) => type), [
    "mouseMoved",
    "mousePressed",
    "mouseReleased",
  ]);
  assert.equal(inputEvents.every(({ pointerType }) => pointerType === "mouse"), true);
  assert.equal(result.completed, true);
  assert.equal(result.attempts, 1);
});

test("click verification revalidates the layout immediately before trusted input", async () => {
  const inputEvents = [];
  let layoutChanged = false;
  const page = fakeInputPage({ dispatch: async (event) => { inputEvents.push(event); } });
  const challenge = clickChallenge(async () => {});
  challenge.candidates[3].boundingBox = async () => (
    layoutChanged
      ? { x: 175, y: 140, width: 40, height: 40 }
      : { x: 100, y: 140, width: 40, height: 40 }
  );

  const result = await attemptAutomaticVerification(page, {
    detectChallenge: async () => challenge,
    fingerprintChallenge: async () => "click-layout-change",
    visionRequest: async () => {
      layoutChanged = true;
      return { ok: true, candidate_index: 1, confidence: 0.91 };
    },
  });

  assert.deepEqual(inputEvents, []);
  assert.equal(result.reason, "click_layout_invalid");
});

test("click verification rejects a candidate hidden while vision is running", async () => {
  const inputEvents = [];
  let candidateVisible = true;
  const page = fakeInputPage({ dispatch: async (event) => { inputEvents.push(event); } });
  const challenge = clickChallenge(async () => {});
  challenge.candidates[1] = fakeHandle({
    box: { x: 100, y: 80, width: 40, height: 40 },
    visible: () => candidateVisible,
    fingerprint: "challenge-a:candidate-1",
  });

  const result = await attemptAutomaticVerification(page, {
    detectChallenge: async () => challenge,
    fingerprintChallenge: async () => "click-hidden-candidate",
    visionRequest: async () => {
      candidateVisible = false;
      return { ok: true, candidate_index: 1, confidence: 0.91 };
    },
  });

  assert.deepEqual(inputEvents, []);
  assert.equal(result.reason, "click_layout_invalid");
});

test("click verification rejects a candidate moved outside its original container", async () => {
  const inputEvents = [];
  let candidateInContainer = true;
  const page = fakeInputPage({ dispatch: async (event) => { inputEvents.push(event); } });
  const challenge = clickChallenge(async () => {});
  const selectedCandidate = challenge.candidates[1];
  challenge.container = fakeHandle({
    box: { x: 0, y: 0, width: 220, height: 220 },
    contains: (handle) => handle !== selectedCandidate || candidateInContainer,
  });

  const result = await attemptAutomaticVerification(page, {
    detectChallenge: async () => challenge,
    fingerprintChallenge: async () => "click-reparented-candidate",
    visionRequest: async () => {
      candidateInContainer = false;
      return { ok: true, candidate_index: 1, confidence: 0.91 };
    },
  });

  assert.deepEqual(inputEvents, []);
  assert.equal(result.reason, "click_layout_invalid");
});

test("click verification rejects a candidate moved to a sibling selection region", async () => {
  const inputEvents = [];
  const page = fakeInputPage({ dispatch: async (event) => { inputEvents.push(event); } });
  const challenge = clickChallenge(async () => {});

  const result = await attemptAutomaticVerification(page, {
    detectChallenge: async () => challenge,
    fingerprintChallenge: async () => "click-sibling-selection-region",
    visionRequest: async () => {
      challenge.candidates[1].selectionRegion = "selection-b";
      return { ok: true, candidate_index: 1, confidence: 0.91 };
    },
    timeoutMs: 100,
  });

  assert.deepEqual(inputEvents, []);
  assert.equal(result.reason, "click_layout_invalid");
});

test("slider always releases the mouse and restores the piece after movement errors", async () => {
  const mouseEvents = [];
  const page = fakeInputPage({
    dispatch: async ({ type }) => {
      if (type === "mouseMoved") {
        mouseEvents.push("move");
        if (mouseEvents.filter((event) => event === "move").length > 1) throw new Error("move failed");
      } else if (type === "mousePressed") mouseEvents.push("down");
      else if (type === "mouseReleased") mouseEvents.push("up");
    },
  });
  const challenge = {
    provider: "geetest4",
    challenge_type: "slider",
    handle: fakeHandle({ box: { x: 20, y: 30, width: 40, height: 40 } }),
    background: fakeHandle({ box: { x: 20, y: 10, width: 300, height: 120 } }),
    piece: fakeHandle({
      box: { x: 50, y: 20, width: 30, height: 30 },
      evaluate: async () => { throw new Error("page callback must not run"); },
    }),
  };

  const result = await attemptAutomaticVerification(page, {
    detectChallenge: async () => challenge,
    fingerprintChallenge: async () => "slider-a",
    visionRequest: async () => ({ ok: true, distance_css: 120, confidence: 0.92 }),
    random: () => 0.5,
    sleep: async () => {},
  });

  assert.equal(result.completed, false);
  assert.equal(mouseEvents.includes("down"), true);
  assert.equal(mouseEvents.at(-1), "up");
});

test("slider geometry uses screenshot-rounded absolute clips at fractional coordinates and DPR 2", async () => {
  let visionPayload;
  const moves = [];
  const page = fakeInputPage({
    dispatch: async ({ type, x, y }) => {
      if (type === "mouseMoved") moves.push({ x, y });
    },
  });
  const challenge = {
    provider: "geetest4",
    challenge_type: "slider",
    handle: fakeHandle({ box: { x: 50, y: 30, width: 40, height: 40 } }),
    background: fakeHandle({
      box: { x: 20.4, y: 10.4, width: 300, height: 120 },
      screenshot: pngBuffer(600, 240),
      viewportOffset: { x: 0.3, y: 0.3 },
    }),
    piece: fakeHandle({
      box: { x: 50.6, y: 20.6, width: 30, height: 30 },
      screenshot: pngBuffer(60, 60),
      evaluate: async (_callback, action) => (
        action?.action === "hide"
          ? { applied: true, id: action.id, value: "", priority: "" }
          : true
      ),
    }),
  };

  const result = await attemptAutomaticVerification(page, {
    detectChallenge: async () => challenge,
    fingerprintChallenge: async () => "slider-dpr",
    isChallengeComplete: async () => true,
    visionRequest: async (payload) => {
      visionPayload = payload;
      return { ok: true, distance_css: 120, confidence: 0.92 };
    },
    random: () => 0.5,
    sleep: async () => {},
  });

  assert.equal(result.completed, true);
  assert.deepEqual(visionPayload.geometry, {
    image_width: 600,
    image_height: 240,
    track_width: 300,
    track_height: 120,
    handle_width: 40,
    piece_offset_x: 60,
    piece_offset_y: 20,
  });
  assert.ok(Math.abs(moves.at(-1).x - 160.4) < 0.001);
});

test("slider rejects a right-edge trajectory whose overshoot leaves the track", async () => {
  const inputEvents = [];
  const page = fakeInputPage({ dispatch: async (event) => { inputEvents.push(event); } });
  const challenge = {
    provider: "geetest4",
    challenge_type: "slider",
    handle: fakeHandle({ box: { x: 20, y: 30, width: 40, height: 40 } }),
    background: fakeHandle({
      box: { x: 20, y: 10, width: 300, height: 120 },
      screenshot: pngBuffer(600, 240),
    }),
    piece: fakeHandle({
      box: { x: 50, y: 20, width: 30, height: 30 },
      screenshot: pngBuffer(60, 60),
    }),
  };

  const result = await attemptAutomaticVerification(page, {
    detectChallenge: async () => challenge,
    fingerprintChallenge: async () => "slider-right-edge",
    visionRequest: async () => ({ ok: true, distance_css: 260, confidence: 0.92 }),
    random: () => 0.5,
    sleep: async () => {},
  });

  assert.equal(result.completed, false);
  assert.equal(result.reason, "trajectory_out_of_bounds");
  assert.deepEqual(inputEvents, []);
});

test("timed out vision results never trigger a late click", async () => {
  let resolveVision;
  let clicks = 0;
  const challenge = clickChallenge(async () => { clicks += 1; });
  const verification = attemptAutomaticVerification({}, {
    detectChallenge: async () => challenge,
    visionRequest: async () => new Promise((resolve) => { resolveVision = resolve; }),
    timeoutMs: 10,
  });

  const result = await verification;
  resolveVision({ ok: true, candidate_index: 1, confidence: 0.9 });
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(result.reason, "timeout");
  assert.equal(clicks, 0);
});

test("a timed out trusted candidate click queues only release before detach", async () => {
  const lifecycle = [];
  const page = fakeInputPage({
    dispatch: async ({ type }) => {
      lifecycle.push(type);
      if (type === "mousePressed") return new Promise(() => {});
      return undefined;
    },
    detach: async () => { lifecycle.push("detach"); },
  });
  const challenge = clickChallenge(async () => {
    throw new Error("synthetic click must not run");
  });
  const result = await attemptAutomaticVerification(page, {
    detectChallenge: async () => challenge,
    fingerprintChallenge: async () => "candidate-timeout",
    visionRequest: async () => ({ ok: true, candidate_index: 1, confidence: 0.9 }),
    timeoutMs: 20,
  });

  assert.equal(result.reason, "timeout");
  assert.deepEqual(lifecycle, ["mouseMoved", "mousePressed", "mouseReleased", "detach"]);
  await new Promise((resolve) => setTimeout(resolve, 30));
  assert.deepEqual(lifecycle, ["mouseMoved", "mousePressed", "mouseReleased", "detach"]);
});

test("a hanging screenshot cannot start vision or click after return", async () => {
  let screenshots = 0;
  let visionCalls = 0;
  let clicks = 0;
  const challenge = clickChallenge(async () => { clicks += 1; });
  challenge.target.screenshot = async () => {
    await new Promise((resolve) => setTimeout(resolve, 40));
    screenshots += 1;
    return pngBuffer(40, 40);
  };
  const result = await attemptAutomaticVerification({}, {
    detectChallenge: async () => challenge,
    visionRequest: async () => {
      visionCalls += 1;
      return { ok: true, candidate_index: 1, confidence: 0.9 };
    },
    timeoutMs: 10,
  });

  assert.equal(result.reason, "timeout");
  await new Promise((resolve) => setTimeout(resolve, 50));
  assert.equal(screenshots, 1);
  assert.equal(visionCalls, 0);
  assert.equal(clicks, 0);
});

test("timed out slider vision never starts a late drag", async () => {
  let resolveVision;
  let mouseEvents = 0;
  const page = fakeInputPage({ dispatch: async () => { mouseEvents += 1; } });
  const challenge = {
    provider: "geetest4",
    challenge_type: "slider",
    handle: fakeHandle(),
    background: fakeHandle({
      box: { x: 10, y: 20, width: 300, height: 120 },
      screenshot: pngBuffer(300, 120),
    }),
    piece: fakeHandle({
      evaluate: async (_callback, action) => (
        action?.action === "hide"
          ? { applied: true, id: action.id, value: "", priority: "" }
          : true
      ),
    }),
  };
  const verification = attemptAutomaticVerification(page, {
    detectChallenge: async () => challenge,
    fingerprintChallenge: async () => "slider-late",
    visionRequest: async () => new Promise((resolve) => { resolveVision = resolve; }),
    timeoutMs: 10,
  });

  const result = await verification;
  resolveVision({ ok: true, distance_css: 100, confidence: 0.9 });
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(result.reason, "timeout");
  assert.equal(mouseEvents, 0);
});

test("mouse cleanup uses a monotonic hard deadline when wall time moves backwards", async () => {
  const originalDateNow = Date.now;
  let wallClockOffset = 0;
  const lifecycle = [];
  const page = fakeInputPage({
    dispatch: async ({ type }) => {
      lifecycle.push(type);
      if (type === "mousePressed") {
        wallClockOffset = -100;
        return new Promise(() => {});
      }
      if (type === "mouseReleased") return new Promise(() => {});
      return undefined;
    },
    detach: async (sessionId) => { lifecycle.push(`detach-${sessionId}`); },
  });
  const challenge = {
    provider: "geetest4",
    challenge_type: "slider",
    handle: fakeHandle(),
    background: fakeHandle({
      box: { x: 10, y: 20, width: 300, height: 120 },
      screenshot: pngBuffer(300, 120),
    }),
    piece: fakeHandle({
      evaluate: async (_callback, action) => (
        action?.action === "hide"
          ? { applied: true, id: action.id, value: "", priority: "" }
          : true
      ),
    }),
  };
  const startedAt = performance.now();
  Date.now = () => originalDateNow() + wallClockOffset;
  let result;
  try {
    result = await attemptAutomaticVerification(page, {
      detectChallenge: async () => challenge,
      fingerprintChallenge: async () => "slider-cdp-hang",
      visionRequest: async () => ({ ok: true, distance_css: 100, confidence: 0.9 }),
      timeoutMs: 20,
    });
  } finally {
    Date.now = originalDateNow;
  }

  assert.equal(result.reason, "timeout");
  assert.ok(performance.now() - startedAt < 80);
  assert.deepEqual(lifecycle, [
    "detach-0",
    "mouseMoved",
    "mousePressed",
    "mouseReleased",
    "detach-1",
  ]);
});

test("total timeout aborts and kills the active Python child", async () => {
  let child;
  const challenge = clickChallenge(async () => {});
  const result = await attemptAutomaticVerification({}, {
    detectChallenge: async () => challenge,
    visionRequest: (payload, { signal }) => runVisionRequest(payload, {
      signal,
      spawnFn: () => {
        child = fakeChild(() => {});
        return child;
      },
    }),
    timeoutMs: 10,
  });

  assert.equal(result.reason, "timeout");
  assert.equal(child.killed, true);
});

test("slider timeout restores the exact piece visibility before returning", async () => {
  const originalStyle = "visibility:collapse!important";
  let style = originalStyle;
  const page = fakeInputPage({
    protocol: async (method, params) => {
      if (method === "DOM.getDocument") return { root: { nodeId: 1 } };
      if (method === "DOM.pushNodesByBackendIdsToFrontend") return { nodeIds: [1] };
      if (method === "DOM.getAttributes") return { attributes: ["style", style] };
      if (method === "DOM.setAttributeValue") {
        style = params.value;
        return {};
      }
      throw new Error(`unexpected protocol method: ${method}`);
    },
  });
  const challenge = {
    provider: "geetest4",
    challenge_type: "slider",
    handle: fakeHandle(),
    background: fakeHandle({ screenshot: () => new Promise(() => {}) }),
    piece: fakeHandle({ evaluate: async () => { throw new Error("page callback must not run"); } }),
  };

  const result = await attemptAutomaticVerification(page, {
    detectChallenge: async () => challenge,
    fingerprintChallenge: async () => "slider-timeout",
    timeoutMs: 10,
  });

  assert.equal(result.reason, "timeout");
  assert.equal(style, originalStyle);
});

test("piece visibility is restored through ordered CDP DOM commands without page callbacks", async () => {
  const originalStyle = "visibility:collapse!important;color:red";
  let style = originalStyle;
  const lifecycle = [];
  const page = fakeInputPage({
    protocol: async (method, params) => {
      lifecycle.push(method);
      if (method === "DOM.getDocument") return { root: { nodeId: 1 } };
      if (method === "DOM.pushNodesByBackendIdsToFrontend") return { nodeIds: [7] };
      if (method === "DOM.getAttributes") return { attributes: ["class", "piece", "style", style] };
      if (method === "DOM.setAttributeValue") {
        assert.equal(params.nodeId, 7);
        assert.equal(params.name, "style");
        style = params.value;
        return {};
      }
      if (method === "DOM.removeAttribute") {
        style = null;
        return {};
      }
      throw new Error(`unexpected protocol method: ${method}`);
    },
    detach: async () => { lifecycle.push("detach"); },
  });
  const challenge = {
    provider: "geetest4",
    challenge_type: "slider",
    handle: fakeHandle(),
    background: fakeHandle({ screenshot: () => new Promise(() => {}) }),
    piece: fakeHandle({
      backendNodeId: 42,
      evaluate: async () => { throw new Error("page callback must not run"); },
    }),
  };

  const result = await attemptAutomaticVerification(page, {
    detectChallenge: async () => challenge,
    fingerprintChallenge: async () => "slider-hide-timeout",
    timeoutMs: 20,
  });

  assert.equal(result.reason, "timeout");
  assert.equal(style, originalStyle);
  assert.deepEqual(lifecycle, [
    "DOM.getDocument",
    "DOM.pushNodesByBackendIdsToFrontend",
    "DOM.getAttributes",
    "DOM.setAttributeValue",
    "DOM.setAttributeValue",
    "detach",
  ]);
});

test("piece restoration failure returns a controlled result and never drags", async () => {
  let mouseEvents = 0;
  let detached = false;
  const page = fakeInputPage({
    dispatch: async () => { mouseEvents += 1; },
    protocol: async (method) => {
      if (method === "DOM.getDocument") return { root: { nodeId: 1 } };
      if (method === "DOM.pushNodesByBackendIdsToFrontend") return { nodeIds: [1] };
      if (method === "DOM.getAttributes") return { attributes: [] };
      if (method === "DOM.setAttributeValue") return {};
      if (method === "DOM.removeAttribute") throw new Error("detached");
      throw new Error(`unexpected protocol method: ${method}`);
    },
    detach: async () => { detached = true; },
  });
  const challenge = {
    provider: "geetest4",
    challenge_type: "slider",
    handle: fakeHandle(),
    background: fakeHandle({ screenshot: pngBuffer(300, 120) }),
    piece: fakeHandle({ evaluate: async () => { throw new Error("page callback must not run"); } }),
  };

  const result = await attemptAutomaticVerification(page, {
    detectChallenge: async () => challenge,
    fingerprintChallenge: async () => "slider-restore",
    visionRequest: async () => ({ ok: true, distance_css: 100, confidence: 0.9 }),
  });

  assert.equal(result.reason, "piece_restore_failed");
  assert.equal(mouseEvents, 0);
  assert.equal(detached, true);
});

test("piece session detaches when backend node lookup fails", async () => {
  let detached = false;
  const page = fakeInputPage({ detach: async () => { detached = true; } });
  const challenge = {
    provider: "geetest4",
    challenge_type: "slider",
    handle: fakeHandle(),
    background: fakeHandle({ screenshot: pngBuffer(300, 120) }),
    piece: fakeHandle(),
  };
  challenge.piece.backendNodeId = async () => { throw new Error("detached node"); };

  const result = await attemptAutomaticVerification(page, {
    detectChallenge: async () => challenge,
    fingerprintChallenge: async () => "slider-node-failure",
  });

  assert.equal(result.reason, "interaction_failed");
  assert.equal(detached, true);
});

test("automatic verification polls for a delayed first challenge mount", async () => {
  let detections = 0;
  let clicks = 0;
  const challenge = clickChallenge(async () => {});
  const page = fakeInputPage({
    dispatch: async ({ type }) => {
      if (type === "mouseReleased") clicks += 1;
    },
  });

  const result = await attemptAutomaticVerification(page, {
    detectChallenge: async () => {
      detections += 1;
      return detections === 1 ? null : challenge;
    },
    fingerprintChallenge: async () => "delayed-challenge",
    isChallengeComplete: async () => clicks === 1,
    visionRequest: async () => ({ ok: true, candidate_index: 1, confidence: 0.9 }),
    sleep: async () => {},
    timeoutMs: 200,
  });

  assert.ok(detections >= 2);
  assert.equal(clicks, 1);
  assert.equal(result.completed, true);
  assert.equal(result.attempts, 1);
});

test("automatic verification keeps polling through a null refresh window without a third interaction", async () => {
  let clicks = 0;
  let refreshPolls = 0;
  const challengeA = clickChallenge(async () => {}, "challenge-a");
  const challengeB = clickChallenge(async () => {}, "challenge-b");
  const challengeC = clickChallenge(async () => {}, "challenge-c");
  const page = fakeInputPage({
    dispatch: async ({ type }) => {
      if (type === "mouseReleased") clicks += 1;
    },
  });

  const result = await attemptAutomaticVerification(page, {
    detectChallenge: async () => {
      if (clicks === 0) return challengeA;
      if (clicks === 1) {
        refreshPolls += 1;
        return refreshPolls === 1 ? null : challengeB;
      }
      return challengeC;
    },
    fingerprintChallenge: async (challenge) => {
      if (challenge === challengeA) return "challenge-a";
      if (challenge === challengeB) return "challenge-b";
      return "challenge-c";
    },
    isChallengeComplete: async () => false,
    visionRequest: async () => ({ ok: true, candidate_index: 1, confidence: 0.9 }),
    sleep: async () => {},
    timeoutMs: 200,
  });

  assert.ok(refreshPolls >= 2);
  assert.equal(clicks, 2);
  assert.equal(result.attempts, 2);
  assert.equal(result.reason, "attempts_exhausted");
});

test("automatic verification finds a first challenge mounted after 2.1 seconds within the stage", {
  timeout: 5_000,
}, async () => {
  let clicks = 0;
  const mountedAt = performance.now() + 2_100;
  const challenge = clickChallenge(async () => {}, "late-first-challenge");
  const page = fakeInputPage({
    dispatch: async ({ type }) => {
      if (type === "mouseReleased") clicks += 1;
    },
  });

  const result = await attemptAutomaticVerification(page, {
    detectChallenge: async () => (performance.now() >= mountedAt ? challenge : null),
    fingerprintChallenge: async () => "late-first-challenge",
    isChallengeComplete: async () => clicks === 1,
    visionRequest: async () => ({ ok: true, candidate_index: 1, confidence: 0.9 }),
    timeoutMs: 4_000,
  });

  assert.equal(clicks, 1);
  assert.equal(result.completed, true);
  assert.equal(result.attempts, 1);
});

test("automatic verification finds a refreshed challenge after a 5.1 second null window", {
  timeout: 9_000,
}, async () => {
  let clicks = 0;
  let clickedAt = 0;
  const challengeA = clickChallenge(async () => {}, "slow-refresh-a");
  const challengeB = clickChallenge(async () => {}, "slow-refresh-b");
  const page = fakeInputPage({
    dispatch: async ({ type }) => {
      if (type === "mouseReleased") {
        clicks += 1;
        clickedAt = performance.now();
      }
    },
  });

  const result = await attemptAutomaticVerification(page, {
    detectChallenge: async () => {
      if (clicks === 0) return challengeA;
      return performance.now() - clickedAt >= 5_100 ? challengeB : null;
    },
    fingerprintChallenge: async (challenge) => (
      challenge === challengeA ? "slow-refresh-a" : "slow-refresh-b"
    ),
    isChallengeComplete: async (_page, challenge) => challenge === challengeB,
    visionRequest: async () => ({ ok: true, candidate_index: 1, confidence: 0.9 }),
    timeoutMs: 7_500,
  });

  assert.equal(clicks, 1);
  assert.equal(result.completed, true);
  assert.equal(result.attempts, 1);
});

test("candidate selected styling does not trigger a second interaction", async () => {
  let clicks = 0;
  let selected = false;
  const page = fakeInputPage({
    dispatch: async ({ type }) => {
      if (type === "mouseReleased") {
        clicks += 1;
        selected = true;
      }
    },
  });
  const challenge = clickChallenge(async () => { throw new Error("synthetic click must not run"); });
  challenge.candidates[1].screenshot = async () => pngBuffer(40, 40, selected ? 2 : 1);
  const result = await attemptAutomaticVerification(page, {
    detectChallenge: async () => challenge,
    isChallengeComplete: async () => false,
    visionRequest: async () => ({ ok: true, candidate_index: 1, confidence: 0.9 }),
    sleep: async () => {},
    timeoutMs: 100,
  });

  assert.equal(clicks, 1);
  assert.equal(result.attempts, 1);
  assert.equal(result.completed, false);
});

test("a genuinely new click challenge permits exactly one second interaction", async () => {
  let active = "a";
  let clicks = 0;
  const page = fakeInputPage({
    dispatch: async ({ type }) => {
      if (type !== "mouseReleased") return;
      clicks += 1;
      if (clicks === 1) active = "b";
    },
  });
  const challengeA = clickChallenge(async () => { throw new Error("synthetic click must not run"); }, "challenge-a");
  const challengeB = clickChallenge(async () => { throw new Error("synthetic click must not run"); }, "challenge-b");

  const result = await attemptAutomaticVerification(page, {
    detectChallenge: async () => (active === "a" ? challengeA : challengeB),
    isChallengeComplete: async () => false,
    visionRequest: async () => ({ ok: true, candidate_index: 1, confidence: 0.9 }),
    sleep: async () => {},
    timeoutMs: 100,
  });

  assert.equal(clicks, 2);
  assert.equal(result.attempts, 2);
});

test("slider piece movement does not trigger a second drag", async () => {
  let drags = 0;
  let moved = false;
  const page = fakeInputPage({
    dispatch: async ({ type }) => {
      if (type === "mousePressed") drags += 1;
      if (type === "mouseReleased") moved = true;
    },
  });
  const challenge = {
    provider: "geetest4",
    challenge_type: "slider",
    handle: fakeHandle(),
    background: fakeHandle({
      box: { x: 10, y: 20, width: 300, height: 120 },
      screenshot: pngBuffer(300, 120, 7),
    }),
    piece: fakeHandle({
      screenshot: () => pngBuffer(40, 40, moved ? 2 : 1),
      evaluate: async (_callback, action) => (
        action?.action === "hide"
          ? { applied: true, id: action.id, value: "", priority: "" }
          : true
      ),
    }),
  };

  const result = await attemptAutomaticVerification(page, {
    detectChallenge: async () => challenge,
    isChallengeComplete: async () => false,
    visionRequest: async () => ({ ok: true, distance_css: 100, confidence: 0.9 }),
    random: () => 0.5,
    sleep: async () => {},
    timeoutMs: 100,
  });

  assert.equal(drags, 1);
  assert.equal(result.attempts, 1);
});

test("automatic verification reports the total-stage timeout", async () => {
  const challenge = clickChallenge(async () => {});
  const startedAt = Date.now();

  const result = await attemptAutomaticVerification({}, {
    detectChallenge: async () => challenge,
    fingerprintChallenge: async () => new Promise(() => {}),
    timeoutMs: 5,
  });

  assert.equal(result.reason, "timeout");
  assert.ok(Date.now() - startedAt < 100);
});

test("monotonic action deadline wins when the timeout callback cannot run", async () => {
  const result = await attemptAutomaticVerification({}, {
    detectChallenge: async () => null,
    sleep: async () => {},
    timeoutMs: 20,
  });

  assert.equal(result.completed, false);
  assert.equal(result.reason, "timeout");
});

test("Turnstile clicks at most once per attempt without calling vision", async () => {
  let syntheticClicks = 0;
  let visionCalls = 0;
  const inputEvents = [];
  const page = fakeInputPage({ dispatch: async (event) => { inputEvents.push(event); } });
  const challenge = {
    provider: "turnstile",
    challenge_type: "checkbox",
    checkbox: fakeHandle({ click: async () => { syntheticClicks += 1; } }),
    response: fakeHandle({ value: "" }),
  };

  const result = await attemptAutomaticVerification(page, {
    detectChallenge: async () => challenge,
    fingerprintChallenge: async () => "turnstile-checkbox",
    isChallengeComplete: async () => inputEvents.some(({ type }) => type === "mouseReleased"),
    visionRequest: async () => { visionCalls += 1; },
    turnstileResponseWaitMs: 0,
  });

  assert.equal(result.completed, true);
  assert.equal(syntheticClicks, 0);
  assert.deepEqual(inputEvents.map(({ type }) => type), [
    "mouseMoved",
    "mousePressed",
    "mouseReleased",
  ]);
  assert.equal(inputEvents.every(({ pointerType }) => pointerType === "mouse"), true);
  assert.equal(visionCalls, 0);
});

test("Turnstile cutoff sends no new press after a hanging pointer move", async () => {
  const controller = new AbortController();
  const lifecycle = [];
  let observeMove;
  let observeDetach;
  const moved = new Promise((resolve) => { observeMove = resolve; });
  const detached = new Promise((resolve) => { observeDetach = resolve; });
  const rejectAfter = (delay, label) => new Promise((_, reject) => {
    const timer = setTimeout(() => reject(new Error(`${label} timed out`)), delay);
    timer.unref?.();
  });
  const page = fakeInputPage({
    dispatch: async ({ type }) => {
      lifecycle.push(type);
      if (type === "mouseMoved") {
        observeMove();
        return new Promise(() => {});
      }
      return undefined;
    },
    detach: async () => {
      lifecycle.push("detach");
      observeDetach();
    },
  });
  const challenge = {
    provider: "turnstile",
    challenge_type: "checkbox",
    checkbox: fakeHandle({
      fingerprint: "turnstile-checkbox",
      click: async () => { throw new Error("synthetic click must not run"); },
    }),
    response: fakeHandle({ value: "" }),
  };
  const verification = attemptAutomaticVerification(page, {
    signal: controller.signal,
    detectChallenge: async () => challenge,
    turnstileResponseWaitMs: 0,
    timeoutMs: 200,
  });

  await Promise.race([moved, rejectAfter(500, "mouse move")]);
  controller.abort();
  await Promise.race([detached, rejectAfter(500, "session detach")]);
  const result = await verification;

  assert.equal(result.reason, "aborted");
  assert.equal(lifecycle.includes("mouseMoved"), true);
  assert.equal(lifecycle.includes("mousePressed"), false);
  assert.equal(lifecycle.includes("detach"), true);
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(lifecycle.includes("mousePressed"), false);
});

test("Turnstile passive token completion does not count as an interaction", async () => {
  let clicks = 0;
  const challenge = {
    provider: "turnstile",
    challenge_type: "checkbox",
    checkbox: fakeHandle({ click: async () => { clicks += 1; } }),
    response: fakeHandle({ value: "completed-token", box: null }),
  };

  const result = await attemptAutomaticVerification({}, {
    detectChallenge: async () => challenge,
  });

  assert.equal(result.completed, true);
  assert.equal(result.attempted, false);
  assert.equal(result.attempts, 0);
  assert.equal(clicks, 0);
});
