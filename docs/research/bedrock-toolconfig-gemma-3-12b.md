# Bedrock native tool-use (toolConfig) support for Gemma 3 12B IT

**Bottom line:** Gemma 3 12B IT is available today as a first-party Amazon Bedrock foundation model (model ID `google.gemma-3-12b-it`) — not Marketplace, not custom model import. It supports the `Converse`/`ConverseStream` API on the `bedrock-runtime` endpoint, and AWS's own docs show it supports client-side tool calling and Bedrock's "Structured outputs" feature (including "strict" `toolConfig` tool definitions) on `bedrock-runtime`. So the harness's state-machine step should be able to use native `toolConfig` for Gemma 3 12B IT — the existing `handle_bedrock_model` "dead code" branch for `toolUse` can plausibly be turned on for this model instead of the current prompted-JSON-extraction hack, though AWS's per-model docs stop short of an explicit, unambiguous "Tool use: Supported" checkbox for the `bedrock-runtime`/Converse surface (see caveat in §2/§3).

Research date: **2026-09-19**. Bedrock's model catalog, Marketplace listings, and feature-support docs change frequently (new models added, feature flags flipped) — treat this as a point-in-time snapshot, not a permanent guarantee. Re-verify against the live model card before relying on it in production.

---

## 1. Is Gemma 3 12B IT available on Bedrock, and how?

**Yes — as a native Bedrock foundation model in the base model catalog**, not via Marketplace and not via custom model import.

- The Bedrock "Models at a glance" catalog page lists it directly under the Google provider row alongside Gemma 4 and other Gemma 3 models:
  > "**Gemma 3:** [Gemma 3 12B IT], [Gemma 3 27B PT], [Gemma 3 4B IT]"
  Source: https://docs.aws.amazon.com/bedrock/latest/userguide/models.html (the catalog table; this is also what `conversation-inference-supported-models-features.html` now redirects to)

- It has its own first-party model card page (the same URL pattern AWS uses for foundation models like Claude, Nova, Llama — not the Marketplace listing pattern):
  https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-google-gemma-3-12b-it.html

  Exact quote from that page:
  > "Gemma 3 12B IT is Google's 12-billion parameter open model with instruction tuning, supporting text and image inputs with a 128K context window."
  > Model ID: `google.gemma-3-12b-it`
  > Endpoints supported: `bedrock-runtime` (yes), `bedrock-mantle` (yes)
  > APIs supported: Converse (yes), Invoke (yes), Chat Completions (yes), Responses (no), Embedding (no), Audio (no)

- Regional availability table on the same page shows it deployed in-region across many commercial regions (us-east-1, us-east-2, us-west-2, eu-west-1, eu-central-1, ap-south-1, ap-northeast-1, etc.) — the profile of a managed foundation model, not a per-account Marketplace subscription.

- This appears to be a relatively recent change: an earlier AWS "What's New" announcement, "Amazon Bedrock adds 18 fully managed open weight models, the largest expansion of new models to date" (Dec 2025), announced Google Gemma 3 models moving into Bedrock's fully-managed foundation-model catalog:
  https://aws.amazon.com/about-aws/whats-new/2025/12/amazon-bedrock-fully-managed-open-weight-models/
  (This confirms Gemma 3 12B is one of the models covered by that "fully managed" expansion, consistent with it now having its own model-card page rather than only a Marketplace listing.)

- Historical note for context: earlier in 2025, **Gemma 3 27B** was announced as available only via **Bedrock Marketplace** and SageMaker JumpStart (AWS ML blog, "Gemma 3 27B model now available on Amazon Bedrock Marketplace and Amazon SageMaker JumpStart"). As of today's model-card catalog, Gemma 3 27B PT is likewise now listed on the native model-card page pattern (`model-card-google-gemma-3-27b-pt.html`), suggesting the whole Gemma 3 family has since moved (or been mirrored) into the first-party foundation-model catalog rather than staying Marketplace-only. I did not separately re-verify 27B's Marketplace listing status since the question is scoped to 12B IT; flagging this only as corroborating context for the "family moved off Marketplace" pattern.

**Not found**: no separate Bedrock Marketplace listing page or custom-model-import doc reference for Gemma 3 12B IT was found (or needed) — it's a native catalog model, so (b) and (c) from the question don't apply to it today.

## 2. Does Converse's `toolConfig` work for Gemma 3 12B IT?

