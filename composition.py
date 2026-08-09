import numpy as np
from typing import List, Dict, Tuple
# ...existing code...

class CompositionCalculator:
    """Composition & flow rate calculation wrapper."""
    
    def calculate(
        self,
        buffer: Dict,
        lipid_stocks: List[Dict],
        tfr: float,
        frr: float,
        screen_space_mode: str,
        screen_space_params: Dict
    ) -> Tuple[List[List[float]], List[List[float]]]:
        """
        Calculate compositions and flow rates.
        Returns (compositions, flow_rates).
        """
        
        num_lipids = len(lipid_stocks)
        if num_lipids == 0:
            raise ValueError("At least one lipid stock required")
        
        # Pad to 3 lipids for flow_calculator compatibility
        Lp1 = self._make_lipid_array(lipid_stocks[0]) if num_lipids >= 1 else [None, 0, range(0, 1), 0]
        Lp2 = self._make_lipid_array(lipid_stocks[1]) if num_lipids >= 2 else [None, 0, range(0, 1), 0]
        Lp3 = self._make_lipid_array(lipid_stocks[2]) if num_lipids >= 3 else [None, 0, range(0, 1), 0]
        
        Buffer = [buffer["name"], True, [tfr*frr]]
        lipid_FR_total = (tfr / frr) if frr else 0
        buffer_FR_total = max(tfr - lipid_FR_total, 0)

        # Screen space modes
        if screen_space_mode == "Manual":
            compositions = screen_space_params.get("compositions", [[50, 25, 25]])
        elif screen_space_mode == "Scan":
            explicit = screen_space_params.get("compositions", []) or []
            if explicit:
                compositions = [list(c) for c in explicit if isinstance(c, (list, tuple))]
            else:
                # Backward-compatible fallback: generate scan grid when no explicit list exists.
                min_vals = screen_space_params.get("min_vals", [0, 0, 0])
                max_vals = screen_space_params.get("max_vals", [100, 100, 100])
                interval = screen_space_params.get("interval", 10)
                compositions = self._generate_scan_grid(min_vals[:num_lipids], max_vals[:num_lipids], interval)
                extras = screen_space_params.get("extra_compositions", []) or []
                for comp in extras:
                    if isinstance(comp, (list, tuple)):
                        compositions.append(list(comp))
        elif screen_space_mode == "Load":
            explicit = screen_space_params.get("compositions", []) or []
            if explicit:
                compositions = [list(c) for c in explicit if isinstance(c, (list, tuple))]
            else:
                # Backward-compatible fallback: use legacy load+extras fields.
                compositions = screen_space_params.get("compositions", [[50, 25, 25]])
                extras = screen_space_params.get("extra_compositions", []) or []
                for comp in extras:
                    if isinstance(comp, (list, tuple)):
                        compositions.append(list(comp))
        else:
            raise ValueError(f"Unknown screen_space_mode: {screen_space_mode}")

        flow_rates = self._calc_flow_rates_flowcalc_like(
            compositions, lipid_stocks, buffer_FR_total, lipid_FR_total
        )
        return compositions, flow_rates

    def _make_lipid_array(self, lipid: Dict) -> List:
        """Convert lipid dict to flow_calculator format [name, MW, range, concentration]."""
        return [
            lipid["name"],
            lipid.get("mw", 356.5),
            range(0, 101, 50),  # default range
            lipid.get("concentration", 14)
        ]

    def _generate_scan_grid(self, min_vals: List[float], max_vals: List[float], interval: float) -> List[List[float]]:
        """Generate grid of compositions for scan mode."""
        compositions = []
        if len(min_vals) == 1:
            compositions.append([100])
        elif len(min_vals) == 2:
            for b in np.arange(min_vals[1], max_vals[1] + interval, interval):
                a = 100 - b
                if min_vals[0] <= a <= max_vals[0]:
                    compositions.append([a, b])
        else:
            for b in np.arange(min_vals[1], max_vals[1] + interval, interval):
                for c in np.arange(min_vals[2], max_vals[2] + interval, interval):
                    a = 100 - b - c
                    if a >= 0 and min_vals[0] <= a <= max_vals[0]:
                        compositions.append([a, b, c])

        return compositions if compositions else [[50, 25, 25]]

    def _calc_flow_rates_flowcalc_like(
        self,
        compositions: List[List[float]],
        lipid_stocks: List[Dict],
        buffer_FR_total: float,
        lipid_FR_total: float
    ) -> List[List[float]]:
        flow_rates = []
        c = [self._lipid_conc_mM(l) for l in lipid_stocks] + [0.0, 0.0, 0.0]
        c1, c2, c3 = c[0], c[1], c[2]

        for comp in compositions:
            a = comp[0] if len(comp) > 0 else 0
            b = comp[1] if len(comp) > 1 else 0
            cc = comp[2] if len(comp) > 2 else 0

            l1 = l2 = l3 = 0.0

            if a > 0:
                frac1 = (c1 * b) / (c2 * a) if b > 0 and c2 > 0 else 0.0
                frac2 = (c1 * cc) / (c3 * a) if cc > 0 and c3 > 0 else 0.0
                den = 1 + frac1 + frac2
                l1 = lipid_FR_total / den if den else 0.0
                l2 = l1 * frac1
                l3 = l1 * frac2
            else:
                if b > 0 or cc > 0:
                    den = (c2 * b) + (c3 * cc)
                    if den > 0:
                        frac = (c2 * b) / den
                        l2 = lipid_FR_total * frac
                        l3 = lipid_FR_total - l2
                    else:
                        l2 = 0.0
                        l3 = 0.0

            flow_rates.append([buffer_FR_total, l1, l2, l3])

        return flow_rates

    def _lipid_conc_mM(self, lipid: Dict) -> float:
        if lipid.get("concentration_mM") not in (None, ""):
            return float(lipid["concentration_mM"])
        units = lipid.get("units", "mM")
        conc = float(lipid.get("concentration", 0) or 0)
        if units == "mg/ml":
            mw = float(lipid.get("mw", 0) or 0)
            return (conc * 1000.0 / mw) if mw else 0.0
        return conc
