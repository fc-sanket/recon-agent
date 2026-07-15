#!/usr/bin/env python3
"""Headless runner — same agent logic as auto_recon.py but no TUI. Logs to file."""
import asyncio, json, os, sys, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# load env
for p in [Path(__file__).parent / ".env", Path.home() / ".env"]:
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

import anthropic
from auto_recon import (
    SYSTEM, TOOLS, DISPATCH, MODEL,
    _COST_IN, _COST_OUT, _COMPACT_AT, _COMPACT_KEEP,
    _est, _compact, _clean_messages, _is_tool_result,
    _trim_old_results, _SYSTEM_CACHED,
    SESSION_DIR, _save, _load, _sessions,
    _fetch_whoxy_reverse, _whoxy_usage_str,
)
import auto_recon as _ar

TOOL_TIMEOUT = 300  # 5 min hard cap per tool call


def _log_fn(log_path: Path):
    def log(msg):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        print(line, end="", flush=True)
        with log_path.open("a") as f:
            f.write(line)
    return log


async def main():
    LOG = Path(__file__).parent / "headless_recon.log"
    LOG.write_text("")  # clear on each new run
    log = _log_fn(LOG)

    prompt = " ".join(sys.argv[1:]) or "do recon on iocl.com"
    client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    sid = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    messages = []
    meta = {"sid": sid}

    tok_in = tok_out = 0

    # snapshot WhoXY reverse balance at scan start for credit diff at end
    whoxy_start = _fetch_whoxy_reverse()
    if whoxy_start is not None:
        _ar._WHOXY_REV_START = whoxy_start

    log(f"Starting: {prompt}")
    log(f"Session:  {sid}")
    log(f"Model:    {MODEL}")
    if whoxy_start is not None:
        log(f"WhoXY reverse balance at start: {whoxy_start:,}")
    messages.append({"role": "user", "content": prompt})

    for turn in range(300):
        if turn == 280:
            log("⚠  Turn 280/300 — approaching limit. Wrap up: write final report if all phases done.")
        # trim old tool results to keep history lean
        messages = _trim_old_results(messages)

        # ── compaction ────────────────────────────────────────────────────
        if _est(messages) > _COMPACT_AT:
            log(f"Compacting context (~{_est(messages):,} chars)...")
            messages = await _compact(messages, client)
            log("Compaction done.")

        # ── API call with retry (wraps full async with block) ─────────────
        final = None
        text_buf = []  # init before attempt loop so it's always defined
        for attempt in range(3):
            try:
                async with client.messages.stream(
                    model=MODEL, max_tokens=16384,
                    system=_SYSTEM_CACHED, tools=TOOLS,
                    messages=messages,
                ) as stream:
                    text_buf = []
                    async for chunk in stream.text_stream:
                        text_buf.append(chunk)
                    final = await stream.get_final_message()
                break  # success
            except anthropic.RateLimitError:
                log(f"Rate limited (attempt {attempt+1}/3) — waiting 60s")
                await asyncio.sleep(60)
            except anthropic.APIStatusError as e:
                log(f"API ERROR {e.status_code} (attempt {attempt+1}/3): {e.message}")
                if attempt < 2:
                    await asyncio.sleep(10)
                else:
                    break
            except Exception as e:
                log(f"Network error (attempt {attempt+1}/3): {type(e).__name__}: {e}")
                if attempt < 2:
                    await asyncio.sleep(10)
                else:
                    break

        if final is None:
            log("Giving up after 3 failed attempts")
            break

        tok_in  += final.usage.input_tokens
        tok_out += final.usage.output_tokens
        cost = tok_in * _COST_IN + tok_out * _COST_OUT
        log(
            f"Turn {turn+1} | stop={final.stop_reason} | "
            f"{final.usage.input_tokens:,}in {tok_in:,}total_in {tok_out:,}out ${cost:.3f}"
        )

        if text_buf:
            preview = "".join(text_buf)[:300].replace("\n", " ")
            log(f"  Agent: {preview}")

        # build assistant message
        content_dicts = []
        for block in final.content:
            if block.type == "text":
                content_dicts.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                content_dicts.append({"type": "tool_use", "id": block.id,
                                       "name": block.name, "input": block.input})
        messages.append({"role": "assistant", "content": content_dicts})

        if final.stop_reason == "end_turn":
            log("Agent finished (end_turn)")
            _save(sid, messages, meta)
            whoxy_str = _whoxy_usage_str()
            if whoxy_str:
                log(f"WhoXY usage:{whoxy_str}")
            break

        if final.stop_reason == "tool_use":
            tool_blocks = [b for b in final.content if b.type == "tool_use"]
            n = len(tool_blocks)
            if n > 1:
                log(f"  ▸ {n} parallel tool calls this turn")

            async def _run_one(block):
                name = block.name
                inp  = block.input or {}
                if name == "bash":
                    cmd_preview = inp.get("command", "").strip().splitlines()[0][:100]
                    log(f"  ▸ bash: {cmd_preview}")
                elif name in ("read_file", "write_file"):
                    log(f"  ▸ {name}: {inp.get('path', '')}")
                else:
                    log(f"  ▸ {name}: {str(inp)[:80]}")
                handler = DISPATCH.get(name)
                if handler:
                    try:
                        result = await asyncio.wait_for(
                            asyncio.get_event_loop().run_in_executor(None, handler, inp),
                            timeout=TOOL_TIMEOUT,
                        )
                    except asyncio.TimeoutError:
                        result = f"[TOOL TIMEOUT after {TOOL_TIMEOUT}s] Command killed."
                        log(f"  ! TIMEOUT on {name}")
                    except Exception as e:
                        result = f"[TOOL ERROR] {e}"
                else:
                    result = f"[UNKNOWN TOOL] {name}"
                return {"type": "tool_result", "tool_use_id": block.id, "content": result}

            results = await asyncio.gather(*[_run_one(b) for b in tool_blocks])
            messages.append({"role": "user", "content": list(results)})
            _save(sid, messages, meta)

    cost = tok_in * _COST_IN + tok_out * _COST_OUT
    log(f"DONE — {tok_in:,} in / {tok_out:,} out / ${cost:.4f}")
    _save(sid, messages, meta)


if __name__ == "__main__":
    asyncio.run(main())
