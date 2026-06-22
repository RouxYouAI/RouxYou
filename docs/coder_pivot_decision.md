# Coder Pivot — Decision Doc (2026-06-05)

## Problem
The trust-ledger graduation bottleneck is the **coder's code quality**, NOT the chain or the gates (which are flawless — they correctly blocked 3 bad attempts on 2026-06-05). `qwen2.5-coder:14b` fumbles: drops `@contextmanager` decorators, defaults to full-file `write_file` overwrite on edits. It "cuts it" for Claude-curated runs (we banked code-tier 7→8) but NOT for *reliable autonomous* code generation (Roux self-improving without hand-holding).

## Research findings (June 2026 landscape)
1. **Qwen2.5-Coder-14B (current) is still "the top-rated local coding model 2026"** (~85% HumanEval). The **32B** is the bigger sibling — *same family/schema* (pipeline-native, zero adapter), better quality.
2. **Qwen3-Coder has NO small variant.** Only `30B-A3B` + `480B-A35B` (both MoE). **A "qwen3-coder-9B" DOES NOT EXIST** — the 7B/9B are *general* Qwen3, not coder-tuned. (HF community is actively requesting a 30–70B dense coder; not released.) So the *only* Qwen3-Coder that fits a 16GB card is the 30B-A3B we tested.
3. **qwen3-coder-30B-A3B is AGENTIC-TUNED** — built to read-a-file-then-emit-a-diff, 256K context, repo-scale. The problems we hit (nested `tasks→steps` plans, unified-diff `patch` content, instruction misses) are **inherent to its agentic design clashing with our one-shot pipeline — NOT a bug, and NOT from MoE.** DJ's MoE-vs-dense hypothesis is a red herring; a hypothetical 9B coder would share the same *format* behavior.
4. **The field has moved to AGENTIC** (plan → act → observe → fix; "Self-Edit" = fault-aware secondary editing with execution feedback). Our one-shot "complete plan WITH code up front" is the *older* paradigm. Modern coders are *designed* for agentic pipelines — which is exactly why they fight ours.

## DECISION — DJ's 4→3→2 ordering, VALIDATED
The best local coders are agentic-tuned → **align our pipeline to them, don't bolt per-model adapters onto a mismatched pipeline** (architecture beats bolt-ons).

- **#4 FIRST — Agentic refactor (the foundation).** Make the coder pipeline **read-then-edit** (plan → act → observe → iterate) to MATCH how modern coders work. Unblocks qwen3-coder *and* every future agentic coder in one move. This is why 4-before-2 is right.
- **#3 — Code-fix / Self-Edit loop (literature-backed).** generate → compile/run → feed errors back → fix, *before* bigbrain. **Model-agnostic** reliability boost; catches exactly the decorator/syntax fumbles that blocked us tonight.
- **#2 — qwen3-coder via llama-server (NOT ollama).** Once the pipeline is agentic + has the fix-loop, qwen3-coder slots in (it's built for this). Serve via llama-server for the correct chat template — ollama's template likely caused the instruction misses we saw.
- **#1 — ~~Qwen2.5-Coder-32B-Instruct~~ → CUT 2026-06-05 (DJ's call).** Rationale for cutting: it's a DENSE 32B — at Q4 (~19GB) it spills heavily to CPU on the 16GB card, and dense means ALL 32B activate per token → most tokens drag through CPU-resident layers → single-digit tok/s if it runs at all, and it would blow `CODER_TIMEOUT=300s` on real files. Its ONLY advantage was current-pipeline schema-fit, and that evaporates as #4/#3 make the pipeline agentic-friendly. **Decision: there is no dense-32B stopgap. Qwen3-Coder-30B-A3B (MoE, ~3B active, 38–69 tok/s measured) is the coder across #3/#2/#1** — its sparse activation is the only thing that's actually fast on this card. #1's slot is now free for **adjudication** (tier-0; lets edit/patch tasks count toward graduation) or rolls into the #2 work.

## Additional candidates to verify (not yet deep-researched)
- **"Qwen3.6-35B-A3B"** — flagged in search for 16GB agentic coding w/ native tool-calling; newer than Jan-2026 knowledge → verify it exists + benchmarks.
- **DeepSeek-Coder-V2-Lite** (16B MoE, fits 16GB) — strong coder; verify schema/agentic-fit.
- Codestral-22B, Gemma-4-26B-MoE — general/code, lower priority.

## ✅ #2 — qwen3-coder via llama-server: TESTED 2026-06-05 (core thesis VALIDATED; edit-path + smoke-robustness need iteration)
Infra built: `serve_qwen3coder.sh` (llama-server :8091, Vulkan, `--jinja` embedded template, ncmoe 20 → 11.8GB used / 4GB free / 37 t/s) + `llamacpp_coder` provider in `shared/llm.py` + `config.yaml`. **Direct tool-call test (8s):** proper `submit_plan` in the CANONICAL schema (`action`/`details`/`content`) + correct, complete code WITH `@contextmanager` — one-shot. **→ The decision-doc hypothesis was RIGHT: ollama's chat template caused the foreign-schema/instruction-misses; `--jinja` (the model's own template) fixes both.** Full-pipeline A/B:
- ✅ **CASE 2 (new @contextmanager file): qwen3-coder keeps the decorator NATIVELY one-shot** (both flags off AND on) — strictly better than qwen2.5 (which drops it). The content-gen win is real.
- ⚠️ **CASE 1 (edit existing file): rough through our pipeline** — flags-off = recon-only (planned read_file, no patch); flags-on (agentic) = patch_file with EMPTY content. qwen3-coder's agentic edit style (read→THEN→emit-edit-in-a-later-turn) doesn't yet mesh with our patch content-gen. NEEDS work on the edit/patch path before adoption.
- ⚠️ **#3 smoke-test false-failure:** on CASE 2 ON the model-WRITTEN smoke test was itself buggy (assumed `timed_block` yields an object with `.label` → AttributeError on None) → 2 wasted regens → fail-soft to bigbrain (code was actually fine). #3 incr2 needs a guard: distinguish "bad test" from "bad code" (e.g. persistent-identical-error + structurally-fine code ⇒ suspect the test; or simpler/validated tests).
- **DECISION:** core thesis validated (qwen3-coder IS the better coder; llama-server+jinja is the right serving). Kept all infra; **coder alias REVERTED to qwen2.5 resting baseline** (qwen3-coder edit-path not ready to be the default). NEXT before adoption: (a) fix the edit/patch path for qwen3-coder's agentic style, (b) harden #3 smoke-test robustness. To run qwen3-coder: `serve_qwen3coder.sh` up + v53 evicted + set alias `llamacpp_coder/qwen3-coder`.

## Separate lever — ADJUDICATION (tier-0 = Claude+DJ)
Independent of the coder: add adjudication ("APPROVE + no-revert in N days → confirmed_good") so successful **PATCHES** count toward graduation (currently only net-new files do; 4 `unknown`s are stuck). Force-multiplier — lets edit-tasks graduate.

## Sources
- localllm.in/blog/best-local-llms-16gb-vram · insiderllm.com/guides/best-local-coding-models-2026 · sitepoint.com/best-local-llm-models-2026
- github.com/QwenLM/Qwen3-Coder · huggingface.co/Qwen/Qwen3-Coder-480B-A35B-Instruct/discussions/6 (no small variant)
- arxiv 2508.00083 (Survey on Code Generation with LLM-based Agents) · arxiv 2504.15228 (A Self-Improving Coding Agent)
