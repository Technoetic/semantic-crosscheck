# Cube HTTP response limits

The normal Cube client and synthetic Cube replay apply the same response-byte
limit and elapsed acceptance budget. A response that fails either check cannot
become an equal comparison, a surviving mutant, or a confirmed witness.

## Configuration

`CubeClient(base_url, secret, timeout_s=120.0,
max_response_bytes=16 * 1024 * 1024)` accepts:

- `timeout_s`: a finite positive numeric value convertible to a finite positive
  float. Booleans, strings, zero, negative values, infinities, NaN, and conversion
  overflow are rejected with `ValueError` before transport setup. The runtime
  supplies this from `[cube].query_timeout_s`.
- `max_response_bytes`: a positive integer, excluding booleans. The default is
  **16 MiB (16,777,216 bytes)** per HTTP response, including HTTP error responses.
  This is a conservative default, not a measured workload requirement. Direct
  Python callers can override it in the constructor; there is no corresponding
  TOML or CLI option.

The byte allowance applies to the HTTP entity yielded by urllib, before JSON
decoding. Responses of exactly the limit can succeed; the next byte is a
`CubeResponseError`. Both successful and HTTP-error bodies use the same bounded
reader. It requests at most 64 KiB at a time and stops after at most limit + 1
source bytes. The reader closes the response on rejection as well as success.

Redirects are disabled: the loopback Cube REST adapter does not require them.
The client never follows a `Location` target, and urllib's default unbounded
redirect-body drain is bypassed. A 30x response is handled as an HTTP error using
the same bounded reader: a body within the cap reports the HTTP status, while
an oversized body reports `invalid_response`. This applies to normal loads,
replay, and metadata requests. Readiness returns `False` for redirects.

`Content-Length` is not trusted as a substitute for counting received bytes.
Missing lengths and chunked responses receive the same cap. A body that ends
before a valid declared length is rejected even if the received prefix is valid
JSON. HTTP framing still belongs to urllib: when a shorter declared length ends
at a complete JSON document, wire bytes beyond that entity are not part of the
document and cannot be counted or decoded by this reader. Invalid chunk framing
reported by the body reader is a response error.

This is **not a total-memory limit**. Socket and HTTP metadata buffering, a copy
of the accumulated bytes, decoded strings, and the parsed JSON object also use
memory. The source-byte cap does not impose an independent JSON parsing CPU cap.

## Elapsed acceptance budget

Each `load()` call and replay load establishes one monotonic deadline, shared
across request preparation, transport, response reads, JSON decoding, all
`Continue wait` polls, poll sleeps, and final response acceptance. Poll sleeps
are capped to the remaining time. A successful result returned after the budget
is rejected, including results from custom `_req` implementations. Wall-clock
time is used for JWT claims, not elapsed query accounting.

`meta()` and direct `_req()` calls establish their own budget. The private
override signature is now `_req(path, payload=None, *, deadline=None)`; custom
subclasses and test doubles must accept the keyword. A `TypeError` is propagated
without retrying the request without its deadline. `ready()` remains a separate
boolean health probe with a five-second socket timeout.

The opener receives a socket timeout of at most the lesser of 30 seconds and the
remaining operation time. Checks run before and after each public `read1` call,
after decoding, and before the caller accepts the final answer. Socket timeouts
are normalized to `CubeError` with the safe `timeout` report category. Oversize,
incomplete, and malformed load responses use `CubeResponseError` and the
`invalid_response` category. Small HTTP-error diagnostics preserve the existing
500-character exception text, but structured comparison reports retain only the
HTTP status; replay reports retain only a safe runtime failure reason.

## Cancellation limitations

This is an **elapsed acceptance budget, not hard wall-clock cancellation**.
DNS resolution, connection attempts, response-header parsing, and
chunk-size/trailer parsing may remain inside urllib beyond the deadline. A
socket retains the timeout assigned when opened, so a later idle read can also
outlast the remaining budget. Checks reject late results when control returns;
a pathological peer can prevent it returning promptly. No private socket access
or background client worker is used.

Finite loopback experiments on Windows with Python 3.12 reproduced these limits:
a 0.25-second idle socket timeout admitted a roughly 0.9-second drip-fed body in
the former implementation. Public `read1` plus elapsed checks rejected slow
bodies, while slow headers and chunk framing still returned control after the
budget. Those observations justify acceptance checks without claiming transport
termination. Automated regressions use a synchronized local server, finite
handlers, and server/thread cleanup; the supported platform matrix remains a CI
verification responsibility.

The proxy-disabled opener, strict duplicate-key/nonfinite JSON rejection, exact
decimal decoding, row limits, and replay provenance checks remain in force.
