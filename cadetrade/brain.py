"""Il cervello: propone ordini in JSON a schema fisso. Non vede la gabbia come regola da rispettare:
la gabbia la applica il codice dopo. Se risponde male, vale come cervello non disponibile."""
import json
import logging
import os
import subprocess

from .config import BRAIN_TIMEOUT_S, CAGE, CORE

log = logging.getLogger("cervello")

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["orders", "reasoning", "planNext"],
    "properties": {
        "orders": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["symbol", "side", "qty", "why"],
            "properties": {"symbol": {"type": "string"}, "side": {"type": "string", "enum": ["buy", "sell"]},
                           "qty": {"type": "number"}, "why": {"type": "string"}}}},
        "reasoning": {"type": "string"},
        "planNext": {"type": "string"},
    },
}

SYSTEM = f"""Sei il gestore di un conto di PAPER TRADING (soldi finti, prezzi veri). Scrivi sempre in italiano semplice.
A ogni sveglia ricevi: il testimone della sveglia precedente (cosa avevi pianificato), lo stato del conto dal broker,
gli indicatori calcolati in codice e la sveglia attuale. Decidi se comprare, vendere o non fare niente.

Regole che il codice applica comunque, qualunque cosa tu proponga (proporre ordini fuori regola è inutile):
nucleo fisso {', '.join(f'{k} ({v})' for k, v in CORE.items())}, più i 1-2 titoli "scelti oggi" che trovi nel contesto
(selezionati dal codice per volumi anomali e slancio: sono i più rischiosi, massimo {int(CAGE.max_pick_weight*100)}% ciascuno);
massimo {CAGE.max_positions} posizioni; massimo {int(CAGE.max_weight*100)}% del conto per titolo del nucleo;
crypto in totale massimo {int(CAGE.max_crypto_weight*100)}%; stop loss automatico a {int(CAGE.stop_loss*100)}% dal carico;
niente short, niente leva; azioni intere, crypto anche frazionarie; nessun ordine nel briefing o sui mercati chiusi;
nessun riacquisto il giorno stesso di un titolo chiuso dallo stop.

Metodo: preferisci poche posizioni capite bene; entra per gradi; prima di ogni acquisto scrivi nel "why" il prezzo a cui
avresti torto. Non inseguire i massimi: i titoli singoli si comprano sui ritracciamenti. La liquidità è una posizione legittima.
Il tuo obiettivo non è fare tanti trade ma battere nel tempo un benchmark semplice (SPY) senza violare regole.
Nel contesto trovi le "lezioni_imparate" dalle tue pagelle settimanali e l'"esperimento_in_corso": seguile, a meno che
i dati di oggi non le smentiscano chiaramente (in quel caso scrivilo nel reasoning). I titoli scelti oggi sono un'opzione,
non un obbligo: la fomo del mercato non è una buona ragione da sola.

Rispondi SOLO con un oggetto JSON: {{"orders":[{{"symbol","side":"buy"|"sell","qty",numero,"why"}}],
"reasoning":"3-5 frasi","planNext":"cosa controllare alla prossima sveglia"}}. Nessun testo fuori dal JSON."""


class BrainError(Exception):
    pass


def validate(obj) -> dict:
    """Controllo rigido dello schema: un campo mancante o una quantità negativa rendono nulla tutta la risposta."""
    if not isinstance(obj, dict):
        raise BrainError("la risposta non è un oggetto JSON")
    for k in ("orders", "reasoning", "planNext"):
        if k not in obj:
            raise BrainError(f"campo mancante: {k}")
    if not isinstance(obj["orders"], list) or not isinstance(obj["reasoning"], str) or not isinstance(obj["planNext"], str):
        raise BrainError("tipi dei campi errati")
    orders = []
    for o in obj["orders"]:
        if not isinstance(o, dict) or not {"symbol", "side", "qty", "why"} <= o.keys():
            raise BrainError(f"ordine malformato: {o}")
        if o["side"] not in ("buy", "sell"):
            raise BrainError(f"lato non valido: {o['side']}")
        qty = o["qty"]
        if isinstance(qty, bool) or not isinstance(qty, (int, float)) or not qty > 0 or qty != qty:
            raise BrainError(f"quantità non valida: {qty}")
        orders.append({"symbol": str(o["symbol"]).upper().strip(), "side": o["side"], "qty": float(qty), "why": str(o["why"])[:300]})
    return {"orders": orders, "reasoning": obj["reasoning"][:1500], "planNext": obj["planNext"][:800]}


def parse_text(text: str) -> dict:
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t[t.find("{"):]
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        raise BrainError("nessun JSON nella risposta")
    try:
        return validate(json.loads(t[start:end + 1]))
    except json.JSONDecodeError as e:
        raise BrainError(f"JSON non valido: {e}")


def build_prompt(context: dict) -> str:
    return "Contesto della sveglia (JSON):\n" + json.dumps(context, ensure_ascii=False, indent=1, default=str)


REVIEW_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["grade", "summary", "lessons", "experiment"],
    "properties": {
        "grade": {"type": "integer"},
        "summary": {"type": "string"},
        "lessons": {"type": "array", "items": {"type": "string"}},
        "experiment": {"type": "string"},
    },
}

