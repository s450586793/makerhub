export function frameModelPreviewObject(THREE, object, camera, controls = null, grid = null) {
  const box = new THREE.Box3().setFromObject(object);
  if (box.isEmpty() || ![...box.min.toArray(), ...box.max.toArray()].every(Number.isFinite)) {
    throw new Error("Model geometry has no finite bounds");
  }
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());
  object.position.sub(center);
  object.updateMatrixWorld(true);
  box.translate(center.negate());

  const maxDim = Math.max(size.x, size.y, size.z, Number.EPSILON);
  const direction = new THREE.Vector3(1.18, 1.05, 1.45).normalize();
  const target = new THREE.Vector3();
  camera.position.copy(direction);
  camera.lookAt(target);
  const inverseRotation = camera.quaternion.clone().invert();
  const verticalLimit = Math.tan(THREE.MathUtils.degToRad(camera.fov) / 2) * 0.9 / camera.zoom;
  const horizontalLimit = verticalLimit * camera.aspect;
  let distance = 0;
  // 在相机坐标中逐角约束透视投影，给任意比例的模型保留 5% 画布边距。
  for (const x of [box.min.x, box.max.x]) {
    for (const y of [box.min.y, box.max.y]) {
      for (const z of [box.min.z, box.max.z]) {
        const corner = new THREE.Vector3(x, y, z).applyQuaternion(inverseRotation);
        distance = Math.max(distance,
          corner.z + Math.abs(corner.x) / horizontalLimit,
          corner.z + Math.abs(corner.y) / verticalLimit);
      }
    }
  }
  distance = Math.max(distance, maxDim / 1000);
  camera.position.copy(direction.multiplyScalar(distance));
  camera.near = Math.max(distance / 1000, Number.EPSILON);
  camera.far = distance * 20;
  camera.updateProjectionMatrix();
  camera.updateMatrixWorld(true);

  if (controls) {
    controls.target.copy(target);
    controls.minDistance = maxDim * 0.22;
    controls.maxDistance = Math.max(distance * 4, maxDim * 8);
    controls.update();
  }
  if (grid) {
    grid.scale.setScalar(maxDim / 10);
    grid.position.y = -size.y / 2;
  }
  return { position: camera.position.clone(), target, maxDim };
}
