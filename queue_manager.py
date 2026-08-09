import json
import threading
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Optional
from composition import CompositionCalculator
import composition as calc

@dataclass
class QueuedExperiment:
    """Single queued experiment with calculated compositions."""
    exp_id: str
    name: str
    buffer: Dict  # {"name": str, "concentration": float}
    lipid_stocks: List[Dict]  # [{"name": str, "concentration": float, "mw": float}, ...]
    tfr: float
    frr: float
    volume: float
    repeats: int
    screen_space_mode: str  # "Manual", "Load", "Scan"
    screen_space_params: Dict  # varies by mode
    compositions: List[List[float]]  # [[a,b,c], ...]
    flow_rates: List[List[float]]  # [[buf_FR, lip1_FR, lip2_FR, lip3_FR], ...]
    status: str  # "pending", "running", "paused", "completed", "failed", "skipped"
    active_channels: List[int]  # [1,2,3,4] or subset
    line3_constant_flow_enabled: bool = False
    line3_constant_flow_rate: float = 0.0
    extra_flow_enabled: bool = False
    extra_flow_setpoint: float = 0.0
    record_id: Optional[int] = None  # numeric records/log ID (FlowData folder prefix)
    error_message: Optional[str] = None
    output_wells: List[List[int]] = field(default_factory=list)  # [[plate,row,col], ...]
    comp_status: List[str] = field(default_factory=list)         # ["pending", ...]
    plot_links: List[str] = field(default_factory=list)          # ["", ...]

