# Polymarket Paper Trading Bot 📈

Bot de **paper trading** (operativa simulada) para Polymarket con dashboard en
Streamlit. Lee mercados **reales** vía las APIs públicas de Polymarket, genera
señales, aplica gestión de riesgo, **simula** órdenes con slippage/comisiones,
guarda todo en SQLite y lo visualiza en un panel local.

> ⚠️ **SOLO PAPER TRADING.** Este proyecto **no** opera con dinero real. No tiene
> claves privadas, no firma órdenes, no se conecta a ninguna wallet y solo usa
> endpoints **públicos de lectura**. Es imposible que ejecute una orden real:
> incluso poner `LIVE_TRADING=true` provoca un error de arranque a propósito.

---

## 1. Investigación: fuentes y qué tomé de cada una

Antes de programar revisé la documentación oficial y varios repositorios
públicos. No copié código (para evitar problemas de licencia y para entender lo
que hago); extraje **patrones e ideas**:

### Documentación oficial de Polymarket
- **[docs.polymarket.com](https://docs.polymarket.com/)** — arquitectura de las
  tres APIs:
  - **Gamma API** (`https://gamma-api.polymarket.com`): descubrimiento y
    metadatos de mercados (`/markets` con filtros `active`, `closed`, `limit`,
    `order`, `liquidity_num_min`, `volume_num_min`, `end_date_min`…). De aquí
    saqué los campos `clobTokenIds`, `outcomes`, `outcomePrices` (que vienen como
    **strings JSON**), `liquidity`, `volume`, `volume24hr`, `endDate`, `bestBid`,
    `bestAsk`, `spread`.
  - **CLOB API** (`https://clob.polymarket.com`): endpoints públicos de lectura
    `/price`, `/midpoint`, `/spread`, `/book` por `token_id`. De aquí saqué la
    forma de obtener best bid/ask, midpoint y spread en vivo.
  - **Data API / WebSocket**: revisados; para paper trading con polling no son
    imprescindibles, así que el cliente usa REST (más simple y robusto). El
    diseño deja la puerta abierta a añadir WebSocket más adelante.

### Repositorios oficiales de Polymarket
- **[Polymarket/py-clob-client](https://github.com/Polymarket/py-clob-client)** —
  SDK Python oficial del CLOB. Inspiró la **interfaz del cliente** (métodos
  `get_price(side)`, `get_midpoint`, `get_spread`, `get_order_book`) y la
  separación lectura/escritura. **Importante:** aquí solo implemento la parte de
  **lectura**; deliberadamente omito todo lo de firma/autenticación/órdenes.
- **[Polymarket/agents](https://github.com/Polymarket) / ejemplos oficiales** —
  para confirmar formatos de respuesta y el patrón "Gamma para descubrir, CLOB
  para precios".

### Repositorios públicos de la comunidad (inspiración de patrones)
- **Bots de trading en Python (arquitectura)** — separación clásica en capas
  *data → strategy → risk → execution → portfolio → storage*, que es la columna
  vertebral de este proyecto. Mantener la **estrategia pura** (solo decide
  BUY/SELL/HOLD) y delegar el sizing al *risk manager* es un patrón muy extendido
  (p. ej. estilo Freqtrade / backtrader) que adopté.
- **Simuladores de paper trading** — la idea de **rejugar las órdenes** desde la
  base de datos para reconstruir el portfolio de forma determinista (en vez de
  guardar un estado mutable frágil) viene de varios *paper trading simulators*.
- **Dashboards de trading en Streamlit** — layout de *KPIs arriba (st.metric) →
  gráficos → tablas con filtros en el sidebar*, y uso de Plotly para equity
  curve / drawdown.
- **Clientes CLOB / Gamma de la comunidad** (Go/TS/Python) — para validar nombres
  de campos y parámetros sin depender de un único origen.

> Ningún fragmento se copió literalmente. Todo el código de `src/` es propio y
> está comentado para que se entienda cada decisión.

---

## 2. Arquitectura

```
                 ┌─────────────────┐
   Polymarket    │ polymarket_      │   (solo GET públicos)
   Gamma + CLOB ─┤ client.py        │
                 └────────┬─────────┘
                          │ raw markets + quotes
                 ┌────────▼─────────┐
                 │ market_scanner.py│  normaliza → MarketSnapshot (por outcome)
                 └────────┬─────────┘
                          │
                 ┌────────▼─────────┐
                 │ strategy.py      │  genera Signal (BUY/SELL/HOLD) — pura
                 └────────┬─────────┘
                          │ signal
                 ┌────────▼─────────┐   consulta estado
                 │ risk_manager.py  │◄──────────────┐  aprueba/rechaza + sizing
                 └────────┬─────────┘               │
                          │ decisión aprobada       │
                 ┌────────▼─────────┐               │
                 │ paper_executor.py│  fill simulado (slippage + fees)
                 └────────┬─────────┘               │
                          │ Order                   │
                 ┌────────▼─────────┐               │
                 │ portfolio.py     │───────────────┘  cash, posiciones, PnL
                 └────────┬─────────┘
                          │
                 ┌────────▼─────────┐
                 │ storage.py       │  SQLite (fuente de verdad: replay de orders)
                 └────────┬─────────┘
                          │
                 ┌────────▼─────────┐
                 │ dashboard.py     │  Streamlit + Plotly (solo lectura)
                 └──────────────────┘
```

El orquestador (`main.py` → `PaperEngine.run_cycle`) ejecuta un ciclo:
**scan → marca a mercado → para cada token: señal → riesgo → fill → persistir →
snapshot de equity**.

Decisiones de diseño clave:
- **Estrategia pura**: no sabe nada de saldos ni límites. Solo dice qué quiere
  hacer y por qué. Esto la hace trivial de testear y extender.
- **El portfolio se reconstruye rejugando las órdenes** (`Portfolio.from_orders`).
  La base de datos es la única fuente de verdad; no hay estado mutable oculto.
- **Contabilidad a coste medio** (average cost): las compras actualizan el precio
  medio ponderado; las ventas realizan PnL contra ese medio.
- **`Config` inmutable** inyectada explícitamente: sin variables globales.

---

## 3. Instalación

Requiere **Python 3.11+**.

```bash
cd polymarket-paper-bot
python -m venv .venv && source .venv/bin/activate   # opcional pero recomendado
pip install -r requirements.txt
cp .env.example .env
```

## 4. Configuración

Toda la configuración vive en `.env` (ver `.env.example`, con valores **seguros**
por defecto). Variables principales:

| Variable | Significado |
|---|---|
| `INITIAL_PAPER_BALANCE_USDC` | Saldo inicial simulado |
| `ALLOW_NO` | Permitir comprar el outcome NO (por defecto `false`) |
| `MAX_TRADE_SIZE_USDC` | Tamaño máximo por operación |
| `MAX_POSITION_SIZE_USDC` | Exposición máxima por posición |
| `MAX_MARKET_EXPOSURE_USDC` / `MAX_TOTAL_EXPOSURE_USDC` | Topes de exposición |
| `MAX_OPEN_POSITIONS` | Nº máximo de posiciones abiertas |
| `MAX_DAILY_LOSS_USDC` | Cortacircuitos de pérdida diaria |
| `MAX_SPREAD` / `MIN_LIQUIDITY` | Filtros de calidad de mercado |
| `MIN_HOURS_TO_CLOSE` / `EXIT_HOURS_BEFORE_CLOSE` | Ventanas temporales |
| `ENTRY_PRICE_MAX` | Precio máximo de entrada para BUY |
| `TAKE_PROFIT_PCT` / `STOP_LOSS_PCT` | Salidas |
| `SLIPPAGE_BPS` / `FEE_BPS` | Realismo de la simulación |
| `POLL_INTERVAL_SECONDS` | Periodicidad del bucle `paper` |

## 5. Ejecución (CLI)

```bash
python -m src.main scan            # escanea y muestra mercados activos
python -m src.main paper           # bucle de paper trading (Ctrl+C para parar)
python -m src.main paper --once    # un solo ciclo
python -m src.main paper --iterations 10
python -m src.main status          # resumen de la cartera
python -m src.main dashboard       # lanza el dashboard de Streamlit
python -m src.main export          # exporta todas las tablas a CSV (data/exports/)
python -m src.main reset-paper     # borra el estado de paper trading
python -m src.main close <slug>    # cierra manualmente posiciones que coincidan
```

## 6. Dashboard

```bash
streamlit run src/dashboard.py
# o:  python -m src.main dashboard
```

Muestra:
1. **Resumen**: balance inicial, equity, PnL total / diario / realizado / no
   realizado, drawdown, win rate, nº de operaciones, posiciones abiertas,
   exposición total, cash.
2. **Gráficos**: equity curve, PnL acumulado, drawdown, PnL diario, exposición
   por mercado, ganadoras vs perdedoras, señales por día.
3. **Tablas**: posiciones (abiertas/cerradas), operaciones simuladas, señales y
   mercados analizados.
4. **Filtros** (sidebar): outcome, estado de orden y mercado (slug).

## 7. Cómo funciona el paper trading (realismo)

- Compras al **best ask**, ventas al **best bid**.
- **Slippage adverso** configurable (`SLIPPAGE_BPS`): las compras pagan un poco
  más, las ventas reciben un poco menos.
- **Comisiones** opcionales (`FEE_BPS`; Polymarket hoy es 0, por eso el default
  es 0, pero el gancho está para escenarios "what-if").
- Precios de fill **acotados** a `(0, 1)` (son probabilidades).
- Se calcula **PnL realizado y no realizado**, **equity**, **drawdown**,
  **win rate** y **exposición por mercado/total**.

### Estrategia inicial (`SimpleThresholdStrategy`)
- **Entrada (BUY)**: sin posición previa, outcome YES (o NO si `ALLOW_NO=true`),
  `best_ask ≤ ENTRY_PRICE_MAX`, `spread ≤ MAX_SPREAD`,
  `liquidity ≥ MIN_LIQUIDITY` y faltan `≥ MIN_HOURS_TO_CLOSE` para el cierre.
- **Salida (SELL)**: take profit (`≥ TAKE_PROFIT_PCT`), stop loss
  (`≤ -STOP_LOSS_PCT`) o cierre antes del vencimiento
  (`≤ EXIT_HOURS_BEFORE_CLOSE`).

## 8. Tests

```bash
pytest            # 44 tests
```

Cubren: estrategia (entradas/salidas/rechazos), risk manager (topes y
cortacircuitos), paper executor (slippage/fees/rechazos), portfolio (PnL,
equity, coste medio, exposición, win rate), storage (persistencia, replay,
PnL diario, reset/export), parsing de mercados Gamma y un test **end-to-end**
del ciclo completo (abrir posición + salir por take profit).

## 9. Cómo añadir una nueva estrategia

1. Crea una subclase de `Strategy` en `src/strategy.py`:

   ```python
   class MyStrategy(Strategy):
       name = "my_strategy"
       def generate(self, snap, position):
           # devuelve self._signal(snap, BUY/SELL/HOLD, price, reason)
           ...
   ```
2. Regístrala en `build_strategy()`.
3. Añade tests en `tests/test_strategy.py`.

La estrategia **no** debe preocuparse por tamaños ni límites: de eso se encarga
el `RiskManager`. Solo decide la intención y la razón.

## 10. Límites y riesgos

- Es una **simulación**: los fills asumen que tu orden se ejecuta al top of book.
  En mercados poco líquidos el fill real podría ser peor (impacto de mercado no
  modelado más allá del slippage fijo).
- No modela resolución del mercado (settlement a 0/1); las salidas son por
  precio/tiempo. Puedes ampliarlo.
- Las APIs públicas tienen **rate limits**; el polling por defecto es
  conservador (`POLL_INTERVAL_SECONDS=30`).
- **No es asesoramiento financiero ni una herramienta de trading real.** Sirve
  para aprender y prototipar estrategias sin arriesgar capital.

## 11. Seguridad

- Sin secretos: el bot no necesita ninguna clave. `.env` está en `.gitignore`.
- Solo peticiones **GET públicas**; no hay endpoints de escritura/órdenes.
- `LIVE_TRADING=true` o `PAPER_TRADING=false` **abortan el arranque** con un
  error explícito (ver `config.load_config`).

---

*Hecho con fines educativos. Inspirado (en patrones, no en código) por la
documentación oficial de Polymarket, `py-clob-client` y proyectos abiertos de
bots de trading y dashboards en Streamlit citados arriba.*
