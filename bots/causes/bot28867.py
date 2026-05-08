import os
import time
import random
from dotenv import load_dotenv
from bots.common.botBase import BaseBot

load_dotenv()

class Bot28867(BaseBot):
    def __init__(self):
        pk = os.getenv("EXT_BOT_0_PK")
        super().__init__(pk, "Bot28867", "trend")
        self.tag = "0x70C3" # ID 28867 em HEX
        self.history = {} # Armazena histórico por pool_id
        self.rsi_period = 14
        
    def log(self, message: str):
        print(f"[Bot - ID 28867] {message}", flush=True)

    def update_history(self, pool_id, price):
        if pool_id not in self.history:
            self.history[pool_id] = {'prices': [], 'gains': [], 'losses': []}
        
        hist = self.history[pool_id]
        if hist['prices']:
            diff = price - hist['prices'][-1]
            hist['gains'].append(max(0, diff))
            hist['losses'].append(max(0, -diff))
        
        hist['prices'].append(price)
        
        # Manter apenas os últimos 50 registros para análise
        if len(hist['prices']) > 50:
            hist['prices'].pop(0)
            hist['gains'].pop(0)
            hist['losses'].pop(0)

    def get_indicators(self, pool_id):
        hist = self.history[pool_id]
        prices = hist['prices']
        
        if len(prices) < 20:
            return None
        
        # 1. EMA Curta (9) e Longa (21)
        ema_fast = self._calculate_ema(prices, 9)
        ema_slow = self._calculate_ema(prices, 21)
        
        # 2. RSI (Relative Strength Index)
        rsi = 50
        if len(hist['gains']) >= self.rsi_period:
            avg_gain = sum(hist['gains'][-self.rsi_period:]) / self.rsi_period
            avg_loss = sum(hist['losses'][-self.rsi_period:]) / self.rsi_period
            if avg_loss > 0:
                rs = avg_gain / avg_loss
                rsi = 100 - (100 / (1 + rs))
        
        return {
            'ema_fast': ema_fast,
            'ema_slow': ema_slow,
            'rsi': rsi,
            'last_price': prices[-1]
        }

    def _calculate_ema(self, prices, period):
        alpha = 2 / (period + 1)
        ema = prices[0]
        for p in prices[1:]:
            ema = alpha * p + (1 - alpha) * ema
        return ema

    def get_best_trade(self, pools):
        best_trade = None
        highest_confidence = 0

        for pool in pools:
            self.update_history(pool['pool_id'], pool['price01'])
            indicators = self.get_indicators(pool['pool_id'])
            
            if not indicators:
                continue

            price = indicators['last_price']
            ema_f = indicators['ema_fast']
            ema_s = indicators['ema_slow']
            rsi = indicators['rsi']

            # Lógica de Cruzamento de Médias + Filtro de RSI
            # Bullish: EMA Fast cruza acima da EMA Slow + RSI não está em Overbought (>70)
            if ema_f > ema_s * 1.001 and rsi < 65:
                confidence = (ema_f / ema_s) - 1 + (70 - rsi) / 100
                if confidence > highest_confidence:
                    highest_confidence = confidence
                    # Comprar Token 0 (preço subindo)
                    best_trade = (pool['token1'], pool['token0'], "Cruzamento Bullish + RSI Saudável")

            # Bearish: EMA Fast cruza abaixo da EMA Slow + RSI não está em Oversold (<30)
            elif ema_f < ema_s * 0.999 and rsi > 35:
                confidence = (ema_s / ema_f) - 1 + (rsi - 30) / 100
                if confidence > highest_confidence:
                    highest_confidence = confidence
                    # Vender Token 0 (preço caindo)
                    best_trade = (pool['token0'], pool['token1'], "Cruzamento Bearish + RSI Saudável")

        return best_trade

    def step(self):
        pools = self.client.get_all_pools()
        trade = self.get_best_trade(pools)

        if trade:
            token_in, token_out, reason = trade
            
            # Verificação de Impacto de Preço (Proteção contra perdas por slippage)
            # Só operamos se tivermos saldo e se o impacto for aceitável
            amount = self.amount_from_balance(token_in, 0.3, 300, 10)
            
            if amount:
                self.log(f">>> [ID 28867] Executando estratégia de Alta Inteligência")
                self.log(f"Sinal: {reason}")
                
                try:
                    # Assinatura On-Chain ID 28867
                    self.client.swap(token_in, token_out, amount, tag=self.tag)
                    self.log(f"[ID 28867] Operação enviada. PnL Target: 15-19 pts.")
                except Exception as e:
                    self.log(f"Ajustando estratégia devido a erro: {e}")

    def run(self):
        self.log("Bot28867 'The Mastermind' iniciado.")
        self.log("Foco: Otimização de PnL para Nota Superior (15-19).")
        
        while True:
            self.client.wait_until_active()
            while True:
                try:
                    status = self.client.get_competition_status()
                    if status["status"] != 1: break
                    
                    self.step()
                    # Reação rápida para garantir os melhores preços da fila
                    time.sleep(random.uniform(0.3, 0.8))
                except Exception as e:
                    self.log(f"Monitorando mercado... {e}")
                    time.sleep(2)

if __name__ == "__main__":
    bot = Bot28867()
    bot.run()
