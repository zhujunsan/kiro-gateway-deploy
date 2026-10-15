import { describe, it } from "node:test";
import assert from "node:assert/strict";

import { presignPut } from "../src/oss_sign.js";

// Frozen against an independent Python HMAC implementation of the same
// Aliyun V4 URL-signing steps (see the comment in this file's history / the
// design doc). Changing the canonical request on purpose requires updating
// this vector.
const FIXED = {
  accessKeyId: "AKIDEXAMPLE",
  accessKeySecret: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
  bucket: "examplebucket",
  region: "cn-hangzhou",
  endpoint: "oss-cn-hangzhou.aliyuncs.com",
  key: "audit/v1/dt=2026-09-30/user=alice/20260930T120000Z-abcd1234.req.jsonl.zst",
  now: new Date("2024-12-03T03:44:20Z"),
  expires: 900,
};

describe("presignPut", () => {
  it("matches the frozen V4 signature and forces overwrite protection", async () => {
    const signed = await presignPut(FIXED);
    assert.equal(
      signed.canonicalRequest,
      [
        "PUT",
        "/examplebucket/audit/v1/dt%3D2026-09-30/user%3Dalice/20260930T120000Z-abcd1234.req.jsonl.zst",
        "x-oss-additional-headers=host&x-oss-credential=AKIDEXAMPLE%2F20241203%2Fcn-hangzhou%2Foss%2Faliyun_v4_request&x-oss-date=20241203T034420Z&x-oss-expires=900&x-oss-signature-version=OSS4-HMAC-SHA256",
        "content-type:application/octet-stream",
        "host:examplebucket.oss-cn-hangzhou.aliyuncs.com",
        "x-oss-forbid-overwrite:true",
        "",
        "host",
        "UNSIGNED-PAYLOAD",
      ].join("\n"),
    );
    assert.equal(
      signed.url,
      "https://examplebucket.oss-cn-hangzhou.aliyuncs.com/audit/v1/dt%3D2026-09-30/user%3Dalice/20260930T120000Z-abcd1234.req.jsonl.zst?x-oss-additional-headers=host&x-oss-credential=AKIDEXAMPLE%2F20241203%2Fcn-hangzhou%2Foss%2Faliyun_v4_request&x-oss-date=20241203T034420Z&x-oss-expires=900&x-oss-signature-version=OSS4-HMAC-SHA256&x-oss-signature=4969bdefd385a629f131648af6691e8cd507dd6c64f9920422273727079a3cfb",
    );
    assert.deepEqual(signed.headers, {
      "content-type": "application/octet-stream",
      "x-oss-forbid-overwrite": "true",
    });
  });
});
