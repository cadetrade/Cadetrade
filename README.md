# CadeTrade

Agente di **paper trading** che si sveglia a orari fissi, legge il conto, chiede a Claude una decisione,
la fa passare da regole scritte nel codice ("la gabbia"), esegue su **Alpaca paper** e scrive su Telegram.
Il modello propone, il codice dispone. Soldi finti, prezzi veri: il programma si rifiuta di partire con chiavi di un conto vero.

Basato sulla guida *Il mio trader in quattro passi*, con questi miglioramenti:

| Guida | CadeTrade |
|---|---|
| 8 titoli fissi | Nucleo fisso **oro (GLD), Nasdaq 100 (QQQ), S&P 500 (SPY), Bitcoin** + **1-2 titoli scelti ogni giorno** dal codice per volumi anomali e slancio ("fomo"), max 10% ciascuno |
| La pagella la domenica, ma il bot non la usa | **Impara dagli errori**: diario di ogni trade (tesi all'acquisto, esito), pagella settimanale che riscrive le lezioni e propone un esperimento; lezioni ed esperimento entrano in ogni decisione |
| Solo azioni USA, 4 sveglie al giorno | Anche crypto su Alpaca: 7 sveglie, quelle notturne solo crypto, quindi **h24** |
| Stop a −3% controllato solo alle sveglie (NVDA venduta a −4,8%) | Stop sulla **volatilità** (2 ATR) piazzato **sul broker** per le azioni; il **guardiano** lo fa salire ogni minuto (pareggio dopo +1R, poi 2,5 ATR dal massimo) e protegge le crypto |
| Quantità a sensazione | Quantità calcolata dal **rischio**: se scatta lo stop si perde lo 0,75% del conto (0,4% sui titoli scelti) |
| Decisioni a ogni sveglia | Si **entra** solo all'apertura (15:40) e alla chiusura (21:40); le altre sveglie gestiscono il rischio |
| Nessun limite sul conto intero | **Perdita giornaliera −3%**: stop agli acquisti fino a domani. **Drawdown −15%** dal massimo: solo vendite |
| Il "niente riacquisto dopo lo stop" era una scelta del modello | È una regola della gabbia |
| Il cervello decide "a sensazione" | Riceve **indicatori calcolati in codice** (trend EMA50/200, RSI, ATR, distanza dai massimi) |
| Crypto assenti | Esposizione crypto totale **max 30%** |
| Solo Claude Code (abbonamento) | Claude Code headless **oppure** API (`--brain api`), con output a schema JSON fisso |

## Il giro di una sveglia
1. Se c'è `state/run.lock` la sveglia prima è morta: riconcilia con Alpaca e avvisa.
2. Legge il testimone `state/baton.json`, segnala le sveglie saltate (non le recupera).
3. Chiede ad Alpaca conto, posizioni, ordini 48h, orologio; calcola gli indicatori.
4. Chiede la decisione al cervello. Se risponde male o non risponde: nessun ordine nuovo, lo stop gira lo stesso.
5. La gabbia filtra. 6. Invia con id fisso `AAAA-MM-GG-HHMM-SIMBOLO-lato`: Alpaca rifiuta i doppioni.
7. Scrive il testimone in modo atomico (+ copia in `state/history/`). 8. Telegram, toglie il lucchetto.

## Sveglie (ora di Roma)
01:00 notte · 05:00 alba · 13:00 mezzogiorno → solo crypto · 09:00 briefing → nessun ordine ·
15:40 apertura · 18:30 metà seduta · 21:40 chiusura → azioni e crypto. Il guardiano gira ogni minuto.

## Scelta del giorno (`cadetrade/picker.py`)
Alla prima sveglia del giorno il codice legge lo screener di Alpaca (azioni più scambiate, maggiori rialzi, crypto in rialzo)
e tiene i 2 con il punteggio più alto: volume di oggi almeno 1,5x la media a 20 giorni, in rialzo ma non oltre +25%,
prezzo sopra 5 $, almeno 50 M$ scambiati, niente ETF a leva o inversi. Il cervello decide se comprarli; la gabbia ne limita il peso.

## Imparare dagli errori (`cadetrade/learning.py`)
Ogni acquisto salva la tesi ("perché" e prezzo a cui avrei torto); ogni vendita la chiude nel diario con il risultato.
La domenica alle 09:00 (o con `python -m cadetrade review`) il cervello legge diario, statistiche, confronto con SPY,
rifiuti della gabbia e l'esperimento della settimana; dà un voto, riscrive fino a 8 lezioni e propone un nuovo esperimento.
Le lezioni cambiano *come sceglie e quando entra*, mai le regole della gabbia. `python -m cadetrade lessons` le mostra.

## La gabbia (`cadetrade/config.py`, `cadetrade/cage.py`)
Nucleo GLD QQQ SPY BTC/USD + le scelte del giorno · max 6 posizioni · max 20% per titolo del nucleo, 10% per le scelte ·
crypto max 30% · QQQ+SPY insieme max 30% · stop 1-3,5 ATR (−3% se manca l'ATR) · rischio 0,75% per operazione · niente short, niente leva, azioni intere · max 6 ordini a sveglia ·
perdita del giorno −3% → niente acquisti · drawdown −15% → solo vendite · niente riacquisto il giorno dello stop.

## Uso
```bash
pip install -r requirements.txt
pytest                                              # 47 test, devono essere tutti verdi
python -m cadetrade once --fake --slot 1540         # prova senza rete: broker e cervello finti
cp secrets.example.toml secrets.toml                # poi riempi le chiavi
python -m cadetrade once --slot 1540                # sveglia vera (Alpaca paper + Telegram)
python -m cadetrade schedule                        # sempre acceso: sveglie + guardiano
python -m cadetrade status                          # lucchetto, testimone, conto
```
Su un server: `docker build -t cadetrade .` e volumi persistenti su `state/` e `logs/`.
Con Claude Code serve `claude setup-token` e il token in `secrets.toml`.

## Ricerca
`python -m cadetrade.research.backtest` fa il backtest di strategie a regole (`research.toml`) contro il buy & hold.

## Limiti che conosco
- Il broker vero non è stato provato da qui: i test usano quello finto. La prima sveglia vera va guardata.
- Le azioni hanno un ordine stop vero su Alpaca: scatta anche a server spento. In un gap di apertura si esce al primo prezzo disponibile, che può essere sotto lo stop.
- Le crypto non hanno stop sul broker: le protegge il guardiano ogni minuto, quindi se il server è spento non scattano.
- Le notizie (Benzinga via Alpaca) arrivano al cervello come titoli e sintesi: niente calendario macro ancora.
- La perdita del giorno usa `last_equity` di Alpaca (chiusura precedente), quindi il "giorno" è quello della borsa USA.
- I test li ha scritti lo stesso modello che ha scritto il codice: non bastano. Prima di fidarti, settimane di paper.
- Oro, Nasdaq e S&P 500 sono gli ETF (orari della borsa USA), non i CFD XAU/USD, US100, US500 che girano quasi 24 ore.
- "Imparare" qui vuol dire lezioni scritte e riviste ogni settimana: con pochi trade il rischio è inseguire il rumore.
- Nessuna garanzia di guadagno. Questo sistema misura disciplina, non dimostra un vantaggio sul mercato.


## Pannello dal vivo

Il bot serve una pagina animata sulla porta 8080 del server: `http://IP-DEL-SERVER:8080/?k=CHIAVE`.
La chiave è la variabile `DASHBOARD_KEY` del progetto Docker; senza chiave il pannello resta spento.
Mostra cosa sta facendo adesso (Claude che ragiona, il guardiano), conto, rischio aperto, posizioni con i loro stop,
grafico a candele, ultimo ragionamento, messaggi in diretta, lezioni e trade chiusi. È di sola lettura.