class QueueManager:
    """Thread-safe queue management."""

    def __init__(self, config_mgr):
        self.config_mgr = config_mgr
        self.composition_calculator = CompositionCalculator()
        self.queue: List[QueuedExperiment] = []
        self._lock = threading.RLock()
        self._next_well = [1, 1, 1]  # plate, row, col
        self._load_queue_from_disk()

    def add_experiment(self, exp_data: Dict) -> QueuedExperiment:
        """Calculate compositions/FRs and add to queue."""
        with self._lock:
            exp_id = f"exp_{len(self.queue)}_{int(__import__('time').time())}"
            line3_constant_flow_enabled = bool(exp_data.get("line3_constant_flow_enabled", False))
            line3_constant_flow_rate = float(exp_data.get("line3_constant_flow_rate", 0.0) or 0.0)
            if line3_constant_flow_rate <= 0:
                line3_constant_flow_enabled = False
            line3_uses_main_pump = bool(
                line3_constant_flow_enabled and exp_data.get("line3_uses_main_pump", line3_constant_flow_enabled)
            )
            
            # Extract active lipids (non-empty)
            lipid_stocks = []
            for i, lipid in enumerate(exp_data.get("lipid_stocks", [None, None, None])):
                if line3_uses_main_pump and i == 2:
                    continue
                if lipid:
                    lipid_stocks.append(lipid)
            
            active_channels = [1] + [i+2 for i in range(len(lipid_stocks))]
            if line3_uses_main_pump:
                active_channels.append(4)
            
            # Calculate compositions and flow rates
            try:
                base_compositions, base_flow_rates = self.composition_calculator.calculate(
                    buffer=exp_data["buffer"],
                    lipid_stocks=lipid_stocks,
                    tfr=exp_data["tfr"],
                    frr=exp_data["frr"],
                    screen_space_mode=exp_data["screen_space_mode"],
                    screen_space_params=exp_data["screen_space_params"]
                )
            except Exception as e:
                raise ValueError(f"Composition calculation failed: {e}")
            
            # Apply repeats: duplicate each composition
            repeats = exp_data.get("repeats", 1)
            compositions = []
            flow_rates = []
            for comp, fr in zip(base_compositions, base_flow_rates):
                fr_row = list(fr)
                while len(fr_row) < 4:
                    fr_row.append(0.0)
                if line3_uses_main_pump:
                    fr_row[3] = float(line3_constant_flow_rate)
                for _ in range(repeats):
                    compositions.append(comp)
                    flow_rates.append(list(fr_row))
            
            # Prefer provided output_wells if present and length matches compositions
            provided_wells = exp_data.get("output_wells")
            if provided_wells and len(provided_wells) == len(compositions):
                output_wells = [list(w) for w in provided_wells]
            else:
                output_wells = self._alloc_output_wells(len(compositions))

            queued_exp = QueuedExperiment(
                exp_id=exp_id,
                name=exp_data["name"],
                buffer=exp_data["buffer"],
                lipid_stocks=lipid_stocks,
                tfr=exp_data["tfr"],
                frr=exp_data["frr"],
                volume=exp_data["volume"],
                repeats=repeats,
                screen_space_mode=exp_data["screen_space_mode"],
                screen_space_params=exp_data["screen_space_params"],
                compositions=compositions,
                flow_rates=flow_rates,
                status="pending",
                active_channels=active_channels,
                line3_constant_flow_enabled=line3_constant_flow_enabled,
                line3_constant_flow_rate=float(line3_constant_flow_rate if line3_constant_flow_enabled else 0.0),
                extra_flow_enabled=bool(exp_data.get("extra_flow_enabled", False)),
                extra_flow_setpoint=float(exp_data.get("extra_flow_setpoint", 0.0) or 0.0),
                output_wells=output_wells,
                comp_status=["pending"] * len(compositions),
                plot_links=[""] * len(compositions),
            )
            
            self.queue.append(queued_exp)
            self._align_queue_line_preferences()
            self._recompute_next_well()
            self._save_queue_to_disk()
            return queued_exp

    def reorder_queue(self, indices: List[int]) -> None:
        """Reorder queue by new indices. Fails if any experiment is running."""
        with self._lock:
            if any(exp.status == "running" for exp in self.queue):
                raise RuntimeError("Cannot reorder queue while experiment is running")
            self.queue = [self.queue[i] for i in indices]
            self._align_queue_line_preferences()
            self._save_queue_to_disk()

    def remove_experiment(self, exp_id: str) -> bool:
        """Remove experiment if not running."""
        with self._lock:
            exp = self.get_experiment(exp_id)
            if exp and exp.status == "running":
                return False
            self.queue = [e for e in self.queue if e.exp_id != exp_id]
            self._save_queue_to_disk()
            return True

    def duplicate_experiment(self, exp_id: str) -> Optional[QueuedExperiment]:
        """Duplicate an experiment and append to queue as pending."""
        with self._lock:
            exp = self.get_experiment(exp_id)
            if not exp:
                return None

            new_id = f"exp_{len(self.queue)}_{int(__import__('time').time())}"
            new_exp = QueuedExperiment(
                exp_id=new_id,
                name=exp.name,
                buffer=exp.buffer,
                lipid_stocks=list(exp.lipid_stocks),
                tfr=exp.tfr,
                frr=exp.frr,
                volume=exp.volume,
                repeats=exp.repeats,
                screen_space_mode=exp.screen_space_mode,
                screen_space_params=exp.screen_space_params,
                compositions=[list(c) for c in exp.compositions],
                flow_rates=[list(fr) for fr in exp.flow_rates],
                status="pending",
                active_channels=list(exp.active_channels),
                line3_constant_flow_enabled=bool(getattr(exp, "line3_constant_flow_enabled", False)),
                line3_constant_flow_rate=float(getattr(exp, "line3_constant_flow_rate", 0.0) or 0.0),
                extra_flow_enabled=bool(getattr(exp, "extra_flow_enabled", False)),
                extra_flow_setpoint=float(getattr(exp, "extra_flow_setpoint", 0.0) or 0.0),
                error_message=None,
                output_wells=[list(w) for w in exp.output_wells],
                comp_status=["pending"] * len(exp.compositions),
                plot_links=[""] * len(exp.compositions),
            )

            self.queue.append(new_exp)
            self._align_queue_line_preferences()
            self._recompute_next_well()
            self._save_queue_to_disk()
            return new_exp

    def get_experiment(self, exp_id: str) -> Optional[QueuedExperiment]:
        """Get experiment by ID."""
        with self._lock:
            for exp in self.queue:
                if exp.exp_id == exp_id:
                    return exp
            return None

    def edit_experiment(self, exp_id: str, exp_data: Dict) -> Optional[QueuedExperiment]:
        """Edit an experiment's parameters (only if pending)."""
        with self._lock:
            exp = self.get_experiment(exp_id)
            if not exp:
                return None
            if exp.status != "pending":
                raise ValueError("Cannot edit experiment that is not pending")
            line3_constant_flow_enabled = bool(exp_data.get("line3_constant_flow_enabled", False))
            line3_constant_flow_rate = float(exp_data.get("line3_constant_flow_rate", 0.0) or 0.0)
            if line3_constant_flow_rate <= 0:
                line3_constant_flow_enabled = False
            line3_uses_main_pump = bool(
                line3_constant_flow_enabled and exp_data.get("line3_uses_main_pump", line3_constant_flow_enabled)
            )
            
            # Extract active lipids
            lipid_stocks = []
            for i, lipid in enumerate(exp_data.get("lipid_stocks", [])):
                if line3_uses_main_pump and i == 2:
                    continue
                if lipid:
                    lipid_stocks.append(lipid)
            
            active_channels = [1] + [i+2 for i in range(len(lipid_stocks))]
            if line3_uses_main_pump:
                active_channels.append(4)
            
            # Recalculate compositions and flow rates
            try:
                base_compositions, base_flow_rates = self.composition_calculator.calculate(
                    buffer=exp_data["buffer"],
                    lipid_stocks=lipid_stocks,
                    tfr=exp_data["tfr"],
                    frr=exp_data["frr"],
                    screen_space_mode=exp_data["screen_space_mode"],
                    screen_space_params=exp_data["screen_space_params"]
                )
            except Exception as e:
                raise ValueError(f"Composition calculation failed: {e}")
            
            # Apply repeats
            repeats = exp_data.get("repeats", 1)
            compositions = []
            flow_rates = []
            for comp, fr in zip(base_compositions, base_flow_rates):
                fr_row = list(fr)
                while len(fr_row) < 4:
                    fr_row.append(0.0)
                if line3_uses_main_pump:
                    fr_row[3] = float(line3_constant_flow_rate)
                for _ in range(repeats):
                    compositions.append(comp)
                    flow_rates.append(list(fr_row))
            
            # Update experiment
            exp.name = exp_data["name"]
            exp.buffer = exp_data["buffer"]
            exp.lipid_stocks = lipid_stocks
            exp.tfr = exp_data["tfr"]
            exp.frr = exp_data["frr"]
            exp.volume = exp_data["volume"]
            exp.repeats = repeats
            exp.screen_space_mode = exp_data["screen_space_mode"]
            exp.screen_space_params = exp_data["screen_space_params"]
            exp.compositions = compositions
            exp.flow_rates = flow_rates
            exp.active_channels = active_channels
            exp.line3_constant_flow_enabled = line3_constant_flow_enabled
            exp.line3_constant_flow_rate = float(line3_constant_flow_rate if line3_constant_flow_enabled else 0.0)
            exp.extra_flow_enabled = bool(exp_data.get("extra_flow_enabled", getattr(exp, "extra_flow_enabled", False)))
            exp.extra_flow_setpoint = float(exp_data.get("extra_flow_setpoint", getattr(exp, "extra_flow_setpoint", 0.0)) or 0.0)
            # Preserve existing or provided output_wells when possible
            provided_wells = exp_data.get("output_wells")
            if provided_wells and len(provided_wells) == len(compositions):
                exp.output_wells = [list(w) for w in provided_wells]
            elif exp.output_wells and len(exp.output_wells) == len(compositions):
                exp.output_wells = [list(w) for w in exp.output_wells]
            else:
                exp.output_wells = self._alloc_output_wells(len(compositions))
            exp.comp_status = ["pending"] * len(compositions)
            exp.plot_links = [""] * len(compositions)
            
            self._align_queue_line_preferences()
            self._recompute_next_well()
            self._save_queue_to_disk()
            return exp

    def get_queue(self) -> List[QueuedExperiment]:
        """Get full queue (copy)."""
        with self._lock:
            return [e for e in self.queue]

    def update_status(self, exp_id: str, status: str, error: Optional[str] = None) -> None:
        """Update experiment status."""
        with self._lock:
            exp = self.get_experiment(exp_id)
            if exp:
                exp.status = status
                exp.error_message = error
                self._save_queue_to_disk()

    def delete_experiment(self, exp_id: str) -> None:
        """Delete an experiment from the queue."""
        with self._lock:
            self.queue = [exp for exp in self.queue if exp.exp_id != exp_id]
            self._save_queue_to_disk()

    def set_record_id(self, exp_id: str, record_id: int) -> None:
        """Persist records/log numeric ID for an experiment."""
        with self._lock:
            exp = self.get_experiment(exp_id)
            if not exp:
                return
            try:
                exp.record_id = int(record_id)
            except Exception:
                return
            self._save_queue_to_disk()

    def mark_experiments_red(self, lipid_name: str) -> None:
        """Mark all pending experiments using lipid_name as failed."""
        with self._lock:
            for exp in self.queue:
                if exp.status == "pending":
                    for lipid in exp.lipid_stocks:
                        if lipid["name"] == lipid_name:
                            exp.status = "failed"
                            exp.error_message = f"Lipid '{lipid_name}' depleted"
                            break
            self._save_queue_to_disk()

    def skip_unrunnable_experiments(self, lipid_manager) -> List[str]:
        """Skip pending experiments missing lipids from intake."""
        skipped = []
        with self._lock:
            for exp in self.queue:
                if exp.status != "pending":
                    continue
                
                missing_lipids = []
                for lipid in exp.lipid_stocks:
                    wells = lipid_manager.find_intake_wells_with_lipid(lipid["name"])
                    if not wells:
                        missing_lipids.append(lipid["name"])
                
                if missing_lipids:
                    exp.status = "skipped"
                    exp.error_message = f"Missing from intake: {', '.join(missing_lipids)}"
                    skipped.append(exp.exp_id)
            
            if skipped:
                self._save_queue_to_disk()
        
        return skipped

    def _find_lipid_in_inventory(self, lipid_name: str, lipid_inventory) -> List:
        """Check if lipid is allocated in inventory."""
        # UNUSED - Remove this method
        return lipid_inventory.find_wells_with_lipid(lipid_name)

    def _save_queue_to_disk(self) -> None:
        """Persist queue to JSON."""
        try:
            data = [asdict(exp) for exp in self.queue]
            with open("./temp_state/queue.json", "w") as f:
                json.dump(data, f)
        except Exception as e:
            print(f"Warning: Could not save queue: {e}")

    def _load_queue_from_disk(self) -> None:
        """Load queue from disk if exists."""
        try:
            with open("./temp_state/queue.json", "r") as f:
                data = json.load(f)
                normalized = []
                for item in data:
                    if "extra_flow_enabled" not in item:
                        item["extra_flow_enabled"] = False
                    if "extra_flow_setpoint" not in item:
                        item["extra_flow_setpoint"] = 0.0
                    if "line3_constant_flow_enabled" not in item:
                        item["line3_constant_flow_enabled"] = False
                    if "line3_constant_flow_rate" not in item:
                        item["line3_constant_flow_rate"] = 0.0
                    normalized.append(QueuedExperiment(**item))
                self.queue = normalized
                self._recompute_next_well()
        except FileNotFoundError:
            self.queue = []
            self._next_well = [1, 1, 1]
        except Exception as e:
            print(f"Warning: Could not load queue: {e}")
            self.queue = []
            self._next_well = [1, 1, 1]

    def _alloc_output_wells(self, n):
        wells = []
        for _ in range(n):
            plate, row, col = self._next_well
            wells.append([plate, row, col])
            # advance row-wise
            col += 1
            if col > 12:
                col = 1
                row += 1
                if row > 8:
                    row = 1
                    plate += 1
                    if plate > 6:
                        plate = 1
            self._next_well = [plate, row, col]
        return wells

    def _recompute_next_well(self) -> None:
        """Recompute _next_well based on the last allocated well in the queue."""
        if not self.queue:
            self._next_well = [1, 1, 1]
            return
        last_well = [1, 1, 1]
        for exp in self.queue:
            if exp.output_wells:
                last_well = list(exp.output_wells[-1])
        plate, row, col = last_well
        col += 1
        if col > 12:
            col = 1
            row += 1
            if row > 8:
                row = 1
                plate += 1
                if plate > 6:
                    plate = 1
        self._next_well = [plate, row, col]

    def reassign_output_wells(self, start_well):
        """Reassign output wells for all experiments from a new starting well."""
        with self._lock:
            plate, row, col = start_well
            self._next_well = [plate, row, col]
            for exp in self.queue:
                exp.output_wells = self._alloc_output_wells(len(exp.compositions))
            self._save_queue_to_disk()

    def move_experiment(self, exp_id: str, direction: int) -> bool:
        """Move experiment up (-1) or down (+1) in the queue."""
        with self._lock:
            if any(exp.status == "running" for exp in self.queue):
                return False
            idx = next((i for i, e in enumerate(self.queue) if e.exp_id == exp_id), None)
            if idx is None:
                return False
            new_idx = idx + direction
            if new_idx < 0 or new_idx >= len(self.queue):
                return False
            self.queue[idx], self.queue[new_idx] = self.queue[new_idx], self.queue[idx]
            self._align_queue_line_preferences()
            self._save_queue_to_disk()
            return True

    def _align_queue_line_preferences(self) -> None:
        """Prefer keeping same lipid on same line as the immediately previous experiment."""
        prev_line_by_lipid = {}
        for exp in self.queue:
            if not exp.lipid_stocks:
                continue

            old_order = [l["name"] for l in exp.lipid_stocks]
            new_order = [None] * len(old_order)
            used = set()

            # Try to place lipids in the same line index as the immediately previous experiment.
            for name in old_order:
                target_idx = prev_line_by_lipid.get(name)
                if target_idx is None:
                    continue
                if target_idx < len(new_order) and new_order[target_idx] is None and name not in used:
                    new_order[target_idx] = name
                    used.add(name)

            # Fill remaining slots in original order
            remaining = [n for n in old_order if n not in used]
            for i in range(len(new_order)):
                if new_order[i] is None:
                    new_order[i] = remaining.pop(0)

            if new_order != old_order:
                perm = [old_order.index(n) for n in new_order]

                exp.lipid_stocks = [exp.lipid_stocks[i] for i in perm]
                exp.compositions = [
                    [comp[i] if i < len(comp) else 0 for i in perm]
                    for comp in exp.compositions
                ]

                new_flow_rates = []
                for fr in exp.flow_rates:
                    lipid_frs = fr[1:4]
                    reordered = [0.0, 0.0, 0.0]
                    for new_idx, old_idx in enumerate(perm):
                        if old_idx < len(lipid_frs):
                            reordered[new_idx] = lipid_frs[old_idx]
                    line3_uses_main_pump = bool(
                        getattr(exp, "line3_constant_flow_enabled", False)
                        and len(getattr(exp, "lipid_stocks", []) or []) < 3
                    )
                    if line3_uses_main_pump:
                        reordered[2] = float(getattr(exp, "line3_constant_flow_rate", 0.0) or 0.0)
                    new_flow_rates.append([fr[0]] + reordered)
                exp.flow_rates = new_flow_rates

            # Update mapping using only this experiment, so the next experiment
            # aligns against the immediately previous one (not older history).
            prev_line_by_lipid = {
                name: idx for idx, name in enumerate([l["name"] for l in exp.lipid_stocks])
            }
