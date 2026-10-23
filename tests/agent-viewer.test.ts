import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

test("embedded ArkUI roots keep distinct pointers and honor their host expansion", () => {
  const source = readFileSync(new URL("../viewer/app.js", import.meta.url), "utf8");
  const context = vm.createContext({ console });
  vm.runInContext(source.replace(/initialize\(\);\s*$/, ""), context);
  const result = vm.runInContext(`
    const local = { attributes: { type: "root", ID: "0" }, children: [] };
    const remote = { attributes: { type: "root", ID: "0" }, children: [
      { attributes: { type: "Text", ID: "1" }, children: [] }
    ] };
    local.embedded_trees = [{ roots: [remote] }];
    indexTree(local, "");
    const rootEntry = state.entries[0];
    const embeddedEntry = state.entries[1];
    const childEntry = state.entries[2];
    const collapsed = state.entries.filter(isVisibleEntry).map(entry => entry.pointer);
    state.expanded.add("");
    const hostOpen = state.entries.filter(isVisibleEntry).map(entry => entry.pointer);
    state.expanded.add(embeddedEntry.pointer);
    const allOpen = state.entries.filter(isVisibleEntry).map(entry => entry.pointer);
    JSON.stringify({ collapsed, hostOpen, allOpen, embedded: embeddedEntry.embedded,
      count: rootEntry.childCount, childParent: childEntry.parentPointer,
      distinct: state.byPointer.size, hasGeometry: "geometry" in embeddedEntry.node });
  `, context);
  const actual = JSON.parse(result);
  assert.deepEqual(actual.collapsed, [""]);
  assert.deepEqual(actual.hostOpen, ["", "/embedded_trees/0/roots/0"]);
  assert.deepEqual(actual.allOpen, ["", "/embedded_trees/0/roots/0", "/embedded_trees/0/roots/0/children/0"]);
  assert.equal(actual.embedded, true);
  assert.equal(actual.childParent, "/embedded_trees/0/roots/0");
  assert.equal(actual.count, 1);
  assert.equal(actual.distinct, 3);
  assert.equal(actual.hasGeometry, false);
});
