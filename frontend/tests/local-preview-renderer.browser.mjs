import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import * as THREE from "three";
import { STLExporter } from "three/addons/exporters/STLExporter.js";
import { OBJExporter } from "three/addons/exporters/OBJExporter.js";
import { strToU8, zipSync } from "three/addons/libs/fflate.module.js";

const root = fileURLToPath(new URL("../../", import.meta.url));
const execute = promisify(execFile);
const modelXml = `<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><resources><object id="1" type="model"><mesh><vertices><vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/><vertex x="0" y="10" z="0"/><vertex x="0" y="0" z="10"/></vertices><triangles><triangle v1="0" v2="2" v3="1"/><triangle v1="0" v2="1" v3="3"/><triangle v1="0" v2="3" v3="2"/><triangle v1="1" v2="2" v3="3"/></triangles></mesh></object></resources><build><item objectid="1"/></build></model>`;
const relationships = `<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>`;

test("离线 renderer 通过真实 Chromium 加载本地 STL、OBJ 和 3MF 并生成 PNG", {timeout:90000}, async () => {
  const temp = await mkdtemp(path.join(os.tmpdir(), "makerhub-预览 #1?-"));
  const mesh = new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1));
  const stl = new STLExporter().parse(mesh, {binary:true});
  const inputs = {
    stl: Buffer.from(stl.buffer),
    obj: new OBJExporter().parse(mesh),
    "3mf": zipSync({"_rels/.rels":strToU8(relationships),"3D/3dmodel.model":strToU8(modelXml)}),
  };
  try {
    for (const [extension, bytes] of Object.entries(inputs)) {
      const input = path.join(temp, `模型 #1?.${extension}`);
      const output = path.join(temp, `${extension}.png`);
      await writeFile(input, bytes);
      const {stdout} = await execute(process.execPath, ["app/services/local_preview_renderer.mjs", "--input", input, "--output", output], {
        cwd:root, timeout:25000, env:process.env,
      });
      assert.match(stdout, /"ok":true/);
      const png = await readFile(output);
      assert.equal(png.subarray(0,8).toString("hex"), "89504e470d0a1a0a", extension);
      assert.equal(png.readUInt32BE(16), 720);
      assert.equal(png.readUInt32BE(20), 720);
      assert.ok(png.length > 1000, `${extension}: 预览不应为空白`);
    }
  } finally {
    mesh.geometry.dispose();
    mesh.material.dispose();
    await rm(temp, {recursive:true,force:true});
  }
});
