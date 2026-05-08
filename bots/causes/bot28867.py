import os
import time
import random
import math
from dotenv import load_dotenv
from bots.common.botBase import BaseBot

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# Bot28867 — "The Mastermind v2"
#
# Estratégia multi-sinal de alta precisão:
#   1. EMA dupla (fast=7 / slow=21) para tendência
#   2. RSI(14) para filtro de momentum e deteção de reversão
#   3. Volatilidade (std) para ajuste dinâmico de confiança
#   4. Gestão de risco adaptativa: fracções de saldo ajustadas por confiança
#   5. Rebalanceamento agressivo: converte tokens fracos em tokens fortes
#
# Objectivo académico: nota mínima 15, máxima 18-19 (nunca abaixo de 10).
# ID do aluno: 28867  |  Tag on-chain: 0x70C3
# ─────────────────────────────────────────────────────────────────────────────

class Bot28867(BaseBot):

    # ── Parâmetros de estratégia ──────────────────────────────────────────────
    EMA_FAST       = 7       # período EMA rápida
    EMA_SLOW       = 21      # período EMA lenta
    RSI_PERIOD     = 14      # período RSI
    WARMUP         = 22      # mínimo de preços necessários
    HISTORY_MAX    = 60      # tamanho máximo do histórico por pool

    # Limiares de sinal
    EMA_BULL_THRESH = 1.0005  # EMA fast > EMA slow * thresh  → compra
    EMA_BEAR_THRESH = 0.9995  # EMA fast < EMA slow * thresh  → venda
    RSI_OB          = 72      # RSI acima → overbought (evitar compras)
    RSI_OS          = 28      # RSI abaixo → oversold  (evitar vendas)
    RSI_BULL_MAX    = 68      # RSI máx. aceitável para sinal de compra
    RSI_BEAR_MIN    = 32      # RSI mín. aceitável para sinal de venda

    # Gestão de risco
    BASE_FRACTION   = 0.30   # fracção base do saldo por operação
    MAX_FRACTION    = 0.55   # fracção máxima (sinais de muito alta confiança)
    MIN_AMOUNT      = 8      # montante mínimo em unidades de token
    MAX_AMOUNT      = 350    # montante máximo em unidades de token
    MIN_CONFIDENCE  = 0.003  # confiança mínima para executar trade

    def __init__(self):
        pk = os.getenv("EXT_BOT_0_PK")
        super().__init__(pk, "Bot28867", "trend")
        self.tag  = "0x70C3"              # ID 28867 em hexadecimal
        self.history: dict = {}           # histórico por pool_id
        self.trade_count = 0              # contador de operações
        self._step_count = 0             # contador de ciclos

    # ── Logging ───────────────────────────────────────────────────────────────

    def log(self, message: str):
        print(f"[Bot28867 | ID-28867] {message}", flush=True)

    # ── Gestão de histórico ───────────────────────────────────────────────────

    def _init_pool(self, pool_id):
        self.history[pool_id] = {
            "prices": [],
            "gains":  [],
            "losses": [],
        }

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

    # ── Cálculos de indicadores ───────────────────────────────────────────────

    def _ema(self, prices: list, period: int) -> float:
        """EMA exponencialmente ponderada."""
        alpha = 2.0 / (period + 1)
        ema = prices[0]
        for p in prices[1:]:
            ema = alpha * p + (1 - alpha) * ema
        return ema

    def _rsi(self, gains: list, losses: list) -> float:
        """RSI de Wilder."""
        period = self.RSI_PERIOD
        if len(gains) < period:
            return 50.0
        g = gains[-period:]
        l = losses[-period:]
        avg_gain = sum(g) / period
        avg_loss = sum(l) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def _volatility(self, prices: list) -> float:
        """Desvio padrão normalizado (coeff. de variação) dos últimos 20 preços."""
        window = prices[-20:]
        if len(window) < 5:
            return 0.0
        mean = sum(window) / len(window)
        if mean == 0:
            return 0.0
        variance = sum((p - mean) ** 2 for p in window) / len(window)
        return math.sqrt(variance) / mean  # coeff. de variação

    def _momentum(self, prices: list, period: int = 5) -> float:
        """Momentum: variação percentual nos últimos N preços."""
        if len(prices) < period + 1:
            return 0.0
        return (prices[-1] - prices[-period - 1]) / prices[-period - 1]

    def get_indicators(self, pool_id: str) -> dict | None:
        hist = self.history.get(pool_id)
        if hist is None or len(hist["prices"]) < self.WARMUP:
            return None

        prices = hist["prices"]
        return {
            "ema_fast":   self._ema(prices, self.EMA_FAST),
            "ema_slow":   self._ema(prices, self.EMA_SLOW),
            "rsi":        self._rsi(hist["gains"], hist["losses"]),
            "volatility": self._volatility(prices),
            "momentum":   self._momentum(prices),
            "last_price": prices[-1],
        }

    # ── Seleção do melhor trade ───────────────────────────────────────────────

    def get_best_trade(self, pools: list) -> tuple | None:
        """
        Avalia todos os pools e retorna o trade de maior confiança,
        com discriminação de direcção (compra/venda) e fracção de saldo.
        Retorna (token_in, token_out, reason, confidence_fraction)
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

            ema_f = ind["ema_fast"]
            ema_s = ind["ema_slow"]
            rsi   = ind["rsi"]
            vol   = ind["volatility"]
            mom   = ind["momentum"]

            # ── Sinal de COMPRA (Bullish) ─────────────────────────────────
            if (ema_f > ema_s * self.EMA_BULL_THRESH
                    and rsi < self.RSI_BULL_MAX
                    and rsi > self.RSI_OS):

                # Confiança = divergência das EMAs + momentum positivo
                conf = (ema_f / ema_s - 1.0) * 10 + max(0, mom) * 5

                # Bónus se RSI estiver em zona neutra/saudável (40-60)
                if 40 < rsi < 60:
                    conf *= 1.20

                # Bónus por momentum positivo forte
                if mom > 0.01:
                    conf *= 1.15

                # Penalidade em alta volatilidade (risco de reversão)
                if vol > 0.02:
                    conf *= 0.90

                if conf > best_conf:
                    best_conf = conf
                    fraction = min(self.MAX_FRACTION,
                                   self.BASE_FRACTION + conf * 0.5)
                    best = (
                        pool["token1"], pool["token0"],
                        f"COMPRA | EMA:{ema_f:.4f}>{ema_s:.4f} RSI:{rsi:.1f} Mom:{mom:.4f}",
                        fraction
                    )

            # ── Sinal de VENDA (Bearish) ──────────────────────────────────
            elif (ema_f < ema_s * self.EMA_BEAR_THRESH
                  and rsi > self.RSI_BEAR_MIN
                  and rsi < self.RSI_OB):

                conf = (ema_s / ema_f - 1.0) * 10 + max(0, -mom) * 5

                if 40 < rsi < 60:
                    conf *= 1.20

                if mom < -0.01:
                    conf *= 1.15

                if vol > 0.02:
                    conf *= 0.90

                if conf > best_conf:
                    best_conf = conf
                    fraction = min(self.MAX_FRACTION,
                                   self.BASE_FRACTION + conf * 0.5)
                    best = (
                        pool["token0"], pool["token1"],
                        f"VENDA  | EMA:{ema_f:.4f}<{ema_s:.4f} RSI:{rsi:.1f} Mom:{mom:.4f}",
                        fraction
                    )

            # ── Sinal de REVERSÃO RSI (contrarian de curto prazo) ─────────
            elif rsi < self.RSI_OS and mom > -0.005:
                # Oversold + momentum a estabilizar → potencial bounce
                conf = (self.RSI_OS - rsi) / 100.0 * 0.8
                if conf > best_conf:
                    best_conf = conf
                    fraction = self.BASE_FRACTION * 0.8  # posição mais cautelosa
                    best = (
                        pool["token1"], pool["token0"],
                        f"REVERSÃO ALTA | RSI={rsi:.1f} (oversold)",
                        fraction
                    )

            elif rsi > self.RSI_OB and mom < 0.005:
                # Overbought + momentum a estabilizar → potencial queda
                conf = (rsi - self.RSI_OB) / 100.0 * 0.8
                if conf > best_conf:
                    best_conf = conf
                    fraction = self.BASE_FRACTION * 0.8
                    best = (
                        pool["token0"], pool["token1"],
                        f"REVERSÃO BAIXA | RSI={rsi:.1f} (overbought)",
                        fraction
                    )

        return best

    # ── Rebalanceamento de carteira ───────────────────────────────────────────

    def _rebalance_if_needed(self, pools: list):
        """
        Se o bot ficar sem saldo em token base, converte tokens secundários
        para garantir que sempre tem capacidade de operar.
        """
        if not pools:
            return

        # Verificar balances de todos os tokens
        try:
            all_balances = self.client.get_all_balances()
        except Exception:
            return

        # Token com maior saldo
        best_token = max(all_balances, key=lambda t: all_balances[t])
        best_balance = all_balances[best_token]

        if best_balance < 15:
            return  # nada a fazer

        # Token com menor saldo (candidato a receber)
        worst_token = min(all_balances, key=lambda t: all_balances[t])
        if worst_token == best_token:
            return

        worst_balance = all_balances[worst_token]

        # Só rebalanceia se o desequilíbrio for significativo
        if best_balance < worst_balance * 3:
            return

        # Transferir ~20% do token abundante para o mais escasso
        amount = round(best_balance * 0.20, 4)
        amount = max(self.MIN_AMOUNT, min(amount, 100))

        try:
            self.log(f"[REBALANCEAMENTO] {best_token[:8]}…→{worst_token[:8]}… ({amount:.2f})")
            self.client.swap(best_token, worst_token, amount, tag=self.tag)
            self.trade_count += 1
        except Exception as e:
            self.log(f"[REBALANCEAMENTO] Falha: {e}")

    # ── Passo principal ───────────────────────────────────────────────────────

    def step(self):
        self._step_count += 1
        pools = self.client.get_all_pools()

        if not pools:
            self.log("Sem pools disponíveis.")
            return

        trade = self.get_best_trade(pools)

        if trade:
            token_in, token_out, reason, fraction = trade

            amount = self.amount_from_balance(
                token_in,
                fraction,
                self.MAX_AMOUNT,
                self.MIN_AMOUNT
            )

            if amount:
                self.log(f"━━━ OPERAÇÃO #{self.trade_count + 1} ━━━")
                self.log(f"Sinal  : {reason}")
                self.log(f"Amount : {amount:.4f} | Fracção: {fraction:.2%}")
                try:
                    self.client.swap(token_in, token_out, amount, tag=self.tag)
                    self.trade_count += 1
                    self.log(f"✔ Operação concluída. Total trades: {self.trade_count}")
                except Exception as e:
                    self.log(f"✘ Erro na operação: {e}")
            else:
                self.log(f"Saldo insuficiente para: {reason}")
        else:
            # Sem sinal forte → verificar rebalanceamento a cada 10 ciclos
            if self._step_count % 10 == 0:
                self._rebalance_if_needed(pools)

    # ── Loop principal ────────────────────────────────────────────────────────

    def run(self):
        self.log("══════════════════════════════════════════")
        self.log("  Bot28867 'The Mastermind v2' — INICIADO")
        self.log("  Estratégia: EMA + RSI + Momentum + Rebalanceamento")
        self.log("  Alvo académico: nota 15-19 (nunca abaixo de 10)")
        self.log("  ID: 28867 | Tag on-chain: 0x70C3")
        self.log("══════════════════════════════════════════")

        while True:
            self.client.wait_until_active()
            self.log("Competição ACTIVA — estratégia em execução.")

            while True:
                try:
                    status = self.client.get_competition_status()
                    if status["status"] != 1:
                        self.log(f"Competição encerrada. Total de operações: {self.trade_count}")
                        break

                    self.step()
                    # Intervalo agressivo: 0.3-0.7 s para capturar mais oportunidades
                    time.sleep(random.uniform(0.3, 0.7))

                except Exception as e:
                    self.log(f"Erro no ciclo: {e}")
                    time.sleep(1.5)


if __name__ == "__main__":
    bot = Bot28867()
    bot.run()
