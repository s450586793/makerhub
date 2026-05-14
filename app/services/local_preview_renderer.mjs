import fs from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";


const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT_DIR = path.resolve(__dirname, "..", "..");
const FRONTEND_NODE_MODULES = path.join(ROOT_DIR, "frontend", "node_modules");
const requireFromFrontend = createRequire(path.join(FRONTEND_NODE_MODULES, "package.json"));
const puppeteer = requireFromFrontend("puppeteer-core");
const THREE_MODULE_URL = pathToFileURL(path.join(FRONTEND_NODE_MODULES, "three", "build", "three.module.js")).href;
const THREE_ADDONS_URL = pathToFileURL(path.join(FRONTEND_NODE_MODULES, "three", "examples", "jsm")).href + "/";
const STL_LOADER_URL = pathToFileURL(path.join(FRONTEND_NODE_MODULES, "three", "examples", "jsm", "loaders", "STLLoader.js")).href;
const OBJ_LOADER_URL = pathToFileURL(path.join(FRONTEND_NODE_MODULES, "three", "examples", "jsm", "loaders", "OBJLoader.js")).href;
const THREE_MF_LOADER_URL = pathToFileURL(path.join(FRONTEND_NODE_MODULES, "three", "examples", "jsm", "loaders", "3MFLoader.js")).href;

function parseArgs(argv) {
  const args = {};
  for (let index = 2; index < argv.length; index += 1) {
    const item = argv[index];
    if (!item.startsWith("--")) {
      continue;
    }
    const key = item.slice(2);
    const next = argv[index + 1] || "";
    if (next && !next.startsWith("--")) {
      args[key] = next;
      index += 1;
    } else {
      args[key] = "1";
    }
  }
  return args;
}

async function resolveChromiumExecutable() {
  const candidates = [
    process.env.MAKERHUB_CHROMIUM_PATH,
    process.env.CHROMIUM_PATH,
    process.env.PUPPETEER_EXECUTABLE_PATH,
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
  ];
  for (const candidate of candidates) {
    if (!candidate) {
      continue;
    }
    try {
      await fs.access(candidate);
      return candidate;
    } catch {
      continue;
    }
  }
  throw new Error("Chromium executable not found");
}

function escapeScriptValue(value) {
  return JSON.stringify(String(value || ""));
}

function buildRendererHtml({ inputPath, inputName, outputPath, size }) {
  const inputUrl = pathToFileURL(inputPath).href;
  const outputUrl = pathToFileURL(outputPath).href;
  return `<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>MakerHub Preview Renderer</title>
  <style>
    html, body { margin: 0; width: 100%; height: 100%; overflow: hidden; background: #111827; }
    canvas { display: block; width: ${size}px; height: ${size}px; }
  </style>
</head>
<body>
<canvas id="preview" width="${size}" height="${size}"></canvas>
<script type="importmap">
{
  "imports": {
    "three": ${JSON.stringify(THREE_MODULE_URL)},
    "three/addons/": ${JSON.stringify(THREE_ADDONS_URL)}
  }
}
</script>
<script type="module">
const inputUrl = ${escapeScriptValue(inputUrl)};
const inputName = ${escapeScriptValue(inputName)};
const outputUrl = ${escapeScriptValue(outputUrl)};
const size = ${Number(size) || 720};
const THREE = await import(${escapeScriptValue(THREE_MODULE_URL)});

function normalizeModelPreviewObject(object) {
  object.updateMatrixWorld(true);
  object.traverse?.((item) => {
    if (item?.isMesh) {
      if (item.geometry && !item.geometry.getAttribute("normal")) {
        item.geometry.computeVertexNormals();
      }
      if (!item.material) {
        item.material = new THREE.MeshStandardMaterial({
          color: 0xdfe7ef,
          roughness: 0.55,
          metalness: 0.04,
        });
      }
      item.castShadow = false;
      item.receiveShadow = false;
    }
  });
  return object;
}

async function loadObject() {
  const name = String(inputName || inputUrl || "").toLowerCase();
  if (name.endsWith(".3mf")) {
    const { ThreeMFLoader } = await import(${escapeScriptValue(THREE_MF_LOADER_URL)});
    const loader = new ThreeMFLoader();
    return normalizeModelPreviewObject(await loader.loadAsync(inputUrl));
  }
  if (name.endsWith(".obj")) {
    const { OBJLoader } = await import(${escapeScriptValue(OBJ_LOADER_URL)});
    const loader = new OBJLoader();
    const object = await loader.loadAsync(inputUrl);
    object.traverse((item) => {
      if (item?.isMesh) {
        item.material = new THREE.MeshStandardMaterial({
          color: 0xdfe7ef,
          roughness: 0.55,
          metalness: 0.04,
        });
      }
    });
    return normalizeModelPreviewObject(object);
  }
  const { STLLoader } = await import(${escapeScriptValue(STL_LOADER_URL)});
  const loader = new STLLoader();
  const geometry = await loader.loadAsync(inputUrl);
  geometry.computeVertexNormals();
  const material = new THREE.MeshStandardMaterial({
    color: 0xdfe7ef,
    roughness: 0.55,
    metalness: 0.04,
  });
  return new THREE.Mesh(geometry, material);
}

function frameObject(object, camera, grid) {
  const box = new THREE.Box3().setFromObject(object);
  const modelSize = new THREE.Vector3();
  const center = new THREE.Vector3();
  box.getSize(modelSize);
  box.getCenter(center);
  object.position.sub(center);
  const maxDim = Math.max(modelSize.x, modelSize.y, modelSize.z, 1);
  const distance = maxDim * 1.7;
  camera.near = Math.max(distance / 100, 0.01);
  camera.far = Math.max(distance * 12, 1000);
  camera.position.set(maxDim * 1.18, maxDim * 1.05, maxDim * 1.45);
  camera.lookAt(0, 0, 0);
  camera.updateProjectionMatrix();
  if (grid) {
    grid.scale.setScalar(maxDim / 10);
    grid.position.y = -modelSize.y / 2;
  }
}

async function writePng(dataUrl) {
  await window.__makerhubWriteBase64File(outputUrl, dataUrl.split(",", 2)[1] || "");
}

try {
  const canvas = document.getElementById("preview");
  const renderer = new THREE.WebGLRenderer({
    antialias: true,
    alpha: false,
    canvas,
    preserveDrawingBuffer: true,
  });
  renderer.setPixelRatio(1);
  renderer.setSize(size, size, false);
  renderer.outputColorSpace = THREE.SRGBColorSpace;

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x111827);
  const camera = new THREE.PerspectiveCamera(45, 1, 0.01, 1000);
  const ambient = new THREE.HemisphereLight(0xffffff, 0x26313f, 2.2);
  scene.add(ambient);
  const keyLight = new THREE.DirectionalLight(0xffffff, 2.3);
  keyLight.position.set(3, 5, 4);
  scene.add(keyLight);
  const fillLight = new THREE.DirectionalLight(0x9fd4ff, 0.85);
  fillLight.position.set(-4, 2, -3);
  scene.add(fillLight);
  const grid = new THREE.GridHelper(10, 20, 0x4b5563, 0x273244);
  grid.material.transparent = true;
  grid.material.opacity = 0.32;
  scene.add(grid);

  const object = await loadObject();
  scene.add(object);
  frameObject(object, camera, grid);
  renderer.render(scene, camera);
  await writePng(canvas.toDataURL("image/png", 0.92));
  window.__MAKERHUB_RENDER_RESULT__ = { ok: true };
} catch (error) {
  window.__MAKERHUB_RENDER_RESULT__ = {
    ok: false,
    message: error instanceof Error ? error.message : String(error || "render failed"),
  };
}
</script>
</body>
</html>`;
}

