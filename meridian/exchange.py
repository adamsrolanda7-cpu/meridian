"""Adaptateur exchange ccxt pour le compte réel Meridian.

Ce module ne prend aucune décision de trading. Il expose uniquement :

- un instantané du compte (soldes libres/totaux, prix, equity) ;
- l'arrondi des quantités et des prix aux règles de l'exchange ;
- l'envoi d'un ordre spot déjà validé par ``live_trade.py``.

ccxt reste une dépendance optionnelle : ``pip install -e ".[live]"``.
Aucune clé n'est lue ici : les credentials sont passés explicitement par
l'appelant, qui les a lus depuis l'environnement.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


class ExchangeError(RuntimeError):
    """Erreur de connexion, de compte ou d'ordre sur l'exchange."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def split_symbol(asset: str, quote: str) -> str:
    """``BTCUSDT`` + ``USDT`` -> ``BTC/USDT``."""
    quote = quote.upper()
    asset = asset.upper()
    if not asset.endswith(quote):
        raise ExchangeError(f"actif {asset} incompatible avec la devise {quote}")
    base = asset[: -len(quote)]
    if not base:
        raise ExchangeError(f"actif invalide: {asset}")
    return f"{base}/{quote}"


@dataclass
class Balance:
    asset: str
    free: float = 0.0
    total: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"asset": self.asset, "free": self.free, "total": self.total}


@dataclass
class AccountSnapshot:
    """Photo du compte à un instant donné."""

    exchange_id: str
    sandbox: bool
    quote: str
    balances: dict[str, Balance] = field(default_factory=dict)
    prices: dict[str, float] = field(default_factory=dict)
    equity: float = 0.0
    fetched_at: str = field(default_factory=_now)

    def balance(self, asset: str) -> Balance:
        return self.balances.get(asset, Balance(asset))

    def free(self, asset: str) -> float:
        return self.balance(asset).free

    def total(self, asset: str) -> float:
        return self.balance(asset).total

    def weight(self, asset: str) -> float:
        if self.equity <= 0:
            return 0.0
        price = self.prices.get(asset, 0.0)
        return self.total(asset) * price / self.equity

    def as_dict(self) -> dict[str, Any]:
        return {
            "exchange_id": self.exchange_id,
            "sandbox": self.sandbox,
            "quote": self.quote,
            "equity": self.equity,
            "fetched_at": self.fetched_at,
            "balances": {a: b.as_dict() for a, b in self.balances.items()},
            "prices": dict(self.prices),
        }


