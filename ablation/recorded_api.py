"""HTTP attempts and usage accounting for path and prompt experiments."""
import json
import math
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def save_json(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, indent=2, ensure_ascii=False,
                                   default=lambda x: sorted(x) if isinstance(x, set) else str(x)),
                          encoding="utf-8")


def usage_fields(provider, body):
    """Output includes reasoning; never add reasoning a second time when pricing."""
    u = body.get("usageMetadata" if provider == "gemini" else "usage") or {}
    if provider == "openai":
        inp, out = u.get("input_tokens"), u.get("output_tokens")
        cache = (u.get("input_tokens_details") or {}).get("cached_tokens")
        reason = (u.get("output_tokens_details") or {}).get("reasoning_tokens")
        visible = out - reason if out is not None and reason is not None else None
    elif provider == "deepseek":
        inp, out = u.get("prompt_tokens"), u.get("completion_tokens")
        cache = u.get("prompt_cache_hit_tokens")
        reason = (u.get("completion_tokens_details") or {}).get("reasoning_tokens")
        visible = out - reason if out is not None and reason is not None else None
    else:
        inp, total = u.get("promptTokenCount"), u.get("totalTokenCount")
        # Google omits zero-valued optional cache counters. No explicit caches are created.
        cache = u.get("cachedContentTokenCount", 0) if u else None
        out = total - inp if total is not None and inp is not None else None
        reason, visible = u.get("thoughtsTokenCount"), u.get("candidatesTokenCount")
    result = dict(input_tokens=inp, cached_input_tokens=cache,
                  billable_output_tokens=out, reasoning_tokens=reason,
                  visible_output_tokens=visible)
    if any(v is not None and (not isinstance(v, int) or v < 0) for v in result.values()):
        raise ValueError("Invalid usage counter")
    if inp is not None and cache is not None and cache > inp:
        raise ValueError("Cached input exceeds input")
    return result


def tariff(provider, started):
    if provider == "openai":
        return dict(input=0.75, cached=0.075, output=4.50, period="standard")
    if provider == "gemini":
        return dict(input=0.75, cached=0.075, output=3.75, period="through_2026_12_31")
    dt = datetime.fromisoformat(started).astimezone(timezone.utc)
    # This campaign is dated 28 October 2026, outside Chinese public holidays.
    peak = dt.weekday() < 5 and (1 <= dt.hour < 4 or 6 <= dt.hour < 10)
    return dict(input=1.32 if peak else 0.66, cached=0.044 if peak else 0.022,
                output=3.96 if peak else 1.98, period="peak" if peak else "off_peak")


def cost_fields(usage, rates):
    i, c, o = (usage.get(k) for k in
               ("input_tokens", "cached_input_tokens", "billable_output_tokens"))
    no_cache = (i * rates["input"] + o * rates["output"]) / 1e6 if i is not None and o is not None else None
    observed = ((i-c)*rates["input"]+c*rates["cached"]+o*rates["output"])/1e6 if None not in (i,c,o) else None
    return dict(estimated_usd=observed, no_cache_usd=no_cache)


