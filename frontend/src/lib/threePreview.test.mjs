import assert from "node:assert/strict";
import test from "node:test";
import * as THREE from "three";

import { frameModelPreviewObject } from "./threePreview.js";

function projectedBounds(object, camera) {
  const box = new THREE.Box3().setFromObject(object);
  camera.updateMatrixWorld(true);
  const points = [];
  for (const x of [box.min.x, box.max.x]) {
    for (const y of [box.min.y, box.max.y]) {
      for (const z of [box.min.z, box.max.z]) {
        points.push(new THREE.Vector3(x, y, z).project(camera));
      }
    }
  }
  return points;
}

for (const [name, dimensions] of [
  ["立方体", [1, 1, 1]],
  ["竖直长条", [1, 100, 1]],
  ["水平长条", [100, 1, 1]],
  ["扁平板件", [60, 0.1, 45]],
  ["微小零件", [0.001, 0.001, 0.001]],
]) {
  test(`${name} 在方形、横向和窄屏预览中完整入镜并保留边距`, () => {
    for (const aspect of [1, 4 / 3, 0.55, 2.4]) {
      for (const withControls of [false, true]) {
        const object = new THREE.Mesh(new THREE.BoxGeometry(...dimensions));
        object.position.set(500, -230, 1200);
        const camera = new THREE.PerspectiveCamera(45, aspect, 0.01, 1000);
        const grid = new THREE.GridHelper(10, 20);
        const controls = withControls ? {
          target: new THREE.Vector3(),
          update() { camera.lookAt(this.target); },
        } : null;
        const home = frameModelPreviewObject(THREE, object, camera, controls, grid);
        const points = projectedBounds(object, camera);
        for (const point of points) {
          assert.ok(Math.abs(point.x) <= 0.9 + 1e-8, `${aspect}: x=${point.x}`);
          assert.ok(Math.abs(point.y) <= 0.9 + 1e-8, `${aspect}: y=${point.y}`);
          assert.ok(point.z > -1 && point.z < 1, `${aspect}: z=${point.z}`);
        }
        assert.ok(Math.max(...points.map((point) => Math.max(Math.abs(point.x), Math.abs(point.y)))) > 0.7,
          "模型不能因固定最小尺寸而变成难以辨认的小点");
        assert.deepEqual(home.target.toArray(), [0, 0, 0]);
        assert.deepEqual(home.position.toArray(), camera.position.toArray());
        assert.ok(new THREE.Box3().setFromObject(object).getCenter(new THREE.Vector3()).length() < 1e-8);
        assert.ok(Math.abs(grid.position.y + dimensions[1] / 2) < 1e-6);
        assert.ok(camera.near > 0 && camera.far > camera.near);
        object.geometry.dispose();
        object.material.dispose();
        grid.geometry.dispose();
        grid.material.dispose();
      }
    }
  });
}
