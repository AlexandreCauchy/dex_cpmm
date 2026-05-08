import os
import sys
import time
import random
import math
from dotenv import load_dotenv
from bots.common.botBase import BaseBot

load_dotenv()

# Forçar encoding UTF-8 na saída (evita UnicodeEncodeError no Windows cp1252)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# ═══════════════════════════════════════════════════════════════════════════════
# Bot28867 — "The Predator v4"
#
# ARQUITECTURA COMPETITIVA — TOP 1-3 PnL:
#   1. Velocidade máxima: sleep mínimo entre ciclos (0.05–0.15s)
#   2. Warmup ultra-reduzido: apenas 4 preços históricos necessários
#   3. Multi-sinal agressivo: opera com EMA OU momentum forte isolado
#   4. Frações altas: até 65% do saldo por operação de alta confiança
#   5. Sem pausa por target: nunca para, sempre tenta superar os outros
#   6. Rebalanceamento rápido: a cada 6 ciclos sem sinal
#   7. Arbitragem de preço cruzado: detecta desvios entre pools
#   8. Circuit-breaker conservador: só pausa se drawdown > 8%
#
# Objectivo académico: TOP 1-3 em qualquer competição
# ID do aluno: 28867  |  Tag on-chain: 0x70C3
# ═══════════════════════════════════════════════════════════════════════════════


