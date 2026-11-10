"""Tutte le regole e gli orari in un posto solo. La gabbia legge da qui; il modello non può cambiarle."""
from dataclasses import dataclass
from zoneinfo import ZoneInfo

# --- L'unica riga che protegge dai soldi veri. Non toccarla. ---
PAPER_URL = "https://paper-api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"

TZ = ZoneInfo("Europe/Rome")

# Il nucleo fisso. Su Alpaca oro, Nasdaq 100 e S&P 500 si tradano con gli ETF che li replicano.
CORE = {
    "GLD": "oro (XAU/USD)",
    "QQQ": "Nasdaq 100",
    "SPY": "S&P 500",
    "BTC/USD": "Bitcoin",
}
UNIVERSE = list(CORE)

# Le crypto che Alpaca permette di tradare: tra queste può cadere la scelta del giorno.
CRYPTO_TRADABLE = ["BTC", "ETH", "SOL", "AVAX", "LINK", "DOGE", "LTC", "XRP", "DOT", "UNI", "AAVE", "BCH", "SHIB"]
CRYPTO = [c + "/USD" for c in CRYPTO_TRADABLE]


def is_crypto(symbol: str) -> bool:
    return "/" in symbol


def norm(symbol: str) -> str:
    """Alpaca restituisce le posizioni crypto come BTCUSD: le riportiamo a BTC/USD."""
    s = symbol.upper().strip()
    if "/" not in s and s.endswith("USD") and s[:-3] in CRYPTO_TRADABLE:
        return s[:-3] + "/USD"
    return s


@dataclass(frozen=True)
class Slot:
    hhmm: str        # ora di Roma
    name: str
    markets: frozenset   # cosa può tradare: "stock", "crypto"
    entries: bool = False    # posizioni nuove o aumenti solo qui; le altre sveglie gestiscono il rischio (vendite)


# Borsa USA 15:30-22:00 ora di Roma. Le crypto sono aperte h24: tre sveglie notturne/mattutine solo crypto.
# Si entra solo due volte al giorno (apertura e chiusura): meno decisioni, più qualità. Le altre sveglie possono solo vendere.
SLOTS = [
    Slot("0100", "notte", frozenset({"crypto"})),
    Slot("0500", "alba", frozenset({"crypto"})),
    Slot("0900", "briefing", frozenset()),            # nessun ordine: legge e fissa il piano
    Slot("1300", "mezzogiorno", frozenset({"crypto"})),
    Slot("1540", "apertura", frozenset({"stock", "crypto"}), entries=True),
    Slot("1830", "metà seduta", frozenset({"stock", "crypto"})),
    Slot("2140", "chiusura", frozenset({"stock", "crypto"}), entries=True),
]
SLOT_BY_HHMM = {s.hhmm: s for s in SLOTS}
SLOT_GRACE_MIN = 60          # oltre 60 minuti una sveglia persa non si recupera


@dataclass(frozen=True)
class Cage:
    max_positions: int = 6            # i 4 del nucleo + le 2 scelte del giorno
    max_weight: float = 0.20          # per titolo del nucleo, sul valore del conto
    max_pick_weight: float = 0.10     # per titolo scelto dal bot: più volatile, metà del peso
    max_crypto_weight: float = 0.30   # tutte le crypto insieme
    # titoli che si muovono quasi insieme contano come una scommessa sola
    groups: tuple = (("indici USA (QQQ+SPY)", ("QQQ", "SPY"), 0.30),)
    # rischio per operazione: quanto del conto si perde se scatta lo stop. La quantità la calcola il codice.
    risk_per_trade: float = 0.0075    # nucleo: 0,75% del conto
    pick_risk_per_trade: float = 0.004  # scelti per fomo: 0,4%
    # stop in base alla volatilità (ATR 14 giorni): normale 2 ATR, mai sotto 1 né oltre 3,5
    stop_atr: float = 2.0
    min_stop_atr: float = 1.0
    max_stop_atr: float = 3.5
    stop_loss: float = -0.03          # stop di riserva per posizioni senza ATR (o aperte a mano)
    # proteggere i guadagni: dopo +1R lo stop sale a pareggio e poi segue il prezzo a 2,5 ATR dal massimo
    breakeven_r: float = 1.0
    trail_atr: float = 2.5
    daily_loss_halt: float = -0.03    # P&L del giorno sotto -3%: niente acquisti nuovi fino a domani
    max_drawdown: float = -0.15       # dal massimo storico del conto: solo vendite, serve un umano
    max_orders_per_run: int = 6
    min_order_usd: float = 10.0
    guard_interval_s: int = 60        # il guardiano controlla gli stop ogni minuto


CAGE = Cage()


@dataclass(frozen=True)
class PickRules:
    """Come il codice sceglie i 1-2 titoli del giorno ("fomo" = volumi anomali + slancio)."""
    max_picks: int = 2
    min_price: float = 5.0
    min_dollar_volume: float = 50_000_000      # liquidità minima giornaliera in $ (azioni)
    min_volume_ratio: float = 1.5              # volume di oggi / media 20 giorni
    max_change_1d: float = 0.25                # oltre +25% in un giorno è una scommessa, non un trend
    # ETF a leva o inversi: vietati anche se sono i più scambiati
    banned: tuple = ("TQQQ", "SQQQ", "SOXL", "SOXS", "SPXL", "SPXS", "UPRO", "SPXU", "TNA", "TZA", "LABU",
                     "LABD", "UVXY", "SVXY", "VXX", "NVDL", "TSLL", "TSLQ", "MSTU", "MSTZ", "BITX", "CONL", "NUGT", "DUST")


PICKS = PickRules()

BROKER_RETRIES = 3
BRAIN_TIMEOUT_S = 240