async function main() {
  const args = parseArgs(process.argv);
  const inputPath = path.resolve(String(args.input || ""));
  const outputPath = path.resolve(String(args.output || ""));
  const inputName = String(args.name || path.basename(inputPath));
  const size = Math.min(Math.max(Number(args.size || 720), 256), 1200);

  if (!inputPath || !outputPath) {
    throw new Error("Usage: local_preview_renderer.mjs --input <model> --output <png> [--name name]");
  }
  await fs.access(inputPath);
  const executablePath = await resolveChromiumExecutable();
  const htmlPath = path.join(path.dirname(outputPath), `.makerhub-preview-${Date.now()}-${Math.random().toString(16).slice(2)}.html`);
  await fs.writeFile(htmlPath, buildRendererHtml({ inputPath, inputName, outputPath, size }), "utf8");

  const browser = await puppeteer.launch({
    executablePath,
    headless: "new",
    args: [
      "--no-sandbox",
      "--disable-setuid-sandbox",
      "--disable-dev-shm-usage",
      "--enable-webgl",
      "--ignore-gpu-blocklist",
      "--use-angle=swiftshader",
      "--allow-file-access-from-files",
      "--disable-web-security",
    ],
  });
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: size, height: size, deviceScaleFactor: 1 });
    await page.exposeFunction("__makerhubWriteBase64File", async (targetUrl, base64Data) => {
      const targetPath = fileURLToPath(targetUrl);
      await fs.writeFile(targetPath, Buffer.from(String(base64Data || ""), "base64"));
      return true;
    });
    await page.goto(pathToFileURL(htmlPath).href, { waitUntil: "networkidle0", timeout: 45000 });
    await page.waitForFunction("window.__MAKERHUB_RENDER_RESULT__ !== undefined", { timeout: 45000 });
    const result = await page.evaluate(() => window.__MAKERHUB_RENDER_RESULT__);
    if (!result?.ok) {
      throw new Error(result?.message || "Three.js render failed");
    }
  } finally {
    await browser.close();
    await fs.rm(htmlPath, { force: true });
  }

  const stat = await fs.stat(outputPath);
  if (!stat.size) {
    throw new Error("Preview image is empty");
  }
  process.stdout.write(JSON.stringify({ ok: true, output: outputPath, size: stat.size }) + "\\n");
}

main().catch((error) => {
  process.stderr.write((error instanceof Error ? error.message : String(error || "render failed")) + "\\n");
  process.exit(1);
});