class RecordedAPI:
    def __init__(self, run_dir, provider, model, api_key, schema, gemini_schema,
                 previous_cost=0, transport=None):
        self.directory = Path(run_dir)
        (self.directory / "requests").mkdir(parents=True, exist_ok=True)
        self.provider, self.model, self.key = provider, model, api_key
        self.schema, self.gemini_schema = schema, gemini_schema
        self.previous_cost, self.start = previous_cost, time.perf_counter()
        self.lock, self.stopped = threading.Lock(), threading.Event()
        self.logical = 0
        self.events = []
        self.http = httpx.Client(timeout=httpx.Timeout(300, connect=30),
                                 transport=transport, follow_redirects=False,
                                 limits=httpx.Limits(max_connections=5))

    def request_parts(self, system, user):
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        if self.provider == "openai":
            return ("https://api.openai.com/v1/responses", {"Authorization": f"Bearer {self.key}"},
                    {"model": self.model, "input": messages, "store": False,
                     "text": {"format": {"type": "json_schema", "name": "batch_response",
                                          "strict": True, "schema": self.schema}}})
        if self.provider == "deepseek":
            return ("https://api.deepseek.com/beta/chat/completions", {"Authorization": f"Bearer {self.key}"},
                    {"model": self.model, "messages": messages, "stream": False,
                     "tools": [{"type": "function", "function": {
                         "name": "submit_candidate_evaluations", "strict": True,
                         "description": "Return INCLUDE or EXCLUDE decisions with confidence for every numbered candidate.",
                         "parameters": self.schema}}]})
        return (f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
                {"x-goog-api-key": self.key},
                {"systemInstruction": {"parts": [{"text": system}]},
                 "contents": [{"role": "user", "parts": [{"text": user}]}],
                 "generationConfig": {"responseMimeType": "application/json",
                                      "responseSchema": self.gemini_schema}})

    def parse(self, body, count):
        if self.provider == "openai":
            if body.get("status") != "completed":
                raise ValueError("Response not completed")
            text = "".join(c["text"] for m in body.get("output", [])
                           for c in m.get("content", []) if c.get("type") == "output_text")
        elif self.provider == "deepseek":
            choices = body.get("choices", [])
            if len(choices) != 1 or choices[0].get("finish_reason") != "tool_calls":
                raise ValueError("Expected one complete tool-call response")
            calls = choices[0]["message"].get("tool_calls") or []
            if len(calls) != 1 or calls[0]["function"]["name"] != "submit_candidate_evaluations":
                raise ValueError("Expected the extraction tool")
            text = calls[0]["function"]["arguments"]
        else:
            choices = body.get("candidates", [])
            if len(choices) != 1 or choices[0].get("finishReason") != "STOP":
                raise ValueError("Expected one completed candidate response")
            text = "".join(p.get("text", "") for p in choices[0].get("content", {}).get("parts", [])
                           if not p.get("thought"))
        decisions = json.loads(text)["evaluations"]
        if any(type(d.get("idx")) is not int for d in decisions):
            raise ValueError("Decision indices must be integers")
        if sorted(d.get("idx") for d in decisions) != list(range(1, count+1)):
            raise ValueError("Missing, duplicate, or out-of-range decision indices")
        for decision in decisions:
            if decision.get("classification") not in ("INCLUDE", "EXCLUDE"):
                raise ValueError("Invalid classification")
            conf = decision.get("confidence")
            if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not math.isfinite(conf) or not 0 <= conf <= 1:
                raise ValueError("Invalid confidence")
        return sorted(decisions, key=lambda d: d["idx"])

    def guard(self):
        spent = sum(e.get("estimated_usd") if e.get("estimated_usd") is not None
                    else (e.get("no_cache_usd") or 0) for e in self.events)
        if spent >= 5 or spent + self.previous_cost >= 25 or time.perf_counter()-self.start >= 5400:
            self.stopped.set()
        if self.stopped.is_set():
            raise RuntimeError("Run stopped after error or dispatch limit")

    def infer(self, system, user, candidates, metadata):
        if not 1 <= len(candidates) <= 20:
            raise ValueError("Each request must contain between one and 20 candidates")
        with self.lock:
            self.guard()
            self.logical += 1
            logical = self.logical
        url, headers, payload = self.request_parts(system, user)
        prefix = self.directory / "requests" / f"{logical:05d}"
        save_json(str(prefix)+"_request.json", dict(endpoint=url, body=payload,
                  metadata=metadata, candidate_ids=candidates))
        for attempt in range(1, 4):
            with self.lock:
                self.guard()
            start, began = time.perf_counter(), utc_now()
            event = dict(logical_request=logical, attempt=attempt, candidate_count=len(candidates),
                         candidate_ids=candidates, metadata=metadata, started_utc=began,
                         started_monotonic=start, valid=False, usage={})
            event.update(estimated_usd=None, no_cache_usd=None)
            result = None
            retryable = True
            try:
                response = self.http.post(url, headers=headers, json=payload)
                event["http_completed_monotonic"] = time.perf_counter()
                event["http_latency_seconds"] = event["http_completed_monotonic"]-start
                event.update(status_code=response.status_code, request_id=response.headers.get("x-request-id"))
                # Keep the response bytes even if JSON/schema parsing subsequently fails.
                Path(str(prefix)+f"_attempt{attempt}_response.json").write_text(
                    response.text.replace(self.key, "[REDACTED]"), encoding="utf-8")
                retryable = response.status_code in (408, 409, 429) or response.status_code >= 500 or response.is_success
                body = response.json()
                event.update(response_id=body.get("id") or body.get("responseId"),
                             served_model=body.get("model") or body.get("modelVersion"),
                             service_tier=body.get("service_tier"))
                try:
                    event["usage"] = usage_fields(self.provider, body)
                    event["tariff"] = tariff(self.provider, began)
                    event.update(cost_fields(event["usage"], event["tariff"]))
                except (ValueError, TypeError) as exc:
                    event["usage_error"] = str(exc)
                if not response.is_success:
                    raise ValueError(f"HTTP {response.status_code}")
                result = self.parse(body, len(candidates))
                event["valid"] = True
            except Exception as exc:
                event["error"] = str(exc).replace(self.key, "[REDACTED]")[:500]
            finally:
                event.update(ended_utc=utc_now(), ended_monotonic=time.perf_counter())
                event["latency_seconds"] = event["ended_monotonic"]-start
                with self.lock:
                    self.events.append(event)
                    with (self.directory / "attempts.jsonl").open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(event, ensure_ascii=False)+"\n")
                    save_json(self.directory / "telemetry.json", self.summary())
                    if len(self.events) % 10 == 0:
                        print(f"HTTP attempts={len(self.events)} estimated USD={self.summary()['known_estimated_usd']:.4f}", flush=True)
            if result is not None:
                return result
            if not retryable or attempt == 3:
                self.stopped.set()
                raise RuntimeError(f"Request {logical} failed: {event.get('error')}")
            time.sleep(2 ** attempt)

    def summary(self):
        e = self.events
        return dict(provider=self.provider, requested_model=self.model,
                    logical_requests=self.logical, http_attempts=len(e),
                    retry_attempts=sum(x["attempt"] > 1 for x in e),
                    valid_responses=sum(x["valid"] for x in e),
                    classified_decisions=sum(x["candidate_count"] for x in e if x["valid"]),
                    known_estimated_usd=sum(x["estimated_usd"] or 0 for x in e),
                    known_no_cache_usd=sum(x["no_cache_usd"] or 0 for x in e),
                    cost_missing_attempts=sum(x["estimated_usd"] is None for x in e),
                    served_models=sorted({x["served_model"] for x in e if x.get("served_model")}),
                    token_sums={k: sum(x["usage"].get(k) or 0 for x in e) for k in
                                ("input_tokens", "cached_input_tokens", "billable_output_tokens", "reasoning_tokens", "visible_output_tokens")},
                    token_field_coverage={k: sum(x["usage"].get(k) is not None for x in e) for k in
                                ("input_tokens", "cached_input_tokens", "billable_output_tokens", "reasoning_tokens", "visible_output_tokens")})

    def close(self):
        self.http.close()
        save_json(self.directory / "telemetry.json", self.summary())
