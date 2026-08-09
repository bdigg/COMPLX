import json
import threading
from typing import Dict, Tuple, Optional, List
from dataclasses import dataclass

@dataclass
class WellState:
    lipid_name: Optional[str] = None
    ratio_fractions: List[float] = None  # [0.5, 0.3, 0.2] for 3 lipids
    is_empty: bool = False
    color_hex: Optional[str] = None

class PlateTracker:
    """Track well states and compute color overlays."""
    
    def __init__(self):
        self.plates: Dict[int, Dict[Tuple[int, int], WellState]] = {}
        self._lock = threading.RLock()
        # Plate 0 is used elsewhere for holding positions; output plates are 1-6.
        for plate_idx in range(0, 7):
            self._init_plate(plate_idx)

    def _init_plate(self, plate: int) -> None:
        plate = int(plate)
        if plate in self.plates:
            return
        self.plates[plate] = {}
        for row in range(1, 9):  # 96-well output plate: rows 1-8
            for col in range(1, 13):  # cols 1-12
                self.plates[plate][(row, col)] = WellState()

    def _ensure_plate(self, plate: int) -> None:
        plate = int(plate)
        if plate not in self.plates:
            self._init_plate(plate)

    def set_well_lipid(self, plate: int, row: int, col: int, lipid_name: str, ratio_fractions: List[float]) -> None:
        """Update well with lipid and ratio fractions."""
        with self._lock:
            self._ensure_plate(plate)
            color = self._blend_colors(lipid_name, ratio_fractions)
            self.plates[plate][(row, col)] = WellState(
                lipid_name=lipid_name,
                ratio_fractions=ratio_fractions,
                color_hex=color
            )

    def set_well_color(self, plate: int, row: int, col: int, color_hex: str) -> None:
        """Update well with a specific color."""
        with self._lock:
            self._ensure_plate(plate)
            self.plates[plate][(row, col)] = WellState(color_hex=color_hex)

    def mark_well_empty(self, plate: int, row: int, col: int) -> None:
        """Mark well as empty."""
        with self._lock:
            self._ensure_plate(plate)
            self.plates[plate][(row, col)] = WellState(is_empty=True)

    def get_plate(self, plate: int) -> Dict[Tuple[int, int], WellState]:
        """Get all wells on a plate."""
        with self._lock:
            return dict(self.plates.get(plate, {}))

    def get_well(self, plate: int, row: int, col: int) -> WellState:
        """Get single well state."""
        with self._lock:
            return self.plates.get(plate, {}).get((row, col), WellState())

    def _blend_colors(self, lipid_name: str, ratios: List[float]) -> str:
        """Blend colors based on lipid ratios."""
        # UNUSED - This method is incomplete and never called
        # Consider removing or implementing properly
