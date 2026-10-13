# Third-party notices

## LiveCaptions-Translator

Source: https://github.com/SakiRinn/LiveCaptions-Translator
Study commit: a6fee12757b15edbeef7c60f8895e0be694801e1 (master at acquisition, 2026-09-16).
Copyright (c) 2024 SakiRinn and other contributors.
License: Apache License, Version 2.0; unmodified license included at
`third_party/LiveCaptions-Translator/LICENSE`.
No upstream NOTICE file is present in the pinned source tree.

Adaptations, made for Ativia on 2026-09-16:

- `tools/CaptionBridge.cs`: adapts `src/utils/LiveCaptionsHandler.cs` window
  identification, scoped AutomationId lookup, cached text element, `Current.Name`,
  minimize/restore and invalid-element recovery. Uses the Windows .NET Framework
  UIAutomation assemblies instead of importing the upstream application. Replaces
  process killing with attach/ownership checks and graceful close, adds idle-ready
  recognition, a bounded startup timeout, JSON pipe, parent monitoring and stop.
- `translation/captions.py`: adapts the EOS, incomplete-tail, idle stabilization and
  UTF-8 length ideas in `Translator.SyncLoop`, `TextUtil` and `Setting`. The Python
  occurrence alignment, versions, bounded retention and all-sentence enumeration are
  Ativia modifications, not a claim of a literal C# port or equivalent behavior.
- `translation/pipeline.py`: adapts the independent scheduling/display and finite
  successful-context mechanisms of `TranslationTaskQueue`, `Caption`,
  `Translator.DisplayLoop`, and `TranslateAPI.OpenAI`. Adds bounded backlog,
  per-occurrence keys, cancellation propagation, stale result checks and context byte caps.

No upstream screenshots, icons, Fluent UI, installer, updater, or database are
included in Ativia. The independent reference checkout is not part of the product.
The local research reports are excluded from this source snapshot. The adaptations above and license are retained.
