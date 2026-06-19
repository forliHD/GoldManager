# Agent 04 — Decision & Scoring

> Baut FeatureAggregator, ScoringEngine, AIDecisionLayer (OpenRouter/MiniMax BYOK), RuleBasedFallback, TradeQualificationEngine.

## Ownership
`src/xauusd_bot/decision/`

## Deliverables
1. **FeatureAggregator** — sammelt alle Feature-Outputs in ein `feature_snapshot` (Pydantic, siehe Plan §12-JSON-Beispiel der Research).
2. **ScoringEngine** — 0–100, Gewichtung: H1-Zone 20 / M5 15 / TripleVWAP 15 / **HTF Volume Profile 20** / Session-Liq 10 / News 10 / Momentum 10. Teilscores einzeln ausweisen. HTF-VP-Subscore: Yearly 8 / Monthly 5 / Weekly 4 / Acceptance-Qualität 3.
3. **AIDecisionLayer** — OpenRouter-Client, siehe unten.
4. **RuleBasedFallback** — deterministische Entscheidung aus Score + harten Regeln. **Sicherheitsautoritativ.**
5. **TradeQualificationEngine** — kombiniert KI/Fallback-Entscheidung mit aktuellen Risiko-/Kontoparametern → finaler Enter/No-Enter + Entry-Typ (Scout/reduziert/voll).

## AIDecisionLayer — Spec
- OpenAI-kompatibler Client → `https://openrouter.ai/api/v1/chat/completions`.
- `OPENROUTER_API_KEY` + `OPENROUTER_MODEL` aus Env (Default: MiniMax via BYOK; Modell-String beim Setup verifizieren). ZDR-Routing aktiv.
- System-Prompt aus `runtime_prompts/decision_agent.md` laden (nicht hardcoden).
- Input: `feature_snapshot` + Score (kein Rohpreisverlauf, keine Erfindungsfreiheit).
- Output: striktes JSON, mit Pydantic validiert. Ungültig → 1 Retry → `no_trade`.
- **Aufruf nur ab Score-Schwelle** (z.B. ≥65), nicht jeder Bar. Timeout (z.B. 8 s) → Fallback.
- LLM berechnet **nie** Lotgröße/SL/TP-Preise — nur Empfehlung, Invalidations, Management-Hints, Begründung.

## Schwellen-Logik
<55 kein Trade · 55–64 beobachten · 65–74 vorbereiten · 75–84 reduzierter Entry · ≥85 voller Entry. High-Impact-News → Score gedeckelt oder Entry geblockt (Fallback erzwingt das, unabhängig vom LLM).

## Sicherheits-Override
Wenn LLM-Empfehlung gegen eine harte Regel verstößt (News-Blackout, Spread-Limit, Tages-/Wochenverlust erreicht), gewinnt der RuleBasedFallback → im Zweifel `no_trade`. Diskrepanz LLM↔Fallback wird ins Journal geloggt (für Review).

## System Prompt (für MiniMax)
```
Du baust den Decision-Layer des XAUUSD-Bots. Implementiere FeatureAggregator (-> feature_snapshot
Pydantic), ScoringEngine (0-100 mit den vorgegebenen Gewichten, Teilscores einzeln), AIDecisionLayer
(OpenRouter OpenAI-kompatibel, Modell + Key aus Env, ZDR aktiv, System-Prompt aus
runtime_prompts/decision_agent.md, striktes JSON via Pydantic, 1 Retry dann no_trade, Aufruf nur ab
Score>=65, Timeout->Fallback), RuleBasedFallback (deterministisch, SICHERHEITSAUTORITATIV) und
TradeQualificationEngine. Das LLM operiert nur auf feature_snapshot+Score und berechnet NIEMALS
Lotgröße/SL/TP - das ist Sache der Execution-Engine. Bei Konflikt LLM vs harte Regel gewinnt der
Fallback, im Zweifel no_trade, und die Diskrepanz wird geloggt.
```
