import os
import time
import random
import math
from dotenv import load_dotenv
from bots.common.botBase import BaseBot

load_dotenv()

# ═══════════════════════════════════════════════════════════════════════════════
# Bot28867 — "The Mastermind v3"
#
# ARQUITECTURA ANTI-PnL-NEGATIVO:
#   1. Snapshot de portfólio no início: mede o valor total em cada token.
#   2. Verificação pre-trade (quote): só executa se o retorno esperado > custo.
#   3. Filtro de confiança elevado: MIN_CONFIDENCE aumentado para evitar ruído.
#   4. Circuit-breaker de drawdown: pausa se PnL cair abaixo de um limiar.
#   5. Cooldown pós-perda: espera mais entre operações após erro ou má saída.
#   6. Stop de alta volatilidade: não opera em mercado caótico.
#   7. Gestão de risco conservadora: fracções menores, mais seguras.
#   8. Confirmação multi-sinal: exige EMA + RSI + Momentum alinhados.
#
# Objectivo académico: nota mínima 15, máxima 18-19. PnL NUNCA negativo.
# ID do aluno: 28867  |  Tag on-chain: 0x70C3
# ═══════════════════════════════════════════════════════════════════════════════


class Bot28867(BaseBot):

    # ── Parâmetros de indicadores ─────────────────────────────────────────────
    EMA_FAST    = 7     # EMA rápida (mais sensível a mudanças recentes)
    EMA_SLOW    = 21    # EMA lenta (tendência de fundo)
    RSI_PERIOD  = 14    # período RSI de Wilder
    WARMUP      = 25    # ciclos mínimos antes de operar (acumular dados)
    HISTORY_MAX = 80    # tamanho máximo do histórico por pool

    # ── Limiares de sinal (conservadores para evitar falsos sinais) ───────────
    EMA_BULL_THRESH = 1.0008   # divergência mínima EMAs para compra
    EMA_BEAR_THRESH = 0.9992   # divergência mínima EMAs para venda
    RSI_OB          = 70       # overbought — não comprar acima
    RSI_OS          = 30       # oversold   — não vender abaixo
    RSI_BULL_MAX    = 62       # compra apenas se RSI < 62 (não sobrecomprado)
    RSI_BEAR_MIN    = 38       # venda apenas se RSI > 38 (não sobrevendido)
    MOM_BULL_MIN    = 0.001    # momentum positivo mínimo para compra
    MOM_BEAR_MAX    = -0.001   # momentum negativo mínimo para venda
    MAX_VOLATILITY  = 0.018    # volatilidade máxima tolerada (≈ 1.8% CV)

    # ── Gestão de risco (conservadora) ────────────────────────────────────────
    BASE_FRACTION  = 0.22    # fracção base do saldo por operação
    MAX_FRACTION   = 0.40    # fracção máxima (sinais excepcionais)
    MIN_AMOUNT     = 10      # montante mínimo em unidades de token
    MAX_AMOUNT     = 250     # montante máximo em unidades de token

    # ── Filtros de qualidade de sinal ─────────────────────────────────────────
    MIN_CONFIDENCE = 0.006   # confiança mínima absoluta para qualquer trade
    MIN_PROFIT_PCT = 0.0015  # retorno mínimo esperado (0.15%) após slippage

    # ── Protecção de capital ──────────────────────────────────────────────────
    MAX_DRAWDOWN_PCT   = 0.04   # pausa se portfólio cair > 4% do valor inicial
    COOLDOWN_AFTER_ERR = 5.0    # segundos de espera após erro/trade falhado
    CONSEC_LOSS_LIMIT  = 3      # nº máximo de "sem sinal" consecutivos antes de esperar

    def __init__(self):
        pk = os.getenv("EXT_BOT_0_PK")
        super().__init__(pk, "Bot28867", "trend")
        self.tag = "0x70C3"            # ID 28867 em hexadecimal

        # Histórico de preços e indicadores por pool
        self.history: dict = {}

        # Controlo de operações
        self.trade_count   = 0
        self._step_count   = 0
        self._no_signal_streak = 0    # ciclos consecutivos sem sinal
        self._last_err_time = 0.0     # timestamp do último erro

        # Snapshot de portfólio para controlo de PnL
        self._initial_portfolio: dict | None = None  # {token: saldo inicial}
        self._initial_total: float = 0.0             # valor total inicial (em unidades token0)

    # ── Logging ───────────────────────────────────────────────────────────────

    def log(self, message: str):
        print(f"[Bot28867 | ID-28867] {message}", flush=True)

    # ── Snapshot de portfólio ─────────────────────────────────────────────────

    def _take_portfolio_snapshot(self):
        """Regista o saldo inicial de todos os tokens no início da competição."""
        try:
            self._initial_portfolio = self.client.get_all_balances()
            self._initial_total = sum(self._initial_portfolio.values())
            self.log(f"📊 Portfólio inicial: {self._initial_total:.2f} unidades totais")
        except Exception as e:
            self.log(f"⚠ Não foi possível registar portfólio inicial: {e}")

    def _current_total(self) -> float:
        """Soma total dos saldos actuais."""
        try:
            balances = self.client.get_all_balances()
            return sum(balances.values())
        except Exception:
            return self._initial_total  # fallback seguro

    def _pnl_is_safe(self) -> bool:
        """
        Verifica se o PnL está acima do drawdown máximo permitido.
        Se o portfólio total caiu mais de MAX_DRAWDOWN_PCT em relação ao início,
        activa o circuit-breaker e pausa operações.
        """
        if self._initial_total <= 0:
            return True  # sem referência → não bloquear

        current = self._current_total()
        drawdown = (self._initial_total - current) / self._initial_total

        if drawdown > self.MAX_DRAWDOWN_PCT:
            self.log(
                f"🛑 CIRCUIT-BREAKER ACTIVADO: drawdown={drawdown:.2%} "
                f"(limite={self.MAX_DRAWDOWN_PCT:.2%}). Pausando operações."
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
        window = prices[-20:]
        if len(window) < 5:
            return 0.0
        mean = sum(window) / len(window)
        if mean == 0:
            return 0.0
        variance = sum((p - mean) ** 2 for p in window) / len(window)
        return math.sqrt(variance) / mean

    def _momentum(self, prices: list, period: int = 5) -> float:
        if len(prices) < period + 1:
            return 0.0
        return (prices[-1] - prices[-period - 1]) / prices[-period - 1]

    def _trend_strength(self, prices: list) -> float:
        """Força da tendência: declive normalizado da regressão linear simples."""
        n = min(len(prices), 15)
        if n < 5:
            return 0.0
        window = prices[-n:]
        x_mean = (n - 1) / 2.0
        y_mean = sum(window) / n
        num = sum((i - x_mean) * (window[i] - y_mean) for i in range(n))
        den = sum((i - x_mean) ** 2 for i in range(n))
        if den == 0 or y_mean == 0:
            return 0.0
        return (num / den) / y_mean  # declive normalizado pelo preço médio

    def get_indicators(self, pool_id: str) -> dict | None:
        hist = self.history.get(pool_id)
        if hist is None or len(hist["prices"]) < self.WARMUP:
            return None

        prices = hist["prices"]
        return {
            "ema_fast":      self._ema(prices, self.EMA_FAST),
            "ema_slow":      self._ema(prices, self.EMA_SLOW),
            "rsi":           self._rsi(hist["gains"], hist["losses"]),
            "volatility":    self._volatility(prices),
            "momentum":      self._momentum(prices),
            "trend_strength": self._trend_strength(prices),
            "last_price":    prices[-1],
        }

    # ── Verificação de lucratividade pré-trade ────────────────────────────────

    def _expected_profit_pct(self, pool, token_in: str, amount: float) -> float:
        """
        Usa o método quote() da DEX para calcular o retorno esperado
        e verifica se supera o custo mínimo de slippage.
        Retorna a percentagem de retorno esperada (positivo = lucro).
        """
        try:
            from web3 import Web3
            token_in_addr  = Web3.to_checksum_address(token_in)
            token_out_addr = Web3.to_checksum_address(
                pool["token0"] if token_in == pool["token1"] else pool["token1"]
            )
            token_data = self.client.tokens[token_in_addr]
            amount_wei = int(amount * (10 ** token_data["decimals"]))

            if amount_wei <= 0:
                return 0.0

            expected_out_wei = self.client.exchange.functions.quote(
                token_in_addr, token_out_addr, amount_wei
            ).call()

            token_out_data = self.client.tokens[token_out_addr]
            expected_out = expected_out_wei / (10 ** token_out_data["decimals"])

            # Retorno relativo ao input (usando preço actual como referência)
            if pool["price01"] > 0 and token_in == pool["token1"]:
                # Compramos token0 com token1: equivalente em token1 de saída
                fair_value = amount / pool["price01"]   # token0 que devíamos receber
                profit_pct = (expected_out - fair_value) / fair_value
            elif pool["price10"] > 0 and token_in == pool["token0"]:
                # Vendemos token0 por token1
                fair_value = amount / pool["price10"]   # token1 que devíamos receber
                profit_pct = (expected_out - fair_value) / fair_value
            else:
                profit_pct = 0.0

            return profit_pct
        except Exception:
            return 0.0  # em caso de erro, não operar

    # ── Seleção do melhor trade ───────────────────────────────────────────────

    def get_best_trade(self, pools: list) -> tuple | None:
        """
        Avalia todos os pools e retorna o trade de MAIOR CONFIANÇA que:
          - Tem todos os indicadores alinhados (EMA + RSI + Momentum)
          - Não está em zona de alta volatilidade
          - Tem retorno esperado positivo (via quote)
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
            trend  = ind["trend_strength"]

            # ── Bloqueio de alta volatilidade ─────────────────────────────
            if vol > self.MAX_VOLATILITY:
                continue  # mercado instável → não operar

            # ── Sinal de COMPRA (Bullish) ─────────────────────────────────
            # Condição: EMA fast > EMA slow AND RSI saudável AND Momentum positivo
            if (ema_f > ema_s * self.EMA_BULL_THRESH
                    and rsi < self.RSI_BULL_MAX
                    and rsi > self.RSI_OS
                    and mom > self.MOM_BULL_MIN
                    and trend > 0):

                conf = (ema_f / ema_s - 1.0) * 15 + mom * 8 + max(0, trend) * 5

                # Bónus RSI em zona óptima (42-58: nem quente nem frio)
                if 42 < rsi < 58:
                    conf *= 1.25
                elif rsi < 45:
                    conf *= 1.10

                # Bónus momentum forte
                if mom > 0.015:
                    conf *= 1.20

                if conf > best_conf:
                    best_conf = conf
                    fraction = min(self.MAX_FRACTION,
                                   self.BASE_FRACTION + conf * 0.4)
                    best = (
                        pool["token1"], pool["token0"], pool,
                        f"COMPRA | EMA:{ema_f:.5f}>{ema_s:.5f} RSI:{rsi:.1f} Mom:{mom:.4f}",
                        fraction
                    )

            # ── Sinal de VENDA (Bearish) ──────────────────────────────────
            # Condição: EMA fast < EMA slow AND RSI saudável AND Momentum negativo
            elif (ema_f < ema_s * self.EMA_BEAR_THRESH
                  and rsi > self.RSI_BEAR_MIN
                  and rsi < self.RSI_OB
                  and mom < self.MOM_BEAR_MAX
                  and trend < 0):

                conf = (ema_s / ema_f - 1.0) * 15 + abs(mom) * 8 + abs(min(0, trend)) * 5

                if 42 < rsi < 58:
                    conf *= 1.25
                elif rsi > 55:
                    conf *= 1.10

                if mom < -0.015:
                    conf *= 1.20

                if conf > best_conf:
                    best_conf = conf
                    fraction = min(self.MAX_FRACTION,
                                   self.BASE_FRACTION + conf * 0.4)
                    best = (
                        pool["token0"], pool["token1"], pool,
                        f"VENDA  | EMA:{ema_f:.5f}<{ema_s:.5f} RSI:{rsi:.1f} Mom:{mom:.4f}",
                        fraction
                    )

        return best

    # ── Rebalanceamento de carteira ───────────────────────────────────────────

    def _rebalance_if_needed(self, pools: list):
        """
        Garante liquidez nos tokens menos representados.
        Só rebalanceia se o desequilíbrio for > 4x e o PnL estiver seguro.
        """
        if not pools or not self._pnl_is_safe():
            return
        try:
            all_balances = self.client.get_all_balances()
        except Exception:
            return

        best_token   = max(all_balances, key=lambda t: all_balances[t])
        best_balance = all_balances[best_token]
        if best_balance < 20:
            return

        worst_token   = min(all_balances, key=lambda t: all_balances[t])
        worst_balance = all_balances[worst_token]

        if worst_token == best_token or best_balance < worst_balance * 4:
            return

        amount = round(best_balance * 0.15, 4)
        amount = max(self.MIN_AMOUNT, min(amount, 80))

        try:
            self.log(f"[REBALANCEAMENTO] {best_token[:10]}→{worst_token[:10]} ({amount:.2f})")
            self.client.swap(best_token, worst_token, amount, tag=self.tag)
            self.trade_count += 1
        except Exception as e:
            self.log(f"[REBALANCEAMENTO] Falha: {e}")

    # ── Passo principal ───────────────────────────────────────────────────────

    def step(self):
        self._step_count += 1

        # 1. Cooldown pós-erro: aguarda antes de tentar novamente
        if time.time() - self._last_err_time < self.COOLDOWN_AFTER_ERR:
            return

        # 2. Circuit-breaker de drawdown
        if not self._pnl_is_safe():
            time.sleep(3.0)  # esperar e verificar novamente depois
            return

        # 3. Obter pools
        pools = self.client.get_all_pools()
        if not pools:
            self.log("Sem pools disponíveis.")
            return

        # 4. Selecionar o melhor trade com todos os filtros aplicados
        trade = self.get_best_trade(pools)

        if trade:
            token_in, token_out, pool, reason, fraction = trade
            self._no_signal_streak = 0  # reset do streak

            # 5. Calcular montante
            amount = self.amount_from_balance(
                token_in, fraction, self.MAX_AMOUNT, self.MIN_AMOUNT
            )
            if not amount:
                self.log(f"Saldo insuficiente para: {reason}")
                return

            # 6. Verificação de lucratividade pré-trade
            profit_pct = self._expected_profit_pct(pool, token_in, amount)
            if profit_pct < self.MIN_PROFIT_PCT:
                self.log(
                    f"⚠ Trade rejeitado (retorno={profit_pct:.4%} < min={self.MIN_PROFIT_PCT:.4%}): {reason}"
                )
                return

            # 7. Executar operação
            self.log(f"━━━ OPERAÇÃO #{self.trade_count + 1} ━━━")
            self.log(f"Sinal    : {reason}")
            self.log(f"Amount   : {amount:.4f} | Fracção: {fraction:.2%}")
            self.log(f"Retorno≈ : {profit_pct:.4%}")
            try:
                self.client.swap(token_in, token_out, amount, tag=self.tag)
                self.trade_count += 1
                self.log(f"✔ Operação concluída. Total trades: {self.trade_count}")
            except Exception as e:
                self.log(f"✘ Erro na operação: {e}")
                self._last_err_time = time.time()  # activar cooldown

        else:
            # Sem sinal → streak tracking e rebalanceamento periódico
            self._no_signal_streak += 1
            if self._no_signal_streak % 15 == 0:
                self.log(f"💤 Aguardando sinal ({self._no_signal_streak} ciclos sem operação)...")
            if self._step_count % 12 == 0:
                self._rebalance_if_needed(pools)

    # ── Loop principal ────────────────────────────────────────────────────────

    def run(self):
        self.log("══════════════════════════════════════════════════")
        self.log("  Bot28867 'The Mastermind v3' — INICIADO")
        self.log("  Estratégia: EMA + RSI + Momentum + Trend + Quote")
        self.log("  Protecção: Circuit-Breaker + Pre-Trade PnL Check")
        self.log("  Alvo: nota 15-19 | PnL NUNCA negativo")
        self.log("  ID: 28867 | Tag on-chain: 0x70C3")
        self.log("══════════════════════════════════════════════════")

        while True:
            self.client.wait_until_active()
            self.log("Competição ACTIVA — estratégia em execução.")

            # Registar portfólio inicial desta competição
            self._take_portfolio_snapshot()
            self._step_count = 0
            self._no_signal_streak = 0
            self._last_err_time = 0.0

            while True:
                try:
                    status = self.client.get_competition_status()
                    if status["status"] != 1:
                        final_total = self._current_total()
                        pnl = final_total - self._initial_total
                        pnl_pct = (pnl / self._initial_total * 100) if self._initial_total > 0 else 0
                        self.log(f"Competição encerrada. Operações: {self.trade_count}")
                        self.log(f"PnL Final: {pnl:+.4f} ({pnl_pct:+.2f}%)")
                        break

                    self.step()
                    time.sleep(random.uniform(0.4, 0.8))

                except Exception as e:
                    self.log(f"Erro no ciclo principal: {e}")
                    time.sleep(2.0)


if __name__ == "__main__":
    bot = Bot28867()
    bot.run()