**Likely yes, with one documentation ambiguity worth flagging.**

Evidence for "yes":
- The model card lists `Converse` = Supported under the `bedrock-runtime` endpoint (the endpoint where `Converse`/`ConverseStream` actually live — confirmed generally: "The Converse API is available on the `bedrock-runtime` endpoint only," https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference.html).
- The model card's `bedrock-mantle` feature table explicitly shows:
  > Supported: "Projects", "Client-side tool calling"
  > Not Supported: "Server-side tool calling"
  (https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-google-gemma-3-12b-it.html)
- AWS's general tool-use doc states client-side tool use (the `toolConfig`/`tools` mechanism, as opposed to Bedrock-executed server-side tools) is a cross-API capability:
  > "Client-side tool use | Your application code, after the model returns a tool-call request. | Most use cases. Available with the Responses, Chat Completions, Converse, and InvokeModel APIs."
  https://docs.aws.amazon.com/bedrock/latest/userguide/tool-use.html
- The model card's `bedrock-runtime` feature table shows **"Structured outputs" = Supported**. AWS's structured-outputs doc explicitly ties this to `toolConfig`:
  > "Structured outputs is a capability on Amazon Bedrock that ensures model responses conform to user-defined JSON schemas **and tool definitions**..."
  > "Strict tool use: Add the `strict: true` flag to tool definitions to enable schema validation on tool names and inputs."
  > Supported APIs table: "Converse and ConverseStream APIs | Yes | Conversational inference."
  https://docs.aws.amazon.com/bedrock/latest/userguide/structured-output.html
  This means Bedrock's own "strict tool use" examples are written directly in terms of a Converse `toolConfig.tools[].toolSpec` payload — and that surface is marked supported for Gemma 3 12B IT on `bedrock-runtime`.

The ambiguity: the model card's per-endpoint capability tables are organized as two separate checklists — one for `bedrock-mantle` (which explicitly lists "Client-side tool calling: Supported") and one for `bedrock-runtime` (which lists streaming/guardrails/eval/prompt-management/flows/agents/structured-outputs, but does **not** include an explicit "tool use" line item either way). AWS's older, more granular "Supported models and model features" per-Converse-feature table (which used to enumerate System prompts / Document chat / Vision / **Tool use** / Streaming tool use / Guardrails per model) now **redirects to the general "Models at a glance" catalog page** rather than rendering that old table — i.e. that specific comparison table appears to have been retired/restructured in AWS's current docs, and per-model tool-use support is now only inferable from the model card's feature checklists plus the general tool-use/structured-output docs, not stated as a single explicit "Tool use: Yes" row for this model on `bedrock-runtime`.

**Conclusion for the harness**: the balance of evidence (Converse=Supported, Structured-outputs=Supported with `toolConfig`-shaped examples, general client-side-tool-use doc listing Converse as a supported API) points to `toolConfig` working for `google.gemma-3-12b-it` via Converse on `bedrock-runtime`. This is not a 100%-explicit AWS statement, so treat it as "supported based on strong indirect evidence" rather than "AWS explicitly documents this," and verify empirically with a real `converse()` call + `toolConfig` before relying on it in production.

## 3. If native tool-use is supported: limitations found

- **JSON schema subset**: `toolSpec.inputSchema` (and structured-outputs schemas generally) are validated against a **JSON Schema Draft 2020-12 subset**. Supported: `object`/`array`/`string`/`integer`/`number`/`boolean`/`null`, `enum`, `const`, `anyOf`, `allOf` (with limitations), `$ref`/`$def`/`definitions` (internal-only), a fixed set of string `format`s, and `minItems` restricted to 0 or 1. **Not supported**: recursive schemas, external `$ref`, numeric constraints (`minimum`/`maximum`/`multipleOf`), string length constraints (`minLength`/`maxLength`), and `additionalProperties` set to anything other than `false`. Source: https://docs.aws.amazon.com/bedrock/latest/userguide/structured-output.html
- **`toolChoice`**: general Bedrock docs describe `toolConfig.toolChoice` as taking auto/any/tool forms, but AWS explicitly calls out per-model variation for forced tool choice (e.g. Anthropic-specific examples); no Gemma-specific `toolChoice` support statement was found in the pages checked. Treat `toolChoice` support for Gemma 3 12B IT as unverified — test empirically (`auto` should work broadly; forced single-tool `toolChoice` is less consistently supported across non-Anthropic models on Bedrock).
- **Server-side tool use is explicitly NOT supported** for this model (`bedrock-mantle` table: "Server-side tool calling: Not Supported"), so any Bedrock-managed tool execution (Lambda/AgentCore Gateway) path is out — only client-side tool use (harness executes the tool itself and round-trips the result) applies.
- **Streaming + tool-use interaction**: not specifically documented for Gemma on this page; the general docs distinguish "tool use" from "streaming tool use" as separate Converse features historically, and Gemma's own capability table doesn't call out `ConverseStream` + tools explicitly. Verify empirically if the harness needs `ConverseStream` with tool calls.
- **Multi-turn tool-use state handling**: no Gemma-specific caveats found; general Converse tool-use pattern (append `toolUse` output message, then a `user` message containing `toolResult`, then re-call `converse`) is the documented mechanism and is model-agnostic per AWS's client-side tool-use walkthrough (https://docs.aws.amazon.com/bedrock/latest/userguide/tool-use-client-side.html).