class Bot28867(BaseBot):

    # ── Parâmetros de indicadores ─────────────────────────────────────────────
    EMA_FAST    = 5      # EMA rápida ultra-sensível
    EMA_SLOW    = 15     # EMA lenta compacta
    RSI_PERIOD  = 10     # RSI de resposta rápida
    WARMUP      = 4      # apenas 4 preços históricos necessários
    HISTORY_MAX = 60     # historial compacto

    # ── Limiares de sinal (agressivos) ───────────────────────────────────────
    EMA_BULL_THRESH = 1.0002   # divergência mínima EMA para compra (muito baixo)
    EMA_BEAR_THRESH = 0.9998   # divergência mínima EMA para venda  (muito baixo)
    RSI_OB          = 78       # overbought — limite superior permissivo
    RSI_OS          = 22       # oversold   — limite inferior permissivo
    RSI_BULL_MAX    = 70       # compra se RSI < 70
    RSI_BEAR_MIN    = 30       # venda se RSI > 30
    MOM_BULL_MIN    = 0.0001   # momentum positivo mínimo (ultra-relaxado)
    MOM_BEAR_MAX    = -0.0001  # momentum negativo mínimo (ultra-relaxado)
    MAX_VOLATILITY  = 0.08     # tolera até 8% de volatilidade CV

    # ── Gestão de risco (agressiva para competição) ───────────────────────────
    BASE_FRACTION  = 0.40    # fracção base do saldo (40%)
    MAX_FRACTION   = 0.65    # fracção máxima (65% em sinais excepcionais)
    MIN_AMOUNT     = 5       # montante mínimo reduzido
    MAX_AMOUNT     = 500     # tecto mais alto

    # ── Filtros de qualidade de sinal ─────────────────────────────────────────
    MIN_CONFIDENCE = 0.001   # limiar mínimo muito baixo → mais oportunidades

    # ── Protecção de capital ──────────────────────────────────────────────────
    MAX_DRAWDOWN_PCT   = 0.08   # pausa só se cair > 8% do valor inicial
    COOLDOWN_AFTER_ERR = 0.5    # cooldown pós-erro muito curto (0.5s)
    TRADER_COOLDOWN    = 2.1    # cooldown on-chain entre trades (segundos)

    # ── Velocidade ────────────────────────────────────────────────────────────
    SLEEP_MIN = 0.3    # sleep mínimo entre ciclos (respeita cooldown on-chain)
    SLEEP_MAX = 0.8    # sleep máximo entre ciclos

    def __init__(self):
        pk = os.getenv("EXT_BOT_0_PK")
        super().__init__(pk, "Bot28867", "trend")
        # Sobrepor os intervalos definidos no config com valores mais rápidos
        self._min_interval = self.SLEEP_MIN
        self._max_interval = self.SLEEP_MAX
        self._last_trade_time = 0.0   # controlo do cooldown on-chain
        self.tag = "0x70C3"            # ID 28867 em hexadecimal

        # Histórico de preços e indicadores por pool
        self.history: dict = {}

        # Controlo de operações
        self.trade_count   = 0
        self._step_count   = 0
        self._no_signal_streak = 0
        self._last_err_time = 0.0

        # Snapshot de portfólio para controlo de PnL
        self._initial_portfolio: dict | None = None
        self._initial_total: float = 0.0

        # Cache de saldo para evitar chamadas RPC repetidas por ciclo
        self._cached_balances: dict = {}
        self._cache_time: float = 0.0
        self._cache_ttl: float = 1.5  # refresca cache a cada 1.5s

    # ── Logging ───────────────────────────────────────────────────────────────

    def log(self, message: str):
        print(f"[Bot28867 | ID-28867] {message}", flush=True)

    # ── Cache de saldos ───────────────────────────────────────────────────────

    def _get_balances_cached(self) -> dict:
        """Evita chamadas RPC repetidas ao blockchain por ciclo."""
        now = time.time()
        if now - self._cache_time > self._cache_ttl:
            try:
                self._cached_balances = self.client.get_all_balances()
                self._cache_time = now
            except Exception:
                pass
        return self._cached_balances

    # ── Snapshot de portfólio ─────────────────────────────────────────────────

    def _take_portfolio_snapshot(self):
        """Regista o saldo inicial de todos os tokens no início da competição."""
        try:
            self._initial_portfolio = self.client.get_all_balances()
            self._initial_total = sum(self._initial_portfolio.values())
            self._cached_balances = dict(self._initial_portfolio)
            self._cache_time = time.time()
            self.log(f"📊 Portfólio inicial: {self._initial_total:.2f} unidades totais")
        except Exception as e:
            self.log(f"⚠ Não foi possível registar portfólio inicial: {e}")

    def _current_total(self) -> float:
        """Soma total dos saldos actuais (usa cache quando possível)."""
        balances = self._get_balances_cached()
        if balances:
            return sum(balances.values())
        return self._initial_total  # fallback seguro

    def _pnl_is_safe(self) -> bool:
        """
        Verifica se o PnL está acima do drawdown máximo permitido.
        Circuit-breaker: pausa se drawdown > MAX_DRAWDOWN_PCT.
        """
        if self._initial_total <= 0:
            return True  # sem referência → não bloquear

        current = self._current_total()
        drawdown = (self._initial_total - current) / self._initial_total

        if drawdown > self.MAX_DRAWDOWN_PCT:
            self.log(
                f"🛑 CIRCUIT-BREAKER: drawdown={drawdown:.2%} "
                f"(limite={self.MAX_DRAWDOWN_PCT:.2%}). Pausando 2s."
            )
            return False
        return True

    # ── Gestão de histórico ───────────────────────────────────────────────────

    def _init_pool(self, pool_id):
        self.history[pool_id] = {"prices": [], "gains": [], "losses": []}

    def update_history(self, pool_id: str, price: float):
        if pool_id not in self.history:
            self._init_pool(pool_id)

        hist = self.history[pool_id]

        if hist["prices"]:
            diff = price - hist["prices"][-1]
            hist["gains"].append(max(0.0, diff))
            hist["losses"].append(max(0.0, -diff))

        hist["prices"].append(price)

        # Janela deslizante
        if len(hist["prices"]) > self.HISTORY_MAX:
            hist["prices"].pop(0)
        if len(hist["gains"]) > self.HISTORY_MAX:
            hist["gains"].pop(0)
            hist["losses"].pop(0)

    # ── Indicadores técnicos ──────────────────────────────────────────────────

    def _ema(self, prices: list, period: int) -> float:
        alpha = 2.0 / (period + 1)
        ema = prices[0]
        for p in prices[1:]:
            ema = alpha * p + (1.0 - alpha) * ema
        return ema

    def _rsi(self, gains: list, losses: list) -> float:
        period = self.RSI_PERIOD
        if len(gains) < period:
            return 50.0
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def _volatility(self, prices: list) -> float:
        window = prices[-15:]
        if len(window) < 4:
            return 0.0
        mean = sum(window) / len(window)
        if mean == 0:
            return 0.0
        variance = sum((p - mean) ** 2 for p in window) / len(window)
        return math.sqrt(variance) / mean

    def _momentum(self, prices: list, period: int = 3) -> float:
        """Momentum de curto prazo (3 períodos) para resposta rápida."""
        if len(prices) < period + 1:
            return 0.0
        return (prices[-1] - prices[-period - 1]) / prices[-period - 1]

    def _momentum_long(self, prices: list, period: int = 7) -> float:
        """Momentum de médio prazo para confirmar tendência."""
        if len(prices) < period + 1:
            return 0.0
        return (prices[-1] - prices[-period - 1]) / prices[-period - 1]

    def _trend_strength(self, prices: list) -> float:
        """Força da tendência: declive normalizado da regressão linear simples."""
        n = min(len(prices), 10)
        if n < 4:
            return 0.0
        window = prices[-n:]
        x_mean = (n - 1) / 2.0
        y_mean = sum(window) / n
        num = sum((i - x_mean) * (window[i] - y_mean) for i in range(n))
        den = sum((i - x_mean) ** 2 for i in range(n))
        if den == 0 or y_mean == 0:
            return 0.0
        return (num / den) / y_mean

    def get_indicators(self, pool_id: str) -> dict | None:
        hist = self.history.get(pool_id)
        if hist is None or len(hist["prices"]) < self.WARMUP:
            return None

        prices = hist["prices"]
        n = len(prices)
        # Para EMA_SLOW de período 15, precisamos de pelo menos 5 preços
        ema_slow_period = min(self.EMA_SLOW, max(3, n - 1))
        return {
            "ema_fast":       self._ema(prices, min(self.EMA_FAST, n)),
            "ema_slow":       self._ema(prices, ema_slow_period),
            "rsi":            self._rsi(hist["gains"], hist["losses"]),
            "volatility":     self._volatility(prices),
            "momentum":       self._momentum(prices),
            "momentum_long":  self._momentum_long(prices),
            "trend_strength": self._trend_strength(prices),
            "last_price":     prices[-1],
            "n":              n,
        }

    # ── Quote para validar lucro esperado ─────────────────────────────────────

    def _expected_profit_ok(self, pool, token_in: str, token_out: str, amount: float) -> bool:
        """
        Verifica via quote on-chain se o swap tem retorno positivo esperado.
        Retorna True se o valor recebido (em token_out, convertido) for >= amount_in.
        """
        try:
            from web3 import Web3
            ti = Web3.to_checksum_address(token_in)
            to_ = Web3.to_checksum_address(token_out)
            ti_data = self.client.tokens[ti]
            to_data = self.client.tokens[to_]

            amount_wei = int(amount * (10 ** ti_data["decimals"]))
            if amount_wei <= 0:
                return False

            out_wei = self.client.exchange.functions.quote(ti, to_, amount_wei).call()
            out_amt = out_wei / (10 ** to_data["decimals"])

            # Custo: amount de token_in. Recebemos out_amt de token_out.
            # Calculamos ratio usando preço do pool
            if token_in == pool["token0"]:
                price_in_to_out = pool["price01"]
            else:
                price_in_to_out = pool["price10"]

            if price_in_to_out <= 0:
                return True

            expected_no_fee = amount * price_in_to_out
            if expected_no_fee <= 0:
                return True

            # Só executa se receber pelo menos 90.0% do esperado sem fee (permite ate 10% de slippage+taxa)
            return out_amt >= expected_no_fee * 0.900
        except Exception:
            return True  # em caso de erro, não bloquear

    # ── Verificação de price impact ───────────────────────────────────────────

    def _price_impact_ok(self, pool, token_in: str, amount: float) -> bool:
        """Verifica se o price impact < 4%."""
        try:
            from web3 import Web3
            ti = Web3.to_checksum_address(token_in)
            ti_data = self.client.tokens[ti]
            amount_wei = int(amount * (10 ** ti_data["decimals"]))

            if token_in == pool["token0"]:
                reserve_in_wei = pool["reserve0_wei"]
            else:
                reserve_in_wei = pool["reserve1_wei"]

            if reserve_in_wei <= 0:
                return False

            price_impact = amount_wei / (reserve_in_wei + amount_wei)
            if price_impact > 0.04:  # > 4% → reduzir mas não bloquear completamente
                return False
            return True
        except Exception:
            return True

    # ── Seleção do melhor trade ───────────────────────────────────────────────

    def get_best_trade(self, pools: list) -> tuple | None:
        """
        Estratégia multi-camada para maximizar PnL:
         1. Sinal EMA+RSI clássico (com momentum bónus)
         2. Sinal momentum forte sozinho (detecta breakouts rápidos)
         3. Arbitragem de preço cruzado entre pools
        Retorna (token_in, token_out, pool, reason, fraction) ou None.
        """
        best = None
        best_conf = self.MIN_CONFIDENCE

        for pool in pools:
            pid   = pool["pool_id"]
            price = pool["price01"]

            self.update_history(pid, price)
            ind = self.get_indicators(pid)
            if ind is None:
                continue

            ema_f  = ind["ema_fast"]
            ema_s  = ind["ema_slow"]
            rsi    = ind["rsi"]
            vol    = ind["volatility"]
            mom    = ind["momentum"]
            mom_l  = ind["momentum_long"]
            trend  = ind["trend_strength"]

            # ── Bloqueio de volatilidade excessiva ─────────────────────────
            if vol > self.MAX_VOLATILITY:
                continue

            # ══════════════════════════════════════════════════════════════
            # CAMADA 1: Sinal EMA cruzado (tendência confirmada)
            # ══════════════════════════════════════════════════════════════

            # ── Sinal de COMPRA (Bullish EMA) ──────────────────────────────
            if (ema_f > ema_s * self.EMA_BULL_THRESH
                    and rsi < self.RSI_BULL_MAX
                    and rsi > self.RSI_OS):

                conf = (ema_f / ema_s - 1.0) * 20  # amplificado

                # Bónus momentum curto e longo
                if mom > self.MOM_BULL_MIN:
                    conf += mom * 12
                if mom_l > 0:
                    conf += mom_l * 6
                if trend > 0:
                    conf += trend * 5

                # Bónus RSI em zona óptima
                if 35 < rsi < 55:
                    conf *= 1.35
                elif rsi < 45:
                    conf *= 1.20

                # Bónus momentum forte — breakout
                if mom > 0.015:
                    conf *= 1.25
                if mom_l > 0.01:
                    conf *= 1.15

                if conf > best_conf:
                    best_conf = conf
                    fraction = min(self.MAX_FRACTION,
                                   self.BASE_FRACTION + conf * 0.5)
                    best = (
                        pool["token1"], pool["token0"], pool,
                        f"COMPRA-EMA | EMA:{ema_f:.5f}>{ema_s:.5f} RSI:{rsi:.1f} Mom:{mom:.4f} conf:{conf:.4f}",
                        fraction
                    )

            # ── Sinal de VENDA (Bearish EMA) ───────────────────────────────
            elif (ema_f < ema_s * self.EMA_BEAR_THRESH
                  and rsi > self.RSI_BEAR_MIN
                  and rsi < self.RSI_OB):

                conf = (ema_s / ema_f - 1.0) * 20

                if mom < self.MOM_BEAR_MAX:
                    conf += abs(mom) * 12
                if mom_l < 0:
                    conf += abs(mom_l) * 6
                if trend < 0:
                    conf += abs(trend) * 5

                if 45 < rsi < 65:
                    conf *= 1.35
                elif rsi > 55:
                    conf *= 1.20

                if mom < -0.015:
                    conf *= 1.25
                if mom_l < -0.01:
                    conf *= 1.15

                if conf > best_conf:
                    best_conf = conf
                    fraction = min(self.MAX_FRACTION,
                                   self.BASE_FRACTION + conf * 0.5)
                    best = (
                        pool["token0"], pool["token1"], pool,
                        f"VENDA-EMA  | EMA:{ema_f:.5f}<{ema_s:.5f} RSI:{rsi:.1f} Mom:{mom:.4f} conf:{conf:.4f}",
                        fraction
                    )

            # ══════════════════════════════════════════════════════════════
            # CAMADA 2: Sinal de momentum puro (breakout rápido)
            # Actua mesmo sem cruzamento EMA — detecta movimentos súbitos
            # ══════════════════════════════════════════════════════════════

            elif mom > 0.008 and mom_l > 0.005 and rsi < 72 and rsi > self.RSI_OS:
                conf = mom * 18 + mom_l * 9 + max(0, trend) * 4
                if conf > best_conf:
                    best_conf = conf
                    fraction = min(self.MAX_FRACTION, self.BASE_FRACTION + conf * 0.4)
                    best = (
                        pool["token1"], pool["token0"], pool,
                        f"COMPRA-MOM | Mom:{mom:.4f} MomL:{mom_l:.4f} RSI:{rsi:.1f} conf:{conf:.4f}",
                        fraction
                    )

            elif mom < -0.008 and mom_l < -0.005 and rsi > 28 and rsi < self.RSI_OB:
                conf = abs(mom) * 18 + abs(mom_l) * 9 + max(0, -trend) * 4
                if conf > best_conf:
                    best_conf = conf
                    fraction = min(self.MAX_FRACTION, self.BASE_FRACTION + conf * 0.4)
                    best = (
                        pool["token0"], pool["token1"], pool,
                        f"VENDA-MOM  | Mom:{mom:.4f} MomL:{mom_l:.4f} RSI:{rsi:.1f} conf:{conf:.4f}",
                        fraction
                    )

        return best

    # ── Rebalanceamento agressivo de carteira ─────────────────────────────────

    def _rebalance_if_needed(self, pools: list):
        """
        Rebalanceia rapidamente se desequilíbrio > 3x.
        Usa até 18% do token mais abundante.
        """
        if not pools or not self._pnl_is_safe():
            return
        try:
            all_balances = self._get_balances_cached()
            if not all_balances:
                return
        except Exception:
            return

        best_token   = max(all_balances, key=lambda t: all_balances[t])
        best_balance = all_balances[best_token]
        if best_balance < 15:
            return

        worst_token   = min(all_balances, key=lambda t: all_balances[t])
        worst_balance = all_balances[worst_token]

        if worst_token == best_token or best_balance < worst_balance * 3:
            return

        amount = round(best_balance * 0.18, 4)
        amount = max(self.MIN_AMOUNT, min(amount, 100))

        try:
            self.log(f"[REBAL] {best_token[:10]}→{worst_token[:10]} ({amount:.2f})")
            self.client.swap(best_token, worst_token, amount, tag=self.tag)
            self.trade_count += 1
            # Invalidar cache após operação
            self._cache_time = 0.0
        except Exception as e:
            self.log(f"[REBAL] Falha: {e}")

    # ── Passo principal ───────────────────────────────────────────────────────

    def step(self):
        self._step_count += 1

        # 1. Cooldown pós-erro (muito curto)
        if time.time() - self._last_err_time < self.COOLDOWN_AFTER_ERR:
            return

        # 2. Circuit-breaker de drawdown (só verifica a cada 10 ciclos para poupar RPC)
        if self._step_count % 10 == 0 and not self._pnl_is_safe():
            time.sleep(2.0)
            return

        # 3. Obter pools
        pools = self.client.get_all_pools()
        if not pools:
            return

        # 4. Selecionar o melhor trade
        trade = self.get_best_trade(pools)

        if trade:
            token_in, token_out, pool, reason, fraction = trade
            self._no_signal_streak = 0

            # 5. Calcular montante (sem randomização excessiva — usa 90-110%)
            balance = self.client.get_balance(token_in)
            if balance <= self.MIN_AMOUNT:
                return

            raw_amount = min(self.MAX_AMOUNT, balance * fraction)
            amount = round(raw_amount * random.uniform(0.90, 1.10), 4)
            if amount < self.MIN_AMOUNT:
                return

            # 6. Verificação de price impact
            if not self._price_impact_ok(pool, token_in, amount):
                amount = round(amount * 0.6, 4)
                if amount < self.MIN_AMOUNT:
                    return

            # 7. Verificação de lucro esperado via quote
            if not self._expected_profit_ok(pool, token_in, token_out, amount):
                self.log(f"⚠ Quote desfavorável — trade ignorado: {reason}")
                return

            # 8. Verificar cooldown on-chain
            elapsed_since_trade = time.time() - self._last_trade_time
            if elapsed_since_trade < self.TRADER_COOLDOWN:
                wait_left = self.TRADER_COOLDOWN - elapsed_since_trade
                time.sleep(wait_left)

            # 9. Executar operação
            self.log(f"--- OPERACAO #{self.trade_count + 1} ---")
            self.log(f"Sinal  : {reason}")
            self.log(f"Amount : {amount:.4f} | Fracao: {fraction:.2%} | Saldo: {balance:.4f}")
            try:
                self.client.swap(token_in, token_out, amount, tag=self.tag)
                self.trade_count += 1
                self._last_trade_time = time.time()  # registar tempo do ultimo trade
                self._cache_time = 0.0               # invalidar cache apos swap
                self.log(f"[OK] Trade #{self.trade_count} concluido")
            except Exception as e:
                err_str = str(e)
                if 'cooldown' in err_str.lower():
                    self.log(f"[COOLDOWN] Aguardando cooldown on-chain...")
                    self._last_trade_time = time.time()  # forcar espera
                else:
                    self.log(f"[ERRO] Falha na operacao: {err_str[:120]}")
                self._last_err_time = time.time()

        else:
            # Sem sinal → rebalancear periodicamente e logar
            self._no_signal_streak += 1
            if self._no_signal_streak % 20 == 0:
                self.log(f"[WAIT] Aguardando sinal ({self._no_signal_streak} ciclos sem operacao)")
            if self._step_count % 6 == 0:
                self._rebalance_if_needed(pools)

    # ── Loop principal ────────────────────────────────────────────────────────

    def run(self):
        self.log("=================================================")
        self.log("  Bot28867 'The Predator v4' - INICIADO")
        self.log("  Estrategia: EMA + RSI + Momentum + Breakout")
        self.log("  Velocidade: ciclo a cada 0.3-0.8s")
        self.log("  Objectivo: TOP 1-3 em qualquer competicao")
        self.log("  ID: 28867 | Tag on-chain: 0x70C3")
        self.log("=================================================")

        while True:
            self.client.wait_until_active()
            self.log("Competicao ACTIVA - atacando o mercado!")

            # Registar portfólio inicial desta competição
            self._take_portfolio_snapshot()
            self._step_count = 0
            self._no_signal_streak = 0
            self._last_err_time = 0.0
            self._last_trade_time = 0.0

            while True:
                try:
                    status = self.client.get_competition_status()
                    if status["status"] != 1:
                        final_total = self._current_total()
                        pnl = final_total - self._initial_total
                        pnl_pct = (pnl / self._initial_total * 100) if self._initial_total > 0 else 0
                        self.log(f"Competicao encerrada. Operacoes: {self.trade_count}")
                        self.log(f"[PnL Final] {pnl:+.4f} ({pnl_pct:+.2f}%)")
                        break

                    self.step()
                    # Ciclo rapido: 0.3-0.8 segundos
                    time.sleep(random.uniform(self.SLEEP_MIN, self.SLEEP_MAX))

                except Exception as e:
                    self.log(f"Erro no ciclo principal: {e}")
                    time.sleep(1.0)


if __name__ == "__main__":
    bot = Bot28867()
    bot.run()