REVIEW_SYSTEM = """Sei lo stesso gestore del conto di paper trading, nel giorno della pagella settimanale. Scrivi in italiano semplice.
Ricevi: il diario dei trade chiusi (con la tesi scritta all'acquisto e come è finita), le statistiche, il confronto con SPY,
i rifiuti della gabbia, le lezioni attuali e l'esperimento della settimana passata con i suoi risultati.

Fai tre cose, con onestà e senza consolarti:
1. summary: risultato contro SPY, l'errore principale della settimana e cosa ha funzionato (4-6 frasi). Distingui la sfortuna
   dall'errore di metodo: un trade in perdita con una tesi buona e lo stop rispettato non è un errore.
2. lessons: la lista COMPLETA e aggiornata delle lezioni (massimo 8, frasi brevi e operative, es. "sui titoli scelti per fomo
   entra solo dopo un ritracciamento sotto la EMA20"). Tieni quelle confermate dai dati, togli quelle smentite, aggiungi
   le nuove solo se hanno almeno due trade a sostegno. Con pochi trade, cambia poco: non inseguire il rumore.
3. experiment: UN nuovo approccio da provare la settimana prossima, con il criterio per giudicarlo alla pagella successiva.
grade: voto da 1 a 10 sulla disciplina e sul metodo, non sul guadagno.
Le regole della gabbia (pesi, stop, limiti) non si cambiano da qui: puoi solo cambiare come scegli e quando entri ed esci.
Rispondi SOLO con un oggetto JSON {"grade", "summary", "lessons", "experiment"}."""


def validate_review(obj) -> dict:
    if not isinstance(obj, dict) or not {"grade", "summary", "lessons", "experiment"} <= obj.keys():
        raise BrainError("pagella malformata")
    g = obj["grade"]
    if isinstance(g, bool) or not isinstance(g, (int, float)) or not 1 <= g <= 10:
        raise BrainError(f"voto non valido: {g}")
    if not isinstance(obj["lessons"], list) or not all(isinstance(x, str) for x in obj["lessons"]):
        raise BrainError("lezioni non valide")
    return {"grade": int(g), "summary": str(obj["summary"])[:2000],
            "lessons": [x.strip()[:300] for x in obj["lessons"] if x.strip()][:8],
            "experiment": str(obj["experiment"])[:600]}


def _extract(text: str):
    t = text.strip()
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        raise BrainError("nessun JSON nella risposta")
    try:
        return json.loads(t[start:end + 1])
    except json.JSONDecodeError as e:
        raise BrainError(f"JSON non valido: {e}")


class _Brain:
    def decide(self, context: dict) -> dict:
        return validate(self._call(SYSTEM, build_prompt(context), SCHEMA))

    def review(self, context: dict) -> dict:
        return validate_review(self._call(REVIEW_SYSTEM, build_prompt(context), REVIEW_SCHEMA))


class ClaudeCodeBrain(_Brain):
    """Claude Code headless con l'abbonamento (`claude -p`), senza strumenti."""

    name = "claude-code"

    def _call(self, system: str, prompt: str, schema: dict):
        cmd = ["claude", "-p", prompt, "--append-system-prompt", system,
               "--output-format", "json", "--json-schema", json.dumps(schema), "--tools", ""]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=BRAIN_TIMEOUT_S, env=os.environ.copy())
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            raise BrainError(f"claude non risponde: {e}")
        if r.returncode != 0:
            raise BrainError(f"claude exit {r.returncode}: {r.stderr[-300:]}")
        try:
            envelope = json.loads(r.stdout)
            if isinstance(envelope, dict) and isinstance(envelope.get("structured_output"), dict):
                return envelope["structured_output"]
            text = envelope.get("result", "") if isinstance(envelope, dict) else r.stdout
        except json.JSONDecodeError:
            text = r.stdout
        return _extract(text)


class ApiBrain(_Brain):
    """Claude via API a consumo (ANTHROPIC_API_KEY), con output a schema fisso."""

    name = "api"

    def __init__(self, model: str = "claude-opus-5-5"):
        self.model = model

    def _call(self, system: str, prompt: str, schema: dict):
        import anthropic

        client = anthropic.Anthropic(timeout=BRAIN_TIMEOUT_S, max_retries=2)
        try:
            resp = client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.APIError as e:
            raise BrainError(f"API Claude: {e}")
        if resp.stop_reason in ("refusal", "max_tokens"):
            raise BrainError(f"risposta interrotta: {resp.stop_reason}")
        return _extract("".join(b.text for b in resp.content if b.type == "text"))


class FakeBrain:
    """Risponde sempre la stessa cosa: per provare tutto il giro senza rete."""

    name = "finto"

    def __init__(self, response: dict | str | None = None, review_response: dict | None = None):
        self.response = response
        self.review_response = review_response
        self.contexts: list[dict] = []   # cosa ha visto il cervello, per i test

    def review(self, context: dict) -> dict:
        self.contexts.append(context)
        return validate_review(self.review_response or {
            "grade": 6, "summary": "Prova: pagella finta.", "experiment": "Entrare sui titoli scelti solo dopo un ritracciamento.",
            "lessons": ["Lo stop si rispetta sempre.", "Non comprare sui massimi del giorno."]})

    def decide(self, context: dict) -> dict:
        self.contexts.append(context)
        if isinstance(self.response, str):
            return parse_text(self.response)
        if self.response is not None:
            return validate(self.response)
        return validate({
            "orders": [
                {"symbol": "SPY", "side": "buy", "qty": 20, "why": "S&P 500, base del portafoglio; torto sotto -3%"},
                {"symbol": "QQQ", "side": "buy", "qty": 10, "why": "Nasdaq 100"},
                {"symbol": "GLD", "side": "buy", "qty": 25, "why": "oro, copertura"},
                {"symbol": "BTC/USD", "side": "buy", "qty": 0.05, "why": "piccola quota Bitcoin"},
            ],
            "reasoning": "Prova con broker e cervello finti: eseguo un piano fisso.",
            "planNext": "Controllare la distanza dagli stop.",
        })
