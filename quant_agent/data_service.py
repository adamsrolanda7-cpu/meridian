"""Accès aux données Meridian pour le CLI.

Par défaut, le CLI lit uniquement le cache local. Un téléchargement réseau
n'a lieu que si l'appelant demande explicitement ``refresh=True``. C'est
volontaire : un agent de recherche ne doit pas modifier silencieusement le
jeu de données d'un backtest.

Les imports lourds (pandas, meridian.data) sont paresseux pour que les
commandes sans données restent rapides.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from meridian.config import ROOT, load_config

from .jsonutil import to_jsonable

if TYPE_CHECKING:  # pragma: no cover
    import pandas as pd


class DataServiceError(RuntimeError):
    """Données absentes, incomplètes ou téléchargement impossible."""


class DataService:
    def __init__(self, allow_synthetic: bool = False):
        self.cfg = load_config()
        self.allow_synthetic = bool(allow_synthetic)

    # ------------------------------------------------------------------- load
    def load(self, refresh: bool = False) -> tuple[dict[str, pd.DataFrame], str]:
        from meridian.data import download_all, load_panel, synthetic_panel

        if refresh:
            try:
                panel = download_all(self.cfg, quiet=True)
                source = "binance"
            except Exception as exc:
                raise DataServiceError(
                    f"téléchargement Binance impossible : {exc}. "
                    "Réessaie plus tard ou utilise le cache existant."
                ) from exc
        else:
            try:
                panel = load_panel(self.cfg)
                source = "binance/cache"
            except Exception as exc:
                if not self.allow_synthetic:
                    raise DataServiceError(
                        "données en cache indisponibles. "
                        "Lance d'abord : python3 -m meridian download"
                    ) from exc
                panel = synthetic_panel(self.cfg)
                source = "synthetic"
        if not panel:
            raise DataServiceError("panel vide : aucune donnée exploitable.")
        if "BTCUSDT" not in panel:
            raise DataServiceError("BTCUSDT est requis comme actif de référence.")
        return panel, source

    # ---------------------------------------------------------------- summary
    @staticmethod
    def last_bar(panel: dict[str, pd.DataFrame]) -> pd.Timestamp:
        return max(frame.index[-1] for frame in panel.values())

    @staticmethod
    def first_bar(panel: dict[str, pd.DataFrame]) -> pd.Timestamp:
        return min(frame.index[0] for frame in panel.values())

    def stale_days(self, panel: dict[str, pd.DataFrame]) -> int:
        import pandas as pd

        last = self.last_bar(panel)
        now = pd.Timestamp.now(tz="UTC")
        return max(0, int((now - last).days))

    def snapshot(self, panel: dict[str, pd.DataFrame]) -> dict[str, Any]:
        """Empreinte des données utilisée dans le hash du run."""
        return {
            symbol: {
                "rows": int(len(frame)),
                "first": frame.index[0].isoformat(),
                "last": frame.index[-1].isoformat(),
            }
            for symbol, frame in sorted(panel.items())
        }

    def quality(self, panel: dict[str, pd.DataFrame]) -> dict[str, Any]:
        from meridian.data import quality_report

        return quality_report(self.cfg, panel)

    def quality_summary(
        self, panel: dict[str, pd.DataFrame], source: str = "binance/cache"
    ) -> dict[str, Any]:
        quality = self.quality(panel)
        bad = [
            symbol
            for symbol, row in quality.items()
            if row.get("status") != "OK"
        ]
        last = self.last_bar(panel)
        return {
            "assets": len(quality),
            "assets_ok": len(quality) - len(bad),
            "assets_bad": bad,
            "last_bar": str(last.date()),
            "stale_days": self.stale_days(panel),
            "source": source,
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    def overview(
        self, panel: dict[str, pd.DataFrame], source: str = "binance/cache"
    ) -> dict[str, Any]:
        """Vue légère pour l'agent : pas de 2 800 bougies par actif."""
        return to_jsonable(self.quality_summary(panel, source=source))


__all__ = [
    "DataService",
    "DataServiceError",
    "ROOT",
]
