// OSS Signature Version 4 presigned PUT.
//
// Canonical request follows Aliyun's "add signatures to URLs" V4 spec:
// the URI includes the bucket as the first segment even though the URL itself
// is virtual-hosted, the payload hash is UNSIGNED-PAYLOAD, and x-oss-signature
// is added only after the signature is computed.
//
// x-oss-forbid-overwrite is an x-oss-* header, so it is part of the canonical
// headers whenever the client sends it. host is not, so it is listed in
// x-oss-additional-headers. A PUT that drops either header will not match.

const TEXT = new TextEncoder();

/** Percent-encode the way OSS UriEncode does: uppercase hex, unreserved set only. */
export function uriEncode(value) {
  return encodeURIComponent(String(value)).replace(/[!'()*]/g, (ch) =>
    `%${ch.charCodeAt(0).toString(16).toUpperCase()}`,
  );
}

function toHex(bytes) {
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function hmac(keyBytes, data) {
  const cryptoKey = await crypto.subtle.importKey(
    "raw",
    keyBytes,
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", cryptoKey, TEXT.encode(data));
  return new Uint8Array(sig);
}

async function sha256Hex(text) {
  const digest = await crypto.subtle.digest("SHA-256", TEXT.encode(text));
  return toHex(new Uint8Array(digest));
}

function pad(n) {
  return String(n).padStart(2, "0");
}

export function formatOssDate(date) {
  return (
    `${date.getUTCFullYear()}${pad(date.getUTCMonth() + 1)}${pad(date.getUTCDate())}` +
    `T${pad(date.getUTCHours())}${pad(date.getUTCMinutes())}${pad(date.getUTCSeconds())}Z`
  );
}

/**
 * Build a presigned PUT URL for one object.
 *
 * @param {object} opts
 * @param {string} opts.accessKeyId
 * @param {string} opts.accessKeySecret
 * @param {string} opts.bucket
 * @param {string} opts.region  e.g. cn-hangzhou
 * @param {string} opts.endpoint  e.g. oss-cn-hangzhou.aliyuncs.com (no scheme)
 * @param {string} opts.key  object key without a leading slash
 * @param {Date} opts.now
 * @param {number} [opts.expires] seconds, default 900
 * @returns {Promise<{url: string, headers: { "x-oss-forbid-overwrite": string, "content-type": string }}>}
 */
export async function presignPut(opts) {
  const expires = opts.expires ?? 900;
  const dateTime = formatOssDate(opts.now);
  const dateStamp = dateTime.slice(0, 8);
  const host = `${opts.bucket}.${opts.endpoint}`;
  const credential = `${opts.accessKeyId}/${dateStamp}/${opts.region}/oss/aliyun_v4_request`;
  const segments = [opts.bucket, ...String(opts.key).split("/").filter((s) => s.length > 0)];
  const canonicalUri = `/${segments.map(uriEncode).join("/")}`;

  const signedHeaders = [
    ["content-type", "application/octet-stream"],
    ["host", host],
    ["x-oss-forbid-overwrite", "true"],
  ];
  const canonicalHeaders = signedHeaders.map(([k, v]) => `${k}:${v}\n`).join("");
  const queryPairs = [
    ["x-oss-additional-headers", "host"],
    ["x-oss-credential", credential],
    ["x-oss-date", dateTime],
    ["x-oss-expires", String(expires)],
    ["x-oss-signature-version", "OSS4-HMAC-SHA256"],
  ];
  const canonicalQuery = queryPairs.map(([k, v]) => `${uriEncode(k)}=${uriEncode(v)}`).join("&");
  const canonicalRequest = [
    "PUT",
    canonicalUri,
    canonicalQuery,
    `${canonicalHeaders}`,
    "host",
    "UNSIGNED-PAYLOAD",
  ].join("\n");
  // canonicalHeaders already ends with \n, and join inserts another \n before
  // "host", which is the required blank line after the header block.

  const scope = `${dateStamp}/${opts.region}/oss/aliyun_v4_request`;
  const stringToSign = `OSS4-HMAC-SHA256\n${dateTime}\n${scope}\n${await sha256Hex(canonicalRequest)}`;

  let key = await hmac(TEXT.encode(`aliyun_v4${opts.accessKeySecret}`), dateStamp);
  key = await hmac(key, opts.region);
  key = await hmac(key, "oss");
  key = await hmac(key, "aliyun_v4_request");
  const signature = toHex(await hmac(key, stringToSign));

  const url =
    `https://${host}${canonicalUri.slice(opts.bucket.length + 1)}` +
    `?${canonicalQuery}&x-oss-signature=${signature}`;
  // canonicalUri is /bucket/key...; the virtual-host URL path is /key...
  return {
    url,
    headers: {
      "content-type": "application/octet-stream",
      "x-oss-forbid-overwrite": "true",
    },
    canonicalRequest,
  };
}
