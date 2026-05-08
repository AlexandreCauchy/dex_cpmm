import os
import time
from typing import Dict, List, Any
from dotenv import load_dotenv
from web3 import Web3

from bots.common.config import CONFIG

load_dotenv()

_SLIPPAGE_PCT = CONFIG["slippage"]["max_price_impact_pct"]
_RETRY_MAX = CONFIG["retry"]["max_attempts"]
_RETRY_DELAY = CONFIG["retry"]["delay_seconds"]

EXCHANGE_ABI = [
    {
        "inputs": [],
        "name": "getTokens",
        "outputs": [{"internalType": "address[]", "name": "", "type": "address[]"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "getPoolIds",
        "outputs": [{"internalType": "bytes32[]", "name": "", "type": "bytes32[]"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"internalType": "bytes32", "name": "poolId", "type": "bytes32"}],
        "name": "getPool",
        "outputs": [
            {"internalType": "bool", "name": "exists", "type": "bool"},
            {"internalType": "address", "name": "token0", "type": "address"},
            {"internalType": "address", "name": "token1", "type": "address"},
            {"internalType": "uint256", "name": "reserve0", "type": "uint256"},
            {"internalType": "uint256", "name": "reserve1", "type": "uint256"}
        ],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [
            {"internalType": "address", "name": "tokenIn", "type": "address"},
            {"internalType": "address", "name": "tokenOut", "type": "address"},
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"}
        ],
        "name": "quote",
        "outputs": [{"internalType": "uint256", "name": "amountOut", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [
            {"internalType": "address", "name": "tokenIn", "type": "address"},
            {"internalType": "address", "name": "tokenOut", "type": "address"},
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "uint256", "name": "minAmountOut", "type": "uint256"}
        ],
        "name": "swap",
        "outputs": [{"internalType": "uint256", "name": "amountOut", "type": "uint256"}],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "getCompetitionStatus",
        "outputs": [
            {"internalType": "uint8", "name": "status", "type": "uint8"},
            {"internalType": "uint256", "name": "startTime", "type": "uint256"},
            {"internalType": "uint256", "name": "endTime", "type": "uint256"}
        ],
        "stateMutability": "view",
        "type": "function"
    }
]

ERC20_ABI = [
    {
        "inputs": [{"internalType": "address", "name": "owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "symbol",
        "outputs": [{"internalType": "string", "name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [
            {"internalType": "address", "name": "owner", "type": "address"},
            {"internalType": "address", "name": "spender", "type": "address"}
        ],
        "name": "allowance",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [
            {"internalType": "address", "name": "spender", "type": "address"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"}
        ],
        "name": "approve",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function"
    }
]


class DexClient:
    def __init__(self, private_key: str):
        if not private_key:
            raise RuntimeError("Private key ausente")

        self.rpc_url = os.getenv("RPC_URL", "http://127.0.0.1:8545")
        self.exchange_address = Web3.to_checksum_address(os.getenv("EXCHANGE_ADDRESS"))

        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        if not self.w3.is_connected():
            raise RuntimeError("Nao foi possivel conectar ao RPC")

        self.account = self.w3.eth.account.from_key(private_key)
        self.address = self.account.address
        self.private_key = private_key

        self.exchange = self.w3.eth.contract(
            address=self.exchange_address,
            abi=EXCHANGE_ABI
        )

        self.tokens = {}
        self.token_addresses = []
        self._load_tokens()

    def _load_tokens(self):
        addresses = self.exchange.functions.getTokens().call()

        self.token_addresses = [Web3.to_checksum_address(a) for a in addresses]

        for address in self.token_addresses:
            contract = self.w3.eth.contract(address=address, abi=ERC20_ABI)
            symbol = contract.functions.symbol().call()
            decimals = contract.functions.decimals().call()

            self.tokens[address] = {
                "address": address,
                "contract": contract,
                "symbol": symbol,
                "decimals": decimals
            }

    def _build_and_send(self, tx_function, tag: str = None):
        nonce = self.w3.eth.get_transaction_count(self.address)
        tx = tx_function.build_transaction({
            "from": self.address,
            "nonce": nonce,
            "gas": 700_000,
            "gasPrice": self.w3.eth.gas_price
        })

        if tag:
            # Append tag as hex data at the end of the transaction data
            # Ensure tag is hex and starts with 0x if not already
            clean_tag = tag[2:] if tag.startswith("0x") else tag
            tx["data"] += clean_tag

        signed = self.w3.eth.account.sign_transaction(
            tx,
            private_key=self.private_key
        )

        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        return self.w3.eth.wait_for_transaction_receipt(tx_hash)

    def _send_with_retry(self, tx_function, tag: str = None):
        last_error = None

        for attempt in range(1, _RETRY_MAX + 1):
            try:
                return self._build_and_send(tx_function, tag)
            except Exception as e:
                last_error = e
                print(f"[{self.address}] tx attempt {attempt} failed: {e}", flush=True)
                time.sleep(_RETRY_DELAY)

        raise last_error

    def get_competition_status(self):
        status, start_time, end_time = self.exchange.functions.getCompetitionStatus().call()

        return {
            "status": int(status),
            "start_time": int(start_time),
            "end_time": int(end_time)
        }

    def wait_until_active(self, interval_seconds=2):
        while True:
            try:
                data = self.get_competition_status()

                if data["status"] == 1:
                    return

                print(f"[{self.address}] waiting for competition...", flush=True)
            except Exception as e:
                print(f"[{self.address}] status error: {e}", flush=True)

            time.sleep(interval_seconds)

    def get_symbol(self, token_address: str) -> str:
        address = Web3.to_checksum_address(token_address)
        return self.tokens[address]["symbol"]

    def get_balance(self, token_address: str) -> float:
        address = Web3.to_checksum_address(token_address)
        token = self.tokens[address]
        raw = token["contract"].functions.balanceOf(self.address).call()

        return raw / (10 ** token["decimals"])

    def get_all_balances(self) -> Dict[str, float]:
        return {
            address: self.get_balance(address)
            for address in self.token_addresses
        }

    def get_pool_ids(self):
        return self.exchange.functions.getPoolIds().call()

    def get_pool(self, pool_id) -> Dict[str, Any]:
        exists, token0, token1, reserve0, reserve1 = self.exchange.functions.getPool(pool_id).call()

        token0 = Web3.to_checksum_address(token0)
        token1 = Web3.to_checksum_address(token1)

        d0 = self.tokens[token0]["decimals"]
        d1 = self.tokens[token1]["decimals"]

        r0 = reserve0 / (10 ** d0)
        r1 = reserve1 / (10 ** d1)

        return {
            "exists": exists,
            "pool_id": pool_id,
            "token0": token0,
            "token1": token1,
            "symbol0": self.tokens[token0]["symbol"],
            "symbol1": self.tokens[token1]["symbol"],
            "reserve0": r0,
            "reserve1": r1,
            "reserve0_wei": reserve0,
            "reserve1_wei": reserve1,
            "price01": r1 / r0 if r0 > 0 else 0,
            "price10": r0 / r1 if r1 > 0 else 0,
            "pair": f"{self.tokens[token0]['symbol']}/{self.tokens[token1]['symbol']}"
        }

    def get_all_pools(self) -> List[Dict[str, Any]]:
        pools = []

        for pool_id in self.get_pool_ids():
            pool = self.get_pool(pool_id)

            if pool["exists"]:
                pools.append(pool)

        return pools

    def ensure_approval(self, token_address: str, amount_wei: int):
        token_address = Web3.to_checksum_address(token_address)
        contract = self.tokens[token_address]["contract"]

        allowance = contract.functions.allowance(
            self.address,
            self.exchange_address
        ).call()

        if allowance >= amount_wei:
            return

        receipt = self._send_with_retry(
            contract.functions.approve(self.exchange_address, amount_wei * 2)
        )

        print(f"[{self.address}] approve {self.get_symbol(token_address)}: {receipt.transactionHash.hex()}", flush=True)

    def swap(self, token_in: str, token_out: str, amount_in: float, tag: str = None):
        token_in = Web3.to_checksum_address(token_in)
        token_out = Web3.to_checksum_address(token_out)

        if token_in == token_out:
            raise ValueError("token_in e token_out iguais")

        token = self.tokens[token_in]
        amount_wei = int(amount_in * (10 ** token["decimals"]))

        if amount_wei <= 0:
            raise ValueError("amount_in invalido")

        self.ensure_approval(token_in, amount_wei)

        expected = self.exchange.functions.quote(
            token_in,
            token_out,
            amount_wei
        ).call()

        amount_out_min = int(expected * (1 - _SLIPPAGE_PCT / 100))

        receipt = self._send_with_retry(
            self.exchange.functions.swap(
                token_in,
                token_out,
                amount_wei,
                amount_out_min
            ),
            tag=tag
        )

        print(
            f"[{self.address}] SWAP {amount_in:.4f} {self.get_symbol(token_in)} -> "
            f"{self.get_symbol(token_out)} | tx={receipt.transactionHash.hex()}",
            flush=True
        )

        return receipt