class CcxtExchange:
    """Connexion ccxt minimale, spot uniquement, sans droit de retrait implicite."""

    def __init__(
        self,
        exchange_id: str = "binance",
        api_key: str = "",
        secret: str = "",
        password: str = "",
        sandbox: bool = True,
        quote: str = "USDT",
        timeout_ms: int = 20_000,
    ) -> None:
        if not api_key or not secret:
            raise ExchangeError(
                "clés API absentes : renseignez MERIDIAN_EXCHANGE_API_KEY et "
                "MERIDIAN_EXCHANGE_API_SECRET dans .env"
            )
        try:
            import ccxt  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - dépend de l'installation
            raise ExchangeError(
                "ccxt est requis pour le mode réel : "
                "python3 -m pip install -e '.[live]'"
            ) from exc

        exchange_id = exchange_id.strip().lower()
        klass = getattr(ccxt, exchange_id, None)
        if klass is None:
            raise ExchangeError(f"exchange ccxt inconnu: {exchange_id}")

        params: dict[str, Any] = {
            "apiKey": api_key,
            "secret": secret,
            "enableRateLimit": True,
            "timeout": timeout_ms,
            "options": {"defaultType": "spot"},
        }
        if password:
            params["password"] = password

        self.exchange_id = exchange_id
        self.quote = quote.upper()
        self.sandbox = bool(sandbox)
        self.exchange = klass(params)
        if self.sandbox:
            self.exchange.set_sandbox_mode(True)
        self.exchange.load_markets()

    # ------------------------------------------------------------------ compte
    def fetch_account(self, assets: list[str]) -> AccountSnapshot:
        """Lit les soldes et les derniers prix des actifs du mandat."""
        try:
            raw = self.exchange.fetch_balance()
        except Exception as exc:
            raise ExchangeError(f"lecture du compte impossible: {exc}") from exc

        free = raw.get("free") or {}
        total = raw.get("total") or {}
        wanted = list(dict.fromkeys([*assets, self.quote]))
        balances: dict[str, Balance] = {}
        for asset in wanted:
            balances[asset] = Balance(
                asset=asset,
                free=float(free.get(asset, 0.0) or 0.0),
                total=float(total.get(asset, 0.0) or 0.0),
            )

        prices: dict[str, float] = {self.quote: 1.0}
        for asset in assets:
            if asset == self.quote:
                continue
            symbol = split_symbol(asset, self.quote)
            try:
                ticker = self.exchange.fetch_ticker(symbol)
            except Exception as exc:
                raise ExchangeError(f"prix indisponible pour {symbol}: {exc}") from exc
            last = ticker.get("last") or ticker.get("close")
            if last is None or not math.isfinite(float(last)) or float(last) <= 0:
                raise ExchangeError(f"prix invalide pour {symbol}: {last!r}")
            prices[asset] = float(last)

        equity = balances[self.quote].total + sum(
            balances[a].total * prices[a]
            for a in assets
            if a != self.quote and a in prices
        )
        return AccountSnapshot(
            exchange_id=self.exchange_id,
            sandbox=self.sandbox,
            quote=self.quote,
            balances=balances,
            prices=prices,
            equity=float(equity),
        )

    # ------------------------------------------------------------------ ordres
    def amount_to_precision(self, asset: str, amount: float) -> float:
        symbol = split_symbol(asset, self.quote)
        try:
            return float(self.exchange.amount_to_precision(symbol, float(amount)))
        except Exception as exc:
            raise ExchangeError(f"arrondi de quantité impossible pour {symbol}: {exc}") from exc

    def price_to_precision(self, asset: str, price: float) -> float:
        symbol = split_symbol(asset, self.quote)
        try:
            return float(self.exchange.price_to_precision(symbol, float(price)))
        except Exception as exc:
            raise ExchangeError(f"arrondi de prix impossible pour {symbol}: {exc}") from exc

    def min_notional(self, asset: str, price: float) -> float:
        """Notionnel minimum imposé par l'exchange, si disponible."""
        symbol = split_symbol(asset, self.quote)
        try:
            market = self.exchange.market(symbol)
        except Exception:
            return 0.0
        limits = market.get("limits") or {}
        cost_min = (limits.get("cost") or {}).get("min") or 0.0
        amount_min = (limits.get("amount") or {}).get("min") or 0.0
        return max(float(cost_min), float(amount_min) * float(price))

    def create_order(
        self,
        asset: str,
        side: str,
        qty: float,
        order_type: str = "market",
        price: float | None = None,
        params: dict | None = None,
    ) -> dict:
        """Envoie un ordre spot. ``side`` vaut ``BUY`` ou ``SELL``."""
        symbol = split_symbol(asset, self.quote)
        amount = self.amount_to_precision(asset, qty)
        if amount <= 0:
            raise ExchangeError(f"quantité arrondie à zéro pour {symbol}")
        side = side.lower()
        if side not in ("buy", "sell"):
            raise ExchangeError(f"side invalide: {side}")
        order_type = order_type.lower()
        try:
            if order_type == "limit":
                if price is None or price <= 0:
                    raise ExchangeError("prix limite absent")
                px = self.price_to_precision(asset, price)
                return self.exchange.create_order(
                    symbol, "limit", side, amount, px, params or {}
                )
            return self.exchange.create_order(
                symbol, "market", side, amount, None, params or {}
            )
        except ExchangeError:
            raise
        except Exception as exc:
            raise ExchangeError(f"ordre refusé par l'exchange ({symbol} {side}): {exc}") from exc

    def close(self) -> None:
        close = getattr(self.exchange, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