## 4. If native tool-use were NOT supported

Not applicable as the primary finding here — Gemma 3 12B IT is a native foundation model with Converse support, not a Marketplace/custom-import model, so the "OpenAI-compatible endpoint only, no toolConfig" scenario described in the task's question 4 does not appear to be the actual situation. Noting for completeness: even if native `toolConfig` turned out to be unreliable in practice, the model card shows Gemma 3 12B IT also supports the **Invoke API** and an **OpenAI-compatible Chat Completions API** on `bedrock-runtime` (`model="google.gemma-3-12b-it"` via `OPENAI_BASE_URL=".../openai/v1"`), which is a viable fallback integration path with its own `tools`/`tool_calls` shape independent of Converse.

## Implication for mitra-service harness

- `chatbot/llm_models/llm_script.py::handle_bedrock_model` already has a dead `toolUse`-reading branch, gated on `tools` always being `None`. Based on this research, Gemma 3 12B IT on Bedrock should support `converse(..., toolConfig={...})` on `bedrock-runtime` — so the recommended next step is to **wire `tools`/`toolConfig` through for the state-machine call and turn that branch on**, replacing the prompted-JSON-text approach that `chatbot/services/response_handlers/common_handler.py::_extract_response_and_reason` has to fragile-parse today.
- Because AWS's docs don't give an explicit, unambiguous "Tool use: Yes" line for this model on the Converse/`bedrock-runtime` surface (only indirect evidence via Structured Outputs + the general client-side-tool-use doc + `bedrock-mantle`'s explicit "Client-side tool calling: Supported"), this should be **validated with a real API call in a dev/staging Bedrock account** before removing the fallback prompted-JSON extraction path. A safe rollout: add `toolConfig`, keep `_extract_response_and_reason`'s string-parse as a fallback for a release or two in case Gemma's tool-call behavior is flaky, then remove the fallback once confidence is established.
- Constrain the state-machine's tool `inputSchema` to the supported JSON Schema subset (no `minLength`/`maxLength`/numeric bounds/recursive refs) since Bedrock will 400 on unsupported schema features.
- Don't rely on Bedrock-managed ("server-side") tool execution for this model — it's explicitly unsupported; the harness's existing "harness runs the tool, sends `toolResult` back" pattern is the correct (and only supported) pattern here anyway.

---

## AWS documentation pages fetched directly for this research

- https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-google-gemma-3-12b-it.html
- https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-google-gemma-3-4b-it.html (comparison/sanity check against a sibling Gemma model)
- https://docs.aws.amazon.com/bedrock/latest/userguide/models.html (catalog — the URL `conversation-inference-supported-models-features.html` currently redirects/resolves here)
- https://docs.aws.amazon.com/bedrock/latest/userguide/apis.html
- https://docs.aws.amazon.com/bedrock/latest/userguide/tool-use.html
- https://docs.aws.amazon.com/bedrock/latest/userguide/tool-use-client-side.html
- https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference.html
- https://docs.aws.amazon.com/bedrock/latest/userguide/structured-output.html
- https://aws.amazon.com/about-aws/whats-new/2025/12/amazon-bedrock-fully-managed-open-weight-models/ (AWS "What's New" announcement, used as corroborating primary-source evidence, not a blog/secondary source)

Secondary sources consulted only to locate the above AWS doc URLs (not cited as authority): AWS ML blog post on Gemma 3 27B Marketplace availability, llmreference.com, modelavailability.com.
