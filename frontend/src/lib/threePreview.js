import { frameModelPreviewObject } from "../../../app/services/model_preview_framing.mjs";

export { frameModelPreviewObject };

const DEFAULT_MODEL_PREVIEW_MAX_BYTES = 24 * 1024 * 1024;
const SNAPSHOT_SIZE = 720;

function formatPreviewSize(bytes) {
  const size = Number(bytes || 0);
  if (!Number.isFinite(size) || size <= 0) {
    return "";
  }
  if (size >= 1024 * 1024 * 1024) {
    return `${(size / 1024 / 1024 / 1024).toFixed(1)} GB`;
  }
  if (size >= 1024 * 1024) {
    return `${(size / 1024 / 1024).toFixed(1)} MB`;
  }
  return `${Math.ceil(size / 1024)} KB`;
}

export async function getModelPreviewFileSize(fileUrl) {
  try {
    const response = await fetch(fileUrl, {
      method: "HEAD",
      credentials: "same-origin",
      cache: "no-store",
    });
    if (!response.ok) {
      return 0;
    }
    return Number(response.headers.get("content-length") || 0);
  } catch {
    return 0;
  }
}

export async function guardModelPreviewFileSize(fileUrl, maxBytes = DEFAULT_MODEL_PREVIEW_MAX_BYTES) {
  const bytes = await getModelPreviewFileSize(fileUrl);
  if (bytes > maxBytes) {
    const label = formatPreviewSize(bytes);
    const limitLabel = formatPreviewSize(maxBytes);
    throw new Error(`模型文件 ${label || "过大"}，超过网页预览上限 ${limitLabel}。`);
  }
  return bytes;
}

export function formatModelPreviewSize(bytes) {
  return formatPreviewSize(bytes);
}

function disposePreviewMaterial(material) {
  if (Array.isArray(material)) {
    for (const item of material) {
      disposePreviewMaterial(item);
    }
    return;
  }
  if (!material) {
    return;
  }
  for (const value of Object.values(material)) {
    if (value?.isTexture) {
      value.dispose();
    }
  }
  material.dispose?.();
}

export function disposeModelPreviewObject(object) {
  if (!object) {
    return;
  }
  object.traverse?.((item) => {
    if (item.geometry) {
      item.geometry.dispose();
    }
    disposePreviewMaterial(item.material);
  });
  if (object.geometry) {
    object.geometry.dispose();
  }
  disposePreviewMaterial(object.material);
}

export function normalizeModelPreviewObject(THREE, object) {
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

export async function loadModelPreviewObject(THREE, fileUrl, fileName = "") {
  const name = String(fileName || fileUrl || "").toLowerCase();
  if (name.endsWith(".3mf")) {
    const { ThreeMFLoader } = await import("three/examples/jsm/loaders/3MFLoader.js");
    const loader = new ThreeMFLoader();
    return normalizeModelPreviewObject(THREE, await loader.loadAsync(fileUrl));
  }
  if (name.endsWith(".obj")) {
    const { OBJLoader } = await import("three/examples/jsm/loaders/OBJLoader.js");
    const loader = new OBJLoader();
    const object = await loader.loadAsync(fileUrl);
    object.traverse((item) => {
      if (item?.isMesh) {
        item.material = new THREE.MeshStandardMaterial({
          color: 0xdfe7ef,
          roughness: 0.55,
          metalness: 0.04,
        });
      }
    });
    return normalizeModelPreviewObject(THREE, object);
  }
  const { STLLoader } = await import("three/examples/jsm/loaders/STLLoader.js");
  const loader = new STLLoader();
  const geometry = await loader.loadAsync(fileUrl);
  geometry.computeVertexNormals();
  const material = new THREE.MeshStandardMaterial({
    color: 0xdfe7ef,
    roughness: 0.55,
    metalness: 0.04,
  });
  return new THREE.Mesh(geometry, material);
}

function createPreviewScene(THREE, { background = 0x111827, withGrid = true } = {}) {
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(background);
  const camera = new THREE.PerspectiveCamera(45, 1, 0.01, 1000);

  const ambient = new THREE.HemisphereLight(0xffffff, 0x26313f, 2.2);
  scene.add(ambient);
  const keyLight = new THREE.DirectionalLight(0xffffff, 2.3);
  keyLight.position.set(3, 5, 4);
  scene.add(keyLight);
  const fillLight = new THREE.DirectionalLight(0x9fd4ff, 0.85);
  fillLight.position.set(-4, 2, -3);
  scene.add(fillLight);

  let grid = null;
  if (withGrid) {
    grid = new THREE.GridHelper(10, 20, 0x4b5563, 0x273244);
    grid.material.transparent = true;
    grid.material.opacity = 0.32;
    scene.add(grid);
  }
  return { scene, camera, grid };
}

export function buildInteractivePreviewScene(THREE, canvas) {
  const renderer = new THREE.WebGLRenderer({
    antialias: true,
    alpha: true,
    canvas,
  });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  return {
    renderer,
    ...createPreviewScene(THREE, { withGrid: true }),
  };
}

export async function renderModelPreviewDataUrl(fileUrl, fileName = "", options = {}) {
  const {
    maxBytes = DEFAULT_MODEL_PREVIEW_MAX_BYTES,
    size = SNAPSHOT_SIZE,
    mimeType = "image/png",
  } = options;
  await guardModelPreviewFileSize(fileUrl, maxBytes);
  const THREE = await import("three");
  if (typeof document === "undefined") {
    throw new Error("当前环境无法生成 Three.js 预览图。");
  }
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const renderer = new THREE.WebGLRenderer({
    antialias: true,
    alpha: false,
    canvas,
    preserveDrawingBuffer: true,
  });
  renderer.setPixelRatio(1);
  renderer.setSize(size, size, false);
  renderer.outputColorSpace = THREE.SRGBColorSpace;

  const { scene, camera, grid } = createPreviewScene(THREE, { withGrid: true });
  const object = await loadModelPreviewObject(THREE, fileUrl, fileName);
  try {
    scene.add(object);
    frameModelPreviewObject(THREE, object, camera, null, grid);
    camera.aspect = 1;
    camera.updateProjectionMatrix();
    renderer.render(scene, camera);
    return canvas.toDataURL(mimeType, 0.92);
  } finally {
    disposeModelPreviewObject(object);
    if (grid) {
      grid.geometry?.dispose?.();
      grid.material?.dispose?.();
    }
    renderer.dispose();
  }
}
