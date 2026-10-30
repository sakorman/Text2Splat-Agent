import test from "node:test";
import assert from "node:assert/strict";
import { acceptsJobUpdate } from "../src/main/resources/static/graphics/job-updates.mjs";

const job = (id, status) => ({ id, status });

test("progress and cancellation can advance the currently selected job", () => {
  for (const status of ["RECONSTRUCTING", "CANCELLING", "READY", "FAILED", "CANCELLED"])
    assert.equal(acceptsJobUpdate(job("a", "MULTIVIEW"), job("a", status), "a"), true);
});

test("a late cancellation response cannot reopen any terminal state", () => {
  for (const state of ["READY", "CONCEPT_READY", "NEEDS_REVIEW", "FAILED", "CANCELLED"])
    for (const incoming of ["REFERENCE", "CANCELLING", "CANCELLED"])
      assert.equal(acceptsJobUpdate(job("a", state), job("a", incoming), "a"), false);
});

test("updates must belong to both the original request and the current job", () => {
  assert.equal(acceptsJobUpdate(job("b", "REFERENCE"), job("a", "CANCELLING"), "a"), false);
  assert.equal(acceptsJobUpdate(job("a", "REFERENCE"), job("b", "CANCELLED"), "a"), false);
  assert.equal(acceptsJobUpdate(null, job("a", "CANCELLED"), "a"), false);
  assert.equal(acceptsJobUpdate(job("a", "REFERENCE"), null, "a"), false);
});

test("cancellation cannot be rewound by older progress but can still finish", () => {
  assert.equal(acceptsJobUpdate(job("a", "CANCELLING"), job("a", "REFERENCE"), "a"), false);
  for (const status of ["CANCELLING", "CANCELLED", "FAILED", "READY"])
    assert.equal(acceptsJobUpdate(job("a", "CANCELLING"), job("a", status), "a"), true);
});
