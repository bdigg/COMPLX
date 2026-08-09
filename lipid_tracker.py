import json
import threading
from typing import Dict, List, Tuple, Optional

class LipidTracker:
    """Unified lipid state:
    - configs: stored via ConfigManager (not here)
    - intake: allocated positions
    - loaded: line state with remaining volume
    """
    
    def __init__(self):
        self.intake_allocations: Dict[Tuple[int, int, int], str] = {}  # (plate,row,col) -> lipid_name
        self.line_state: Dict[int, Dict] = {
            1: {"lipid_name": None, "remaining_volume": 0, "loaded_volume": 0, "source_well": None},
            2: {"lipid_name": None, "remaining_volume": 0, "loaded_volume": 0, "source_well": None},
            3: {"lipid_name": None, "remaining_volume": 0, "loaded_volume": 0, "source_well": None}
        }
        self._lock = threading.RLock()
        self._load_state()

    def allocate_to_intake(self, plate: int, row: int, col: int, lipid_name: str) -> None:
        """Allocate a lipid to an intake well (state 2)."""
        with self._lock:
            self.intake_allocations[(plate, row, col)] = lipid_name
            self._save_state()

    def remove_from_intake(self, plate: int, row: int, col: int) -> None:
        """Remove lipid allocation from intake well."""
        with self._lock:
            self.intake_allocations.pop((plate, row, col), None)
            self._save_state()

    def find_intake_wells_with_lipid(self, lipid_name: str) -> List[Tuple[int, int, int]]:
        """Find intake positions allocated to lipid_name (sorted)."""
        with self._lock:
            wells = [pos for pos, name in self.intake_allocations.items() if name == lipid_name]
            return sorted(wells)

    def load_lipid_to_line(self, line: int, plate: int, row: int, col: int, lipid_name: str, volume: float) -> None:
        """Move lipid from intake (state 2) to loaded line (state 3)."""
        with self._lock:
            key = (plate, row, col)
            if self.intake_allocations.get(key) != lipid_name:
                raise ValueError(f"Intake well {key} not allocated to {lipid_name}")
            # remove from intake (no longer usable)
            self.intake_allocations.pop(key, None)
            # load to line
            self.line_state[line] = {
                "lipid_name": lipid_name,
                "remaining_volume": volume,
                "loaded_volume": volume,
                "source_well": key
            }
            self._save_state()

    def deplete_line(self, line: int, volume: float) -> None:
        """Reduce line volume during collection."""
        with self._lock:
            self.line_state[line]["remaining_volume"] -= volume
            if self.line_state[line]["remaining_volume"] < 0:
                self.line_state[line]["remaining_volume"] = 0
            self._save_state()

    def clear_line(self, line: int) -> None:
        """Clear a line after cleaning."""
        with self._lock:
            self.line_state[line] = {"lipid_name": None, "remaining_volume": 0, "loaded_volume": 0, "source_well": None}
            self._save_state()

    def set_line_loaded_manual(self, line: int, lipid_name: str, volume: float = 450.0) -> None:
        """Manually declare a line as loaded (admin override)."""
        with self._lock:
            vol = max(0.0, float(volume))
            self.line_state[int(line)] = {
                "lipid_name": str(lipid_name),
                "remaining_volume": vol,
                "loaded_volume": vol,
                "source_well": None,
            }
            self._save_state()

    def clear_all_lines(self) -> None:
        """Clear all loaded lipid lines (called after clean_all)."""
        with self._lock:
            for line in (1, 2, 3):
                self.line_state[line] = {"lipid_name": None, "remaining_volume": 0, "loaded_volume": 0, "source_well": None}
            self._save_state()

    def get_line_state(self, line: int) -> Dict:
        """Get current state of a line."""
        with self._lock:
            return dict(self.line_state.get(line, {"lipid_name": None, "remaining_volume": 0, "loaded_volume": 0, "source_well": None}))

    def _save_state(self) -> None:
        """Persist to disk."""
        try:
            data = {
                "intake": {str(k): v for k, v in self.intake_allocations.items()},
                "lines": self.line_state
            }
            with open("./temp_state/lipid_tracker.json", "w") as f:
                json.dump(data, f)
        except Exception as e:
            print(f"Warning: Could not save lipid tracker: {e}")

    def _load_state(self) -> None:
        """Load from disk if exists."""
        try:
            with open("./temp_state/lipid_tracker.json", "r") as f:
                data = json.load(f)
                self.intake_allocations = {eval(k): v for k, v in data.get("intake", {}).items()}
                self.line_state = data.get("lines", self.line_state)
                # Backfill loaded_volume for older saved states
                for line, state in self.line_state.items():
                    if "loaded_volume" not in state:
                        state["loaded_volume"] = state.get("remaining_volume", 0)
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"Warning: Could not load lipid tracker: {e}")
